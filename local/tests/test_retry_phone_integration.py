#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B 轮离线集成：pair → 读活动 → 自动模式 start → 合法扩窗 → 截止一致 → 停止
→ relay 挑战 → 匹配码 → 假校验成功 → 读新登录结论。

临时数据根 + fake worker/clock/Api + loopback HTTP；不读真实账号、不连学校。

@author LiXinwen
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

LOCAL = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
if str(LOCAL) not in sys.path:
    sys.path.insert(0, str(LOCAL))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from application.phone_service import PhoneAccessService  # noqa: E402
from domain.retry_window import (  # noqa: E402
    ADAPTIVE_RETRY, ADAPTIVE_SECONDS, DEFAULT_RETRY_SECONDS,
    extension_cap, initial_deadline,
)
from infrastructure.phone_http import PhoneHttpServer  # noqa: E402

PHONE_DIR = LOCAL / "ui" / "m"
ANCHOR = 1792000000.0


class ScriptedApi:
    """替身 Api：只按脚本回话，任何学校/进程动作都不会真发生。"""

    def __init__(self, service_holder):
        self.calls = []
        self.service_holder = service_holder
        self.login = {"hours_left": 3, "session_present": True}
        self.plan = None
        self.ack_lost = False

    # ---- 读 ----
    def list_activities(self, request_id=""):
        self.calls.append(("list_activities", request_id))
        return {"ok": True, "request_id": request_id, "items": [
            {"id": "a1", "name": "篮球赛", "sign_start": "2026-11-01 09:00:00",
             "sign_end": "2026-11-02 09:00:00", "signed": False}]}

    def check(self, request_id=""):
        self.calls.append(("check", request_id))
        return {"ok": True, "result": "valid", "request_id": request_id}

    # ---- 写 ----
    def start_plan(self, items, headed=False, retry_policy=None, request_id=""):
        self.calls.append(("start_plan", items, request_id))
        policy = retry_policy or {}
        task = {"task_id": "t1", "name": "篮球赛", "status": "submitting",
                "retry_for_s": int(policy.get("retry_for_s", 120)),
                "max_attempts": int(policy.get("max_attempts", 0)),
                "retry_started_at": ANCHOR,
                "retry_deadline": initial_deadline(
                    ANCHOR, policy.get("retry_for_s", 120), ANCHOR + 4000.0),
                "sign_end_ts": ANCHOR + 4000.0}
        self.plan = {"plan_id": "p1", "tasks": [task]}
        if self.ack_lost:
            return {"ok": False, "error": "busy"}   # 应答丢失：调用方以为没收到
        return {"ok": True, "plan_id": "p1", "request_id": request_id}

    def start_request_state(self, request_id=""):
        self.calls.append(("start_request_state", request_id))
        return {"ok": True, "state": "accepted", "plan_id": "p1",
                "request_id": request_id}

    def stop_plan(self, plan_id):
        self.calls.append(("stop_plan", plan_id))
        if self.plan:
            self.plan["tasks"][0]["status"] = "failed"
        return {"ok": True}

    def cancel_task(self, plan_id, task_id):
        self.calls.append(("cancel_task", plan_id, task_id))
        return {"ok": True}

    def save_prefs(self, prefs):
        self.calls.append(("save_prefs", prefs))
        return {"ok": True}

    # ---- relay ----
    def phone_relogin_begin(self):
        self.calls.append(("phone_relogin_begin",))
        return {"ok": True, "flow_id": "flow-1"}

    def phone_relogin_submit(self, flow_id="", challenge_id="", code=""):
        self.calls.append(("phone_relogin_submit", flow_id, challenge_id, code))
        if code == "Ab12":
            self.login = {"hours_left": 17, "session_present": True}
            return {"ok": True, "verified": True}
        return {"ok": False, "error": "bad_code"}

    def phone_relogin_cancel(self, flow_id=""):
        self.calls.append(("phone_relogin_cancel", flow_id))
        return {"ok": True}


