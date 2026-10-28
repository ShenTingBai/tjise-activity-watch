#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""学校时间与重试预算的唯一契约。

两件事必须分开说：

1. **学校时间**永远按 UTC+8 解释。学校接口给的是不带时区的
   ``YYYY-MM-DD HH:MM:SS``，把它交给 ``datetime.timestamp()`` 会按本机时区
   折算——同一串 "2026-10-26 20:00:00" 在 UTC 机器上会差 8 小时。这里显式
   挂上 :data:`SCHOOL_TZ`，并把非法日历日期（2 月 30 日、13 月、24:01）
   一律判为不可用，绝不悄悄滚到别的日期。

2. **本机时间**（session.json 的 saved_at、ui.json 的生命周期文本）不归这里管。
   那些是本机历史记录，按本机时钟读回来才有意义；把它们强行改成东八区，
   会让旧数据凭空偏移 8 小时。

重试窗口另有一个 ``RetryBudget``：用户选的「持续时长」一旦开始，就只能被
单调时钟收紧，不能被系统墙钟回拨延长；学校的 sign_end 仍是独立的绝对硬边界。

@author LiXinwen
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

# 业务时钟 now 的注册点（app/grab 启动时 configure_time_source 注入）
_BUSINESS_NOW = None

# 学校口径固定东八区：不跟随本机时区，也不因夏令时变化
SCHOOL_TZ = timezone(timedelta(hours=8))

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# 学校时间格式的唯一正则形状（供测试与调用方判断「是不是学校时间」）
SCHOOL_TIME_LENGTH = len("2026-10-26 20:00:00")


