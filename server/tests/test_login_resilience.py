#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""登录态误判的两道防线：4001 重试、连续失败才重登（离线，不发网络请求）。

背景（2026-10-30 07:00 实测）：整点 cron 齐发时学校接口会偶发一次 4001。
同一 token 前后两个请求，probe 07:00:01 返回 ok、fetch 07:00:02 返回 4001。
旧逻辑把单次 4001 直接当登录失效：fetch 报错，auto_login 立刻重登换掉 token，
仍在用旧 token 的进程于是全部 4001 —— 自己制造了一次真失效。

这里锁两条防线：
- fetch.call_page：4001 先重试一次，仍失败才上抛 PermissionError；
- auto_login._probe_fail_streak：连续 PROBE_FAIL_STREAK 次才判失效，
  只失败一次时交给调用方现查接口确认。

全部用替身：假 call_api、临时日志文件。不读真实 token，不发任何请求。

@author LiXinwen
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import fetch  # noqa: E402


def _stub_heavy_deps():
    """auto_login 依赖 numpy/PIL/playwright/fcntl，测试环境没有也不该装。

    这些只在真正登录时才用到，探针判据是纯文本逻辑，替身掉即可。
    """
    for name in ("numpy", "PIL", "PIL.Image", "playwright", "playwright.sync_api"):
        sys.modules.setdefault(name, types.ModuleType(name))
    api = sys.modules["playwright.sync_api"]
    if not hasattr(api, "TimeoutError"):
        api.TimeoutError = type("PlaywrightTimeoutError", (Exception,), {})
    if not hasattr(api, "sync_playwright"):
        api.sync_playwright = lambda *a, **k: None
    # fcntl 是 Unix 专有；探针判据用不到文件锁
    sys.modules.setdefault("fcntl", types.ModuleType("fcntl"))


def _load_auto_login():
    _stub_heavy_deps()
    spec = importlib.util.spec_from_file_location(
        "auto_login_under_test", DEPLOY / "auto_login.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


auto_login = _load_auto_login()


def _probe_line(result, when):
    return ("%s token=abcd1234 saved_at=2026-10-30 05:32:03 age=1h00m result=%s"
            % (when.strftime("%Y-%m-%d %H:%M:%S"), result))


class FetchLoginRetryTests(unittest.TestCase):
    """4001 只重试一次，其它错误码立即上抛（重试只会把真错误推迟一轮）。"""

    def _patch(self, responses):
        """按调用顺序返回 responses，超出长度就重复最后一个。"""
        calls = []

        def fake(token, page_num, **kwargs):
            calls.append(page_num)
            idx = min(len(calls) - 1, len(responses) - 1)
            return responses[idx]

        patcher = mock.patch.object(fetch, "call_api", side_effect=fake)
        sleeper = mock.patch.object(fetch.time, "sleep")
        return patcher, sleeper, calls

    def test_retries_once_then_succeeds(self):
        ok = {"code": 1, "data": {"count": 1, "list": [{"hdmc": "甲", "id": "1"}]}}
        patcher, sleeper, calls = self._patch([{"code": 4001, "message": "身份验证失败"}, ok])
        with patcher, sleeper as sleep:
            items = fetch.fetch_all("tok")
        self.assertEqual(len(items), 1)
        self.assertEqual(calls, [1, 1])
        sleep.assert_called_once_with(fetch.LOGIN_RETRY_DELAY_S)

    def test_raises_after_exhausting_retries(self):
        bad = {"code": 4001, "message": "身份验证失败"}
        patcher, sleeper, calls = self._patch([bad])
        with patcher, sleeper:
            with self.assertRaises(PermissionError):
                fetch.fetch_all("tok")
        self.assertEqual(len(calls), fetch.LOGIN_RETRY_ATTEMPTS)

    def test_other_error_not_retried(self):
        patcher, sleeper, calls = self._patch([{"code": 0, "message": "参数错误"}])
        with patcher, sleeper as sleep:
            with self.assertRaises(RuntimeError):
                fetch.fetch_all("tok")
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()


class ProbeStreakTests(unittest.TestCase):
    """连续失败才判失效；单次失败只交给调用方现查接口。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "token_probe.log")
        self._saved = auto_login.PROBE_LOG
        auto_login.PROBE_LOG = self.path
        self.addCleanup(setattr, auto_login, "PROBE_LOG", self._saved)

    def _write(self, results, base=None):
        base = base or datetime.now()
        with open(self.path, "w", encoding="utf-8") as f:
            for idx, result in enumerate(results):
                when = base - timedelta(minutes=15 * (len(results) - 1 - idx))
                f.write(_probe_line(result, when) + "\n")

    def test_zero_when_last_ok(self):
        self._write(["fail", "fail", "ok"])
        self.assertEqual(auto_login._probe_fail_streak(), 0)

    def test_counts_consecutive_failures(self):
        self._write(["ok", "fail", "fail"])
        self.assertEqual(auto_login._probe_fail_streak(), 2)

    def test_single_failure_is_not_enough(self):
        self._write(["ok", "ok", "fail"])
        self.assertEqual(auto_login._probe_fail_streak(), 1)
        self.assertLess(1, auto_login.PROBE_FAIL_STREAK)

    def test_stops_counting_at_limit(self):
        self._write(["fail"] * 6)
        self.assertEqual(auto_login._probe_fail_streak(), auto_login.PROBE_FAIL_STREAK)

    def test_missing_log_returns_unknown(self):
        self.assertEqual(auto_login._probe_fail_streak(), -1)

    def test_stale_log_returns_unknown(self):
        self._write(["fail"], base=datetime.now() - timedelta(hours=2))
        self.assertEqual(auto_login._probe_fail_streak(), -1)

    def test_undecidable_result_invalidates_streak(self):
        # http-error 表示「判定不了」，不能算失败，也不能算 ok
        self._write(["fail", "fail", "http-error"])
        self.assertEqual(auto_login._probe_fail_streak(), -1)


if __name__ == "__main__":
    unittest.main()
