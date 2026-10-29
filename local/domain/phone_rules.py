# -*- coding: utf-8 -*-
"""手机受限访问的纯规则：绑定地址、Host/Origin、动作白名单、状态投影。

只做校验与投影：不起 socket、不读墙钟（此刻一律由调用方传入）、不碰 Api。
敏感字段（token/password/cookie/凭据/路径）在投影里一律剔除；文本截断防日志
被撑爆。所有判断返回可判定的值或错误码，不抛异常给上层拼堆栈。

@author LiXinwen
"""
from __future__ import annotations

import ipaddress
import json
import math
import re

# 受限 HTTP 服务的固定端口（测试可用 0 换临时端口，生产恒 8765）
HTTP_PORT = 8765
# 配对码：6 位密码学随机数字，5 分钟有效，一码一次
PAIR_TTL_S = 300.0
PAIR_CODE_LEN = 6
# 会话 cookie：256 位随机、仅内存、8 小时；校验不刷新寿命
SESSION_TTL_S = 8 * 3600
# 请求体上限与事件缓冲上限
MAX_BODY_BYTES = 64 * 1024
EVENT_BUFFER_LIMIT = 400
# 配对失败限速：单 IP 连错 3 次锁 60 秒；全局连错 3 次也锁（换 IP 也算）
IP_FAIL_LIMIT = 3
IP_LOCK_S = 60.0
GLOBAL_FAIL_LIMIT = 3
# cookie / 头名字（HttpOnly + SameSite=Strict + Path=/，HTTP 上不谎加 Secure）
COOKIE_NAME = "tjise_phone_sid"
CSRF_HEADER = "X-Tjise-Csrf"
CSRF_COOKIE_NAME = "tjise_phone_csrf"
# 投影里永远不许出现的键名片段（大小写不敏感）
SENSITIVE_SUBSTRINGS = ("token", "password", "passwd", "cookie", "secret",
                        "credential", "storage_state", "csrf", "pair_code")
SENSITIVE_EXACT = {"path", "paths", "installer_path", "session_file",
                   "username", "account", "code"}
# 状态投影里整个剔除的顶层块：帮助路径、更新信息都是电脑专属
DROP_TOP_KEYS = {"help", "update", "update_check"}
TEXT_LIMIT = 200

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CODE_RE = re.compile(r"^[0-9A-Za-z]{4}$")


# ---- 绑定与来源校验 -------------------------------------------------------
def is_allowed_bind(host) -> bool:
    """只许 loopback（127/8、localhost）或显式 Tailscale CGNAT（100.64/10）IPv4。

    拒绝 0.0.0.0、公网、普通局域网（10/8、172.16/12、192.168/16）与 IPv6：
    本服务是个人受限适配层，不是公网服务器，不改防火墙也不启隧道。
    """
    text = str(host or "").strip()
    if not text:
        return False
    if text.lower() == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return False
    if addr.version != 4:
        return False
    return addr.is_loopback or addr in ipaddress.ip_network("100.64.0.0/10")


def literal_origin(host, port: int = HTTP_PORT) -> str:
    """服务当前字面 origin：只有这一个值能通过 Host/Origin 校验。"""
    return "http://%s:%d" % (str(host).strip(), int(port))


def host_matches(host_header, host, port: int = HTTP_PORT) -> bool:
    """Host 必须是「字面地址:端口」或裸字面地址（部分客户端省略默认端口）。"""
    raw = str(host_header or "").strip().lower()
    want = str(host or "").strip().lower()
    if not raw or not want:
        return False
    return raw == "%s:%d" % (want, int(port)) or raw == want


def origin_matches(origin_header, host, port: int = HTTP_PORT) -> bool:
    """Origin 必须与服务当前字面 origin 逐字相等：拒绝 CORS 与无 Origin。"""
    raw = str(origin_header or "").strip()
    return bool(raw) and raw == literal_origin(host, port)


