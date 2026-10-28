#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""学校时间与粗校时的边界契约（R07/R09/R14）。

两类证据：

* 同一串学校时间在任何本机时区下都必须解析成同一个 epoch。Python 侧用
  带时区的 datetime 保证；JS 侧真的跑生产 ``toTs``，用 Node 子进程把 ``TZ``
  分别设成 Asia/Shanghai、UTC、America/New_York 各跑一遍对比。
  这里**不改系统时区**，只给子进程注入环境变量。
* HTTP Date 只能做粗校验：差值区间跨过阈值时说「不确定」，不能断言
  「本机慢 4 秒」；网络不通/没有 Date 一律 unknown。

@author LiXinwen
"""
from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import formatdate
from pathlib import Path
from unittest import mock

LOCAL = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(LOCAL))
sys.path.insert(0, str(TESTS))

import grab  # noqa: E402
import time_contract  # noqa: E402
from test_ui_contract import run_js  # noqa: E402

SCHOOL_20 = "2026-10-26 20:00:00"
SCHOOL_20_EPOCH = 1793016000.0
TIME_ZONES = ("Asia/Shanghai", "UTC", "America/New_York")

INVALID_SCHOOL_TIMES = (
    "2026-02-30 20:00:00",      # 2 月没有 30 日
    "2027-02-29 20:00:00",      # 非闰年
    "2026-13-01 00:00:00",      # 13 月
    "2026-10-26 24:01:00",      # 24 点
    "2026/10/26 20:00:00",      # 分隔符不对
    "2026-10-26 20:00",         # 缺秒
    "",
    None,
)


class SchoolTimeParseTests(unittest.TestCase):
    """R09：学校时间显式 UTC+8，不随本机时区漂移。"""

    def test_school_time_is_utc_plus_eight(self):
        moment = time_contract.parse_school_time(SCHOOL_20)
        self.assertIsNotNone(moment)
        self.assertEqual(moment.utcoffset(), timedelta(hours=8),
                         "学校时间必须显式挂东八区，不能是本机 naive 时间")
        self.assertEqual(moment.timestamp(), SCHOOL_20_EPOCH)
        self.assertEqual(time_contract.school_epoch(SCHOOL_20), SCHOOL_20_EPOCH)
        self.assertEqual(time_contract.school_time_text(SCHOOL_20_EPOCH), SCHOOL_20,
                         "epoch → 学校时间必须能原样回写")

    def test_invalid_calendar_dates_are_rejected(self):
        for text in INVALID_SCHOOL_TIMES:
            with self.subTest(text=text):
                self.assertIsNone(time_contract.parse_school_time(text),
                                  "非法学校时间必须返回 None：%r" % (text,))
                self.assertIsNone(time_contract.school_epoch(text))

    def test_leap_day_is_accepted(self):
        self.assertEqual(time_contract.school_time_text(
            time_contract.school_epoch("2028-02-29 09:00:00")), "2028-02-29 09:00:00")


class SchoolTimeJsTests(unittest.TestCase):
    """R09/R14：生产 toTs 在三时区下 epoch 一致，非法日期一律 null。"""

    SNIPPET = r"""
