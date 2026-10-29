# -*- coding: utf-8 -*-
"""worker 输出行的纯解析：[checkpoint] / [clock] 结构化行。

只解析文本：坏 JSON、未知 kind 一律返回 None，调用方保持已有状态不变，
绝不用接收时刻补一个假节点。

@author LiXinwen
"""
from __future__ import annotations

import json


def parse_checkpoint_line(line: str) -> "tuple[str, float] | None":
    """解析 worker 的 [checkpoint] JSON 行，返回 (kind, 源事件 epoch 秒)。

    kind 只认三个真实发生过的事件；坏 JSON、未知 kind 都返回 None，
    调用方保持已有 checkpoint 不变，绝不用接收时刻补一个假节点。
    """
    text = str(line or "")
    marker = "[checkpoint] "
    index = text.find(marker)
    if index < 0:
        return None
    try:
        data = json.loads(text[index + len(marker):].strip())
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    kind = str(data.get("kind") or "")
    if kind not in {"worker_started", "page_opened", "watch_started"}:
        return None
    try:
        at = float(data.get("at"))
    except (TypeError, ValueError):
        return None
    return (kind, at) if at > 0 else None


def parse_clock_line(out: str) -> "dict | None":
    """取输出里最后一条 [clock] 结构化粗校时结论；没有则 None。

    每轮 check 都要带结论（ok/warn/unknown），界面才能用新状态替换旧告警；
    只取最后一行会让「本轮没有采样」被误当成「上一轮仍然有效」。
    """
    found = None
    for line in str(out or "").splitlines():
        marker = "[clock] "
        index = line.find(marker)
        if index < 0:
            continue
        try:
            data = json.loads(line[index + len(marker):].strip())
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and data.get("source") == "http_date":
            found = data
    return found
