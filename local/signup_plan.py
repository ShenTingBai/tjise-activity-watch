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

# 学校时间只有一份解析口径（UTC+8）；本机 saved_at/生命周期文本不走这里。
# 源码态 local/ 在 sys.path 上时用裸导入（与 grab.py 一致）；作为
# local.signup_plan 包导入（测试/工具）时退回相对导入。
try:
    from time_contract import TIME_FORMAT, parse_school_time, school_epoch, default_now
except ImportError:  # pragma: no cover - 取决于导入方式
    from .time_contract import (TIME_FORMAT, parse_school_time, school_epoch,
                                default_now)
# 校时失效由 business_clock 定义；协调器只捕获这一种，不吞其它异常
try:
    from business_clock import ClockUnavailable
except ImportError:  # pragma: no cover
    from .business_clock import ClockUnavailable


TERMINAL_TASK_STATES = {"success", "already_signed", "failed", "canceled"}

# 报名模式三分类：预报名 / 立即报名（抢名额）/ 已截止，都是正式产品模式
SIGNUP_MODES = ("pre_register", "instant_signup", "closed")

# 一次性运行时 checkpoint：第一次写入后固定，迟到/重放事件不得改写；
# attempts/last_reason 是进度字段，跟随最新一次提交。
CHECKPOINT_FIELDS = ("run_started_at", "page_opened_at", "watch_started_at", "retry_deadline")


def parse_timestamp(value: object) -> datetime | None:
    """解析学校返回的时间；缺失、非法值都返回 None，不猜当前日期。

    返回带东八区时区的 datetime：同一串学校时间在任何本机时区下
    `.timestamp()` 都必须相等。
    """
    return parse_school_time(value)


def classify_signup_mode(sign_start: str, sign_end: str,
                         now: float | None = None) -> str:
    """按报名窗口把活动分成三种执行模式（前后端共用的唯一口径）。

    - sign_end 能解析且已过去 → closed：报名资格已结束，只读查看；
    - sign_start 在未来 → pre_register：预报名，等学校原定开放时刻提交；
    - 其余（已开放，含 sign_end 缺失/非法）→ instant_signup：
      立即报名/抢名额，从本次启动立即执行。
    截止缺失沿用 worker 的安全回退，不误当成已截止；
    活动举办时间 act_start/act_end 不参与分类——它只用于冲突检查。
    """
    now = default_now() if now is None else now
    end = parse_timestamp(sign_end)
    if end is not None and end.timestamp() <= now:
        return "closed"
    start = parse_timestamp(sign_start)
    if start is not None and start.timestamp() > now:
        return "pre_register"
    return "instant_signup"


def intervals_overlap(start_a: datetime, end_a: datetime,
                      start_b: datetime, end_b: datetime) -> bool:
    """按半开区间判断举办时间是否重叠，首尾相接不算冲突。"""
    return start_a < end_b and start_b < end_a


@dataclass(frozen=True)
class TaskSpec:
    """协调器需要的最小活动快照，避免后续列表刷新改变任务目标。

    sign_end / retry_for_s / max_attempts 是计划级重试策略：
    同一计划里每场共用策略，sign_end 仍按活动各自的截止时间截断。
    """

    task_id: str
    activity_id: str
    name: str
    sign_start: str
    act_start: str
    act_end: str
    headed: bool = False
    sign_end: str = ""
    retry_for_s: int = 120
    max_attempts: int = 0
    # 报名模式在计划创建时定格：之后时间推移不改写快照里的模式与文案
    signup_mode: str = ""
    # 预报名提前准备窗口（秒）：协调器在 sign_start-lead_s 启动 worker，
    # worker 先打开详情页、开放前 5 秒进入盯按钮；快照携带，重载不丢
    lead_s: int = 100
    # 计划接纳时冻结的重试窗口起点（epoch 秒）：未来场=原定开放，
    # 已开放场=接纳时刻。worker 用它当 --retry-start——鉴权、解包、页面加载
    # 花掉的时间都消耗这段预算，慢页面不得把截止整体后移（R04）
    retry_started_at: float = 0.0

    @classmethod
    def from_activity(cls, activity: dict, task_id: str, headed: bool = False,
                      retry_for_s: int = 120, max_attempts: int = 0,
                      now: float | None = None, lead_s: int = 100) -> "TaskSpec":
        sign_start = str(activity.get("sign_start") or "")
        sign_end = str(activity.get("sign_end") or "")
        # 接纳时刻只取一次：同一计划里所有场次共用同一个 accepted_at，
        # 不能每场各取一次 time.time()（那会让先构建的场次窗口更长）
        accepted = float(default_now() if now is None else now)
        mode = classify_signup_mode(sign_start, sign_end, accepted)
        start_epoch = school_epoch(sign_start)
        # 未来场固定从原定开放起算；已开放场从计划接纳时刻起算
        frozen_start = start_epoch if (mode == "pre_register" and start_epoch is not None) \
            else accepted
        return cls(
            task_id=str(task_id),
            activity_id=str(activity.get("id") or ""),
            name=str(activity.get("name") or "活动"),
            sign_start=sign_start,
            act_start=str(activity.get("act_start") or ""),
            act_end=str(activity.get("act_end") or ""),
            headed=bool(headed),
            sign_end=sign_end,
            retry_for_s=int(retry_for_s),
            max_attempts=int(max_attempts),
            signup_mode=mode,
            lead_s=int(lead_s),
            retry_started_at=float(frozen_start),
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
            "sign_end": self.sign_end,
            "retry_for_s": self.retry_for_s,
            "max_attempts": self.max_attempts,
            "signup_mode": self.signup_mode,
            "lead_s": self.lead_s,
            "retry_started_at": self.retry_started_at,
        }


