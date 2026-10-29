# -*- coding: utf-8 -*-
"""重试窗口纯算式：worker / 父监督 / 桌面投影共用同一口径。

不读时钟、不碰进程、不引混合具体模块：只算绝对截止与合法扩窗增量。
三种模式由 retry_for_s 区分：
  - 正数：用户选的显式时长，sign_end 更早时被截断，不扩窗；
  - 0：直到报名截止，缺截止回退 DEFAULT_RETRY_SECONDS，不扩窗；
  - ADAPTIVE_RETRY(-1)：自动延长，初始 DEFAULT_RETRY_SECONDS，之后由本场
    合法 window_extended 事件按 extension_delta 逐次增长，上限
    anchor + ADAPTIVE_SECONDS 且受 sign_end 硬截。
其他负值/非法值一律按「缺截止回退 120、有截止直达 sign_end」兜底（与旧
CLI 对未定义输入的口径一致），只有 -1 改变含义。

关键算式（父监督扩窗）：new_hard_stop = hard_stop + (candidate - accepted)。

@author LiXinwen
"""
from __future__ import annotations

import math

# 「直到报名截止」缺 sign_end 时的兜底窗口：绝不无限运行
DEFAULT_RETRY_SECONDS = 120.0
# 自动延长模式的扩窗上限（起点起 25 分钟）；sign_end 仍硬截
ADAPTIVE_SECONDS = 1500.0
# retry_for_s 的自动延长标记：显式自动，区别于 0（直到截止）与正数（硬时长）
ADAPTIVE_RETRY = -1


def _as_finite(value):
    """把入参收敛成有限 float；None/非法/NaN/Inf 都返回 None（不猜）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _as_end(value):
    """sign_end 收敛：非正/非法视同「学校没给截止」（与旧真值判断同口径）。"""
    number = _as_finite(value)
    return number if (number is not None and number > 0) else None


def resolve_window(anchor, retry_for_s, sign_end):
    """返回 (初始绝对截止, 约束来源)。约束来源：duration / sign_end / fallback。

    anchor 是本场重试起算点（epoch 秒，调用方冻结）；sign_end 是学校报名
    截止（epoch 秒，可 None）。不读此刻——所有时间都由调用方注入。
    """
    base = _as_finite(anchor)
    if base is None:
        base = 0.0
    end = _as_end(sign_end)
    value = _as_finite(retry_for_s)

    def _cut(deadline, binding):
        # sign_end 是绝对硬上限：任何模式的截止都不许越过它
        if end is not None and end < deadline:
            return end, "sign_end"
        return deadline, binding

    if value is not None and value > 0:
        return _cut(base + value, "duration")
    if value is not None and value == ADAPTIVE_RETRY:
        # 自动延长的初始预算就是 120 秒；后续靠 extension_delta 增长
        return _cut(base + DEFAULT_RETRY_SECONDS, "duration")
    # 0 与其它未定义取值（None/非法/别的负数）沿用旧口径：
    # 有截止直达 sign_end，缺截止才回退 120 秒，绝不无限运行
    if end is not None:
        return end, "sign_end"
    return base + DEFAULT_RETRY_SECONDS, "fallback"


def initial_deadline(anchor, retry_for_s, sign_end):
    """本场初始绝对截止（只取截止值；细节见 resolve_window）。"""
    return resolve_window(anchor, retry_for_s, sign_end)[0]


def extension_cap(anchor, sign_end):
    """自动延长模式的候选截止上限：起点 + 25 分钟，sign_end 仍硬截。"""
    base = _as_finite(anchor)
    if base is None:
        return None
    cap = base + ADAPTIVE_SECONDS
    end = _as_end(sign_end)
    if end is not None and end < cap:
        cap = end
    return cap


def extension_delta(current, candidate, cap, enabled):
    """合法扩窗增量（业务秒）：只接受有限、严格增长且不越 cap 的候选，否则 0。

    刻意不看「此刻」——父监督按 new_hard_stop = hard_stop + 增量 平移单调
    截止，重校时向后修正因此延长不了已接纳的时长。enabled 只有自动延长
    模式为真；重复（相等）、缩短、越界、非法/非有限一律 0 增量。
    """
    if not enabled:
        return 0.0
    cur = _as_finite(current)
    cand = _as_finite(candidate)
    limit = _as_finite(cap)
    if cur is None or cand is None or limit is None:
        return 0.0
    if cand <= cur or cand > limit:
        return 0.0
    return cand - cur