# ---- 请求体与动作白名单 ---------------------------------------------------
def parse_json_body(raw: bytes):
    """请求体 → (对象, 错误码)。上限 64KiB、严格 UTF-8、必须 JSON 对象、拒 NaN。"""
    if raw is None:
        return None, "empty_body"
    if not isinstance(raw, (bytes, bytearray)):
        return None, "bad_body"
    if len(raw) > MAX_BODY_BYTES:
        return None, "body_too_large"
    try:
        text = bytes(raw).decode("utf-8", "strict")
    except UnicodeDecodeError:
        return None, "bad_utf8"
    try:
        data = json.loads(text, parse_constant=_reject_constant)
    except ValueError:
        return None, "bad_json"
    if not isinstance(data, dict):
        return None, "bad_json"
    return data, ""


def _reject_constant(name):
    # NaN / Infinity 不是合法业务数值：拒绝而不是悄悄变成 None
    raise ValueError("constant %s" % name)


def _finite_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number)


# 动作 → 允许的参数键。严格字典白名单：多一个键都拒绝，
# 远程不得反射任意 Api 方法，shutdown/open_data/update 等永远不在表内。
ACTION_PARAMS = {
    "list_activities": {"request_id"},
    "check": {"request_id"},
    "start_plan": {"items", "headed", "retry_policy", "request_id"},
    "start_request_state": {"request_id"},
    "cancel_start_request": {"request_id"},
    "stop_plan": {"plan_id"},
    "cancel_task": {"plan_id", "task_id"},
    "save_retry_policy": {"retry_policy"},
    # relay 三动作（login-relay 任务接入 Api）：白名单先占位，未实现时明确报错
    "phone_relogin_begin": set(),
    "phone_relogin_submit": {"flow_id", "challenge_id", "code"},
    "phone_relogin_cancel": {"flow_id"},
}
# save_prefs 从手机只许改 retry_policy：不许 terms、路径或任意键
RETRY_POLICY_KEYS = {"retry_for_s", "max_attempts"}


def normalize_action(action, args):
    """动作白名单校验：返回 (规范化参数, 错误码)。未知动作/多键/坏类型都拒。"""
    name = str(action or "")
    if name not in ACTION_PARAMS:
        return None, "action_not_allowed"
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return None, "bad_args"
    allowed = ACTION_PARAMS[name]
    unknown = set(args.keys()) - allowed
    if unknown:
        return None, "args_not_allowed"
    clean = {}
    for key, value in args.items():
        if key in ("request_id", "plan_id", "task_id", "challenge_id", "flow_id"):
            if not isinstance(value, str) or not _ID_RE.fullmatch(value):
                return None, "bad_args"
            clean[key] = value
        elif key == "code":
            if not isinstance(value, str) or not _CODE_RE.fullmatch(value):
                return None, "bad_args"
            clean[key] = value
        elif key == "headed":
            if not isinstance(value, bool):
                return None, "bad_args"
            clean[key] = value
        elif key == "items":
            if not isinstance(value, list) or not value:
                return None, "bad_args"
            items = []
            for item in value:
                if not isinstance(item, dict):
                    return None, "bad_args"
                item_id = item.get("id")
                if not isinstance(item_id, str) or not _ID_RE.fullmatch(item_id):
                    return None, "bad_args"
                picked = {"id": item_id}
                if "request_id" in item:
                    rid = item["request_id"]
                    if not isinstance(rid, str) or not _ID_RE.fullmatch(rid):
                        return None, "bad_args"
                    picked["request_id"] = rid
                items.append(picked)
            clean[key] = items
        elif key == "retry_policy":
            if not isinstance(value, dict):
                return None, "bad_args"
            if set(value.keys()) - RETRY_POLICY_KEYS:
                return None, "args_not_allowed"
            policy = {}
            if "retry_for_s" in value:
                if not _finite_number(value["retry_for_s"]):
                    return None, "bad_args"
                policy["retry_for_s"] = int(value["retry_for_s"])
            if "max_attempts" in value:
                if not _finite_number(value["max_attempts"]):
                    return None, "bad_args"
                policy["max_attempts"] = int(value["max_attempts"])
            clean[key] = policy
        else:
            clean[key] = value
    return clean, ""


