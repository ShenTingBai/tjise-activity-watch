# -*- coding: utf-8 -*-
"""生命周期纯记账：由上一周期快照推导关闭时长与质量（v1 文本 / v2 epoch）。

纯函数，供契约测试隔离求值：只接受值并返回值，不读 ui.json、不取时钟。

@author LiXinwen
"""
from __future__ import annotations

from datetime import datetime, timedelta

from domain.time_text import parse_local_time, latest_moment


def compute_lifecycle(prev: dict, started_at: datetime) -> dict:
    """由上一运行周期的 ui.json 快照推导关闭时长与质量（纯函数，供契约测试）。

    周期以 run_open 标记区分（本周期启动写 true，正常关闭写 false）：
    - clean：上一周期 run_open=false，且关闭时刻不早于该周期启动、心跳不晚于关闭
      → 关闭记录确实属于最近这个周期，可给精确时长；
    - abnormal：上一周期 run_open=true（启动后没走正常关闭就没了）→ 只能拿
      「最近有效心跳（无则该周期启动时刻）」给「约」值，绝不能拿更早周期的
      关闭时间冒充本周期正常退出；
    - unknown：首次运行 / 时间不可解析，或系统时钟回拨（参考点晚于本次启动）。
    旧格式 ui.json 没有 run_open：按时间戳兼容推导，但关闭记录早于上一周期
    启动时刻时必须降级为 abnormal——它属于更早的周期，不是最近周期的正常关闭。
    """
    closed = parse_local_time(prev.get("last_closed_at"))
    beat = parse_local_time(prev.get("last_heartbeat_at"))
    started_prev = parse_local_time(prev.get("last_started_at"))
    run_open = prev.get("run_open")
    result = {
        "last_closed_at": str(prev.get("last_closed_at") or ""),
        "last_heartbeat_at": str(prev.get("last_heartbeat_at") or ""),
        "closed_for_s": None,
        "close_quality": "unknown",
    }

    def finish(ref: datetime, quality: str) -> None:
        if started_at - ref < timedelta(0):
            return   # 系统时钟回拨/坏时间：精确与约值都不可信，退回未知
        result["closed_for_s"] = round((started_at - ref).total_seconds(), 1)
        result["close_quality"] = quality

    if run_open is True:
        # 显式存活标记仍在：上一周期没有正常关闭，只有存活参考点可依
        ref = latest_moment(beat, started_prev)
        if ref is not None:
            finish(ref, "abnormal")
        return result
    if run_open is False:
        # 显式正常关闭。时间矛盾（关闭早于本周期启动，如系统回拨后关机）时
        # 精确值不可信，退 unknown——正常关闭标记不能覆盖时间顺序矛盾；
        # 同一秒关闭与启动允许等号。心跳晚于关闭同样按异常处理：正常关闭后
        # 不应再有心跳，出现即说明关闭记录可能被迟到写入污染。
        if closed is not None and started_prev is not None and closed < started_prev:
            return result
        if closed is not None and (beat is None or beat <= closed):
            finish(closed, "clean")
        elif beat is not None:
            finish(beat, "abnormal")
        return result
    # 旧格式（无 run_open 标记）：时间戳兼容推导
    if closed is not None and (started_prev is None or closed >= started_prev) \
            and (beat is None or beat <= closed):
        finish(closed, "clean")
        return result
    ref = latest_moment(beat, started_prev)
    if ref is not None:
        finish(ref, "abnormal")
    return result


def compute_lifecycle_v2(prev_v2: "dict | None", started_epoch: float) -> "dict | None":
    """决策 012：以可信业务 epoch 记账的跨关闭时长（v2）。

    prev_v2 = {"run_open": bool, "started_epoch", "heartbeat_epoch", "closed_epoch",
               "quality": "ready"|"unknown"}
    全部由业务时钟写入：业务重校只影响单个读数的不确定度，不改变两次业务读数
    之差——系统墙钟跳变无法污染关闭时长。旧墙钟文本不迁移成 v2（不可反推）。
    quality=unknown（上一周期没有可信锚点）时不给任何精确/约值。
    返回 None = 无 v2 记账，调用方退回旧文本路径。
    """
    if not isinstance(prev_v2, dict) or "run_open" not in prev_v2:
        return None
    if prev_v2.get("quality") == "unknown":
        return {"closed_for_s": None, "close_quality": "unknown", "close_source": "v2"}
    started_prev = prev_v2.get("started_epoch")
    beat = prev_v2.get("heartbeat_epoch")
    closed = prev_v2.get("closed_epoch")
    result = {"closed_for_s": None, "close_quality": "unknown", "close_source": "v2"}

    def finish(ref, quality):
        if not isinstance(ref, (int, float)) or started_epoch - ref < 0:
            return
        result["closed_for_s"] = round(float(started_epoch) - float(ref), 1)
        result["close_quality"] = quality

    if prev_v2.get("run_open") is True:
        ref = beat if isinstance(beat, (int, float)) else started_prev
        finish(ref, "abnormal")
        return result
    # run_open False：正常关闭
    if isinstance(closed, (int, float)) and isinstance(started_prev, (int, float)) \
            and closed < started_prev:
        return result
    if isinstance(closed, (int, float)) and (beat is None or beat <= closed):
        finish(closed, "clean")
    elif isinstance(beat, (int, float)):
        finish(beat, "abnormal")
    return result
