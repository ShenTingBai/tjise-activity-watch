# -*- coding: utf-8 -*-
"""手机受限访问用例门面：配对、会话、动作派发与有界事件缓冲。

Api、此刻、状态读取与服务启动端口全部由 app 注入；这里不碰 Windows API、
不碰具体路径、不起真实浏览器。会话与配对码只活在内存里：停服务即撤销，
重开工具必须回电脑重新配对。所有锁都是本模块自己的，绝不与 Api/协调器的
锁互相等待（_emit 投递走独立事件锁）。

@author LiXinwen
"""
from __future__ import annotations

import secrets
import threading

from domain.phone_rules import (
    COOKIE_NAME, CSRF_COOKIE_NAME, CSRF_HEADER, EVENT_BUFFER_LIMIT,
    GLOBAL_FAIL_LIMIT, IP_FAIL_LIMIT, IP_LOCK_S, HTTP_PORT,
    PAIR_CODE_LEN, SESSION_TTL_S, cookie_header_value,
    csrf_cookie_header_value, is_allowed_bind, lock_active, normalize_action,
    pair_code_fresh, project_state, select_events, session_alive,
    valid_pair_code,
)


class PhoneAccessService:
    """默认关闭的受限手机入口：只认配对过的会话，动作走严格白名单。"""

    def __init__(self, *, api, now, state_of, server_factory, log=None):
        self._api = api
        self._now = now
        self._state_of = state_of
        self._server_factory = server_factory
        self._log = log or (lambda _msg: None)
        # 配对/会话/限速同一把锁：比较与消费必须原子（一码一次，双兑换只有一赢）
        self._lock = threading.Lock()
        # 事件缓冲独立锁：_emit 投递不与 Api 锁互相等待，也不回调 HTTP 客户端
        self._event_lock = threading.Condition()
        self._events: list = []
        self._seq = 0
        self._pair_code = ""
        self._pair_created = 0.0
        self._pair_used = False
        self._sessions: dict = {}
        self._fail_by_ip: dict = {}
        self._ip_lock_until: dict = {}
        self._global_fails = 0
        self._global_lock_until = 0.0
        self._server = None
        self._bind_host = ""

    # ---------- 生命周期 ----------
    def is_running(self) -> bool:
        return self._server is not None

    def address(self) -> str:
        if self._server is None:
            return ""
        try:
            return str(self._server.address())
        except Exception:
            return ""

    def bind_supported(self, host: str = "127.0.0.1") -> bool:
        return is_allowed_bind(host)

    def current_pair_code(self) -> str:
        """桌面面板显示一次性配对码；不进 URL、不落日志、不给未配对的手机。"""
        with self._lock:
            if self._pair_code and not self._pair_used and pair_code_fresh(
                    self._pair_created, self._now()):
                return self._pair_code
            return ""

    def start(self, host: str = "127.0.0.1", port: int = HTTP_PORT) -> dict:
        """启动受限 HTTP 服务；默认关闭，只有显式开启才建 socket。"""
        if not is_allowed_bind(host):
            return {"ok": False, "error": "bind_not_allowed",
                    "text": "只允许本机回环或 Tailscale 专属地址"}
        with self._lock:
            if self._server is not None:
                return {"ok": False, "error": "already_running",
                        "text": "手机访问已在运行"}
            self._pair_code = "".join(secrets.choice("0123456789")
                                      for _ in range(PAIR_CODE_LEN))
            self._pair_created = float(self._now())
            self._pair_used = False
            self._bind_host = str(host)
        try:
            server = self._server_factory(host, port, self)
            server.start()
        except Exception as exc:  # noqa: BLE001 - 端口占用等要给可读结论
            with self._lock:
                self._pair_code = ""
                self._pair_used = False
            return {"ok": False, "error": "listen_failed",
                    "text": "手机服务没能监听：%s" % type(exc).__name__}
        with self._lock:
            self._server = server
        return {"ok": True, "address": self.address(),
                "pair_code": self.current_pair_code()}

    def stop(self) -> dict:
        """关闭 HTTP 并撤销全部会话：有界收尾，不等慢客户端。"""
        with self._lock:
            server = self._server
            self._server = None
            self._sessions.clear()
            self._pair_code = ""
            self._pair_used = False
            self._fail_by_ip.clear()
            self._ip_lock_until.clear()
            self._global_fails = 0
            self._global_lock_until = 0.0
        if server is not None:
            try:
                server.stop()
            except Exception:  # noqa: BLE001 - 关闭尽力而为，异常不外抛
                pass
        return {"ok": True}

    # ---------- 配对与会话 ----------
    def pair(self, code, ip: str = "") -> dict:
        """一次性兑换配对码 → 会话 cookie + CSRF 头值。比较/消费同一临界区。"""
        now = float(self._now())
        with self._lock:
            if self._locked(ip, now):
                return {"ok": False, "error": "locked",
                        "text": "尝试次数过多，请稍后再试"}
            if not self._pair_code or self._pair_used or not pair_code_fresh(
                    self._pair_created, now):
                return {"ok": False, "error": "no_code",
                        "text": "配对码不可用，请在电脑上重新开启"}
            if not valid_pair_code(code) or not secrets.compare_digest(
                    str(code), self._pair_code):
                self._record_failure(ip, now)
                return {"ok": False, "error": "bad_code",
                        "text": "配对码不对，请核对后重试"}
            self._pair_used = True
            self._fail_by_ip.pop(ip, None)
            self._global_fails = 0
            token = secrets.token_hex(32)      # 256 位
            csrf = secrets.token_hex(32)
            self._sessions[token] = {"csrf": csrf, "created_at": now}
        return {"ok": True,
                "cookie_name": COOKIE_NAME,
                "cookie": cookie_header_value(token),
                "csrf_cookie": csrf_cookie_header_value(csrf),
                "csrf_header": CSRF_HEADER,
                "csrf": csrf,
                "expires_in": SESSION_TTL_S}

    def authenticate(self, cookie_header) -> "dict | None":
        """校验会话 cookie；过期即失效，且校验不刷新寿命。"""
        token = _cookie_value(cookie_header, COOKIE_NAME)
        if not token:
            return None
        now = float(self._now())
        with self._lock:
            session = self._sessions.get(token)
            if session is None or not session_alive(session, now):
                return None
            return {"token": token, "csrf": session.get("csrf", ""),
                    "created_at": session.get("created_at")}

    def _locked(self, ip: str, now: float) -> bool:
        if lock_active(self._global_lock_until, now):
            return True
        return lock_active(self._ip_lock_until.get(ip or "", 0), now)

    def _record_failure(self, ip: str, now: float) -> None:
        key = str(ip or "")
        self._fail_by_ip[key] = self._fail_by_ip.get(key, 0) + 1
        self._global_fails += 1
        if self._fail_by_ip[key] >= IP_FAIL_LIMIT:
            self._ip_lock_until[key] = now + IP_LOCK_S
        if self._global_fails >= GLOBAL_FAIL_LIMIT:
            self._global_lock_until = now + IP_LOCK_S

    # ---------- 状态与事件 ----------
    def safe_state(self, cursor=None) -> dict:
        """手机可读快照：只读本机缓存，绝不在这里碰学校接口。"""
        events, reset = select_events(self._snapshot_events(), cursor)
        return {"state": project_state(self._state_of()),
                "events": events, "reset": reset, "seq": self._seq}

    def record_event(self, kind, payload) -> None:
        """_emit 投递入口：窗口为空也可发布；锁外投递，不回调任何 HTTP 客户端。"""
        from domain.phone_rules import project_event
        safe = project_event(kind, payload)
        with self._event_lock:
            self._seq += 1
            self._events.append({"seq": self._seq, "kind": safe["kind"],
                                 "payload": safe["payload"]})
            if len(self._events) > EVENT_BUFFER_LIMIT:
                del self._events[:-EVENT_BUFFER_LIMIT]
            self._event_lock.notify_all()

    def _snapshot_events(self) -> list:
        with self._event_lock:
            return [dict(event) for event in self._events]

    # ---------- 动作派发 ----------
    def dispatch(self, action, args) -> dict:
        """严格白名单派发：只调白名单内方法与参数，绝不反射任意 Api。"""
        clean, error = normalize_action(action, args)
        if error:
            return {"ok": False, "error": error}
        name = str(action)
        if name == "save_retry_policy":
            # 手机只许改重试策略：terms/路径/任意键一律不进偏好
            return self._call("save_prefs",
                              {"prefs": {"retry_policy": clean.get("retry_policy", {})}})
        return self._call(name, clean)

    def _call(self, name: str, params: dict) -> dict:
        method = getattr(self._api, name, None)
        if method is None:
            return {"ok": False, "error": "not_available"}
        try:
            result = method(**params)
        except Exception as exc:  # noqa: BLE001 - 本机异常只回类型，不回堆栈
            return {"ok": False, "error": "internal",
                    "text": "本机执行失败：%s" % type(exc).__name__}
        return result if isinstance(result, dict) else {"ok": True}

    # ---------- 会话撤销（停服务/关闭时） ----------
    def revoke_all(self) -> None:
        with self._lock:
            self._sessions.clear()


def _cookie_value(header, name: str) -> str:
    """从 Cookie 头里取指定键的值；畸形输入一律当没有。"""
    text = str(header or "")
    for part in text.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value.strip()
    return ""