const cases = [
  "2026-10-26 20:00:00",
  "2026-02-30 20:00:00",
  "2027-02-29 20:00:00",
  "2028-02-29 20:00:00",
  "2026-13-01 00:00:00",
  "2026-10-26 24:01:00",
  "2026-10-26 20:00",
  "",
];
out.push({
  tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
  values: cases.map((text) => toTs(text)),
  clock: fmtClock(1793016000),
  hm: hm(1793016000 * 1000),
});
"""

    def test_js_same_epoch_in_every_time_zone(self):
        seen = []
        for tz in TIME_ZONES:
            payload = run_js(self.SNIPPET, env={"TZ": tz})[0]
            seen.append((payload["tz"], payload["values"]))
            values = payload["values"]
            self.assertEqual(values[0], SCHOOL_20_EPOCH,
                             "%s 下 20:00 的 epoch 漂了" % tz)
            self.assertEqual(values[1], None, "2 月 30 日必须 null，不能滚成 3 月 2 日")
            self.assertEqual(values[2], None, "非闰年 2 月 29 日必须 null")
            self.assertIsNotNone(values[3], "闰年 2 月 29 日必须可解析")
            self.assertEqual(values[4], None, "13 月必须 null")
            self.assertEqual(values[5], None, "24 点必须 null")
            self.assertEqual(values[6], None, "缺秒必须 null")
            self.assertEqual(values[7], None, "空值必须 null")
            # 学校时刻展示也必须固定东八区，不能跟着本机时区走
            self.assertEqual(payload["clock"], "20:00:00",
                             "%s 下学校时刻显示成了本机时区" % tz)
            self.assertEqual(payload["hm"], "20:00")
        epochs = {tuple(values) for _, values in seen}
        self.assertEqual(len(epochs), 1, "三个时区必须得到同一组解析结果")

    def test_js_and_python_agree(self):
        payload = run_js(self.SNIPPET, env={"TZ": "UTC"})[0]
        self.assertEqual(payload["values"][0], time_contract.school_epoch(SCHOOL_20),
                         "前后端对同一串学校时间必须算出同一个 epoch")


class _FakeClock:
    def __init__(self, wall):
        self.wall = float(wall)
        self.mono = 1000.0

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def sleep(self, seconds):
        self.wall += float(seconds)
        self.mono += float(seconds)


class _FakeHttp:
    def __init__(self, clock, rtt, date_text=None, exc=None):
        self.clock = clock
        self.rtt = rtt
        self.date_text = date_text
        self.exc = exc

    def urlopen(self, request, timeout=None):
        self.clock.sleep(self.rtt)
        if self.exc is not None:
            raise self.exc

        class Response:
            def __init__(inner, date):
                inner.headers = {} if date is None else {"Date": date}

            def close(inner):
                return None

        return Response(self.date_text)


class ClockQualityTests(unittest.TestCase):
    """R07：HTTP Date 只作粗校验，给出区间与可信程度，不假装精确。"""

    BASE = 1793016000

    def _sample(self, offset_s=0.0, rtt_s=0.2, date_text="auto", exc=None):
        """跑真实的 grab.clock_sample，只替换 time 与 urllib 两个输入边界。

        不 patch 真实的 time/urllib 模块属性：那会污染同一进程里的其他测试。
        """
        clock = _FakeClock(self.BASE)
        if date_text == "auto":
            date_text = formatdate(self.BASE + offset_s, usegmt=True)
        http = _FakeHttp(clock, rtt_s, date_text, exc)
        fake_time = types.SimpleNamespace(time=clock.time, monotonic=clock.monotonic,
                                          sleep=clock.sleep)

        class _Request:
            def __init__(self, *args, **kwargs):
                pass

            def add_header(self, *args, **kwargs):
                pass

        fake_urllib = types.SimpleNamespace(
            request=types.SimpleNamespace(Request=_Request, urlopen=http.urlopen))
        with mock.patch.object(grab, "time", fake_time), \
                mock.patch.object(grab, "urllib", fake_urllib):
            return grab.clock_sample()

    def test_zero_offset_low_latency_is_ok(self):
        result = self._sample(offset_s=0, rtt_s=0.2)
        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["source"], "http_date")
        self.assertIsNotNone(result["checked_at"])
        self.assertGreaterEqual(result["uncertainty_s"], 1.0,
                                "整秒分辨率至少要算进 1 秒不确定")

    def test_true_offset_eight_seconds_is_warn(self):
        result = self._sample(offset_s=8, rtt_s=0.2)
        self.assertEqual(result["state"], "warn")
        self.assertGreater(result["offset_s"], 6.5)

    def test_slow_response_is_unknown_not_definite(self):
        """钟同源、响应慢 4 秒：只能说不确定，不能报「本机慢 4 秒」。"""
        result = self._sample(offset_s=0, rtt_s=4.0)
        self.assertEqual(result["state"], "unknown")
        self.assertGreater(result["rtt_s"], 2.0)

    def test_threshold_straddle_is_unknown(self):
        """3.1 秒偏差落在阈值两侧：既不是「准」，也不是「确定慢 3.1 秒」。"""
        result = self._sample(offset_s=3.1, rtt_s=0.1)
        self.assertEqual(result["state"], "unknown",
                         "差值区间跨过 3 秒阈值时不得下确定结论")
        low = abs(result["offset_s"]) - result["uncertainty_s"]
        high = abs(result["offset_s"]) + result["uncertainty_s"]
        self.assertLess(low, 3.0)
        self.assertGreater(high, 3.0)

    def test_missing_or_bad_date_is_unknown(self):
        self.assertEqual(self._sample(date_text=None)["state"], "unknown")
        self.assertEqual(self._sample(date_text="not-a-date")["state"], "unknown")

    def test_network_failure_is_unknown(self):
        result = self._sample(exc=OSError("network down"))
        self.assertEqual(result["state"], "unknown")
        self.assertIsNone(result["offset_s"])
        self.assertEqual(result["detail"], "OSError")


if __name__ == "__main__":
    unittest.main()
