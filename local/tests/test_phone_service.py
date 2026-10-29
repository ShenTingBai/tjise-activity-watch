#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手机受限访问用例测试：配对/会话/限速、动作白名单、事件缓冲与关闭收尾。

全部替身：假 Api、假时钟、假 server factory；不连学校、不读真实凭据、
不开 100.x 监听、不启真实浏览器。

@author LiXinwen
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

LOCAL = Path(__file__).resolve().parents[1]
if str(LOCAL) not in sys.path:
    sys.path.insert(0, str(LOCAL))

from application.phone_service import PhoneAccessService  # noqa: E402
from domain.phone_rules import (  # noqa: E402
    COOKIE_NAME, CSRF_HEADER, EVENT_BUFFER_LIMIT, GLOBAL_FAIL_LIMIT,
    IP_FAIL_LIMIT, IP_LOCK_S, PAIR_TTL_S, SESSION_TTL_S,
    is_allowed_bind, project_state,
)


class FakeApi:
    """替身 Api：只记录调用，任何学校/进程动作都不会真发生。"""

    def __init__(self):
        self.calls = []
        self.plan_snapshot = {"plan_id": "p1", "tasks": []}
        self.saved_prefs = []
        self.fail = False

    def list_activities(self, request_id=""):
        self.calls.append(("list_activities", request_id))
        return {"ok": True, "items": [], "request_id": request_id}

    def check(self, request_id=""):
        self.calls.append(("check", request_id))
        return {"ok": True, "result": "valid", "request_id": request_id}

    def start_plan(self, items, headed=False, retry_policy=None, request_id=""):
        self.calls.append(("start_plan", items, headed, retry_policy, request_id))
        if self.fail:
            raise RuntimeError("boom")
        return {"ok": True, "plan_id": "p1", "request_id": request_id}

    def stop_plan(self, plan_id):
        self.calls.append(("stop_plan", plan_id))
        return {"ok": True}

    def cancel_task(self, plan_id, task_id):
        self.calls.append(("cancel_task", plan_id, task_id))
        return {"ok": True}

    def start_request_state(self, request_id):
        self.calls.append(("start_request_state", request_id))
        return {"ok": True}

    def cancel_start_request(self, request_id):
        self.calls.append(("cancel_start_request", request_id))
        return {"ok": True}

    def save_prefs(self, prefs):
        self.calls.append(("save_prefs", prefs))
        self.saved_prefs.append(prefs)
        return {"ok": True}


class FakeServer:
    def __init__(self, host, port, service):
        self.host = host
        self.port = port
        self.service = service
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def address(self):
        return "http://%s:%d" % (self.host, self.port or 8765)


