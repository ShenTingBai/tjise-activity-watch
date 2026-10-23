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
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import webview

from signup_plan import PlanCoordinator, TaskSpec, validate_plan

HERE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", HERE))
UI = RESOURCE_ROOT / "ui" / "index.html"
GRAB = HERE / ("grab-worker.exe" if getattr(sys, "frozen", False) else "grab.py")
PREFS = HERE / "data" / "ui.json"
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
    ]
    flags = {
        "demo-security": (True, False),
        "demo-face": (False, False),
        "demo-lab": (True, True),
        "demo-career": (False, True),
        "demo-workshop": (True, False),
        "demo-volunteer": (None, None),
    }
    out = []
    for i, (aid, name, kind, place, s_off, e_off, act_s_off, act_e_off, signed) in enumerate(raw):
        start = datetime.fromtimestamp(now + s_off).strftime("%Y-%m-%d %H:%M:%S")
        end = datetime.fromtimestamp(now + e_off).strftime("%Y-%m-%d %H:%M:%S")
        act_start = datetime.fromtimestamp(now + act_s_off).strftime("%Y-%m-%d %H:%M:%S")
        act_end = datetime.fromtimestamp(now + act_e_off).strftime("%Y-%m-%d %H:%M:%S")
        checkin_required, checkout_required = flags.get(aid, (None, None))
        out.append({"index": i + 1, "id": aid, "name": name, "type": kind,
                    "place": place, "sign_start": start, "sign_end": end,
                    "act_start": act_start, "act_end": act_end, "signed": signed,
                    "checkin_required": checkin_required, "checkout_required": checkout_required,
                    "state": "signed" if signed else (
                        "open" if s_off <= 0 else "waiting"),
                    "seconds_to_open": s_off})
    return out


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
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
    PREFS.parent.mkdir(exist_ok=True)
    tmp = PREFS.with_suffix(".tmp")
    tmp.write_text(json.dumps(prefs, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, PREFS)


def token_hours_left() -> float | None:
    """按 session.json 的 saved_at 估算剩余时间（足够给界面显示，不参与判断）。"""
    try:
        data = json.loads((HERE / "data" / "session.json").read_text(encoding="utf-8"))
        saved = datetime.strptime(data["saved_at"], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None
    used = (datetime.now() - saved).total_seconds() / 3600.0
    return max(0.0, TOKEN_LIFETIME_H - used)


class Api:
    def __init__(self) -> None:
        self._window: webview.Window | None = None
        self._proc: subprocess.Popen | None = None
        self._plan: PlanCoordinator | None = None
        self._plan_procs: dict[str, subprocess.Popen] = {}
        self._activities: dict[str, dict] = {}
        self._lock = threading.Lock()

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
        return {
            "version": "3.0.0",
            "prefs": prefs,
            "help": self.help(),
            "login": {"hours_left": token_hours_left()},
            "busy": self._is_busy(),
        }

    def help(self) -> dict:
        return {
            "title": "天软活动报名",
            "subtitle": "选好多场活动，开放时按批次自动提交",
            "steps": [
                {"no": 1, "title": "先登录",
                 "text": "点右上角「登录」，在弹出的 Chrome 里登录一次。"
                         "登录态大约能用 17 小时，过期后点同一个按钮重登。"},
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
        threading.Thread(target=self._do_check, daemon=True).start()
        return {"ok": True, "queued": True}

    def list_activities(self) -> dict:
        threading.Thread(target=self._do_list, daemon=True).start()
        return {"ok": True, "queued": True}

    def _do_check(self) -> None:
        res = self._run(worker_command("check"), timeout=90)
        self._emit("check", {
            "ok": res["code"] == 0,
            "hours_left": token_hours_left(),
            "text": (res["out"].strip().splitlines() or [""])[-1],
        })

    def _do_list(self) -> None:
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

    def start_plan(self, items: list[dict], headed: bool = False) -> dict:
        """校验并启动计划；同一开放批次由协调器并发拉起单活动脚本。"""
        if not isinstance(items, list) or not items:
            return {"ok": False, "text": "先至少选择一场活动"}
        with self._lock:
            if self._is_busy_locked():
                return {"ok": False, "text": "已有登录、报名或计划在运行，先停止后再开始"}
        normalized = []
        for item in items:
            current = dict(self._activities.get(str(item.get("id")), item))
            # 前端快照可能没有最新 seconds_to_open，但计划校验只信关键时间字段。
            current.update({key: item.get(key) for key in
                            ("id", "name", "sign_start", "act_start", "act_end", "signed")
                            if key in item})
            normalized.append(current)
        checked = validate_plan(normalized)
        if not checked["ok"]:
            return {"ok": False, "text": "计划里有不能同时报名的活动", "conflicts": checked["conflicts"],
                    "errors": checked["errors"]}

        plan_id = uuid.uuid4().hex[:12]
        specs = [TaskSpec.from_activity(item, "task-%02d-%s" % (i + 1, item["id"]), headed)
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
        return {"ok": plan.stop(), "text": "已请求停止计划"}

    def stop(self) -> dict:
        with self._lock:
            plan = self._plan
        if plan:
            return self.stop_plan(plan.plan_id)
        with self._lock:
            proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
            self._emit("log", "已请求停止")
            return {"ok": True}
        return {"ok": False, "text": "当前没有在跑的任务"}

    def open_data(self) -> dict:
        path = HERE / "data"
        path.mkdir(exist_ok=True)
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
        args = worker_command("grab", "--activity-id", spec.activity_id, "--name", spec.name,
                              "--at", spec.sign_start)
        if spec.headed:
            args.append("--headed")
        proc = subprocess.Popen(
            args, cwd=str(HERE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            env=_env(), creationflags=CREATE_NO_WINDOW)
        with self._lock:
            self._plan_procs[spec.task_id] = proc
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
        while not (reader_done.is_set() and proc.poll() is not None):
            if cancel_event.is_set() and proc.poll() is None:
                canceled = True
                proc.terminate()
            try:
                while True:
                    line = lines.get_nowait()
                    if not line:
                        continue
                    tail.append(line)
                    del tail[:-40]
                    self._emit("log", "[%s] %s" % (spec.name, line))
            except queue.Empty:
                pass
            time.sleep(0.05)
        while True:
            try:
                line = lines.get_nowait()
            except queue.Empty:
                break
            if line:
                tail.append(line)
                del tail[:-40]
                self._emit("log", "[%s] %s" % (spec.name, line))
        code = proc.wait()
        with self._lock:
            self._plan_procs.pop(spec.task_id, None)
        if canceled:
            return {"status": "canceled", "code": code, "reason": "用户取消了这一场"}
        if any("你已经报过这个活动" in line for line in tail):
            return {"status": "already_signed", "code": code, "reason": "你已经报过这一场"}
        if code == 0 and any("报名成功" in line for line in tail):
            return {"status": "success", "code": code, "reason": "报名成功"}
        reason = ""
        for line in reversed(tail):
            if line.startswith("[X] "):
                reason = line[4:].strip()
                break
        return {"status": "failed", "code": code,
                "reason": reason or "退出码 %s" % code}

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


def main() -> int:
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    global DEMO
    if "--demo" in sys.argv:
        DEMO = True
    if not UI.exists():
        print("找不到界面文件：%s" % UI, file=sys.stderr)
        return 1
    api = Api()
    window = webview.create_window(
        "天软活动报名", str(UI),
        js_api=api, width=1120, height=800, min_size=(980, 700),
        background_color="#E9EEF6",
    )
    api._window = window
    webview.start(debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
