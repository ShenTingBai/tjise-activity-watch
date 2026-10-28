#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务器 owner 抢报名的黄金风暴 / 404 退避 / 请求记账（离线替身测试）。

覆盖 2026-10-28 服务器侧契约（与本机 local/grab.py 同一套阶段/字段）：
- 90ms×10 黄金风暴 → 180ms 全速 → 404×3 后 1.6 倍退避上限 3 秒；
- 404 连击自适应扩窗到起点起 25 分钟，sign_end 硬截止绝不越过；
- 每笔 signUp 请求唯一 request_id，响应按归属结算，乱序合法、重复结算只认一次；
- 未决（响应体读不出）不伪造成失败，停止时报 unconfirmed；
- 成功 / 登录失效 / 用户停止立即收口；
- owner 私聊通知只发 p2p chat_id 或 PRIVATE_OWNER_OPEN_ID，不退化到群发。

全部用替身：假页面 / 假请求响应 / 假时钟 / 假通知 / 假 notify 模块。
绝不读取真实 /root/tjise-login、server/data 或 Feishu 凭据，不发任何网络请求。

@author LiXinwen
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
SCRIPTS = ROOT / "scripts"
for extra in (str(SCRIPTS),):
    if extra not in sys.path:
        sys.path.insert(0, extra)


