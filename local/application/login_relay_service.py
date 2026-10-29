# -*- coding: utf-8 -*-
"""手机重登中继的父进程侧：凭据预备、子进程会话、stdin 发送与公开状态投影。

凭据只活在当前进程内存：本机 js_api 预备、stdin 单次下发后清掉临时副本，
绝不写 prefs/文件/命令行/env/日志；手机 HTTP 白名单也读不到这份数据。
一个 Api 只有一个在途 relay（singleflight），与 _spawn/start_plan 同一把
关闭屏障锁；shutdown/stop_phone_access 有界取消。任何响应只暴露布尔与
安全状态，不回显账号密码。

@author LiXinwen
"""
from __future__ import annotations

import json
import queue
import secrets
import threading

from domain.phone_rules import clip_text

# relay 公开状态的阶段名（手机/桌面据此显示，不含任何凭据）
STATE_IDLE = "idle"
STATE_NEEDS_LOCAL_SETUP = "needs_local_setup"
STATE_PLAN_ACTIVE = "plan_active"
STATE_STARTING = "starting"
STATE_WAITING_CODE = "waiting_code"
STATE_VERIFYING = "verifying"
STATE_SUCCESS = "success"
STATE_FAILED = "failed"
STATE_CANCELED = "canceled"

_SAFE_ERRORS = {
    "needs_local_setup", "plan_active", "terms_required", "busy",
    "spawn_failed", "bad_page", "bad_response", "token_unverified",
    "expired", "canceled", "eof", "timeout", "internal", "demo",
}


def public_state(flow) -> dict:
    """公开状态投影：只出布尔与安全码，任何凭据/图像外的秘密都不得出现。"""
    if flow is None:
        return {"ready": False, "state": STATE_IDLE, "flow_id": "",
                "challenge_id": "", "image": "", "expires_in": 0,
                "attempts": 0, "max_attempts": 0, "safe_error": ""}
    return {
        "ready": True,
        "state": str(flow.get("state") or STATE_IDLE),
        "flow_id": str(flow.get("flow_id") or ""),
        "challenge_id": str(flow.get("challenge_id") or ""),
        "image": str(flow.get("image") or ""),
        "expires_in": max(0, int(flow.get("expires_in") or 0)),
        "attempts": max(0, int(flow.get("attempts") or 0)),
        "max_attempts": max(0, int(flow.get("max_attempts") or 0)),
        "safe_error": str(flow.get("safe_error") or ""),
    }


