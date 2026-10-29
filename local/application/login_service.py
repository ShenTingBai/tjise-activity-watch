# -*- coding: utf-8 -*-
"""登录/检查用例的输出整理：check 事件载荷与 done 结论。

纯输出塑形：分类规则在 domain.login_rules，这里只把分类结果、会话三态、
校时粗测拼成冻结契约的事件字段。不读文件、不起进程。

@author LiXinwen
"""
from __future__ import annotations

from domain.login_rules import check_hint, check_reason_line, check_result
from domain.worker_lines import parse_clock_line

# 本轮没有校时样本时的显式 unknown 粗测（界面逐轮替换，不保留上一轮告警）
_CLOCK_UNKNOWN = {
    "source": "http_date", "checked_at": None, "state": "unknown",
    "offset_s": None, "uncertainty_s": None, "rtt_s": None,
    "detail": "本轮没有校时结论",
}


def build_check_event(state: dict, code: int, out: str, err_code: str = ""):
    """构造 check 事件载荷；返回 (四态结论, 粗校时结论, 事件字段)。

    归因只信第一条 [X] 来源行；四态结论与 hint 分类沿用 domain 纯规则，
    组件错误（err_code）一律压成 unknown 且附加 runtime_error 字段。
    """
    reason, text = check_reason_line(out)
    result = check_result(state, code, reason or text)
    if err_code:
        result = "unknown"
    clock = parse_clock_line(out) or dict(_CLOCK_UNKNOWN)
    payload = {
        "ok": code == 0,
        "hours_left": state["hours_left"],
        "session_present": state["session_present"],
        "session_expired": state["session_expired"],
        # 当前凭证估算锚点（与 bootstrap.login 同一份 login_state 来源）：
        # 界面据此在重新登录后立即换新锚点；只作提醒，绝不参与放行/拦截
        "saved_at": state.get("saved_at"),
        "estimated_expires_at": state.get("estimated_expires_at"),
        "text": text,
        # 来源行（附加字段）：界面日志显示真实原因，而不是末尾的登录指引
        "reason": reason,
        # check 只回最后一行（R16）：给界面一个可区分的错误分类，
        # 免得把「没登录」和「断网」都笼统当失败。
        "hint": check_hint(reason or text, code),
        # 四态结论（附加字段）：valid=接口确认有效 / expired=4001 失效 /
        # none=本机无会话 / unknown=网络或接口异常——异常不得误报成重新登录
        "result": result,
        # 本轮粗校时结论：ok/warn/unknown 都带，界面逐轮替换而不是累积
        "clock": clock,
    }
    if err_code:
        payload["runtime_error"] = err_code
    return result, clock, payload


def done_result(kind: str, returncode: int, tail: "list[str]") -> tuple[bool, str]:
    """done 事件的结论：login 只看退出码；grab 还要求见过「报名成功」行。"""
    if kind == "login":
        ok = returncode == 0
    else:
        ok = returncode == 0 and any("报名成功" in t for t in tail)
    reason = ""
    if not ok:
        for t in reversed(tail):
            if t.startswith("[X] "):
                reason = t[4:].strip()
                break
        reason = reason or ("退出码 %s" % returncode)
    return ok, reason