def validate_plan(activities: Iterable[dict], now: float | None = None) -> dict:
    """校验计划并返回冲突双方；结果可直接序列化给 UI。

    报名截止 sign_end 已过的活动报 sign_end_passed：报名资格看报名截止，
    活动举办时间只用于冲突与完整性检查，不因举办时间已开始/结束而拦截。
    """
    rows = list(activities or [])
    now_ts = default_now() if now is None else now
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
        sign_end = str(activity.get("sign_end") or "")
        act_start = str(activity.get("act_start") or "")
        act_end = str(activity.get("act_end") or "")
        if parse_timestamp(sign_start) is None:
            errors.append({"kind": "invalid_sign_start", "activity_id": aid, "name": name})
        if classify_signup_mode(sign_start, sign_end, now_ts) == "closed":
            errors.append({"kind": "sign_end_passed", "activity_id": aid,
                           "name": name, "sign_end": sign_end})
        start = parse_timestamp(act_start)
        end = parse_timestamp(act_end)
        if start is None or end is None:
            errors.append({"kind": "activity_time_unknown", "activity_id": aid, "name": name})
        elif end <= start:
            errors.append({"kind": "activity_time_invalid", "activity_id": aid, "name": name})
        specs.append(TaskSpec.from_activity(activity, "", bool(activity.get("headed")),
                                            now=now_ts))

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
        self.now = now or default_now
        self.sleep = sleep or time.sleep
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._events = {spec.task_id: threading.Event() for spec in self.specs}
        self._states = {spec.task_id: "queued" for spec in self.specs}
        # 运行时 checkpoint（本次启动/页面打开/盯按钮/重试截止）：worker 事件
        # 到达时写入，页面重载后从 plan snapshot 恢复真实节点，不用当前时间
        # 伪造历史。checkpoint 先到先得；attempts/last_reason 跟随最新提交。
        self._details: dict[str, dict] = {}
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

    def next_prepare_epoch(self) -> "float | None":
        """仍未终态场次里最早的准备时刻（业务 epoch）；没有返回 None。
        供父层在最早准备点前 20 秒请求一次时钟重校（决策 012）。"""
        with self._lock:
            pending = [spec for spec in self.specs
                       if self._states.get(spec.task_id) not in TERMINAL_TASK_STATES]
            if not pending:
                return None
            return min(self._prepare_ts(spec) for spec in pending)

    def _run(self) -> None:
        # 每场按自己的准备时刻调度：instant_signup 在计划启动后立即拉起
        # worker，pre_register 在原定开放前 lead_s 秒拉起（提前打开详情页，
        # 开放前 5 秒进入盯按钮）；同一准备时刻的任务仍同时启动。
        # 不再「按 sign_start 分组阻塞等待 + join 整批」——那会让 --lead 失效，
        # 也会让第一场的重试把后面所有批次推迟过自己的准备时刻。
        # 校时失效：不启动新 worker，取消未启动场次并保留已发生事实；
        # 无论走哪条路径都只在 finally 里发一次 plan_done。
        threads: list[threading.Thread] = []
        clock_lost = ""
        try:
            self._started_at = self.now()
            pending = [spec for spec in self.specs
                       if self._states.get(spec.task_id) not in TERMINAL_TASK_STATES]
            for spec in pending:
                self._set_state(spec.task_id, "waiting")
            pending.sort(key=self._prepare_ts)
            index = 0
            while index < len(pending) and not self._stop.is_set():
                prepare_ts = self._prepare_ts(pending[index])
                while self.now() < prepare_ts and not self._stop.is_set():
                    if all(self._events[s.task_id].is_set() for s in pending[index:]):
                        break                     # 剩余任务全部取消：不再等这个时刻
                    self.sleep(min(0.2, max(0.01, prepare_ts - self.now())))
                if self._stop.is_set():
                    break
                while index < len(pending) and self._prepare_ts(pending[index]) == prepare_ts:
                    spec = pending[index]
                    index += 1
                    if self._events[spec.task_id].is_set():
                        continue                  # 已取消/停止：cancel 路径已置终态
                    thread = threading.Thread(target=self._run_one, args=(spec,),
                                              name="signup-task-%s" % spec.task_id,
                                              daemon=True)
                    threads.append(thread)
                    thread.start()
        except ClockUnavailable as exc:
            clock_lost = str(exc)
            # 不启动新 worker；未启动场次如实标「本场未启动」，不冒充学校拒绝
            for task_id, state in list(self._states.items()):
                if state in {"queued", "waiting"}:
                    self._set_state(task_id, "canceled",
                                    {"reason": "北京时间暂不可用，本场未启动"})
            # 已启动场次由各自 worker 的时钟门禁收尾；这里只唤醒等待者
            for event in self._events.values():
                event.set()
        finally:
            # 不在批次间 join：任务线程独立跑完（重试中的场次不阻塞后续调度）；
            # 收尾统一等待全部落定，再发唯一一次 plan_done。校时失效时 join 有界，
            # 不让一个永不结束的等待把 plan_done 藏起来。
            self._join_threads(threads, None if not clock_lost
                               else max(60.0, float(max(
                                   [int(spec.retry_for_s or 0) for spec in self.specs] or [0])))
                               + 30.0)
            self.emit("plan_done", {
                "plan_id": self.plan_id,
                "status": self._plan_status(),
                "ok": self._plan_status() == "completed",
                "clock_unavailable": clock_lost or None,
                "summary": self._summary(),
            })

    @staticmethod
    def _join_threads(threads: list, timeout_s: "float | None" = None) -> None:
        """默认等全部场次落定；给了 timeout_s 时按总时长有界等待（失效收尾用）。"""
        if timeout_s is None:
            for thread in threads:
                thread.join()
            return
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)

    def _prepare_ts(self, spec: TaskSpec) -> float:
        """任务的 worker 启动时刻（支持注入 now 的 fake clock）。

        instant_signup → 计划启动时刻（立即执行，不等历史 sign_start）；
        pre_register → 原定开放前 lead_s 秒（提前准备窗口）；
        模式未指定（旧快照/直接构造）沿用旧行为等到 sign_start；
        开放时刻缺失/非法退回计划启动时刻，可执行性由校验层保证。
        """
        if spec.signup_mode == "instant_signup":
            return self._started_at
        start = parse_timestamp(spec.sign_start)
        if start is None:
            return self._started_at
        lead = max(0, int(spec.lead_s or 0)) if spec.signup_mode == "pre_register" else 0
        return start.timestamp() - lead

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
            # detail 合并进运行时快照：checkpoint 字段先到先得（重放/迟到的
            # 同名字段不得改写已发生的节点，空值不落库）；其余字段跟随最新。
            merged = self._details.setdefault(task_id, {})
            for key, value in (detail or {}).items():
                if key == "status":
                    continue
                if key in CHECKPOINT_FIELDS and (value in (None, "", 0, False)
                                                 or merged.get(key) is not None):
                    continue
                merged[key] = value
            payload = {
                "plan_id": self.plan_id,
                "task_id": task_id,
                "activity_id": spec.activity_id,
                "name": spec.name,
                "sign_start": spec.sign_start,
                "sign_end": spec.sign_end,
                "signup_mode": spec.signup_mode,
                "retry_for_s": spec.retry_for_s,
                "max_attempts": spec.max_attempts,
                "status": status,
            }
            payload.update(merged)
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
        # 静态 TaskSpec + 动态 timeline detail 一起输出：页面重载恢复时
        # 拿得到完整运行时数据（checkpoint/模式/策略），不退回估算时间。
        # detail 在合并序里靠后，与 task 事件「payload.update(detail)」同序。
        return {
            "plan_id": self.plan_id,
            "status": status,
            "tasks": [
                dict(spec.as_dict(), status=self._states.get(spec.task_id, "queued"),
                     **self._details.get(spec.task_id, {}))
                for spec in self.specs
            ],
        }
