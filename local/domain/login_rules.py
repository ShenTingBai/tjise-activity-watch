# -*- coding: utf-8 -*-
"""登录状态分类：三态快照构造、check 四态结论与来源行归因。

纯函数：token / 会话文本 / 剩余小时数由调用方（app 外观）读好传入；
本模块不读 session.json、不读 ui.json、不调用任何时钟对象。

@author LiXinwen
"""
from __future__ import annotations

import hashlib

from domain.time_text import parse_local_time


def credential_id(token: str) -> str:
    """本机内部凭证摘要：只用于把 v2 锚点和具体凭证绑定，不输出到日志。"""
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()[:16]


def component_error(exc: Exception) -> str:
    """把**进程创建**抛出的异常归类为结构化组件错误码（check/list/login 收尾共用）。

    只对进程启动阶段的 OSError 调用：文件/权限/其他 OS 层失败才有资格归组件码。
    数据/解析等普通异常不进这里——数据坏了不是组件坏了，不得诱导重新下载。
    """
    if isinstance(exc, FileNotFoundError):
        return "worker_missing"
    if isinstance(exc, PermissionError):
        return "worker_permission"
    return "spawn_failed"


def build_session_state(token, saved_text, hours_left, now=None) -> dict:
    """登录三态快照（纯函数）：区分「从没登录过」和「登录过期」。

    「没有登录」= 缺 token；只有 token 还在但已过期才叫登录态失效。
    缺 token 时 hours_left 必须是 None——按 saved_at 报一个假的小时数会把
    空壳会话显示成有效。saved_at / estimated_expires_at 只是估算提醒，
    登录有效性永远以学校真实校验为准，二者绝不参与放行或拦截。
    """
    state = {"session_present": False, "session_expired": False, "hours_left": None,
             "saved_at": None, "estimated_expires_at": None}
    token = str(token or "").strip()
    saved_text = str(saved_text or "").strip()
    if not token:
        return state
    state["session_present"] = True
    saved = parse_local_time(saved_text)
    if saved is not None:
        state["saved_at"] = saved_text
    state["hours_left"] = hours_left
    if hours_left is not None and now is not None:
        try:
            state["estimated_expires_at"] = now + hours_left * 3600.0
        except Exception:
            state["estimated_expires_at"] = None
    state["session_expired"] = hours_left is not None and hours_left <= 0
    return state


def hours_left_from_anchor(timing: "dict | None", token: str, now: "float | None",
                           lifetime_h: float) -> "float | None":
    """登录剩余估算（纯函数）：只认与当前凭证匹配的 v2 锚点 + 可信业务时刻。

    旧 saved_at 是本机墙钟文本，无法反推真实 UTC，绝不拿系统当前时间减它
    冒充准确小时数。now=None（无业务时钟）一律返回 None = 「暂无法估算」。
    """
    if not isinstance(timing, dict) or timing.get("credential_id") != credential_id(token):
        return None
    saved_epoch = timing.get("saved_epoch")
    if not isinstance(saved_epoch, (int, float)):
        return None
    if now is None:
        return None
    used = (float(now) - float(saved_epoch)) / 3600.0
    return max(0.0, float(lifetime_h) - used)


def check_reason_line(out: str) -> tuple[str, str]:
    """从 check 的完整输出提取 (来源行, 末行)。

    归因只信第一条 [X] 来源行——失败输出的末尾固定是「请重新登录」指引，
    拿末行分类会把网络错误误判成登录失效；末行仍返回给界面做时钟偏差展示。
    结构化 [clock]/[checkpoint] 行不是人话，既不当来源也不当末行。
    空输出返回 ("", "")，调用方按「认不出的失败」落 unknown。
    """
    lines = [ln.strip() for ln in str(out or "").strip().splitlines() if ln.strip()]
    lines = [ln for ln in lines if "[clock] " not in ln and "[checkpoint] " not in ln]
    reason = next((ln for ln in lines if ln.startswith("[X] ")), "")
    return reason, (lines[-1] if lines else "")


def check_hint(text: str, code: int) -> str:
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


def check_result(state: dict, code: int, text: str) -> str:
    """check 的四态结论：valid / expired / unknown / none（附加字段，旧 UI 可忽略）。

    只有学校接口真实成功才是 valid；本机没有会话是 none；能认出的登录失效
    （4001/登录类字样，且不含网络词）是 expired；网络类与认不出的失败一律
    unknown——网络异常不能被误报成「必须重新登录」。
    """
    if code == 0:
        return "valid"
    if not state.get("session_present"):
        return "none"
    hint = check_hint(text, code)
    if hint == "network":
        return "unknown"
    if hint == "login":
        return "expired"
    return "unknown"