class PhoneServiceBase(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.clock = {"now": 1000.0}
        self.servers = []
        self.raw_state = {
            "activities": [{"id": "a1", "name": "活动", "token": "fake-token",
                            "password": "fake-pass", "path": "C:/secret"}],
            "login": {"hours_left": 3, "session_present": True,
                      "token": "fake-login-token"},
            "plan": {"plan_id": "p1"},
            "business_clock": {"state": "ready"},
            "retry_options": {"default": 120},
            "terms": {"version": "1", "accepted_version": "1"},
            "help": {"steps": [{"text": "路径 C:/x"}]},
            "update_check": {"installer_path": "C:/setup.exe"},
            "login_relay": {"ready": False},
        }

        def factory(host, port, service):
            server = FakeServer(host, port, service)
            self.servers.append(server)
            return server

        self.service = PhoneAccessService(
            api=self.api, now=lambda: self.clock["now"],
            state_of=lambda: self.raw_state, server_factory=factory)

    def tearDown(self):
        self.service.stop()


class ProjectionTests(PhoneServiceBase):
    def test_projection_strips_token_password_path(self):
        projected = project_state(self.raw_state)
        text = str(projected)
        self.assertNotIn("fake-token", text)
        self.assertNotIn("fake-pass", text)
        self.assertNotIn("fake-login-token", text)
        self.assertNotIn("C:/secret", text)
        self.assertNotIn("C:/setup.exe", text)
        self.assertIn("session_present", text, "登录结论要保留")
        self.assertEqual(projected["terms"]["accepted_version"], "1")

    def test_projection_drops_help_and_update_blocks(self):
        projected = project_state(self.raw_state)
        self.assertNotIn("help", projected)
        self.assertNotIn("update_check", projected)
        self.assertIn("retry_options", projected)

    def test_state_is_read_from_local_cache_only(self):
        """GET state 只读本机缓存：不调用任何学校/刷新动作。"""
        self.service.safe_state()
        self.assertEqual(self.api.calls, [],
                         "读状态不得触发学校流量或刷新")


class PairAndSessionTests(PhoneServiceBase):
    def test_default_not_running_and_no_socket(self):
        self.assertFalse(self.service.is_running())
        self.assertEqual(self.servers, [], "默认不得创建任何监听")
        self.assertEqual(self.service.current_pair_code(), "")

    def test_start_issues_pair_code_and_stop_revokes(self):
        result = self.service.start("127.0.0.1", 0)
        self.assertTrue(result["ok"])
        code = result["pair_code"]
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isdigit())
        redeemed = self.service.pair(code, ip="127.0.0.1")
        self.assertTrue(redeemed["ok"])
        self.assertIn("HttpOnly", redeemed["cookie"])
        self.assertIn("SameSite=Strict", redeemed["cookie"])
        self.assertNotIn("Secure", redeemed["cookie"])
        self.assertTrue(self.service.authenticate(redeemed["cookie"]))
        self.service.stop()
        self.assertIsNone(self.service.authenticate(redeemed["cookie"]),
                          "停服务必须撤销全部会话")
        self.assertTrue(self.servers[0].stopped)

    def test_pair_code_is_one_time(self):
        self.service.start("127.0.0.1", 0)
        code = self.service.current_pair_code()
        first = self.service.pair(code, ip="127.0.0.1")
        second = self.service.pair(code, ip="127.0.0.1")
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"], "一码一次：第二次兑换必须失败")

    def test_concurrent_redeem_only_one_wins(self):
        self.service.start("127.0.0.1", 0)
        code = self.service.current_pair_code()
        results = []
        barrier = threading.Barrier(2)

        def redeem():
            barrier.wait()
            results.append(self.service.pair(code, ip="127.0.0.1")["ok"])

        threads = [threading.Thread(target=redeem) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(results.count(True), 1, "并发双兑换只能有一赢")

    def test_pair_code_expires_after_five_minutes(self):
        self.service.start("127.0.0.1", 0)
        code = self.service.current_pair_code()
        self.clock["now"] += PAIR_TTL_S + 1
        self.assertFalse(self.service.pair(code, ip="127.0.0.1")["ok"],
                         "5 分钟后配对码必须作废")
        fresh = self.service.current_pair_code()
        self.assertNotEqual(fresh, code, "过期后面板换一张新的一码一次")

    def test_three_failures_lock_ip_and_global_locks_across_ips(self):
        self.service.start("127.0.0.1", 0)
        for _ in range(IP_FAIL_LIMIT):
            result = self.service.pair("000000", ip="127.0.0.1")
        self.assertEqual(result["error"], "bad_code")
        locked = self.service.pair(self.service.current_pair_code(), ip="127.0.0.1")
        self.assertEqual(locked["error"], "locked", "单 IP 连错 3 次锁 60 秒")
        # 换 IP 也逃不过全局限速
        self.clock["now"] += IP_LOCK_S + 1
        for _ in range(GLOBAL_FAIL_LIMIT):
            self.service.pair("000000", ip="100.64.0.2")
        blocked = self.service.pair(self.service.current_pair_code(), ip="100.64.0.9")
        self.assertEqual(blocked["error"], "locked", "换 IP 也要被全局限速拦下")

    def test_session_expires_in_eight_hours_and_check_does_not_refresh(self):
        self.service.start("127.0.0.1", 0)
        redeemed = self.service.pair(self.service.current_pair_code(), ip="127.0.0.1")
        cookie = redeemed["cookie"]
        first = self.service.authenticate(cookie)
        self.assertIsNotNone(first)
        created = first["created_at"]
        self.clock["now"] += 60
        again = self.service.authenticate(cookie)
        self.assertIsNotNone(again)
        self.assertEqual(again["created_at"], created,
                         "校验不得刷新会话寿命")
        self.clock["now"] += SESSION_TTL_S
        self.assertIsNone(self.service.authenticate(cookie), "8 小时后必须失效")


class DispatchWhitelistTests(PhoneServiceBase):
    def test_forbidden_actions_are_rejected(self):
        for action in ("shutdown", "open_data", "update", "check_update",
                       "save_prefs", "accept_terms", "bootstrap", "login",
                       "eval", "__class__"):
            with self.subTest(action=action):
                result = self.service.dispatch(action, {})
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "action_not_allowed")
        self.assertEqual(self.api.calls, [], "禁止动作不得触达 Api")

    def test_extra_args_are_rejected(self):
        result = self.service.dispatch("check", {"request_id": "r1", "evil": "x"})
        self.assertEqual(result["error"], "args_not_allowed")
        self.assertEqual(self.api.calls, [])

    def test_save_retry_policy_only_allows_policy_keys(self):
        ok = self.service.dispatch(
            "save_retry_policy", {"retry_policy": {"retry_for_s": -1, "max_attempts": 3}})
        self.assertTrue(ok["ok"])
        self.assertEqual(self.api.saved_prefs,
                         [{"retry_policy": {"retry_for_s": -1, "max_attempts": 3}}])
        bad = self.service.dispatch(
            "save_retry_policy", {"retry_policy": {"terms_version": "9"}})
        self.assertEqual(bad["error"], "args_not_allowed")
        self.assertEqual(len(self.api.saved_prefs), 1)

    def test_start_plan_carries_only_whitelisted_shape(self):
        result = self.service.dispatch("start_plan", {
            "items": [{"id": "a1", "signed": True, "sign_end": "2026-01-01 00:00:00"}],
            "headed": True, "retry_policy": {"retry_for_s": 120},
            "request_id": "req-1"})
        self.assertTrue(result["ok"])
        name, items, headed, policy, rid = self.api.calls[0]
        self.assertEqual(name, "start_plan")
        self.assertEqual(items, [{"id": "a1"}], "客户端自报的 signed/时间不得带进计划")
        self.assertTrue(headed)
        self.assertEqual(policy, {"retry_for_s": 120})
        self.assertEqual(rid, "req-1")

    def test_internal_exception_returns_type_only(self):
        self.api.fail = True
        result = self.service.dispatch("start_plan", {"items": [{"id": "a1"}]})
        self.assertEqual(result["error"], "internal")
        self.assertNotIn("Traceback", str(result))
        self.assertIn("RuntimeError", result["text"])