class IntegrationBase(unittest.TestCase):
    def setUp(self):
        self.holder = {}
        self.api = ScriptedApi(self.holder)
        self.ui_dir = Path(tempfile.mkdtemp(prefix="tjise-int-ui-"))
        self.addCleanup(shutil.rmtree, str(self.ui_dir), True)
        for name in ("index.html", "styles.css", "app.js"):
            (self.ui_dir / name).write_text(
                (PHONE_DIR / name).read_text(encoding="utf-8"), encoding="utf-8")
        self.raw = {
            "activities": [{"id": "a1", "name": "篮球赛", "signed": False}],
            "login": dict(self.api.login),
            "plan": None, "busy": False,
            "terms": {"accepted_version": "1"},
            "retry_options": {"default": 120, "fallback_seconds": 120,
                              "adaptive_cap_seconds": ADAPTIVE_SECONDS,
                              "choices": [{"value": 120, "label": "2 分钟"},
                                          {"value": ADAPTIVE_RETRY,
                                           "label": "自动延长（最多25分钟）"},
                                          {"value": 0, "label": "直到报名截止"}]},
        }
        self.service = PhoneAccessService(
            api=self.api, now=lambda: 0.0,
            state_of=self._raw_state,
            server_factory=lambda host, port, svc: PhoneHttpServer(
                host, port, svc, ui_dir=self.ui_dir))
        self.holder["service"] = self.service
        started = self.service.start("127.0.0.1", 0)
        self.assertTrue(started["ok"], started)
        self.base = started["address"].rstrip("/")
        self.origin = self.base
        self.code = started["pair_code"]
        self.addCleanup(self.service.stop)

    def _raw_state(self):
        self.raw["login"] = dict(self.api.login)
        self.raw["plan"] = self.api.plan
        return self.raw

    def _req(self, method, path, body=None, headers=None, cookie=None):
        data = None
        merged = {"Host": self.base.split("//", 1)[1]}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            merged["Content-Type"] = "application/json"
        merged.update(headers or {})
        if cookie:
            merged["Cookie"] = cookie
        req = urllib.request.Request(self.base + path, data=data, method=method)
        for key, value in merged.items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def _pair(self, code=None):
        status, headers, body = self._req(
            "POST", "/pair", body={"code": code or self.code},
            headers={"Origin": self.origin})
        payload = json.loads(body)
        cookie = ""
        for key, value in headers.items():
            if key.lower() == "set-cookie" and value.startswith("tjise_phone_sid="):
                cookie = value.split(";", 1)[0]
        return status, payload, cookie

    def _action(self, action, args, cookie, csrf):
        status, _, body = self._req(
            "POST", "/api/action", body={"action": action, "args": args},
            headers={"Origin": self.origin, "X-Tjise-Csrf": csrf}, cookie=cookie)
        return status, json.loads(body)

    def _state(self, cookie, cursor=0):
        status, _, body = self._req("GET", "/api/state?after=%d" % cursor,
                                    cookie=cookie)
        self.assertEqual(status, 200)
        return json.loads(body)


