# -*- coding: utf-8 -*-
"""时间文本与截止时刻的纯换算（ui.json/session.json 文本、学校时间差值）。

只做文本/数值换算：不读文件（调用方负责取文本）、不查墙钟（now 由调用方传入）。
学校时间文本→epoch 的换算函数由 composition root 注入（epoch_of）：domain 不得
直接依赖 time_contract 等带全局时间源的具体模块。

@author LiXinwen
"""
from __future__ import annotations

from datetime import datetime

from domain.retry_window import initial_deadline


def parse_local_time(text) -> "datetime | None":
    """ui.json / session.json 里的本地时间文本（YYYY-MM-DD HH:MM:SS）→ datetime。"""
    try:
        return datetime.strptime(str(text or "").strip(), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def format_local_text(dt) -> str:
    """datetime → ui.json 本地时间文本（YYYY-MM-DD HH:MM:SS）。

    纯格式化：不读墙钟。此刻值由调用方（infrastructure/interface）取得后传入，
    domain 不做任何墙钟读取。
    """
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def latest_moment(*moments) -> "datetime | None":
    """取若干可解析时刻里最近的一个；全缺失返回 None（供生命周期参考点推导）。"""
    valid = [m for m in moments if m is not None]
    return max(valid) if valid else None


def seconds_until(ts: str | None, now: "float | None", epoch_of) -> int | None:
    """学校时间文本距现在多少秒；非法/缺失/时钟未知都返回 None（不猜）。

    epoch_of：学校时间文本 → epoch 秒（由 app.py 注入 time_contract.school_epoch）。
    """
    if not ts or now is None:
        return None
    epoch = epoch_of(ts)
    return None if epoch is None else int(epoch - now)


def task_deadline(spec, epoch_of) -> "float | None":
    """父层视角的本场绝对截止（与 worker 的预算同一公式）。

    worker 自己也会算，但管道卡死、进程不退出时只有父进程能收走它；
    这里只用于监督兜底，不改变 worker 的调度。epoch_of 同 seconds_until。
    算式统一委托 domain.retry_window.initial_deadline，三种模式
    （正数硬时长 / 0 直达截止 / -1 自动延长初始 120）与 worker 同值。
    """
    start = float(getattr(spec, "retry_started_at", 0) or 0)
    if start <= 0:
        return None
    end = epoch_of(spec.sign_end) if getattr(spec, "sign_end", "") else None
    return initial_deadline(start, getattr(spec, "retry_for_s", 120), end)