class EventBufferTests(PhoneServiceBase):
    def test_events_survive_without_window(self):
        """窗口为空也要能发布：_emit 只进缓冲，不回调任何客户端。"""
        self.service.record_event("log", {"text": "hello", "token": "fake"})
        payload = self.service.safe_state()
        self.assertEqual(len(payload["events"]), 1)
        self.assertEqual(payload["events"][0]["kind"], "log")
        self.assertNotIn("fake", str(payload["events"][0]))

    def test_event_buffer_is_bounded_to_400(self):
        for index in range(EVENT_BUFFER_LIMIT + 50):
            self.service.record_event("log", {"text": "line %d" % index})
        events = self.service.safe_state()["events"]
        self.assertEqual(len(events), EVENT_BUFFER_LIMIT)
        self.assertEqual(events[0]["payload"]["text"],
                         "line %d" % 50, "旧事件按序号淘汰，只留最近 400 条")

    def test_old_cursor_returns_reset_snapshot(self):
        for index in range(EVENT_BUFFER_LIMIT + 50):
            self.service.record_event("log", {"text": "line %d" % index})
        fresh = self.service.safe_state(cursor=EVENT_BUFFER_LIMIT + 50)
        self.assertFalse(fresh["reset"])
        self.assertEqual(fresh["events"], [], "cursor 等于最新序号时没有新事件")
        stale = self.service.safe_state(cursor=0)
        self.assertTrue(stale["reset"], "cursor 太旧必须整体重置，不漏中间状态")
        self.assertEqual(len(stale["events"]), EVENT_BUFFER_LIMIT)

    def test_incremental_cursor_returns_only_new(self):
        self.service.record_event("log", {"text": "one"})
        self.service.record_event("log", {"text": "two"})
        seen = self.service.safe_state(cursor=1)
        self.assertFalse(seen["reset"])
        self.assertEqual([e["payload"]["text"] for e in seen["events"]], ["two"])


class BindAndLifecycleTests(PhoneServiceBase):
    def test_only_loopback_or_tailscale_addresses_allowed(self):
        self.assertTrue(is_allowed_bind("127.0.0.1"))
        self.assertTrue(is_allowed_bind("localhost"))
        self.assertTrue(is_allowed_bind("100.64.1.2"))
        self.assertFalse(is_allowed_bind("0.0.0.0"))
        self.assertFalse(is_allowed_bind("192.168.1.5"))
        self.assertFalse(is_allowed_bind("10.1.2.3"))
        self.assertFalse(is_allowed_bind("8.8.8.8"))
        self.assertFalse(is_allowed_bind("::1"))
        self.assertFalse(is_allowed_bind(""))

    def test_start_rejects_public_bind(self):
        result = self.service.start("0.0.0.0", 0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "bind_not_allowed")
        self.assertEqual(self.servers, [])

    def test_stop_is_bounded_and_touches_only_own_server(self):
        self.service.start("127.0.0.1", 0)
        other = FakeServer("127.0.0.1", 1, self.service)
        result = self.service.stop()
        self.assertTrue(result["ok"])
        self.assertTrue(self.servers[0].stopped)
        self.assertFalse(other.stopped, "关闭不得影响别的进程/服务")
        self.assertFalse(self.service.is_running())
