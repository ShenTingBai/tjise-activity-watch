#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多活动报名计划的纯本地协调核心。

这里刻意不依赖 pywebview、Playwright 或学校站点：报名开放时间只负责分批，
活动举办起止时间只负责冲突校验，实际提交由 app.py 注入的 runner 完成。

@author LiXinwen
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import threading
import time
from typing import Callable, Iterable


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
TERMINAL_TASK_STATES = {"success", "already_signed", "failed", "canceled"}


def parse_timestamp(value: object) -> datetime | None:
    """解析学校返回的本地时间；缺失、非法值都返回 None，不猜当前日期。"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip(), TIME_FORMAT)
    except ValueError:
        return None


def intervals_overlap(start_a: datetime, end_a: datetime,
                      start_b: datetime, end_b: datetime) -> bool:
    """按半开区间判断举办时间是否重叠，首尾相接不算冲突。"""
    return start_a < end_b and start_b < end_a


@dataclass(frozen=True)
class TaskSpec:
    """协调器需要的最小活动快照，避免后续列表刷新改变任务目标。"""

    task_id: str
    activity_id: str
    name: str
    sign_start: str
    act_start: str
    act_end: str
    headed: bool = False

    @classmethod
    def from_activity(cls, activity: dict, task_id: str, headed: bool = False) -> "TaskSpec":
        return cls(
            task_id=str(task_id),
            activity_id=str(activity.get("id") or ""),
            name=str(activity.get("name") or "活动"),
            sign_start=str(activity.get("sign_start") or ""),
            act_start=str(activity.get("act_start") or ""),
            act_end=str(activity.get("act_end") or ""),
            headed=bool(headed),
        )

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "activity_id": self.activity_id,
            "name": self.name,
            "sign_start": self.sign_start,
            "act_start": self.act_start,
            "act_end": self.act_end,
            "headed": self.headed,
        }


def validate_plan(activities: Iterable[dict]) -> dict:
    """校验计划并返回冲突双方；结果可直接序列化给 UI。"""
    rows = list(activities or [])
    errors: list[dict] = []
    conflicts: list[dict] = []
    specs: list[TaskSpec] = []
    seen: set[str] = set()

    for index, activity in enumerate(rows):
        aid = str(activity.get("id") or "")
        name = str(activity.get("name") or "活动")
        if not aid:
            errors.append({"kind": "missing_id", "index": index, "name": name})
            continue
        if aid in seen:
            errors.append({"kind": "duplicate_id", "activity_id": aid, "name": name})
            continue
        seen.add(aid)
        if bool(activity.get("signed")):
            errors.append({"kind": "already_signed", "activity_id": aid, "name": name})
        sign_start = str(activity.get("sign_start") or "")
        act_start = str(activity.get("act_start") or "")
        act_end = str(activity.get("act_end") or "")
        if parse_timestamp(sign_start) is None:
            errors.append({"kind": "invalid_sign_start", "activity_id": aid, "name": name})
        start = parse_timestamp(act_start)
        end = parse_timestamp(act_end)
        if start is None or end is None:
            errors.append({"kind": "activity_time_unknown", "activity_id": aid, "name": name})
        elif end <= start:
            errors.append({"kind": "activity_time_invalid", "activity_id": aid, "name": name})
        specs.append(TaskSpec.from_activity(activity, "", bool(activity.get("headed"))))

    for index, left in enumerate(specs):
        left_start = parse_timestamp(left.act_start)
        left_end = parse_timestamp(left.act_end)
        if left_start is None or left_end is None or left_end <= left_start:
            continue
        for right in specs[index + 1:]:
            right_start = parse_timestamp(right.act_start)
            right_end = parse_timestamp(right.act_end)
            if right_start is None or right_end is None or right_end <= right_start:
                continue
            if intervals_overlap(left_start, left_end, right_start, right_end):
                conflicts.append({
                    "left": {"activity_id": left.activity_id, "name": left.name,
                             "act_start": left.act_start, "act_end": left.act_end},
                    "right": {"activity_id": right.activity_id, "name": right.name,
                              "act_start": right.act_start, "act_end": right.act_end},
                })

    if conflicts:
        errors.append({"kind": "activity_overlap", "conflicts": conflicts})

    return {
        "ok": not errors and bool(specs),
        "errors": errors,
        "conflicts": conflicts,
        "specs": specs,
    }


def group_by_sign_start(specs: Iterable[TaskSpec]) -> list[list[TaskSpec]]:
    """按完整报名开放时间分组，返回从早到晚的并发批次。"""
    groups: dict[datetime, list[TaskSpec]] = {}
    for spec in specs:
        when = parse_timestamp(spec.sign_start)
        if when is None:
            continue
        groups.setdefault(when, []).append(spec)
    return [groups[key] for key in sorted(groups)]


class PlanCoordinator:
    """按开放批次运行任务，并隔离每个任务的状态与取消信号。"""

    def __init__(self, plan_id: str, specs: Iterable[TaskSpec],
                 runner: Callable[[TaskSpec, threading.Event, Callable[[str, dict | None], None]], dict],
                 emit: Callable[[str, dict], None],
                 now: Callable[[], float] | None = None,
                 sleep: Callable[[float], None] | None = None) -> None:
        self.plan_id = str(plan_id)
        self.specs = list(specs)
        self.runner = runner
        self.emit = emit
        self.now = now or time.time
        self.sleep = sleep or time.sleep
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._events = {spec.task_id: threading.Event() for spec in self.specs}
        self._states = {spec.task_id: "queued" for spec in self.specs}
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """只启动一个协调线程；批次内部再各起一个任务线程。"""
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError("计划已经在运行")
            self.emit("plan", self._plan_payload("ready"))
            self._thread = threading.Thread(target=self._run, name="signup-plan", daemon=True)
            self._thread.start()

    def cancel_task(self, task_id: str) -> bool:
        with self._lock:
            event = self._events.get(str(task_id))
            state = self._states.get(str(task_id))
            if event is None or state in TERMINAL_TASK_STATES:
                return False
            event.set()
            if state in {"queued", "waiting"}:
                self._set_state(str(task_id), "canceled", {"reason": "用户取消了这一场"})
            return True

    def stop(self) -> bool:
        with self._lock:
            if self._stop.is_set():
                return False
            self._stop.set()
            for task_id, event in self._events.items():
                event.set()
                if self._states.get(task_id) in {"queued", "waiting"}:
                    self._set_state(task_id, "canceled", {"reason": "计划已停止"})
            return True

    def snapshot(self) -> dict:
        with self._lock:
            return self._plan_payload(self._plan_status())

    def _run(self) -> None:
        for group in group_by_sign_start(self.specs):
            if self._stop.is_set():
                break
            active = [spec for spec in group if self._states.get(spec.task_id) not in TERMINAL_TASK_STATES]
            if not active:
                continue
            for spec in active:
                self._set_state(spec.task_id, "waiting")
            target = parse_timestamp(active[0].sign_start)
            if target is not None:
                target_ts = target.timestamp()
                while self.now() < target_ts and not self._stop.is_set():
                    if all(self._events[s.task_id].is_set() for s in active):
                        break
                    self.sleep(min(0.2, max(0.01, target_ts - self.now())))
            threads = []
            for spec in active:
                if self._events[spec.task_id].is_set():
                    if self._states.get(spec.task_id) not in TERMINAL_TASK_STATES:
                        self._set_state(spec.task_id, "canceled", {"reason": "用户取消了这一场"})
                    continue
                thread = threading.Thread(target=self._run_one, args=(spec,),
                                          name="signup-task-%s" % spec.task_id, daemon=True)
                threads.append(thread)
                thread.start()
            for thread in threads:
                thread.join()
        self.emit("plan_done", {
            "plan_id": self.plan_id,
            "status": self._plan_status(),
            "ok": self._plan_status() == "completed",
            "summary": self._summary(),
        })

    def _run_one(self, spec: TaskSpec) -> None:
        self._set_state(spec.task_id, "submitting")
        try:
            result = self.runner(spec, self._events[spec.task_id],
                                 lambda status, detail=None: self._set_state(spec.task_id, status, detail)) or {}
        except Exception as exc:  # runner 失败只影响当前任务，其他批次继续
            result = {"status": "failed", "reason": type(exc).__name__}
        status = str(result.get("status") or ("canceled" if self._events[spec.task_id].is_set() else "failed"))
        if status not in TERMINAL_TASK_STATES:
            status = "failed"
        self._set_state(spec.task_id, status, result)

    def _set_state(self, task_id: str, status: str, detail: dict | None = None) -> None:
        with self._lock:
            if task_id not in self._states:
                return
            if self._states[task_id] in TERMINAL_TASK_STATES and status not in TERMINAL_TASK_STATES:
                return
            self._states[task_id] = status
            spec = next((item for item in self.specs if item.task_id == task_id), None)
            if not spec:
                return
            payload = {
                "plan_id": self.plan_id,
                "task_id": task_id,
                "activity_id": spec.activity_id,
                "name": spec.name,
                "sign_start": spec.sign_start,
                "status": status,
            }
            if detail:
                payload.update({k: v for k, v in detail.items() if k not in {"status"}})
            self.emit("task", payload)

    def _plan_status(self) -> str:
        states = list(self._states.values())
        if self._stop.is_set():
            return "stopped"
        if any(state in {"queued", "waiting", "submitting"} for state in states):
            return "running"
        if any(state in {"failed", "canceled"} for state in states) and any(state in {"success", "already_signed"} for state in states):
            return "partial"
        if all(state in {"success", "already_signed"} for state in states):
            return "completed"
        return "failed"

    def _summary(self) -> dict:
        counts: dict[str, int] = {}
        for state in self._states.values():
            counts[state] = counts.get(state, 0) + 1
        return {"total": len(self._states), "counts": counts}

    def _plan_payload(self, status: str) -> dict:
        return {
            "plan_id": self.plan_id,
            "status": status,
            "tasks": [
                dict(spec.as_dict(), status=self._states.get(spec.task_id, "queued"))
                for spec in self.specs
            ],
        }
