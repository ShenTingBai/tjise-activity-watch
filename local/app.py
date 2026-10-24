#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校园活动自动报名 · 本机版（WebView2 外壳）。

界面在 ui/ 下用 HTML/CSS 写，这里只做三件事：

1. 起窗口、加载 ui/index.html；
2. 把 grab.py 的能力暴露成页面可调的方法（window.pywebview.api.*）；
3. 把子进程输出实时推给页面（window.onBackend.log / .state / .done）。

业务逻辑一律留在 grab.py：换界面不用碰它，界面出问题也能直接命令行复现。

@author LiXinwen
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import webview

from signup_plan import PlanCoordinator, TaskSpec, classify_signup_mode, validate_plan

# 登录态/偏好统一数据根：开发用 local/data，冻结 EXE 用 %LOCALAPPDATA%
# （TJISE_DATA_DIR 可覆盖），与 grab-worker 解析出同一份登录文件
from runtime_paths import DATA_DIR, SESSION_FILE, UI_STATE_FILE, ensure_data_dir

HERE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", HERE))
UI = RESOURCE_ROOT / "ui" / "index.html"
GRAB = HERE / ("grab-worker.exe" if getattr(sys, "frozen", False) else "grab.py")
PREFS = UI_STATE_FILE
TOKEN_LIFETIME_H = 17.5

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# --demo：用假数据起界面，方便调样式、截图，不需要登录
DEMO = False


def demo_items() -> list[dict]:
    now = time.time()
    raw = [
        # lab 与 career 报名同时开放，但举办时段重叠；workshop 同批开放却不重叠。
        ("demo-security", "网络安全·数字时代大长城", "实践学习", "教学 A301", -3, 3600, 7200, 10800, False),
        ("demo-face", "我与学生面对面（天软信息化发展之路）", "讲座", "报告厅", -7200, 7200, -3600, 1800, True),
        ("demo-lab", "程序设计实验室开放日", "实践学习", "实训楼 B204", 1800, 5400, 7200, 10800, False),
        ("demo-career", "简历门诊：一对一改简历", "就业指导", "线上", 1800, 5400, 9000, 12600, False),
        ("demo-workshop", "创新作品夜场导览", "创新实践", "创新中心展厅", 1800, 5400, 14400, 16200, False),
        ("demo-volunteer", "图书馆志愿服务（第 12 期）", "志愿服务", "图书馆一层", 86400, 90000, 172800, 176400, False),
        # 活动举办时间缺失：演示「不能静默加入计划」的校验边界（act 两项传 None）
        ("demo-exhibit", "宿舍文化节摄影征集", "文化活动", "线上", 90000, 93600, None, None, False),
    ]
    flags = {
        "demo-security": (True, False),
        "demo-face": (False, False),
        "demo-lab": (True, True),
        "demo-career": (False, True),
        "demo-workshop": (True, False),
        "demo-volunteer": (None, None),
        "demo-exhibit": (None, True),
    }
    out = []
    for i, (aid, name, kind, place, s_off, e_off, act_s_off, act_e_off, signed) in enumerate(raw):
        start = datetime.fromtimestamp(now + s_off).strftime("%Y-%m-%d %H:%M:%S")
        end = datetime.fromtimestamp(now + e_off).strftime("%Y-%m-%d %H:%M:%S")
        # 举办时间未公布时保持空串，和 grab.py list --json 的缺失形态一致
        act_start = "" if act_s_off is None else datetime.fromtimestamp(now + act_s_off).strftime("%Y-%m-%d %H:%M:%S")
        act_end = "" if act_e_off is None else datetime.fromtimestamp(now + act_e_off).strftime("%Y-%m-%d %H:%M:%S")
        checkin_required, checkout_required = flags.get(aid, (None, None))
        out.append({"index": i + 1, "id": aid, "name": name, "type": kind,
                    "place": place, "sign_start": start, "sign_end": end,
                    "act_start": act_start, "act_end": act_end, "signed": signed,
                    "checkin_required": checkin_required, "checkout_required": checkout_required,
                    # 状态文案与 grab.py list --json 同一套中文口径，避免演示与真实契约不一致
                    "state": "已报名" if signed else (
                        "开放中" if s_off <= 0 else "未开放"),
                    "seconds_to_open": s_off})
    return out


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # 把主程序解析好的数据根传给 worker：即使 worker 所在目录不同，
    # 两侧也会读写同一份 session/storage_state
    env["TJISE_DATA_DIR"] = str(DATA_DIR)
    return env