# ---- 状态与事件投影 -------------------------------------------------------
def _is_sensitive_key(key) -> bool:
    name = str(key or "").lower()
    if name in SENSITIVE_EXACT:
        return True
    return any(part in name for part in SENSITIVE_SUBSTRINGS)


def clip_text(value, limit: int = TEXT_LIMIT):
    """文本限长：日志/事件里的长串不会把手机页面和缓冲撑爆。"""
    if not isinstance(value, str):
        return value
    return value if len(value) <= limit else value[:limit] + "…"


def strip_sensitive(value):
    """递归剔除敏感键并截断文本；结构保持可 JSON 序列化。"""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in DROP_TOP_KEYS or _is_sensitive_key(key):
                continue
            out[key] = strip_sensitive(item)
        return out
    if isinstance(value, list):
        return [strip_sensitive(item) for item in value]
    return clip_text(value)


def project_state(raw) -> dict:
    """手机可读的本机权威状态：剔除 help/update/凭据/路径，不透传 bootstrap。"""
    return strip_sensitive(raw if isinstance(raw, dict) else {})


def project_event(kind, payload) -> dict:
    """事件投影：同样剔除敏感字段并截断文本，供有界缓冲回放。"""
    return {"kind": str(kind or ""), "payload": strip_sensitive(payload) if payload is not None else None}


# ---- 配对 / 会话纯判定 ----------------------------------------------------
def valid_pair_code(code) -> bool:
    """配对码形状：恰好 PAIR_CODE_LEN 位数字（生成端用密码学随机）。"""
    text = str(code or "")
    return bool(re.fullmatch(r"\d{%d}" % PAIR_CODE_LEN, text))


def pair_code_fresh(created_at, now) -> bool:
    """配对码在 5 分钟内才算新鲜；缺时间戳/超时都按过期处理。"""
    try:
        made = float(created_at)
        moment = float(now)
    except (TypeError, ValueError):
        return False
    return 0 <= moment - made <= PAIR_TTL_S


def session_alive(session, now) -> bool:
    """会话在 8 小时内才算有效；校验不刷新寿命（created_at 只写一次）。"""
    if not isinstance(session, dict):
        return False
    try:
        made = float(session.get("created_at"))
        moment = float(now)
    except (TypeError, ValueError):
        return False
    return 0 <= moment - made <= SESSION_TTL_S


def lock_active(locked_until, now) -> bool:
    try:
        return float(now) < float(locked_until)
    except (TypeError, ValueError):
        return False


def cookie_header_value(token) -> str:
    """会话 cookie 文本：HttpOnly + SameSite=Strict + Path=/，HTTP 上不加 Secure。"""
    return "%s=%s; HttpOnly; SameSite=Strict; Path=/" % (COOKIE_NAME, token)


def csrf_cookie_header_value(token) -> str:
    """CSRF 头取值来源：可被 JS 读到才能随写请求回传，故不加 HttpOnly。"""
    return "%s=%s; SameSite=Strict; Path=/" % (CSRF_COOKIE_NAME, token)


# ---- 事件回放（重连对账）--------------------------------------------------
def select_events(events, cursor):
    """按序号回放：cursor 太旧返回 (全量, True)；否则返回 (增量, False)。

    事件缓冲只有 400 条，断线太久时旧事件已被挤掉——这时必须整体重置，
    让手机重新拉权威快照，而不是漏掉中间状态装作连续。
    """
    seqs = [int(event.get("seq") or 0) for event in events]
    if not seqs:
        return [], False
    try:
        wanted = int(cursor)
    except (TypeError, ValueError):
        return list(events), True
    if wanted < 0:
        return list(events), True
    oldest = min(seqs)
    if wanted < oldest - 1:
        return list(events), True
    return [event for event in events if int(event.get("seq") or 0) > wanted], False