class FullChainTests(IntegrationBase):
    def test_pair_read_start_extend_stop_relogin_chain(self):
        """计划要求的完整链路（每一步都用真 HTTP + 替身 Api）。"""
        # 1) 配对
        status, payload, cookie = self._pair()
        self.assertEqual(status, 200)
        self.assertTrue(cookie)

        # 2) 读活动（只读本机缓存）
        state = self._state(cookie)
        self.assertEqual(state["state"]["activities"][0]["id"], "a1")
        self.assertEqual(self.api.calls, [], "读状态不得触发学校流量")

        # 3) 自动模式 start
        status, result = self._action("start_plan", {
            "items": [{"id": "a1"}],
            "retry_policy": {"retry_for_s": ADAPTIVE_RETRY, "max_attempts": 0},
            "request_id": "req-1"}, cookie, payload["csrf"])
        self.assertTrue(result["ok"])
        self.assertEqual(self.api.calls[-1][0], "start_plan")
        self.assertEqual(self.api.calls[-1][1], [{"id": "a1"}])
        task = self.api.plan["tasks"][0]
        self.assertEqual(task["retry_for_s"], ADAPTIVE_RETRY)
        want_initial = initial_deadline(ANCHOR, ADAPTIVE_RETRY, ANCHOR + 4000.0)
        self.assertEqual(task["retry_deadline"], want_initial,
                         "自动模式初始截止必须是 120 秒口径")

        # 4) 合法扩窗：worker 事件进缓冲，权威截止前移
        candidate = ANCHOR + DEFAULT_RETRY_SECONDS + 300.0
        self.assertLessEqual(candidate,
                             extension_cap(ANCHOR, ANCHOR + 4000.0))
        self.service.record_event("log", {"kind": "window_extended",
                                          "retry_deadline": candidate})
        task["retry_deadline"] = candidate
        state = self._state(cookie)
        shown = state["state"]["plan"]["tasks"][0]
        self.assertEqual(shown["retry_deadline"], candidate,
                         "快照 deadline 必须与后端接纳值一致")

        # 5) 停止（显式）
        status, result = self._action("stop_plan", {"plan_id": "p1"},
                                      cookie, payload["csrf"])
        self.assertTrue(result["ok"])
        self.assertEqual(self.api.calls[-1], ("stop_plan", "p1"))

        # 6) relay 挑战 → 7) 匹配码 → 8) 假校验成功
        status, result = self._action("phone_relogin_begin", {}, cookie,
                                      payload["csrf"])
        self.assertTrue(result["ok"])
        self.service.record_event("phone_relogin", {
            "ready": True, "state": "waiting_code", "flow_id": "flow-1",
            "challenge_id": "chg-1", "image": "data:image/png;base64,AAAA",
            "expires_in": 60, "attempts": 0, "max_attempts": 3, "safe_error": ""})
        state = self._state(cookie)
        relay = state["events"][-1]["payload"]
        self.assertEqual(relay["challenge_id"], "chg-1")
        self.assertTrue(relay["image"].startswith("data:image/png;base64,"))

        status, result = self._action("phone_relogin_submit", {
            "flow_id": "flow-1", "challenge_id": "chg-1", "code": "Ab12"},
            cookie, payload["csrf"])
        self.assertTrue(result["ok"])

        # 9) 读新登录结论
        state = self._state(cookie)
        self.assertEqual(state["state"]["login"]["hours_left"], 17,
                         "新登录结论必须反映假校验成功后的会话")

    def test_lost_ack_reconciles_exactly_once(self):
        """丢 ack：同 request_id 只读对账只接纳一次，绝不重复开计划。"""
        self.api.ack_lost = True
        status, payload, cookie = self._pair()
        status, result = self._action("start_plan", {
            "items": [{"id": "a1"}], "request_id": "req-9"},
            cookie, payload["csrf"])
        self.assertFalse(result["ok"], "应答丢失时调用方看不到成功")
        started = [c for c in self.api.calls if c[0] == "start_plan"]
        self.assertEqual(len(started), 1, "对账不得再开一场计划")
        status, result = self._action("start_request_state",
                                      {"request_id": "req-9"}, cookie,
                                      payload["csrf"])
        self.assertEqual(result["state"], "accepted")
        status, result = self._action("start_request_state",
                                      {"request_id": "req-9"}, cookie,
                                      payload["csrf"])
        self.assertEqual(result["state"], "accepted")
        started = [c for c in self.api.calls if c[0] == "start_plan"]
        self.assertEqual(len(started), 1, "对账多少次都只接纳一次")

    def test_repair_after_401_does_not_replay_writes(self):
        """401 后重新配对：不得自动重放任何写动作。"""
        status, payload, cookie = self._pair()
        self._action("start_plan", {"items": [{"id": "a1"}], "request_id": "r1"},
                     cookie, payload["csrf"])
        writes_before = [c for c in self.api.calls if c[0] != "list_activities"]
        # 会话失效（撤销 cookie）→ 401
        self.service.revoke_all()
        status, _, _ = self._req("GET", "/api/state", cookie=cookie)
        self.assertIn(status, (401, 403))
        # 重新配对（新的一码一次）后只读状态，不重放写操作
        fresh = self.service.current_pair_code()
        status, payload2, cookie2 = self._pair(fresh)
        self.assertEqual(status, 200, "新的一码一次必须能重新配对")
        state = self._state(cookie2)
        self.assertEqual(state["state"]["activities"][0]["id"], "a1")
        writes_after = [c for c in self.api.calls if c[0] != "list_activities"]
        self.assertEqual(writes_after, writes_before + [],
                         "重新配对后不得自动重放写操作")

    def test_unknown_result_does_not_auto_restart(self):
        """结果未知（pending）不自动重启计划，也不重发未确认任务。"""
        status, payload, cookie = self._pair()
        self._action("start_plan", {"items": [{"id": "a1"}], "request_id": "r1"},
                     cookie, payload["csrf"])
        self.api.plan["tasks"][0]["status"] = "failed"
        self.api.plan["tasks"][0]["stop_reason"] = "unconfirmed"
        state = self._state(cookie)
        task = state["state"]["plan"]["tasks"][0]
        self.assertEqual(task["stop_reason"], "unconfirmed")
        started = [c for c in self.api.calls if c[0] == "start_plan"]
        self.assertEqual(len(started), 1,
                         "未知结果不得自动重启/重发（决策 6）")

    def test_deadline_matches_across_worker_parent_desktop(self):
        """三种模式下 worker 公式 / 父截止 / 手机投影同值（离线对照）。"""
        from domain.time_text import task_deadline
        from grab import compute_retry_window
        for mode, want in ((120, ANCHOR + 120.0),
                           (ADAPTIVE_RETRY, ANCHOR + DEFAULT_RETRY_SECONDS),
                           (0, ANCHOR + 4000.0)):
            _, worker_deadline, _ = compute_retry_window(
                ANCHOR, ANCHOR + 4000.0, mode, now=ANCHOR)

            class Spec:
                retry_started_at = ANCHOR
                sign_end = "2026-11-02 09:00:00"
                retry_for_s = mode

            parent = task_deadline(Spec(), lambda _t: ANCHOR + 4000.0)
            self.assertEqual(worker_deadline, want)
            self.assertEqual(parent, want)
            self.assertEqual(
                initial_deadline(ANCHOR, mode, ANCHOR + 4000.0), want)


if __name__ == "__main__":
    unittest.main()