def worker_command(*args: str) -> list[str]:
    """普通 Python 调 grab.py；冻结主程序改调同目录的独立 worker exe。"""
    if getattr(sys, "frozen", False):
        return [str(GRAB)] + list(args)
    return [sys.executable, str(GRAB)] + list(args)


def load_prefs() -> dict:
    try:
        return json.loads(PREFS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_prefs(prefs: dict) -> None:
    # 偏好与登录态同根：先确保统一数据目录存在，避免首次保存时目录还没建
    ensure_data_dir()
    tmp = PREFS.with_suffix(".tmp")
    tmp.write_text(json.dumps(prefs, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, PREFS)


def token_hours_left() -> float | None:
    """按 session.json 的 saved_at 估算剩余时间（足够给界面显示，不参与判断）。"""
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        saved = datetime.strptime(data["saved_at"], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None
    used = (datetime.now() - saved).total_seconds() / 3600.0
    return max(0.0, TOKEN_LIFETIME_H - used)


def login_state() -> dict:
    """登录三态快照：界面靠它区分「从没登录过」和「登录过期」。

    「没有登录」= session.json 不存在、无法解析或缺少 token；
    只有 token 还在但已过期才叫登录态失效。缺 token 时 hours_left
    必须是 None——按 saved_at 报一个假的小时数会把空壳会话显示成有效。
    """
    state = {"session_present": False, "session_expired": False, "hours_left": None}
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        token = str(data.get("token") or "").strip()
    except Exception:
        return state
    if not token:
        return state
    state["session_present"] = True
    state["hours_left"] = token_hours_left()
    state["session_expired"] = state["hours_left"] is not None and state["hours_left"] <= 0
    return state


class Api:
    def __init__(self) -> None:
        self._window: webview.Window | None = None
        self._proc: subprocess.Popen | None = None
        self._plan: PlanCoordinator | None = None
        self._plan_procs: dict[str, subprocess.Popen] = {}
        self._activities: dict[str, dict] = {}
        self._lock = threading.Lock()
        # 刷新去重：连点「刷新」不该堆起多个 check/list 子进程
        self._checking = False
        self._listing = False
        # 最近一次 check 的四态结论：start_plan 的放行/拒绝只信它
        self._last_check: dict = {}

    # ---------- 事件推送 ----------
    def _emit(self, kind: str, payload) -> None:
        if not self._window:
            return
        js = "window.onBackend && window.onBackend.%s(%s)" % (
            kind, json.dumps(payload, ensure_ascii=False))
        try:
            self._window.evaluate_js(js)
        except Exception:
            pass

    # ---------- 读数据 ----------
    def bootstrap(self) -> dict:
        prefs = load_prefs()
        payload = {
            "version": "3.0.0",
            "prefs": prefs,
            "help": self.help(),
            "login": login_state(),
            "busy": self._is_busy(),
            # demo 标记：界面据此放行演示计划（真实模式仍要求登录校验 valid）
            "demo": DEMO,
        }
        with self._lock:
            plan = self._plan
        if plan is not None:
            # 页面重载会丢掉 plan/task 事件：把计划快照一并交给 bootstrap，
            # 界面才能恢复任务列表和「停止」按钮；计划本身不会重复提交。
            payload["plan"] = plan.snapshot()
        return payload

    def help(self) -> dict:
        return {
            "title": "天软活动预报名工具",
            "subtitle": "选好多场活动，开放时按批次自动提交",
            "steps": [
                {"no": 1, "title": "先登录",
                 "text": "点「登录」完成首次登录。之后重开会自动检查，只有失效才重登。"},
                {"no": 2, "title": "选活动",
                 "text": "把想报的场次加入计划。活动时间重叠时只能保留其中一场；"
                         "同刻开放但不重叠的场次可以一起提交。"},
                {"no": 3, "title": "保持在线",
                 "text": "电脑别睡眠、别断网；窗口可以最小化，但不要关掉。"
                         "每场活动都会单独显示结果。"},
            ],
            "note": "只给本人账号使用，不要借别人的账号；同一计划由本机统一协调。",
        }

    def front_ready(self) -> dict:
        # 仅排障：前端 boot 完成时打一声招呼，确认桥接真的通了
        self._trace("front_ready")
        return {"ok": True}

    def check(self) -> dict:
        # js_api 的处理跑在 UI 线程上：一旦在这里跑 subprocess，
        # 窗口会直接"未响应"。所以只负责起线程，结果用事件推回去。
        with self._lock:
            if self._checking:
                return {"ok": True, "queued": True, "skipped": "已在检查登录态"}
            self._checking = True
        threading.Thread(target=self._do_check, daemon=True).start()
        return {"ok": True, "queued": True}

    def list_activities(self) -> dict:
        with self._lock:
            if self._listing:
                return {"ok": True, "queued": True, "skipped": "已经在拉活动列表"}
            self._listing = True
        threading.Thread(target=self._do_list, daemon=True).start()
        return {"ok": True, "queued": True}

    def _do_check(self) -> None:
        try:
            try:
                res = self._run(worker_command("check"), timeout=90)
                code, out = res["code"], res["out"]
            except Exception as exc:  # worker 起不来（缺文件/权限）也必须给界面一个结论
                code, out = -1, "[X] 本机脚本没能启动：%s" % type(exc).__name__
            # 归因只信第一条 [X] 来源行：失败输出的末尾常常是「请重新登录」指引，
            # 拿最后一行分类会把网络错误误判成登录失效。末行仍作 text 供时钟展示。
            reason, text = _check_reason_line(out)
            # 三态字段跟结果一起发：check 失败可能是「没登录过」也可能是「过期」，
            # 界面只看 ok 会把首次使用误判成登录态失效。
            state = login_state()
            result = _check_result(state, code, reason or text)
            self._last_check = {"result": result, "checked_at": time.time()}
            self._emit("check", {
                "ok": code == 0,
                "hours_left": state["hours_left"],
                "session_present": state["session_present"],
                "session_expired": state["session_expired"],
                "text": text,
                # 来源行（附加字段）：界面日志显示真实原因，而不是末尾的登录指引
                "reason": reason,
                # check 只回最后一行（R16）：给界面一个可区分的错误分类，
                # 免得把「没登录」和「断网」都笼统当失败。
                "hint": _check_hint(reason or text, code),
                # 四态结论（附加字段）：valid=接口确认有效 / expired=4001 失效 /
                # none=本机无会话 / unknown=网络或接口异常——异常不得误报成重新登录
                "result": result,
            })
        finally:
            with self._lock:
                self._checking = False

    def _do_list(self) -> None:
        try:
            self._do_list_inner()
        finally:
            with self._lock:
                self._listing = False

    def _do_list_inner(self) -> None:
        if DEMO:
            items = demo_items()
            self._activities = {str(it["id"]): it for it in items}
            self._emit("activities", {"ok": True, "items": items})
            return
        res = self._run(worker_command("list", "--json"), timeout=120)
        if res["code"] != 0:
            self._emit("activities", {"ok": False,
                                      "text": res["out"].strip(), "items": []})
            return
        try:
            items = json.loads(res["out"].strip() or "[]")
        except json.JSONDecodeError:
            self._emit("activities", {"ok": False,
                                      "text": "活动列表解析失败", "items": []})
            return
        now = time.time()
        for it in items:
            it["seconds_to_open"] = _seconds_until(it.get("sign_start"), now)
        self._activities = {str(it.get("id")): dict(it) for it in items if it.get("id")}
        self._emit("activities", {"ok": True, "items": items})

    # ---------- 动作 ----------
    def login(self) -> dict:
        with self._lock:
            if self._plan and self._plan.snapshot().get("status") in {"running", "ready"}:
                return {"ok": False, "text": "报名计划正在运行，先停止计划再登录"}
        return self._spawn(worker_command("login"), "登录", kind="login")

    def grab(self, activity_id: str, name: str, at: str = "",
             headed: bool = False) -> dict:
        args = worker_command("grab", "--activity-id", activity_id, "--name", name)
        if at:
            args += ["--at", at]
        if headed:
            args.append("--headed")
        return self._spawn(args, "等待并抢报名：%s" % name, kind="grab")

    def start_plan(self, items: list[dict], headed: bool = False,
                   retry_policy: dict | None = None) -> dict:
        """校验并启动计划；同一开放批次由协调器并发拉起单活动脚本。

        retry_policy 是计划级重试策略：{retry_for_s, max_attempts}，
        0 分别表示「直到报名截止」和「不限次数」；非法值回落默认
        （120 秒 / 不限），不让脏输入把 worker 挂成无限重试。
        """
        if not isinstance(items, list) or not items:
            return {"ok": False, "text": "先至少选择一场活动"}
        with self._lock:
            if self._is_busy_locked():
                return {"ok": False, "text": "已有登录、报名或计划在运行，先停止后再开始"}
        # 真实计划只在学校接口确认登录有效后开始（最终防线，界面按钮门禁之外）：
        # checking / unknown / none / expired 一律拒绝并给出下一步；demo 没有真实
        # 登录态，继续走演示流程不受此限。
        if not DEMO:
            with self._lock:
                checking = self._checking
                last = dict(self._last_check)
            if checking:
                return {"ok": False, "text": "正在检查登录态，等这次检查完成再开始"}
            result = last.get("result") or "unknown"
            if result == "valid":
                pass                     # 学校接口确认有效：唯一放行形态
            elif result == "none":
                return {"ok": False, "text": "还没有登录：点右上角「登录」先登录一次"}
            elif result == "expired":
                return {"ok": False, "text": "登录态已失效：点右上角「重新登录」后再开始"}
            else:
                return {"ok": False,
                        "text": "暂时无法确认登录态：点「刷新」重试，确认登录有效后再开始"}

        def _policy_int(value, fallback: int, minimum: int = 0) -> int:
            try:
                number = int(value)
            except (TypeError, ValueError):
                return fallback
            return number if number >= minimum else fallback

        policy = retry_policy if isinstance(retry_policy, dict) else {}
        retry_for_s = _policy_int(policy.get("retry_for_s"), 120)
        max_attempts = _policy_int(policy.get("max_attempts"), 0)

        normalized = []
        for item in items:
            current = dict(self._activities.get(str(item.get("id")), item))
            # 前端快照可能没有最新 seconds_to_open，但计划校验只信关键时间字段。
            current.update({key: item.get(key) for key in
                            ("id", "name", "sign_start", "sign_end", "act_start", "act_end", "signed")
                            if key in item})
            normalized.append(current)
        # 报名截止是硬门禁（前端复选/校验之外的后端最终防线）：
        # 已截止活动不能进入 worker；pre_register 与 instant_signup 是两个
        # 正式模式，混合计划允许共存，逐场按各自模式运行。
        closed = [item for item in normalized
                  if classify_signup_mode(str(item.get("sign_start") or ""),
                                          str(item.get("sign_end") or "")) == "closed"]
        if closed:
            names = "、".join("「%s」" % (item.get("name") or "活动") for item in closed[:3])
            more = " 等 %d 场" % len(closed) if len(closed) > 3 else ""
            return {"ok": False,
                    "text": "报名已截止：%s%s（报名截止 %s）不能加入计划，只能在列表里查看。"
                            % (names, more, closed[0].get("sign_end") or "时间未公布"),
                    "errors": [{"kind": "sign_end_passed", "activity_id": item.get("id"),
                                "name": item.get("name"), "sign_end": item.get("sign_end")}
                               for item in closed]}
        checked = validate_plan(normalized)
        if not checked["ok"]:
            return {"ok": False, "text": "计划里有不能同时报名的活动", "conflicts": checked["conflicts"],
                    "errors": checked["errors"]}

        plan_id = uuid.uuid4().hex[:12]
        specs = [TaskSpec.from_activity(item, "task-%02d-%s" % (i + 1, item["id"]), headed,
                                        retry_for_s=retry_for_s, max_attempts=max_attempts)
                 for i, item in enumerate(normalized)]

        def emit(kind: str, payload: dict) -> None:
            self._emit(kind, payload)
            if kind == "plan_done":
                with self._lock:
                    if self._plan is coordinator:
                        self._plan = None

        def runner(spec: TaskSpec, cancel_event: threading.Event,
                   status: callable) -> dict:
            if DEMO:
                # demo 只演示任务状态，不创建 grab.py 子进程，也不访问学校站点。
                status("submitting", {"detail": "演示：正在提交"})
                for _ in range(4):
                    if cancel_event.is_set():
                        return {"status": "canceled", "reason": "用户取消了这一场"}
                    time.sleep(0.05)
                if spec.activity_id.endswith("career"):
                    return {"status": "failed", "code": 2, "reason": "演示：名额已满"}
                return {"status": "success", "code": 0, "reason": "演示：报名成功"}
            return self._run_plan_task(spec, cancel_event, status)

        coordinator = PlanCoordinator(plan_id, specs, runner, emit)
        with self._lock:
            self._plan = coordinator
        coordinator.start()
        return {"ok": True, "queued": True, "plan_id": plan_id}

    def cancel_task(self, plan_id: str, task_id: str) -> dict:
        with self._lock:
            plan = self._plan
        if not plan or plan.plan_id != str(plan_id):
            return {"ok": False, "text": "计划已经结束"}
        ok = plan.cancel_task(str(task_id))
        return {"ok": ok, "text": "已请求取消这一场" if ok else "这一场已经结束"}

    def stop_plan(self, plan_id: str) -> dict:
        with self._lock:
            plan = self._plan
        if not plan or plan.plan_id != str(plan_id):
            return {"ok": False, "text": "当前没有这个计划"}
        # 幂等：重复停止同一个计划也返回 ok。顺序必须是「先置停协调器，
        # 再杀还活着的 worker」——反过来杀，等待中的批次可能在杀完之后
        # 又被拉起来。树终止不等 Playwright 自然超时，确认后立即生效。
        plan.stop()
        self._terminate_plan_procs()
        return {"ok": True, "text": "已停止计划"}

    def _terminate_plan_procs(self) -> None:
        """立刻按进程树终止计划里所有活着的 worker。"""
        with self._lock:
            procs = list(self._plan_procs.values())
        for proc in procs:
            _kill_tree(proc)

    def stop(self) -> dict:
        with self._lock:
            plan = self._plan
        if plan:
            return self.stop_plan(plan.plan_id)
        with self._lock:
            proc = self._proc
        if proc and proc.poll() is None:
            _kill_tree(proc)
            self._emit("log", "已请求停止")
            return {"ok": True}
        return {"ok": False, "text": "当前没有在跑的任务"}

    def open_data(self) -> dict:
        path = ensure_data_dir()
        os.startfile(str(path))            # noqa: S606 - Windows 桌面工具
        return {"ok": True}

    def save_prefs(self, prefs: dict) -> dict:
        merged = load_prefs()
        merged.update(prefs or {})
        save_prefs(merged)
        return {"ok": True}

    # ---------- 内部 ----------
    def _is_busy_locked(self) -> bool:
        if self._proc and self._proc.poll() is None:
            return True
        if self._plan:
            status = self._plan.snapshot().get("status")
            if status in {"ready", "running"}:
                return True
        return any(proc.poll() is None for proc in self._plan_procs.values())

    def _is_busy(self) -> bool:
        with self._lock:
            return self._is_busy_locked()

    def _run_plan_task(self, spec: TaskSpec, cancel_event: threading.Event,
                       status: callable) -> dict:
        """运行一个单活动脚本；取消只终止对应进程，不影响同批其他任务。"""
        # 已开放的场次不能再把原开放时刻传给脚本：grab.py 的重试窗口
        # 是「at + 120 秒」，开放超过 2 分钟的 at 会让循环一次都不进、
        # 直接报「名额已满」。这里把过期的 at 换成现在，窗口从现在起算。
        at_text = spec.sign_start
        try:
            at_ts = datetime.strptime(spec.sign_start, "%Y-%m-%d %H:%M:%S").timestamp()
        except (ValueError, TypeError):
            at_ts = None
        if at_ts is None or at_ts <= time.time():
            at_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        args = worker_command("grab", "--activity-id", spec.activity_id, "--name", spec.name,
                              "--at", at_text,
                              "--lead", str(max(0, int(spec.lead_s or 0))),
                              "--retry-for", str(spec.retry_for_s),
                              "--max-attempts", str(spec.max_attempts))
        if spec.sign_end:
            args += ["--sign-end", spec.sign_end]
        if spec.headed:
            args.append("--headed")
        proc = subprocess.Popen(
            args, cwd=str(HERE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            env=_env(), creationflags=CREATE_NO_WINDOW)
        with self._lock:
            self._plan_procs[spec.task_id] = proc
        if status:
            # run_started_at 必须在子进程创建之后记录：这是 worker 真正启动
            # 的时刻，不能提前写成计划启动时刻（预报名要靠它区分准备窗口）
            status("submitting", {"run_started_at": time.time()})
        self._emit("log", "▶ [%s] 等待并抢报名" % spec.name)

        lines: queue.Queue[str] = queue.Queue()
        reader_done = threading.Event()

        def read_output() -> None:
            try:
                for line in proc.stdout or []:
                    lines.put(line.rstrip("\r\n"))
            finally:
                reader_done.set()

        threading.Thread(target=read_output, name="signup-output", daemon=True).start()
        tail: list[str] = []
        canceled = False
        killed = False

        def feed(line: str) -> None:
            """输出行统一入口：落日志、存尾巴，并把 checkpoint 与 telemetry 推成任务 detail。

            时间戳取事件实际到达时刻（time.time()），前端据此显示真实节点；
            缺事件时才由前端按模式安全回退，这里不伪造。"""
            tail.append(line)
            del tail[:-40]
            self._emit("log", "[%s] %s" % (spec.name, line))
            if status and "详情页已加载" in line:
                status("submitting", {"page_opened_at": time.time()})
            if status and "开始盯按钮" in line:
                status("submitting", {"watch_started_at": time.time()})
            hit = _ATTEMPT_RE.match(line)
            if hit and status:
                # 只数真正发起 signUp 的行；刷新 / is_joined 查询不会长这样
                status("submitting", {"attempts": int(hit.group(1)),
                                      "last_reason": hit.group(2).strip()})
            parsed = _parse_retry_line(line)
            if parsed and status:
                # 结构化 retry 行到达即写统一 detail：重试截止不必等进程收尾
                # 才上屏，路线/任务卡/LED 与终态共用同一份 retry_deadline。
                # attempts/last_reason 不在这里推——提交行已逐次推过、终态
                # 事件会带最终值，再推一次只会让提交序列出现重复计数。
                stop = _retry_payload(parsed, spec,
                                      stop_reason=str(parsed.get("stop_reason") or ""))
                status("submitting", {"retry_deadline": stop.get("retry_deadline")})

        try:
            # 正常收尾要求 reader_done（输出读完）；取消路径只要求进程死透——
            # onefile 引导进程被树终止后，真实 worker 可能还握着 stdout 管道，
            # 再等 EOF 这条循环就永远出不去，canceled / plan_done 都发不出去。
            while True:
                if cancel_event.is_set() and not killed and proc.poll() is None:
                    canceled = True
                    killed = True
                    _kill_tree(proc)
                if proc.poll() is not None and (canceled or reader_done.is_set()):
                    break
                try:
                    while True:
                        line = lines.get_nowait()
                        if line:
                            feed(line)
                except queue.Empty:
                    pass
                time.sleep(0.05)
            while True:
                try:
                    line = lines.get_nowait()
                except queue.Empty:
                    break
                if line:
                    feed(line)
            code = proc.wait()
        finally:
            # 监控循环无论怎么退出（含异常），进程表都必须清干净——
            # 否则 _is_busy_locked 会把界面永远锁在「计划运行中」。
            # 顺带关掉输出管道：进程树已终止，读线程要么已 EOF 要么因关闭而退出。
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass
            with self._lock:
                self._plan_procs.pop(spec.task_id, None)
        # 结构化终态行 [retry] 是策略信息的第一来源，没有也不影响退出码语义
        stop: dict = {}
        for line in reversed(tail):
            parsed = _parse_retry_line(line)
            if parsed:
                stop = parsed
                break

        if canceled:
            return {"status": "canceled", "code": code, "reason": "用户取消了这一场",
                    **_retry_payload(stop, spec, stop_reason="user_stopped")}
        if any("你已经报过这个活动" in line for line in tail) or stop.get("stop_reason") == "already_signed":
            return {"status": "already_signed", "code": code, "reason": "你已经报过这一场",
                    **_retry_payload(stop, spec, stop_reason="already_signed")}
        if code == 0 and any("报名成功" in line for line in tail):
            return {"status": "success", "code": code, "reason": "报名成功",
                    **_retry_payload(stop, spec, stop_reason=stop.get("stop_reason") or "success")}
        reason = ""
        for line in reversed(tail):
            if line.startswith("[X] "):
                reason = line[4:].strip()
                break
        stop_reason = stop.get("stop_reason") or ("login_invalid" if code == 3 else "process_error")
        return {"status": "failed", "code": code,
                "reason": reason or "退出码 %s" % code,
                **_retry_payload(stop, spec, stop_reason=stop_reason)}

    def _trace(self, msg: str) -> None:
        """调试用：只有设了 TJISE_TRACE 才落盘，平时零副作用。"""
        path = os.environ.get("TJISE_TRACE")
        if not path:
            return
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(str(msg) + "\n")
        except Exception:
            pass

    def _run(self, args: list[str], timeout: int) -> dict:
        try:
            res = subprocess.run(args, cwd=str(HERE), capture_output=True,
                                 text=True, encoding="utf-8", errors="replace",
                                 env=_env(), timeout=timeout,
                                 creationflags=CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired:
            return {"code": -1, "out": "[X] 执行超时（%d 秒）" % timeout}
        out = (res.stdout or "") + (res.stderr or "")
        return {"code": res.returncode, "out": out}

    def _spawn(self, args: list[str], label: str, kind: str = "grab") -> dict:
        with self._lock:
            if self._is_busy_locked():
                return {"ok": False, "text": "已经有一个任务在跑，先等它结束"}
            self._proc = subprocess.Popen(
                args, cwd=str(HERE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=_env(), creationflags=CREATE_NO_WINDOW)
            proc = self._proc

        threading.Thread(target=self._pump, args=(proc, label, kind),
                         daemon=True).start()
        return {"ok": True}

    def _pump(self, proc: subprocess.Popen, label: str, kind: str = "grab") -> None:
        self._emit("log", "▶ " + label)
        tail: list[str] = []
        for line in proc.stdout or []:
            line = line.rstrip("\r\n")
            if not line:
                continue
            tail.append(line)
            del tail[:-40]
            self._emit("log", line)
        proc.wait()
        if kind == "login":
            # 登录只看退出码：grab.py login 成功时不会打印「报名成功」
            ok = proc.returncode == 0
        else:
            ok = proc.returncode == 0 and any("报名成功" in t for t in tail)
        reason = ""
        if not ok:
            for t in reversed(tail):
                if t.startswith("[X] "):
                    reason = t[4:].strip()
                    break
            reason = reason or ("退出码 %s" % proc.returncode)
        self._emit("done", {"ok": ok, "code": proc.returncode,
                            "reason": reason, "label": label, "kind": kind})
        with self._lock:
            if self._proc is proc:
                self._proc = None


def _seconds_until(ts: str | None, now: float) -> int | None:
    if not ts:
        return None
    try:
        return int(datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").timestamp() - now)
    except Exception:
        return None


def _kill_tree(proc: subprocess.Popen) -> None:
    """按进程树终止 worker：taskkill /F /T 连 Playwright node / chromium 一起收。

    只 terminate 直接子进程的话，PyInstaller onefile 的引导进程死后，
    解压出的真实 worker 会带着浏览器继续按重试节奏跑——就是
    「停止不了、还在无限重试」的那个形态。收尸（wait）交给调用方
    的监控循环，这里只负责让它死透。
    """
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=3,
                creationflags=CREATE_NO_WINDOW)
        except Exception:
            pass
    if proc.poll() is None:
        # taskkill 不可用/失败时的兜底：至少终止直接子进程
        try:
            proc.kill()
        except Exception:
            pass


def _parse_retry_line(line: str) -> dict | None:
    """解析 worker 的结构化终态行：[retry] attempts=N stop_reason=X retry_deadline=<epoch> sign_end=<epoch> last_reason=<原文>。

    时间字段是 epoch 秒（无空格）；last_reason 恒为最后一个字段（可含空格）。
    解析失败返回 None，调用方按「无策略信息」处理，不影响既有退出码语义。
    """
    text = str(line or "").strip()
    prefix = "[retry] "
    if not text.startswith(prefix):
        return None
    head, _, reason = text[len(prefix):].partition(" last_reason=")
    fields: dict = {}
    for pair in head.split(" "):
        key, _, value = pair.partition("=")
        if key:
            fields[key] = value
    if reason:
        fields["last_reason"] = reason
    return fields or None


def _retry_payload(stop: dict, spec: "TaskSpec", stop_reason: str) -> dict:
    """把 [retry] 行整理成任务终态附带的 telemetry 字段（缺省值兜底）。"""

    def epoch_or_blank(value) -> str:
        """epoch 秒文本；0/非法表示学校没给，落空串交给活动自身字段兜底。"""
        try:
            number = int(value or 0)
        except (TypeError, ValueError):
            return ""
        return str(number) if number > 0 else ""

    try:
        attempts = int((stop or {}).get("attempts", 0) or 0)
    except (TypeError, ValueError):
        attempts = 0
    payload = {
        "stop_reason": (stop or {}).get("stop_reason") or stop_reason,
        "attempts": attempts,
        "retry_deadline": epoch_or_blank((stop or {}).get("retry_deadline")),
        "sign_end": epoch_or_blank((stop or {}).get("sign_end")) or spec.sign_end,
    }
    if (stop or {}).get("last_reason"):
        payload["last_reason"] = stop["last_reason"]
    return payload


# worker 每次真正发起 signUp 时打印的行（带 [i] 前缀落日志）；
# 只匹配这个形状，刷新 / is_joined 查询不会撞上
_ATTEMPT_RE = re.compile(r"^(?:\[i\]\s*)?第 (\d+) 次提交[：:]\s*(.*)$")


def _check_reason_line(out: str) -> tuple[str, str]:
    """从 check 的完整输出提取 (来源行, 末行)。

    归因只信第一条 [X] 来源行——失败输出的末尾固定是「请重新登录」指引，
    拿末行分类会把网络错误误判成登录失效；末行仍返回给界面做时钟偏差展示。
    空输出返回 ("", "")，调用方按「认不出的失败」落 unknown。
    """
    lines = [ln.strip() for ln in str(out or "").strip().splitlines() if ln.strip()]
    reason = next((ln for ln in lines if ln.startswith("[X] ")), "")
    return reason, (lines[-1] if lines else "")


def _check_result(state: dict, code: int, text: str) -> str:
    """check 的四态结论：valid / expired / unknown / none（附加字段，旧 UI 可忽略）。

    只有学校接口真实成功才是 valid；本机没有会话是 none；能认出的登录失效
    （4001/登录类字样，且不含网络词）是 expired；网络类与认不出的失败一律
    unknown——网络异常不能被误报成「必须重新登录」。
    """
    if code == 0:
        return "valid"
    if not state.get("session_present"):
        return "none"
    hint = _check_hint(text, code)
    if hint == "network":
        return "unknown"
    if hint == "login":
        return "expired"
    return "unknown"


def _check_hint(text: str, code: int) -> str:
    """把 check 的来源行粗分类：network / login / error（成功返回空）。

    成功输出本来就常含「登录」字样（如「登录态还有 17 小时」），
    必须先按退出码认成功。归因顺序网络词优先：身份验证服务的超时、
    连接失败是网络问题而不是登录失效（「身份验证服务超时」不得误归
    login）；网络词都不命中时，4001/「登录」/「身份验证失败」才算登录类；
    其余含糊文本落 error（上游统一归 unknown，宁可暂不确认也不误导重登）。
    """
    raw = str(text or "")
    if code == 0:
        return ""
    if any(word in raw for word in ("网络", "连接", "超时", "URLError", "HTTPError", "timed out", "Timeout")):
        return "network"
    if "4001" in raw or "登录" in raw or "身份验证失败" in raw:
        return "login"
    return "error"


def main() -> int:
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    # 启动即确保统一数据根可用；冻结进程首次启动会顺带迁移旧 EXE 旁数据
    ensure_data_dir()
    global DEMO
    if "--demo" in sys.argv:
        DEMO = True
    if not UI.exists():
        print("找不到界面文件：%s" % UI, file=sys.stderr)
        return 1
    api = Api()
    window = webview.create_window(
        "天软活动预报名工具", str(UI),
        js_api=api, width=1120, height=800, min_size=(980, 700),
        background_color="#E9EEF6",
    )
    api._window = window
    webview.start(debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