def parse_school_time(value: object) -> "datetime | None":
    """把学校返回的时间文本解析成带时区的 datetime；非法一律返回 None。

    只接受严格的 ``YYYY-MM-DD HH:MM:SS``：多一个空格、少一位、写成
    ``2026/10/26``、``2026-02-30``、``24:01:00`` 都算不可用。返回 None 的
    调用方必须显示「时间待确认」，不得拿当前时间顶替。
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) != SCHOOL_TIME_LENGTH:
        return None
    try:
        parsed = datetime.strptime(text, TIME_FORMAT)
    except ValueError:
        # strptime 会拒绝 2 月 30 日、13 月、24 点等非法日历值
        return None
    return parsed.replace(tzinfo=SCHOOL_TZ)


def school_epoch(value: object) -> "float | None":
    """学校时间文本 → epoch 秒；非法返回 None（不抛异常，便于判空）。"""
    parsed = parse_school_time(value)
    return None if parsed is None else parsed.timestamp()


def configure_time_source(now_fn) -> None:
    """注册统一业务时钟的 now 可调用对象（main/CLI 启动时调用一次）。
    注册后，classify/validate/from_activity/PlanCoordinator 等的默认 now
    都取业务时钟；不注册则保持旧缺省——生产入口必须注册，绝不静默回退。"""
    global _BUSINESS_NOW
    _BUSINESS_NOW = now_fn


def default_now() -> float:
    """模块内「缺省当前时刻」的唯一入口：已注册业务时钟则用之，否则抛错。"""
    if _BUSINESS_NOW is None:
        raise RuntimeError("业务时钟未配置：请先调用 configure_time_source()")
    return float(_BUSINESS_NOW())


def school_time_text(epoch: "float | None" = None,
                     now_fn: "callable | None" = None) -> str:
    """epoch 秒 → 东八区 ``YYYY-MM-DD HH:MM:SS``。

    ``epoch`` 缺省时必须显式给 ``now_fn``（业务时钟），不再暗用 OS 当前时间——
    本机日期不是权威，直接拿 datetime.now 会把错误系统日期当北京时间输出。
    """
    if epoch is None:
        if now_fn is None:
            raise ValueError("school_time_text 需要显式 epoch 或 now_fn（业务时钟）")
        epoch = float(now_fn())
    moment = datetime.fromtimestamp(float(epoch), SCHOOL_TZ)
    return moment.strftime(TIME_FORMAT)


class BudgetExhausted(RuntimeError):
    """重试预算耗尽：本地到点，不是学校给的失败结论。

    单独一个异常类型，是为了让调用方能把它和「网络超时」「学校拒绝」分开：
    预算耗尽要收成结构化终态，而不是把最后一条网络错误当成原因。
    """


class RetryBudget:
    """一次重试窗口的硬预算（墙钟绝对截止 + 单调时长，取更紧的那个）。

    两个边界必须同时成立：

    * ``deadline_epoch``：学校/策略给出的绝对截止（epoch 秒，墙钟语义）；
    * ``monotonic_deadline``：从预算建立起按 :func:`time.monotonic` 计的长度，
      墙钟被回拨 10 秒也不会把它延后 10 秒。

    ``remaining_s`` 取两者的较小非负值：任何一个到点就结束。`timeout_ms`
    永不返回 0——Playwright 把 0 当作「禁用超时」，返回 0 会让一次阻塞
    无限期挂住，正好破坏硬截止。
    """

    def __init__(self, deadline_epoch, monotonic_deadline=None,
                 wall_now=None, monotonic_now=None, business_clock=None):
        # 业务时钟模式：绝对截止按业务上界收紧判断，时长上限按独立经过时长
        # 计账——校时重置/系统回拨都不得延长已冻结的时长（决策 012）。
        self.business_clock = business_clock
        if business_clock is not None:
            self.deadline_epoch = float(deadline_epoch)
            # 校时不可用时 bounds 抛 ClockUnavailable：显式传播给统一收尾，
            # 绝不吞掉后退到「无误差的 now」继续跑
            now_lower, now_upper = business_clock.bounds()
            self._elapsed0 = business_clock.elapsed_s()
            # 时长上限：用业务上界保守换算「截止还剩多少秒」，只紧不松；
            # 一次性冻结，重校向后纠正也不会延长已接纳的时长
            self._duration_deadline = self._elapsed0 + max(
                0.0, self.deadline_epoch - now_upper)
            self.wall_now = time.time()
            self.monotonic_now = time.monotonic()
            self.monotonic_deadline = self.monotonic_now
            return
        self.wall_now = time.time() if wall_now is None else float(wall_now)
        self.monotonic_now = time.monotonic() if monotonic_now is None else float(monotonic_now)
        self.deadline_epoch = float(deadline_epoch)
        if monotonic_deadline is None:
            # 建立预算时把「绝对截止到现在的剩余」转成单调长度；
            # 之后墙钟怎么跳都不再影响这条长度
            monotonic_deadline = self.monotonic_now + max(
                0.0, self.deadline_epoch - self.wall_now)
        self.monotonic_deadline = float(monotonic_deadline)

    # -- 读 ---------------------------------------------------------------
    def remaining_s(self, wall_now=None, monotonic_now=None) -> float:
        if self.business_clock is not None:
            clock = self.business_clock
            # bounds 失败 = 校时失效：让 ClockUnavailable 冒出去，由调用方收成
            # 唯一 clock_unavailable 终态，不能在这里退回「无误差的 now」
            now_lower, now_upper = clock.bounds()
            by_business = self.deadline_epoch - now_upper   # 上界判断：宁可提前收
            by_duration = self._duration_deadline - clock.elapsed_s()
            return max(0.0, min(by_business, by_duration))
        wall = time.time() if wall_now is None else float(wall_now)
        mono = time.monotonic() if monotonic_now is None else float(monotonic_now)
        by_wall = self.deadline_epoch - wall
        by_mono = self.monotonic_deadline - mono
        return max(0.0, min(by_wall, by_mono))

    def expired(self, wall_now=None, monotonic_now=None) -> bool:
        return self.remaining_s(wall_now, monotonic_now) <= 0.0

    # -- 约束阻塞 ---------------------------------------------------------
    def timeout_ms(self, want_ms, wall_now=None, monotonic_now=None) -> int:
        """把一次阻塞的超时压进剩余预算。

        到点直接抛 :class:`BudgetExhausted`：把 0 剩余换成 1ms 会让 Playwright
        真的开始一个动作（1ms 超时只是立刻失败，但动作已经发起），
        而「到点后不得再发起」必须是拒绝，不是尽力而为。
        """
        remain = self.remaining_s(wall_now, monotonic_now)
        if remain <= 0:
            raise BudgetExhausted("重试窗口已到点，拒绝再发起阻塞动作")
        return max(1, min(int(want_ms), int(remain * 1000)))

    def timeout_s(self, want_s, wall_now=None, monotonic_now=None) -> float:
        """秒级超时（给 urllib 这类接口）；到点同样拒绝，不扩大到 50ms。"""
        remain = self.remaining_s(wall_now, monotonic_now)
        if remain <= 0:
            raise BudgetExhausted("重试窗口已到点，拒绝再发起网络动作")
        return max(0.01, min(float(want_s), remain))

    def sleep(self, seconds, monotonic_now=None) -> None:
        """按预算截断的等待；睡完刚好到点也算正常结束，不额外抛异常。"""
        remain = self.remaining_s(monotonic_now=monotonic_now)
        time.sleep(max(0.0, min(float(seconds), remain)))

    def check(self, wall_now=None, monotonic_now=None) -> None:
        """到点即抛 :class:`BudgetExhausted`，供「不能再发新请求」的检查点使用。"""
        if self.expired(wall_now, monotonic_now):
            raise BudgetExhausted("重试窗口已到点")

    # -- 诊断 -------------------------------------------------------------
    def describe(self) -> dict:
        return {
            "deadline_epoch": int(self.deadline_epoch),
            "monotonic_deadline": round(self.monotonic_deadline, 3),
            "remaining_s": round(self.remaining_s(), 3),
        }