class LoginRelayService:
    """手机重登中继：单飞会话管理 + 子进程 stdin/stdout 桥。"""

    def __init__(self, *, spawn, now, emit=None, log=None,
                 on_success=None, on_finish=None):
        self._spawn = spawn          # (args) -> 进程树句柄（stdin/stdout 在 proc 上）
        self._now = now              # 单调秒读数（deadline 用）
        self._emit = emit or (lambda _kind, _payload: None)
        self._log = log or (lambda _msg: None)
        self._on_success = on_success or (lambda: None)
        self._on_finish = on_finish or (lambda: None)
        self._lock = threading.Lock()
        self._staged = None          # {"username","password"}：仅进程内存
        self._flow = None            # 当前在途 flow（dict）
        self._last = None            # 最近一场的公开终态（手机显示收尾结论）
        self._proc = None
        self._stdin = None
        self._reader = None
        self._pump_thread = None
        self._lines: "queue.Queue[str]" = queue.Queue()
        self._reader_done = threading.Event()

    # ---------- 本机凭据预备（desktop js_api） ----------
    def prepare(self, username, password) -> dict:
        """暂存账号密码到进程内存；响应只回 ready 布尔，绝不回显内容。"""
        user = str(username or "")
        secret = str(password or "")
        if not user or not secret:
            with self._lock:
                self._staged = None
            return {"ok": False, "ready": False, "error": "needs_local_setup"}
        with self._lock:
            self._staged = {"username": user, "password": secret}
        return {"ok": True, "ready": True}

    def prepared(self) -> bool:
        with self._lock:
            return self._staged is not None

    def clear_staged(self) -> None:
        with self._lock:
            self._staged = None

    # ---------- 会话生命周期 ----------
    def begin(self, *, plan_active: bool = False, terms_ok: bool = True,
              demo: bool = False, worker_command=None) -> dict:
        """开始一场中继：单飞、条款、计划活跃都在这里拦下。"""
        with self._lock:
            if demo:
                return {"ok": False, "error": "demo",
                        "text": "演示模式不联网，不会启动重登。"}
            if self._flow is not None:
                return {"ok": False, "error": "busy", "text": "已有一次重登在进行"}
            if not terms_ok:
                return {"ok": False, "error": "terms_required",
                        "text": "请先在电脑上同意使用条款"}
            if plan_active:
                return {"ok": False, "error": "plan_active",
                        "text": "报名计划仍在运行：先停止并等终态，再重登"}
            if self._staged is None:
                return {"ok": False, "error": "needs_local_setup",
                        "text": "请先在电脑上准备账号密码"}
            creds = dict(self._staged)
        if worker_command is None:
            return {"ok": False, "error": "spawn_failed", "text": "缺少 worker 命令"}
        flow_id = secrets.token_hex(8)
        args = list(worker_command) + ["--relay"]
        try:
            tree = self._spawn(args)
        except Exception as exc:  # noqa: BLE001 - 只回类型，不回堆栈
            return {"ok": False, "error": "spawn_failed",
                    "text": "重登进程没能启动：%s" % type(exc).__name__}
        proc = getattr(tree, "proc", tree)
        stdin = getattr(proc, "stdin", None)
        if stdin is None:
            try:
                tree.terminate()
            except Exception:  # noqa: BLE001
                pass
            return {"ok": False, "error": "spawn_failed", "text": "重登进程没有输入管道"}
        flow = {"flow_id": flow_id, "state": STATE_STARTING, "challenge_id": "",
                "image": "", "expires_in": 0, "attempts": 0, "max_attempts": 3,
                "safe_error": "", "tree": tree, "success_fired": False}
        with self._lock:
            self._flow = flow
            self._proc = tree
            self._stdin = stdin
        # 首行下发凭据后立刻清掉这份临时副本；预备库保留到关闭/显式清除
        self._send({"kind": "credentials", "flow_id": flow_id,
                    "username": creds["username"], "password": creds["password"]})
        creds.clear()
        self._reader_done.clear()
        self._lines = queue.Queue()
        self._reader = threading.Thread(target=self._read_stdout,
                                        args=(proc,), name="relay-out", daemon=True)
        self._reader.start()
        self._pump_thread = threading.Thread(target=self._pump, args=(flow,),
                                             name="relay-pump", daemon=True)
        self._pump_thread.start()
        return {"ok": True, "flow_id": flow_id}

    def submit(self, flow_id, challenge_id, code) -> dict:
        with self._lock:
            flow = self._flow
            stdin = self._stdin
        if flow is None:
            return {"ok": False, "error": "no_flow", "text": "当前没有进行中的重登"}
        if str(flow_id or "") != str(flow.get("flow_id") or ""):
            return {"ok": False, "error": "stale_flow", "text": "这次重登已经结束"}
        if stdin is None:
            return {"ok": False, "error": "no_flow", "text": "重登进程已结束"}
        self._send({"kind": "submit", "flow_id": flow.get("flow_id"),
                    "challenge_id": str(challenge_id or ""),
                    "code": str(code or "")})
        return {"ok": True}

    def cancel(self) -> dict:
        with self._lock:
            flow = self._flow
            tree = self._proc
            self._flow = None
            self._proc = None
            self._stdin = None
        if flow is not None:
            flow["state"] = STATE_CANCELED
            with self._lock:
                self._last = public_state(flow)
        if tree is not None:
            try:
                tree.terminate()
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True}

    def shutdown(self) -> None:
        """关闭工具时收口：取消中继并清除预备凭据。"""
        self.cancel()
        self.clear_staged()
        if self._reader is not None:
            self._reader.join(timeout=2.0)
        if self._pump_thread is not None:
            self._pump_thread.join(timeout=2.0)

    def state(self) -> dict:
        with self._lock:
            flow = self._flow
            if flow is not None:
                return public_state(flow)
            return dict(self._last) if self._last is not None else public_state(None)

    def observe_line(self, line: str) -> None:
        """_pump 识别 [login] 结构行后转交这里；无在途 flow 时静默丢弃。"""
        with self._lock:
            flow = self._flow
        if flow is not None:
            self._handle_line(flow, str(line or ""))

    # ---------- 内部：stdout 读取与结构行分发 ----------
    def _send(self, payload: dict) -> None:
        line = json.dumps(payload, ensure_ascii=False)
        try:
            self._stdin.write(line + "\n")
            self._stdin.flush()
        except Exception:  # noqa: BLE001 - 管道断了由收口逻辑处理
            pass

    def _read_stdout(self, proc) -> None:
        try:
            for line in proc.stdout or []:
                self._lines.put(line.rstrip("\r\n"))
        finally:
            self._reader_done.set()

    def _pump(self, flow) -> None:
        """逐行取 [login] 结构行更新 flow；结束时收口，不回调 HTTP 客户端。"""
        while True:
            try:
                line = self._lines.get(timeout=0.2)
            except queue.Empty:
                if self._reader_done.is_set():
                    break
                continue
            if not line:
                continue
            self._log(clip_text(line, 200))
            self._handle_line(flow, line)
        self._finish(flow)

    def _handle_line(self, flow, line: str) -> None:
        prefix = "[login] "
        if not line.startswith(prefix):
            return
        try:
            data = json.loads(line[len(prefix):])
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        if str(data.get("flow_id") or "") != str(flow.get("flow_id") or ""):
            return                      # 旧 flow 的行不得改写当前会话
        kind = str(data.get("kind") or "")
        if kind == "challenge":
            flow["state"] = STATE_WAITING_CODE
            flow["challenge_id"] = str(data.get("challenge_id") or "")
            image = str(data.get("image") or "")
            flow["image"] = image if image.startswith("data:image/png;base64,") else ""
            flow["expires_in"] = max(0, int(data.get("expires_in") or 0))
            flow["attempts"] = max(0, int(data.get("attempts") or 0))
            flow["max_attempts"] = max(0, int(data.get("max_attempts") or 3))
        elif kind == "rejected":
            flow["attempts"] = max(0, int(data.get("attempts") or 0))
            flow["safe_error"] = _safe(data.get("reason"))
        elif kind == "verifying":
            flow["state"] = STATE_VERIFYING
        elif kind == "success":
            flow["state"] = STATE_SUCCESS
            flow["safe_error"] = ""
            if not flow.get("success_fired"):
                flow["success_fired"] = True
                self._on_success()
        elif kind in ("failed", "canceled"):
            flow["state"] = (STATE_FAILED if kind == "failed" else STATE_CANCELED)
            flow["safe_error"] = _safe(data.get("error"))
        self._emit("phone_relogin", public_state(flow))

    def _finish(self, flow) -> None:
        with self._lock:
            if self._flow is flow:
                self._flow = None
                self._proc = None
                self._stdin = None
        if flow.get("state") in (STATE_STARTING, STATE_WAITING_CODE,
                                 STATE_VERIFYING):
            flow["state"] = STATE_FAILED
            flow["safe_error"] = flow.get("safe_error") or "eof"
        tree = flow.get("tree")
        if tree is not None:
            try:
                tree.terminate()
            except Exception:  # noqa: BLE001
                pass
        snapshot = public_state(flow)
        with self._lock:
            self._last = snapshot
        self._emit("phone_relogin", snapshot)
        try:
            self._on_finish()
        except Exception:  # noqa: BLE001
            pass


def _safe(value) -> str:
    text = str(value or "")
    return text if text in _SAFE_ERRORS else "internal"
