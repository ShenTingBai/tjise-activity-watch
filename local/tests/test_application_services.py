# -*- coding: utf-8 -*-
"""application 用例的内存替身测试：端口全注入，零文件/零进程/零网络。

覆盖：更新检查用例（fetch 端口）、check 事件整理（纯分类）、计划项归一化
（domain 纯策略经 application 边界暴露的形状）。

@author LiXinwen
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

LOCAL = Path(__file__).resolve().parents[1]
if str(LOCAL) not in sys.path:
    sys.path.insert(0, str(LOCAL))

from application import login_service, update_service  # noqa: E402
from domain import plan_rules  # noqa: E402


class FakeState:
    """UpdateState 协议的内存替身：只记字段与事件。"""

    def __init__(self, closing: bool = False):
        self._lock = threading.Lock()
        self._closing = closing
        self._updating = True
        self._update_result = {"state": "old"}
        self._update_installer = {"name": "stale.exe"}
        self._update_installer_path = "D:/@Temp/stale.exe"
        self._update_cancelled = False
        self.events = []

    def _emit(self, kind, payload):
        self.events.append((kind, payload))


class UpdateServiceTests(unittest.TestCase):
    def test_check_success_stores_installer_and_clears_old_download(self):
        state = FakeState()
        expected = {"state": "available", "latest_version": "1.2.0",
                    "installer": {"name": "a.exe"}}

        def fetch(current_version):
            self.assertEqual(current_version, "1.1.0")
            return expected

        update_service.run_check(state, "req-1", fetch=fetch,
                                 current_version="1.1.0", latest_url="https://x")
        self.assertEqual(state._update_result["request_id"], "req-1")
        self.assertEqual(state._update_installer["name"], "a.exe")
        self.assertEqual(state._update_installer_path, "", "旧产物必须作废")
        self.assertFalse(state._updating)
        self.assertEqual(state.events, [("update", state._update_result)])

    def test_check_crash_unlocks_and_reports_error_state(self):
        state = FakeState()

        def fetch(current_version):
            raise RuntimeError("boom")

        update_service.run_check(state, "req-2", fetch=fetch,
                                 current_version="1.1.0", latest_url="https://x")
        self.assertFalse(state._updating)
        self.assertEqual(state._update_result["state"], "error")
        self.assertEqual(state._update_result["error_code"], "unexpected_error")
        self.assertEqual(state._update_result["request_id"], "req-2")

    def test_closing_suppresses_update_event(self):
        state = FakeState(closing=True)
        update_service.run_check(state, "req-3", fetch=lambda v: {"state": "current"},
                                 current_version="1.1.0", latest_url="https://x")
        self.assertEqual(state.events, [], "关闭中不推事件（终态仍留档）")
        self.assertEqual(state._update_result["state"], "current")


class LoginServiceTests(unittest.TestCase):
    def test_build_check_event_carries_frozen_fields(self):
        state = {"session_present": True, "session_expired": False,
                 "hours_left": 12.5, "saved_at": "2026-10-29 08:00:00",
                 "estimated_expires_at": 1793016000.0}
        result, clock, payload = login_service.build_check_event(
            state, 1, "[X] 登录态已失效 code:4001\n请重新登录", "")
        self.assertEqual(result, "expired")
        self.assertEqual(payload["hint"], "login")
        self.assertEqual(payload["hours_left"], 12.5)
        self.assertTrue(payload["session_present"])
        self.assertFalse(payload["session_expired"])
        self.assertEqual(payload["clock"]["state"], "unknown")

    def test_done_result_login_only_needs_exit_code(self):
        self.assertEqual(login_service.done_result("login", 0, ["无关行"]), (True, ""))
        ok, reason = login_service.done_result("login", 3, ["[X] 没登录"])
        self.assertFalse(ok)
        self.assertEqual(reason, "没登录")


class PlanRulesTests(unittest.TestCase):
    def test_normalize_rejects_duplicate_ids(self):
        items = [{"id": "a1"}, {"id": "a1"}]
        normalized, error = plan_rules.normalize_plan_items(items, {"a1": {"id": "a1"}}, 0)
        self.assertIsNone(normalized)
        self.assertEqual(error["errors"][0]["kind"], "duplicate_id")

    def test_merge_keeps_local_confirm_within_window(self):
        prev = {"a1": {"signed": True, "signed_source": "plan_success",
                       "signed_confirmed_at": 100.0}}
        merged = plan_rules.merge_list_activities(
            [{"id": "a1", "signed": False}], prev, now=160.0, window_s=120.0)
        self.assertTrue(merged[0]["signed"])
        self.assertTrue(merged[0]["sync_pending"])

    def test_merge_drops_local_confirm_after_window(self):
        prev = {"a1": {"signed": True, "signed_source": "plan_success",
                       "signed_confirmed_at": 100.0}}
        merged = plan_rules.merge_list_activities(
            [{"id": "a1", "signed": False}], prev, now=300.0, window_s=120.0)
        self.assertFalse(merged[0]["signed"])


if __name__ == "__main__":
    unittest.main()
