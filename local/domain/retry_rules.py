# -*- coding: utf-8 -*-
"""重试策略纯规则：worker 结构化行解析、次数归并、telemetry 载荷、截止计算。

只处理文本/数值：不读管道、不起进程。解析失败一律返回 None/0 兜底，
调用方按「无策略信息」处理，绝不影响既有退出码语义。

@author LiXinwen
"""
from __future__ import annotations

import re

from domain.time_text import task_deadline, seconds_until  # noqa: F401  (对外统一出口)

# worker 每次真正发起 signUp 时打印的行（带 [i] 前缀落日志）；
# 只匹配这个形状，刷新 / is_joined 查询不会撞上
ATTEMPT_RE = re.compile(r"^(?:\[i\]\s*)?第 (\d+) 次提交[：:]\s*(.*)$")


def parse_retry_line(line: str) -> "dict | None":
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


def attempts_of(stop: dict) -> int:
    """从结构化终态行里读「已承认的提交次数」；非法值按 0。"""
    try:
        return max(0, int((stop or {}).get("attempts", 0) or 0))
    except (TypeError, ValueError):
        return 0


def with_attempts(stop: dict, attempts: int) -> dict:
    """终态载荷统一写「两路归并后的次数」，不让某一路缺失把次数清零。"""
    merged = dict(stop or {})
    merged["attempts"] = max(attempts, attempts_of(stop))
    return merged


def retry_payload(stop: dict, spec, stop_reason: str) -> dict:
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


def policy_int(value, fallback: int, minimum: int = 0) -> int:
    """重试策略字段的整数归一：非法/低于下限回退默认值（不猜）。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return number if number >= minimum else fallback