def _load_grab():
    """按文件路径加载 server/deploy/grab_signup.py（不跑 main）。"""
    spec = importlib.util.spec_from_file_location(
        "grab_signup_under_test", DEPLOY / "grab_signup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


grab = _load_grab()


class FakeClock:
    """虚拟时钟：时间只随 wait_for_timeout 推进，绝不真实 sleep。"""

    def __init__(self, wall=1000.0, mono=500.0):
        self.wall = wall
        self.mono = mono

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def advance(self, seconds):
        self.wall += float(seconds)
        self.mono += float(seconds)


class FakeRequest:
    def __init__(self, url):
        self.url = url


class FakeResponse:
    def __init__(self, request, status=200, payload=None, unreadable=False):
        self.request = request
        self.url = request.url
        self.status = status
        self._payload = payload
        self._unreadable = unreadable

    def json(self):
        if self._unreadable:
            raise ValueError("body unreadable")
        return self._payload


class FakeLocator:
    def __init__(self, page, kind):
        self._page = page
        self._kind = kind

    def filter(self, has_text=None):
        return self

    def count(self):
        if self._kind == "button":
            return 1 if self._page.button_visible else 0
        return 1

    @property
    def first(self):
        return self

    def click(self, timeout=None):
        if self._kind == "confirm":
            self._page.click_times.append(self._page.clock.wall)
            self._page._on_confirm_click()
        else:
            self._page._on_signup_click()


# 结果脚本元素：None = 点了但没发出请求
NO_REQUEST = None


def out_404(delay_ms=0):
    return {"status": 404, "payload": None, "unreadable": False, "delay_ms": delay_ms}


def out_real(payload, delay_ms=0):
    return {"status": 200, "payload": payload, "unreadable": False, "delay_ms": delay_ms}


def out_lost(delay_ms=0):
    return {"status": 200, "payload": None, "unreadable": True, "delay_ms": delay_ms}


class FakePage:
    """假 Playwright 页面：点「确定」按脚本发出 signUp 请求/响应。

    响应可延迟送达（delay_ms），支持乱序与多笔未决；wait_for_timeout
    只推进假时钟并在片内派发到期响应，模拟同步 API 的事件泵。
    """

    SIGNUP_URL = "https://xg.tjise.edu.cn/dr/api/rjyHdCyr/signUp/fake-hash"

    def __init__(self, clock, outcomes, button_visible=True):
        self.clock = clock
        self.outcomes = list(outcomes)
        self.default_outcome = NO_REQUEST
        self.button_visible = button_visible
        self.listeners = {}
        self.click_times = []
        self.waits_ms = []
        self.reloads = 0
        self._deferred = []

    # -- 事件 -------------------------------------------------------------
    def on(self, event, handler):
        self.listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        if event in self.listeners and handler in self.listeners[event]:
            self.listeners[event].remove(handler)

    def _fire(self, event, payload):
        for handler in list(self.listeners.get(event, [])):
            handler(payload)

    # -- 页面动作 ---------------------------------------------------------
    def locator(self, selector):
        return FakeLocator(self, "button" if "uni-button" in selector else "other")

    def get_by_text(self, text, exact=False):
        return FakeLocator(self, "confirm")

    def reload(self, wait_until=None, timeout=None):
        self.reloads += 1

    def eval_on_selector_all(self, selector, script):
        return []

    def wait_for_timeout(self, ms):
        self.waits_ms.append(int(ms))
        self.clock.advance(int(ms) / 1000.0)
        # 到期响应按到期顺序派发；delay 模拟乱序到达
        due = [item for item in self._deferred if item["due"] <= self.clock.mono]
        self._deferred = [item for item in self._deferred if item["due"] > self.clock.mono]
        for item in sorted(due, key=lambda it: it["due"]):
            self._fire("response", item["response"])

    # -- 提交动作 ---------------------------------------------------------
    def _on_signup_click(self):
        pass    # 真实点击只开确认框，请求在点「确定」后发出

    def _on_confirm_click(self):
        outcome = self.outcomes.pop(0) if self.outcomes else self.default_outcome
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is NO_REQUEST:
            return
        request = FakeRequest(self.SIGNUP_URL)
        self._fire("request", request)
        response = FakeResponse(request,
                                status=outcome.get("status", 200),
                                payload=outcome.get("payload"),
                                unreadable=outcome.get("unreadable", False))
        delay_ms = int(outcome.get("delay_ms") or 0)
        if delay_ms <= 0:
            self._fire("response", response)
        else:
            self._deferred.append({"due": self.clock.mono + delay_ms / 1000.0,
                                   "response": response})


class BurstPolicyPureTests(unittest.TestCase):
    """黄金风暴状态机纯逻辑：阶段转移、间隔、404 连击与扩窗。"""

    START = 1000.0

    def setUp(self):
        self.clock = FakeClock(wall=self.START, mono=500.0)

    def _policy(self, **kw):
        args = dict(open_ts=self.START, base_deadline=self.START + 120,
                    clock=self.clock)
        args.update(kw)
        return grab.BurstPolicy(**args)

    def test_strategy_constants_frozen(self):
        # 与本机黄金风暴契约逐项一致（2026-10-28 用户确认方案）
        self.assertEqual(grab.GOLDEN_WINDOW_S, 1.6)
        self.assertEqual(grab.GOLDEN_INTERVAL_S, 0.09)
        self.assertEqual(grab.GOLDEN_ROUNDS, 10)
        self.assertEqual(grab.FULL_SPEED_INTERVAL_S, 0.18)
        self.assertEqual(grab.NOT_READY_404_LIMIT, 3)
        self.assertEqual(grab.BACKOFF_FACTOR, 1.6)
        self.assertEqual(grab.BACKOFF_MAX_S, 3.0)
        self.assertEqual(grab.ADAPTIVE_WINDOW_S, 1500.0)

    def test_golden_phase_ten_rounds_at_90ms(self):
        policy = self._policy()
        self.assertEqual(policy.phase, policy.PHASE_GOLDEN)
        self.assertEqual(policy.interval_s(), grab.GOLDEN_INTERVAL_S)
        for _ in range(grab.GOLDEN_ROUNDS):
            rid = policy.register_request()
            self.assertTrue(rid.startswith("req-"))
        self.assertEqual(policy.golden_rounds, grab.GOLDEN_ROUNDS)
        self.assertEqual(policy.phase, policy.PHASE_FULL_SPEED)
        self.assertEqual(policy.interval_s(), grab.FULL_SPEED_INTERVAL_S)
        self.assertEqual(policy.round, policy.attempts)

    def test_golden_window_expiry_exits_to_full_speed(self):
        policy = self._policy()
        self.clock.wall = self.START + grab.GOLDEN_WINDOW_S + 0.01
        self.assertEqual(policy.phase, policy.PHASE_FULL_SPEED)

    def test_golden_skipped_when_open_already_past(self):
        self.clock.wall = self.START + grab.GOLDEN_WINDOW_S + 5.0
        policy = self._policy()
        self.assertEqual(policy.phase, policy.PHASE_FULL_SPEED)
        policy.register_request()
        self.assertEqual(policy.golden_rounds, 0)

    def test_404_streak_enters_adaptive_and_extends_window(self):
        policy = self._policy()
        self.clock.wall = self.START + grab.GOLDEN_WINDOW_S + 1.0
        for _ in range(grab.NOT_READY_404_LIMIT):
            rid = policy.register_request()
            policy.settle(rid, "404")
        self.assertEqual(policy.streak_404, grab.NOT_READY_404_LIMIT)
        self.assertEqual(policy.phase, policy.PHASE_ADAPTIVE)
        self.assertEqual(policy.effective_deadline,
                         self.START + grab.ADAPTIVE_WINDOW_S)
        self.assertEqual(policy.backoff_s,
                         min(grab.BACKOFF_MAX_S,
                             grab.FULL_SPEED_INTERVAL_S * grab.BACKOFF_FACTOR))
        gap1 = policy.interval_s()
        policy.next_interval_after_wait()
        gap2 = policy.interval_s()
        self.assertAlmostEqual(gap2, min(grab.BACKOFF_MAX_S, gap1 * grab.BACKOFF_FACTOR),
                               places=6)

    def test_404_extension_never_passes_sign_end(self):
        sign_end = self.START + 300
        # 窗口比 sign_end 短：扩窗只能到 sign_end 为止，不能拉到 25 分钟
        policy = self._policy(sign_end_ts=sign_end)
        for _ in range(grab.NOT_READY_404_LIMIT):
            rid = policy.register_request()
            policy.settle(rid, "404")
        self.assertEqual(policy.effective_deadline, sign_end)
        self.assertLess(policy.effective_deadline, self.START + grab.ADAPTIVE_WINDOW_S)
        # 窗口比 sign_end 长：硬截止被 sign_end 冻住，扩窗同样拉不过去
        policy2 = self._policy(base_deadline=self.START + 2000, sign_end_ts=sign_end)
        self.assertEqual(policy2.hard_deadline, sign_end)
        for _ in range(grab.NOT_READY_404_LIMIT):
            rid = policy2.register_request()
            policy2.settle(rid, "404")
        self.assertEqual(policy2.effective_deadline, sign_end)

    def test_non_404_response_resets_streak_and_back_to_full_speed(self):
        policy = self._policy()
        self.clock.wall = self.START + grab.GOLDEN_WINDOW_S + 1.0
        for _ in range(grab.NOT_READY_404_LIMIT):
            rid = policy.register_request()
            policy.settle(rid, "404")
        self.assertEqual(policy.phase, policy.PHASE_ADAPTIVE)
        rid = policy.register_request()
        policy.settle(rid, "real")
        self.assertEqual(policy.streak_404, 0)
        self.assertEqual(policy.phase, policy.PHASE_FULL_SPEED)
        self.assertEqual(policy.interval_s(), grab.FULL_SPEED_INTERVAL_S)

    def test_real_response_exits_golden_immediately(self):
        policy = self._policy()
        rid = policy.register_request()
        policy.settle(rid, "real")
        self.assertEqual(policy.phase, policy.PHASE_FULL_SPEED)

    def test_out_of_order_settlement_and_unique_request_ids(self):
        policy = self._policy()
        ids = [policy.register_request() for _ in range(3)]
        self.assertEqual(len(set(ids)), 3, "request_id 必须唯一")
        self.assertEqual(policy.pending_count(), 3)
        # 乱序结算：最后一笔先回来
        policy.settle(ids[2], "real")
        policy.settle(ids[0], "404")
        self.assertEqual(policy.settled_kind(ids[2]), "real")
        self.assertEqual(policy.settled_kind(ids[0]), "404")
        self.assertEqual(policy.pending_count(), 1)
        # 重复结算同一笔只认第一次
        self.assertIsNone(policy.settle(ids[2], "404"))
        self.assertEqual(policy.settled_kind(ids[2]), "real")
        self.assertEqual(policy.streak_404, 1)

    def test_lost_response_stays_pending(self):
        policy = self._policy()
        rid = policy.register_request()
        policy.settle(rid, "lost")
        self.assertEqual(policy.pending_count(), 1, "响应读不出必须保持未决")
        self.assertIsNone(policy.settled_kind(rid))
        self.assertEqual(policy.streak_404, 0)

    def test_can_attempt_respects_max_attempts_deadline_and_stop(self):
        policy = self._policy(max_attempts=2)
        self.assertTrue(policy.can_attempt())
        policy.register_request()
        policy.register_request()
        self.assertFalse(policy.can_attempt(), "达到最大提交次数后不再发起")
        self.clock.wall = self.START + 120
        policy2 = self._policy()
        self.assertFalse(policy2.can_attempt(), "过了有效截止不再发起")
        policy3 = self._policy()
        policy3.stop("user_stopped")
        self.assertFalse(policy3.can_attempt())
        self.assertTrue(policy3.stopped)
        self.assertEqual(policy3.stop_reason, "user_stopped")


class SubmitLoopTests(unittest.TestCase):
    """run_submit_loop 集成：替身页面上跑完整提交阶段。"""

    START = 1000.0

    def setUp(self):
        self.clock = FakeClock(wall=self.START, mono=500.0)
        self.notify_calls = []

    def _notify(self, title, lines, template="green"):
        self.notify_calls.append({"title": title, "lines": list(lines),
                                  "template": template})

    def _run(self, outcomes, window_s=120, sign_end_ts=None, at_ts=None,
             button_visible=True, is_joined_fn=None, default_outcome=NO_REQUEST,
             max_attempts=0):
        page = FakePage(self.clock, outcomes, button_visible=button_visible)
        page.default_outcome = default_outcome
        logs = []

        def log_fn(msg):
            logs.append(str(msg))

        stop_reason, state = grab.run_submit_loop(
            page, activity_id="act-test",
            at_ts=self.START if at_ts is None else at_ts,
            window_s=window_s, sign_end_ts=sign_end_ts,
            max_attempts=max_attempts,
            is_joined_fn=is_joined_fn, notify_fn=self._notify,
            clock=self.clock, log_fn=log_fn, name="测试活动")
        return page, logs, stop_reason, state

    def test_golden_storm_ten_rounds_then_full_speed(self):
        # 响应延迟 2 秒到达：黄金风暴全程不被真实应答打断
        outcomes = [out_lost(delay_ms=2000) for _ in range(11)]
        page, logs, stop_reason, state = self._run(outcomes, window_s=3)
        self.assertGreaterEqual(state["attempts"], 10)
        golden_deltas = [page.click_times[i + 1] - page.click_times[i]
                         for i in range(9)]
        for delta in golden_deltas:
            self.assertGreaterEqual(delta, grab.GOLDEN_INTERVAL_S - 0.001,
                                    "黄金阶段每轮间隔不短于 90ms")
            self.assertLessEqual(delta, grab.GOLDEN_INTERVAL_S + 0.02)
        # 第 10 轮之后退出黄金，间隔升到 180ms
        self.assertGreaterEqual(page.click_times[10] - page.click_times[9],
                                grab.FULL_SPEED_INTERVAL_S - 0.001)
        submit_lines = [line for line in logs if "kind=submit" in line]
        self.assertEqual(len(submit_lines), state["attempts"])
        self.assertTrue(any("phase=golden" in line for line in submit_lines))
        self.assertTrue(any("phase=full_speed" in line for line in submit_lines))

    def test_404_streak_backoff_and_25min_window_extension(self):
        # 3 笔 404 与 1 笔成功都延迟 2 秒到达：黄金阶段不被真实应答打断，
        # 404×3 结算时先扩窗到起点起 25 分钟，随后成功立即收口
        outcomes = [out_404(delay_ms=2000) for _ in range(3)]
        outcomes.append(out_real({"code": 1, "message": "成功"}, delay_ms=2000))
        page, logs, stop_reason, state = self._run(outcomes, window_s=3)
        self.assertEqual(stop_reason, "success")
        self.assertEqual(state["retry_deadline"],
                         self.START + grab.ADAPTIVE_WINDOW_S,
                         "404×3 后有效截止扩到起点起 25 分钟")
        self.assertTrue(any("404_streak=3" in line for line in logs))
        self.assertTrue(any("phase=adaptive_backoff" in line for line in logs))

    def test_sign_end_hard_deadline_not_crossed_by_extension(self):
        sign_end = self.START + 5
        page, logs, stop_reason, state = self._run(
            [], window_s=120, sign_end_ts=sign_end,
            default_outcome=out_404())
        self.assertEqual(state["retry_deadline"], sign_end,
                         "自适应扩窗绝不越过 sign_end")
        self.assertLessEqual(self.clock.wall, sign_end + 0.5)
        self.assertEqual(stop_reason, "sign_end")

    def test_success_response_closes_immediately(self):
        outcomes = [out_real({"code": 0, "message": "报名人数已满"}),
                    out_real({"code": 1, "message": "成功"})]
        page, logs, stop_reason, state = self._run(outcomes)
        self.assertEqual(stop_reason, "success")
        self.assertEqual(state["attempts"], 2)
        self.assertEqual(self.notify_calls[-1]["title"], "🎟 报名成功")
        self.assertIn("**测试活动**", self.notify_calls[-1]["lines"][0])

    def test_login_invalid_closes_without_further_attempts(self):
        outcomes = [out_real({"code": 4001, "message": "登录失效"}),
                    out_real({"code": 1, "message": "成功"})]
        page, logs, stop_reason, state = self._run(outcomes)
        self.assertEqual(stop_reason, "login_invalid")
        self.assertEqual(state["attempts"], 1, "登录失效后不得再发起")
        self.assertEqual(self.notify_calls[-1]["template"], "red")

    def test_unconfirmed_pending_reported_not_faked_failure(self):
        outcomes = [out_lost(), out_lost()]
        page, logs, stop_reason, state = self._run(outcomes, window_s=0.3)
        self.assertEqual(stop_reason, "unconfirmed")
        self.assertEqual(state["pending"], 2, "两笔未决如实保留")
        self.assertEqual(state["attempts"], 2)
        body = "\n".join(self.notify_calls[-1]["lines"])
        self.assertIn("结果未确认，请到活动页核对", body)
        self.assertNotIn("未成功", body, "未决不得伪造成失败")

    def test_out_of_order_responses_settled_by_request_id(self):
        # 第 1 笔 300ms 后到，第 2 笔 50ms 后到：结算顺序与发出顺序相反
        outcomes = [out_real({"code": 0, "message": "先发后到"}, delay_ms=300),
                    out_real({"code": 0, "message": "后发先到"}, delay_ms=50)]
        page, logs, stop_reason, state = self._run(outcomes, window_s=1.5)
        settle_lines = [line for line in logs if "kind=settle" in line]
        self.assertEqual(len(settle_lines), 2)
        self.assertIn("request_id=req-2", settle_lines[0])
        self.assertIn("request_id=req-1", settle_lines[1])
        self.assertEqual(state["attempts"], 2)
        self.assertEqual(state["pending"], 0)

    def test_unique_request_ids_and_accounting_fields_in_logs(self):
        outcomes = [out_lost() for _ in range(3)]
        page, logs, stop_reason, state = self._run(outcomes, window_s=0.3)
        submit_lines = [line for line in logs if "kind=submit" in line]
        req_ids = [line.split("request_id=")[1].split()[0] for line in submit_lines]
        self.assertEqual(req_ids, ["req-1", "req-2", "req-3"])
        done_lines = [line for line in logs if "kind=done" in line]
        self.assertEqual(len(done_lines), 1, "终态日志恰一条")
        done = done_lines[0]
        for field in ("stop_reason=", "phase=", "round=", "404_streak=",
                      "backoff_s=", "attempts=", "pending=", "retry_deadline="):
            self.assertIn(field, done, "终态日志缺少 %s" % field)

    def test_user_stopped_closes_with_pending_kept(self):
        outcomes = [out_lost(), KeyboardInterrupt()]
        page = FakePage(self.clock, outcomes)
        logs = []

        def log_fn(msg):
            logs.append(str(msg))

        stop_reason, state = grab.run_submit_loop(
            page, activity_id="act-test", at_ts=self.START, window_s=120,
            is_joined_fn=None, notify_fn=self._notify,
            clock=self.clock, log_fn=log_fn, name="测试活动")
        self.assertEqual(stop_reason, "user_stopped")
        self.assertEqual(state["attempts"], 1, "停止前已发出的请求如实保留")
        self.assertEqual(state["pending"], 1)
        self.assertIn("stop_reason=user_stopped",
                      [line for line in logs if "kind=done" in line][0])

    def test_closeout_query_can_confirm_success(self):
        outcomes = [out_404()]
        page, logs, stop_reason, state = self._run(
            outcomes, window_s=0.2, is_joined_fn=lambda act_id: True)
        self.assertEqual(stop_reason, "success", "收尾查询确认已报名也算成功")

    def test_listeners_removed_after_run(self):
        outcomes = [out_real({"code": 1})]
        page, logs, stop_reason, state = self._run(outcomes)
        self.assertEqual(page.listeners.get("request", []), [],
                         "停止后不得残留 request 监听")
        self.assertEqual(page.listeners.get("response", []), [],
                         "停止后不得残留 response 监听")


class NotifyOwnerBoundaryTests(unittest.TestCase):
    """owner 私聊通知边界：只发 p2p chat_id / PRIVATE_OWNER_OPEN_ID。"""

    def setUp(self):
        self._old_env = os.environ.get("PRIVATE_OWNER_OPEN_ID")
        self._old_module = sys.modules.get("notify")
        self.tmp = tempfile.mkdtemp(prefix="grab-notify-", dir=_test_root())
        os.environ["GRAB_LOG_FILE"] = str(Path(self.tmp) / "grab.log")
        self.sent = []

        fake = types.ModuleType("notify")
        fake.load_config = lambda: {"notify": {"feishu_app": {"id": "cli_x", "secret": "s"}}}
        fake._tenant_token = lambda app: "t-token-fixture"
        fake._p2p_chat_id = lambda cfg: self.p2p_chat_id
        fake._business_ok = lambda body, platform="feishu": True

        def _post(url, payload, token=""):
            self.sent.append({"url": url, "payload": payload, "token": token})
            return True, {"code": 0}

        fake._post = _post
        fake.send_private = lambda text: True
        sys.modules["notify"] = fake

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("PRIVATE_OWNER_OPEN_ID", None)
        else:
            os.environ["PRIVATE_OWNER_OPEN_ID"] = self._old_env
        if self._old_module is None:
            sys.modules.pop("notify", None)
        else:
            sys.modules["notify"] = self._old_module

    def test_p2p_chat_id_preferred_over_open_id(self):
        self.p2p_chat_id = "oc_owner_p2p"
        os.environ["PRIVATE_OWNER_OPEN_ID"] = "ou_owner_fixture"
        self.assertTrue(grab.notify("标题", ["正文"], "green"))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("receive_id_type=chat_id", self.sent[0]["url"])
        self.assertEqual(self.sent[0]["payload"]["receive_id"], "oc_owner_p2p")

    def test_open_id_fallback_when_no_p2p_record(self):
        self.p2p_chat_id = ""
        os.environ["PRIVATE_OWNER_OPEN_ID"] = "ou_owner_fixture"
        self.assertTrue(grab.notify("标题", ["正文"], "green"))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("receive_id_type=open_id", self.sent[0]["url"])
        self.assertEqual(self.sent[0]["payload"]["receive_id"], "ou_owner_fixture")

    def test_no_send_without_owner_channel(self):
        # 既无 p2p chat_id 又无 owner open_id：宁可不发，也绝不退化到群发
        self.p2p_chat_id = ""
        os.environ.pop("PRIVATE_OWNER_OPEN_ID", None)
        self.assertFalse(grab.notify("标题", ["正文"], "green"))
        self.assertEqual(self.sent, [], "没有任何收件人时不得发送")


def _test_root() -> Path:
    root = Path(os.environ.get("TJISE_TEST_ROOT")
                or "D:/@Temp/tjise-grab-signup-retry-20261028")
    if os.name != "nt" and not os.environ.get("TJISE_TEST_ROOT"):
        root = Path(tempfile.gettempdir()) / "tjise-grab-signup-retry-20261028"
    root.mkdir(parents=True, exist_ok=True)
    return root


if __name__ == "__main__":
    unittest.main()
