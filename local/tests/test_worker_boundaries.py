#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生产 worker 的边界行为回归：直接调用 cmd_grab，只替换输入边界。

与上一轮的区别（对应复审 C01–C03）：

* 合法报名请求按仓库契约建模为 ``GET signUp/<opaque-hash>``，hash 不含明文活动 ID；
  替身默认第一次「立即报名」只打开确认框，「确定」才真正发请求。
* 请求按 Request 身份去重与计数：同一个请求重复通知只算 1，两个不同请求算 2。
* 响应必须对得上本轮承认的请求，别家响应不得改本场结论。
* 预算从进程入口就生效，鉴权/校时/查目标都要受限；确认前再拦一次。
* 已发出但没有确定结果的请求会一直算未决，下一次本地失败不能把它清掉。

@author LiXinwen
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

LOCAL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LOCAL))

import grab  # noqa: E402
import time_contract  # noqa: E402

START = 1792000000.0
OPEN_AHEAD = 20.0
OPAQUE = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"   # 学校前端算出的 hash，不含活动 ID


class FakeTimeout(Exception):
    """替身 Playwright 的超时类型；grab 按类名捕获它。"""


class FakeClock:
    def __init__(self, wall=START, mono=1000.0):
        self.wall = float(wall)
        self.mono = float(mono)
        self.lost = False

    def lose(self):
        """模拟校时失效：业务读数一律抛 ClockUnavailable，墙钟读数照旧。"""
        self.lost = True

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def sleep(self, seconds):
        step = max(0.0, float(seconds))
        self.wall += step
        self.mono += step

    def jump_wall(self, delta):
        self.wall += float(delta)

    # ---- 业务时钟视图（决策 012）：cmd_grab 需要注入独立北京时间 ----
    def now(self):
        self._require()
        return self.wall

    def bounds(self):
        self._require()
        return self.wall, self.wall

    def _require(self):
        if self.lost:
            from business_clock import ClockUnavailable
            raise ClockUnavailable("测试注入：北京时间暂不可用")

    def elapsed_s(self):
        return self.mono

    def state(self):
        return "ready"

    def reason(self):
        return "测试注入"

    def request_resync(self):
        pass

    def snapshot(self):
        return {"schema": 1, "owner_id": "fake-worker", "generation": 1,
                "state": "ready", "epoch_s": self.wall, "tick_s": self.mono,
                "sampled_tick_s": self.mono, "uncertainty_s": 0.0,
                "source": "test", "age_s": 0.0, "reason": "测试注入"}

    def sync(self, timeout_s=None):
        return "ready"

    def close(self):
        pass


class FakeRequest:
    def __init__(self, url, method="GET", post_data=""):
        self.url = url
        self.method = method
        self.post_data = post_data


class FakeResponse:
    def __init__(self, request, payload):
        self.request = request
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeInfo:
    def __init__(self, page):
        self._page = page

    @property
    def value(self):
        if self._page.last_response is None:
            raise FakeTimeout("没有响应")
        return self._page.last_response


class FakeExpect:
    """expect_response 替身：确认点击产生的响应在块内排队。"""

    def __init__(self, page, timeout):
        self.page = page
        self.timeout = timeout

    def __enter__(self):
        self.page.last_response = None
        return FakeInfo(self.page)

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            return False
        if self.page.last_response is None:
            raise FakeTimeout("等 signUp 响应超时")
        return False


class FakeLocator:
    def __init__(self, page, name):
        self.page = page
        self.name = name

    def filter(self, has_text=None):
        return self

    @property
    def first(self):
        return self

    def count(self):
        if self.name == "uni-button":
            return 1 if self.page.button_visible else 0
        return 1

    def click(self, timeout=None):
        self.page.clicks.append((self.page.clock.time(), self.name, timeout))
        behavior = self.page.behavior
        if self.name == "uni-button":
            if behavior == "click_fail":
                raise FakeTimeout("点击超时")
            if behavior == "slow_dialog":
                # 打开确认框本身就花掉整个窗口：确认必须被拦下
                self.page.clock.sleep(self.page.click_delay)
            if behavior == "lose_before_confirm":
                # 打开确认框后校时失效：确认前检查必须拒绝，不产生提交
                self.page.clock.lose()
            return                      # 默认只开确认框，不发请求
        if self.name == "确定":
            self.page.confirm_clicks += 1
            if behavior == "confirm_fail":
                raise FakeTimeout("确认框点击失败")
            self.page.fire_from_confirm()
            return


class FakePage:
    def __init__(self, clock, behavior, button_visible=True, click_delay=0.0,
                 foreign=False):
        self.clock = clock
        self.behavior = behavior
        self.button_visible = button_visible
        self.click_delay = click_delay
        self.foreign = foreign
        self.request_handlers = []
        self.requests = []
        self.clicks = []
        self.confirm_clicks = 0
        self.last_response = None

    def on(self, event, handler):
        if event == "request":
            self.request_handlers.append(handler)

    def goto(self, url, wait_until=None, timeout=None):
        if self.behavior == "goto_fail":
            raise FakeTimeout("goto 超时")
        self.url = url
        if self.foreign:
            # 页面自己发的 signUp：不在受控窗口内，不得计入本场
            self._notify(FakeRequest(
                "https://xg.tjise.edu.cn/dr/api/rjyHdCyr/signUp/other-hash"))

    def reload(self, wait_until=None, timeout=None):
        if self.behavior == "reload_fail":
            raise FakeTimeout("reload 超时")

    def wait_for_timeout(self, ms):
        self.clock.sleep(float(ms) / 1000.0)

    def eval_on_selector_all(self, selector, script):
        return ["立即报名"]

    def locator(self, selector):
        return FakeLocator(self, "uni-button" if "uni-button" in selector else selector)

    def get_by_text(self, text, exact=False):
        return FakeLocator(self, "确定" if text == "确定" else text)

    def expect_response(self, predicate, timeout=None):
        return FakeExpect(self, timeout)

    def _notify(self, request):
        self.requests.append((self.clock.time(), request.url))
        for handler in self.request_handlers:
            handler(request)

    def fire_from_confirm(self):
        behavior = self.behavior
        payload = {"code": 0, "message": "报名人数已满"}
        if behavior == "success":
            payload = {"code": 1, "message": "ok"}
        if behavior == "foreign_only":
            # 我方动作没有发出请求；别家请求在更早的 goto 阶段就发过了
            self.last_response = None
            return
        if behavior == "lost":
            request = FakeRequest("https://xg.tjise.edu.cn/dr/api/rjyHdCyr/signUp/" + OPAQUE)
            self._notify(request)
            self.last_response = None
            return
        if behavior == "lose_after_request":
            # 提交已发出、校时随即失效：未决事实必须保留，不能判成名额已满
            request = FakeRequest("https://xg.tjise.edu.cn/dr/api/rjyHdCyr/signUp/" + OPAQUE)
            self._notify(request)
            self.clock.lose()
            self.last_response = None
            return
        if behavior == "response_stuck":
            request = FakeRequest("https://xg.tjise.edu.cn/dr/api/rjyHdCyr/signUp/" + OPAQUE)
            self._notify(request)
            self.last_response = FakeResponse(request, FakeTimeout("读响应卡住"))
            return
        if behavior == "dup_notify":
            request = FakeRequest("https://xg.tjise.edu.cn/dr/api/rjyHdCyr/signUp/" + OPAQUE)
            self._notify(request)
            self._notify(request)          # 同一个 Request 重复通知
            self.last_response = FakeResponse(request, {"code": 1, "message": "ok"})
            return
        count = 2 if behavior == "double" else 1
        for index in range(count):
            suffix = OPAQUE if count == 1 else "%s-%d" % (OPAQUE, index)
            request = FakeRequest("https://xg.tjise.edu.cn/dr/api/rjyHdCyr/signUp/" + suffix)
            if count == 2:
                # 两个真正不同的请求都要被承认（C01）
                request.post_data = ""
            self._notify(request)
            self.last_response = FakeResponse(request, dict(payload))


class FakeBrowser:
    def __init__(self, page):
        self.page = page
        self.closed = False

    def new_context(self, **kwargs):
        page = self.page

        class Ctx:
            def new_page(inner):
                return page

            def close(inner):
                return None

        return Ctx()

    def close(self):
        self.closed = True


class _PassPw:
    def __enter__(self):
        return object()

    def __exit__(self, exc_type, exc, tb):
        return False


def install_fake_playwright():
    pkg = types.ModuleType("playwright")
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = _PassPw
    sync_api.TimeoutError = FakeTimeout
    pkg.sync_api = sync_api
    old = {name: sys.modules.get(name) for name in ("playwright", "playwright.sync_api")}
    sys.modules["playwright"] = pkg
    sys.modules["playwright.sync_api"] = sync_api

    def restore():
        for name, module in old.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    return restore


class Result:
    def __init__(self, code, logs, page, clock, elapsed_s, browser_created, posts):
        self.code = code
        self.logs = logs
        self.page = page
        self.clock = clock
        self.elapsed_s = elapsed_s
        self.browser_created = browser_created
        self.posts = posts

    @property
    def attempts(self):
        return max([int(line.split("attempts=")[1].split(" ")[0])
                    for line in self.logs if "kind=done" in line] or [0])

    @property
    def terminal_lines(self):
        return [line for line in self.logs if "[retry] " in line and "stop_reason=" in line]

    @property
    def stop_reason(self):
        if not self.terminal_lines:
            return None
        return self.terminal_lines[-1].split("stop_reason=")[1].split(" ")[0]

    @property
    def window_line(self):
        return next((line for line in self.logs if "kind=window" in line), None)

    @property
    def requests(self):
        return self.page.requests if self.page else []


def run_worker(*, behavior="success", at_offset=0.0, retry_for=120.0, max_attempts=0,
               sign_end="", retry_start=None, probe_delay=0.0,
               probe_state="valid", probe_detail="ok",
               post_delays=None, post_hook=None, button_visible=True,
               click_delay=0.0, foreign=False, launch_fails=False):
    clock = FakeClock()
    page = FakePage(clock, behavior, button_visible, click_delay, foreign)
    logs, posts = [], []
    created = {"browser": False}
    delays = list(post_delays or [])

    def fake_log(msg):
        logs.append(str(msg))

    def fake_probe(budget=None):
        # 假网络必须像真网络一样尊重预算：否则「整流程受限」根本没被检验
        wait = probe_delay
        if budget is not None:
            wait = min(wait, max(0.0, budget.remaining_s()))
        clock.sleep(wait)
        if budget is not None and budget.expired() and wait < probe_delay:
            raise time_contract.BudgetExhausted("鉴权超时")
        return probe_state, probe_detail

    def fake_post(path, payload=None, tok=None, timeout=None, budget=None):
        posts.append({"path": path, "timeout": timeout, "at": clock.time()})
        delay = delays.pop(0) if delays else 0.0
        effective = delay if timeout is None else min(delay, max(0.0, float(timeout)))
        clock.sleep(effective)
        if post_hook:
            post_hook(len(posts), clock)
        if effective < delay:
            return {"code": -1, "message": "TimeoutError"}
        if "/getJoinInfo/" in path:
            return {"code": 1, "data": None}
        return {"code": 1, "data": {}}

    def fake_launch(pw, headless):
        created["browser"] = True
        if launch_fails:
            raise RuntimeError("browser launch failed")
        return FakeBrowser(page)

    at_ts = START + at_offset
    args = SimpleNamespace(
        dry_run=False, headed=False, lead=100.0, window=120.0,
        retry_for=retry_for, max_attempts=max_attempts, sign_end=sign_end,
        retry_start=START if retry_start is None else retry_start,
        activity_id="act-1", at=grab.school_time_text(at_ts), name="边界实验活动",
    )

    patches = {"log": fake_log, "probe_token": fake_probe,
               "setup_business_clock": lambda *a, **k: (grab.configure_business_clock(clock), clock)[1],
               # 兼容入场实现（旧 cmd_grab 调 check_token）；新实现不读它
               "check_token": lambda budget=None: (probe_state == "valid", probe_detail),
               "clock_sample": lambda *a, **k: {"source": "http_date", "state": "unknown",
                                                "checked_at": START, "offset_s": None,
                                                "uncertainty_s": None, "rtt_s": None},
               "resolve_target": lambda a, budget=None: {"id": "act-1", "at": args.at,
                                                         "original_at": args.at,
                                                         "name": args.name},
               "_post": fake_post, "launch_browser": fake_launch,
               "notify": lambda *a, **k: False}
    # 入场实现没有 probe_token：用 getattr 兜底，红例装置才能在旧代码上跑起来
    originals = {name: getattr(grab, name, None) for name in patches}
    original_times = (grab.time, time_contract.time)
    restore_pw = install_fake_playwright()
    start_mono = clock.mono
    grab.time = clock
    time_contract.time = clock
    try:
        for name, value in patches.items():
            setattr(grab, name, value)
        with contextlib.redirect_stdout(io.StringIO()):
            code = grab.cmd_grab(args)
    finally:
        for name, value in originals.items():
            if value is None:
                try:
                    delattr(grab, name)
                except AttributeError:
                    pass
            else:
                setattr(grab, name, value)
        grab.time, time_contract.time = original_times
        restore_pw()
    return Result(code, logs, page if created["browser"] else None, clock,
                  clock.mono - start_mono, created["browser"], posts)


class RequestCorrelationTests(unittest.TestCase):
    """C01：opaque hash 也要认领；请求按身份计数，响应不串场。"""

    def test_opaque_get_request_is_counted(self):
        res = run_worker(behavior="success")
        self.assertEqual(len(res.requests), 1)
        self.assertIn("/signUp/" + OPAQUE, res.requests[0][1])
        self.assertEqual(res.attempts, 1, "GET signUp/<opaque> 无正文也必须计数")
        self.assertEqual(res.stop_reason, "success")
        self.assertEqual(res.code, 0)

    def test_same_request_notified_twice_counts_once(self):
        res = run_worker(behavior="dup_notify")
        self.assertEqual(len(res.requests), 2, "装置确实通知了两次")
        self.assertEqual(res.attempts, 1, "同一 Request 重复通知只计 1")

    def test_two_distinct_requests_are_not_merged(self):
        res = run_worker(behavior="double", max_attempts=5)
        self.assertEqual(len(res.requests), 2, "页面真的发了两次就要记两次")
        self.assertEqual(res.attempts, 2)
        self.assertEqual(res.stop_reason, "unconfirmed",
                         "两笔里只拿到一个确定结果，另一笔仍未决")
        self.assertEqual(res.code, 2)

    def test_foreign_request_does_not_change_conclusion(self):
        res = run_worker(behavior="foreign_only", foreign=True, retry_for=3.0,
                         max_attempts=1)
        self.assertEqual(len(res.requests), 1, "装置确实发过别家请求")
        self.assertEqual(res.attempts, 0, "不属于受控提交的请求不得计数")
        self.assertNotEqual(res.stop_reason, "success")
        self.assertEqual(res.code, 2)


class AdmissionTests(unittest.TestCase):
    """C01/R01/R02：只有真正发出的请求才计次，开放前不点。"""

    def test_click_failure_keeps_zero(self):
        res = run_worker(behavior="click_fail", retry_for=6.0, max_attempts=3)
        self.assertEqual(res.requests, [])
        self.assertEqual(res.attempts, 0)
        self.assertEqual(res.stop_reason, "retry_duration")
        self.assertEqual(len(res.terminal_lines), 1)

    def test_confirm_failure_keeps_zero(self):
        res = run_worker(behavior="confirm_fail", retry_for=4.0, max_attempts=2)
        self.assertEqual(res.requests, [])
        self.assertEqual(res.attempts, 0)
        self.assertGreaterEqual(res.page.confirm_clicks, 1)

    def test_preopen_button_does_not_submit(self):
        res = run_worker(behavior="full", at_offset=OPEN_AHEAD, retry_for=60.0,
                         max_attempts=3)
        self.assertTrue(res.requests)
        for at, _ in res.requests:
            self.assertGreaterEqual(at, START + OPEN_AHEAD - 1e-6)
        self.assertEqual(res.attempts, len(res.requests))
        self.assertEqual(res.stop_reason, "max_attempts")

    def test_full_until_max_counts_exactly(self):
        res = run_worker(behavior="full", retry_for=120.0, max_attempts=2)
        self.assertEqual(len(res.requests), 2)
        self.assertEqual(res.attempts, 2)
        self.assertEqual(res.stop_reason, "max_attempts")


class BudgetEntireFlowTests(unittest.TestCase):
    """C02：前置阶段也受限，确认前再拦，异常恰一条终态。"""

    def test_preflight_auth_cannot_exceed_short_window(self):
        res = run_worker(behavior="success", probe_delay=7.0, retry_for=2.0)
        self.assertLessEqual(res.elapsed_s, 2.1, "鉴权把 2 秒窗口拖成了 7 秒")
        self.assertFalse(res.browser_created, "窗口已耗尽不得再开浏览器")
        self.assertEqual(res.requests, [])
        self.assertEqual(len(res.terminal_lines), 1)

    def test_expired_window_touches_no_network(self):
        res = run_worker(behavior="success", retry_for=2.0, retry_start=START - 10.0)
        self.assertEqual(res.posts, [], "窗口入场已过就不该再发任何网络请求")
        self.assertFalse(res.browser_created)
        self.assertEqual(res.stop_reason, "retry_duration")

    def test_confirm_blocked_when_dialog_consumes_budget(self):
        res = run_worker(behavior="slow_dialog", retry_for=2.0, click_delay=2.0,
                         max_attempts=3)
        self.assertEqual(res.requests, [], "预算在确认前耗尽，不得再点确定")
        self.assertEqual(res.page.confirm_clicks, 0, "确认按钮必须没被点过")

    def test_goto_failure_has_single_terminal(self):
        res = run_worker(behavior="goto_fail")
        self.assertEqual(len(res.terminal_lines), 1)
        self.assertEqual(res.stop_reason, "process_error")
        self.assertEqual(res.code, 2)

    def test_reload_failure_has_single_terminal(self):
        res = run_worker(behavior="reload_fail", button_visible=False, retry_for=3.0)
        self.assertEqual(len(res.terminal_lines), 1)

    def test_launch_failure_has_single_terminal(self):
        res = run_worker(behavior="success", launch_fails=True)
        self.assertEqual(len(res.terminal_lines), 1)
        self.assertEqual(res.stop_reason, "process_error")

    def test_response_read_failure_is_unconfirmed(self):
        res = run_worker(behavior="response_stuck", retry_for=4.0, max_attempts=3)
        self.assertEqual(res.attempts, 1, "请求已发出就要计数")
        self.assertEqual(res.stop_reason, "unconfirmed")
        self.assertEqual(len(res.terminal_lines), 1)


class UnknownOutcomeTests(unittest.TestCase):
    """C03：未决事实不会被下一次本地失败抹掉。"""

    def test_lost_response_stays_unconfirmed(self):
        res = run_worker(behavior="lost", retry_for=4.0, max_attempts=3)
        self.assertEqual(res.attempts, 1, "不能自动重发未决请求")
        self.assertEqual(res.stop_reason, "unconfirmed")
        self.assertEqual(res.code, 2)

    def test_lost_then_local_failure_keeps_unconfirmed(self):
        res = run_worker(behavior="lost", retry_for=4.0, max_attempts=3,
                         button_visible=True)
        # 丢响应后循环直接停手（pending>0），不会再点一次
        self.assertEqual(res.page.confirm_clicks, 1)
        self.assertEqual(res.stop_reason, "unconfirmed")

    def test_auth_unknown_is_not_login_invalid(self):
        res = run_worker(probe_state="unknown", probe_detail="接口异常：URLError")
        self.assertEqual(res.stop_reason, "auth_unavailable")
        self.assertEqual(res.code, 3)
        self.assertNotIn("登录失效", "\n".join(res.logs))

    def test_auth_expired_is_login_invalid(self):
        res = run_worker(probe_state="expired", probe_detail="登录态已过期（4001）")
        self.assertEqual(res.stop_reason, "login_invalid")
        self.assertEqual(res.code, 3)


class WallClockTests(unittest.TestCase):
    def test_backward_jump_does_not_extend(self):
        def hook(index, clock):
            if index == 1:
                clock.jump_wall(-10.0)

        res = run_worker(behavior="full", retry_for=2.0, max_attempts=0,
                         post_delays=[0.2, 5.0, 5.0], post_hook=hook)
        self.assertLessEqual(res.elapsed_s, 2.1)


def school_row(aid, name, sign_start, sign_end, *, signed=False, place="A101"):
    """学校 /rjyHd/listActivity 的原始行形状（字段名是拼音缩写）。"""
    return {
        "id": aid, "hdmc": name, "hdlx": "讲座", "hddd": place,
        "bmkssj": sign_start, "bmjssj": sign_end,
        "hdkssj": "2099-01-03 09:00:00", "hdjssj": "2099-01-03 11:00:00",
        "bmzt": 1 if signed else 0,
    }


class ListEntryTests(unittest.TestCase):
    """F01：真实 cmd_list 成功路径与 resolve_target 三条路径的预算回归。

    只替换网络边界（_post / check_token），绝不 patch 掉 cmd_list、resolve_target
    或伪造它们自己的返回值——否则测的就不是生产入口。
    """

    def _run_list(self, replies, *, token=(True, "ok"), all_rows=False):
        clock = FakeClock()
        seen = []
        queue = list(replies) if isinstance(replies, list) else [replies]

        def fake_post(path, payload=None, tok=None, timeout=None, budget=None):
            seen.append({"path": path, "budget": budget, "timeout": timeout})
            item = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(item, Exception):
                raise item
            return item

        args = SimpleNamespace(all=all_rows, json=True)
        out = io.StringIO()
        patches = {"_post": fake_post, "check_token": lambda budget=None: token}
        originals = {name: getattr(grab, name, None) for name in patches}
        original_time = grab.time
        original_clock = dict(grab._CLOCK)
        original_now = time_contract._BUSINESS_NOW
        grab.time = clock
        # 列表筛选按业务北京时间：测试显式注入可控时钟，不用系统墙钟
        grab.configure_business_clock(clock)
        try:
            for name, value in patches.items():
                setattr(grab, name, value)
            with contextlib.redirect_stdout(out):
                code = grab.cmd_list(args)
        finally:
            for name, value in originals.items():
                if value is None:
                    try:
                        delattr(grab, name)
                    except AttributeError:
                        pass
                else:
                    setattr(grab, name, value)
            grab.time = original_time
            grab._CLOCK.update(original_clock)
            time_contract.configure_time_source(original_now)
        return code, out.getvalue(), seen

    def test_cmd_list_returns_sorted_json_for_real_rows(self):
        now = START
        rows = [
            school_row("signed", "已报名活动", grab.school_time_text(now - 7200),
                       grab.school_time_text(now + 7200), signed=True),
            school_row("open", "开放中活动", grab.school_time_text(now - 3600),
                       grab.school_time_text(now + 7200)),
            school_row("future", "未开放活动", grab.school_time_text(now + 3600),
                       grab.school_time_text(now + 7200)),
        ]
        code, out, seen = self._run_list({"code": 1, "data": {"count": 3, "list": rows}})
        self.assertEqual(code, 0, "真实 cmd_list 成功路径不得报错：%s" % out)
        data = json.loads(out.strip())
        self.assertEqual([row["id"] for row in data], ["future", "open", "signed"],
                         "未开放排最前，其次开放中，已报名垫底")
        first = data[0]
        for key in ("index", "id", "name", "sign_start", "sign_end", "act_start",
                    "act_end", "place", "signed", "checkin_required",
                    "checkout_required", "state"):
            self.assertIn(key, first, "JSON 契约字段缺失：%s" % key)
        self.assertEqual(first["index"], 1)
        self.assertEqual(data[-1]["state"], "已报名")
        self.assertEqual(len(seen), 1, "单页数据应只请求一次列表")

    def test_cmd_list_zero_rows_is_empty_json(self):
        code, out, _ = self._run_list({"code": 1, "data": {"count": 0, "list": []}})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.strip()), [],
                         "学校确实零行时必须输出可解析的空数组")

    def test_cmd_list_permission_error_is_attributed_to_login(self):
        code, out, _ = self._run_list({"code": 4001, "message": "身份验证失败"})
        self.assertEqual(code, 2)
        self.assertIn("登录态已失效", out)
        self.assertNotIn("拉取活动失败", out)

    def test_cmd_list_network_error_is_not_reported_as_expired_login(self):
        # 真实 _post 把 URLError 收成 {code:-1, message:"URLError"}，这里按同一形状喂入
        code, out, _ = self._run_list({"code": -1, "message": "URLError"})
        self.assertEqual(code, 2)
        self.assertIn("拉取活动失败", out)
        self.assertIn("URLError", out)
        self.assertNotIn("登录态已失效", out, "网络错误不得归因成登录过期")

    def _run_resolve(self, **kwargs):
        clock = FakeClock()
        budgets = []
        rows = [school_row("a1", "目标活动", grab.school_time_text(START + 3600),
                           grab.school_time_text(START + 7200))]

        def fake_post(path, payload=None, tok=None, timeout=None, budget=None):
            budgets.append(budget)
            return {"code": 1, "data": {"count": 1, "list": rows}}

        args = SimpleNamespace(activity_id=kwargs.get("activity_id", ""),
                               at=kwargs.get("at", ""),
                               name=kwargs.get("name", ""),
                               pick=kwargs.get("pick", 0))
        budget = time_contract.RetryBudget(START + 600, wall_now=START,
                                           monotonic_now=1000.0)
        patches = {"_post": fake_post}
        originals = {name: getattr(grab, name, None) for name in patches}
        original_time = grab.time
        grab.time = clock
        out = io.StringIO()
        try:
            for name, value in patches.items():
                setattr(grab, name, value)
            with contextlib.redirect_stdout(out):
                target = grab.resolve_target(args, budget=budget)
        finally:
            for name, value in originals.items():
                if value is None:
                    try:
                        delattr(grab, name)
                    except AttributeError:
                        pass
                else:
                    setattr(grab, name, value)
            grab.time = original_time
        return target, budgets, budget, out.getvalue()

    def test_resolve_target_three_paths_share_the_same_budget(self):
        cases = {
            "按ID无--at": {"activity_id": "a1"},
            "按名称": {"name": "目标活动"},
            "按序号": {"pick": 1},
        }
        for label, kwargs in cases.items():
            with self.subTest(path=label):
                target, budgets, budget, out = self._run_resolve(**kwargs)
                self.assertIsNotNone(target, "%s 没选出目标：%s" % (label, out))
                self.assertEqual(target["id"], "a1")
                self.assertTrue(budgets, "%s 没有经过网络边界" % label)
                self.assertTrue(all(item is budget for item in budgets),
                                "%s 丢了调用方传入的预算对象" % label)

    def test_exhausted_lookup_budget_never_opens_browser(self):
        clock = FakeClock()
        logs, posts = [], []
        created = {"browser": False}

        def fake_clock_sample(*a, **k):
            # 粗校时把 30 秒前置预算花光：真实网络慢起来完全可能
            clock.sleep(35.0)
            return {"state": "unknown"}

        def fake_post(path, payload=None, tok=None, timeout=None, budget=None):
            # 与生产 _post 同序：先按剩余预算算超时，已到点就拒绝发起请求
            if budget is not None:
                budget.timeout_s(15)
            posts.append(path)
            return {"code": 1, "data": {"count": 0, "list": []}}

        def fake_launch(pw, headless):
            created["browser"] = True
            raise AssertionError("预算已尽时不得打开浏览器")

        args = SimpleNamespace(dry_run=False, headed=False, lead=100.0, window=120.0,
                               retry_for=120.0, max_attempts=0, sign_end="",
                               retry_start=0, activity_id="", at="", pick=1, name="")
        patches = {"_post": fake_post,
                   "probe_token": lambda budget=None: ("valid", "ok"),
                   "clock_sample": fake_clock_sample,
                   "launch_browser": fake_launch,
                   "log": logs.append,
                   "notify": lambda *a, **k: False,
                   "setup_business_clock": lambda *a, **k: (grab.configure_business_clock(clock), clock)[1]}
        originals = {name: getattr(grab, name, None) for name in patches}
        original_times = (grab.time, time_contract.time)
        restore_pw = install_fake_playwright()
        grab.time = clock
        time_contract.time = clock
        try:
            for name, value in patches.items():
                setattr(grab, name, value)
            with contextlib.redirect_stdout(io.StringIO()):
                code = grab.cmd_grab(args)
        finally:
            for name, value in originals.items():
                if value is None:
                    try:
                        delattr(grab, name)
                    except AttributeError:
                        pass
                else:
                    setattr(grab, name, value)
            grab.time, time_contract.time = original_times
            restore_pw()
        self.assertFalse(created["browser"], "查目标耗尽预算后不得进入浏览器阶段")
        self.assertEqual(posts, [], "预算已尽时一次列表请求都不该发出去")
        self.assertEqual(code, 2)
        done = [line for line in logs if "kind=done" in line]
        self.assertEqual(len(done), 1, "终态必须唯一")
        self.assertIn("stop_reason=retry_duration", done[0])
        self.assertIn("attempts=0", done[0], "没有提交过就不能记次数")


class ClockLossMidFlowTests(unittest.TestCase):
    """C03/C04：校时在流程中途失效时的终态与事实保留。"""

    def test_clock_loss_before_confirm_keeps_zero_submit(self):
        res = run_worker(behavior="lose_before_confirm")
        self.assertEqual(res.stop_reason, "clock_unavailable")
        self.assertEqual(res.code, 3)
        self.assertEqual(res.attempts, 0, "未确认前失效：零提交，不能记一次")
        self.assertEqual(res.requests, [], "失效后不得发出 signUp 请求")
        self.assertEqual(len(res.terminal_lines), 1, "终态必须唯一")

    def test_clock_loss_with_pending_is_unconfirmed_not_missing(self):
        res = run_worker(behavior="lose_after_request")
        self.assertEqual(len(res.requests), 1, "装置确实发出了一次提交")
        self.assertEqual(res.attempts, 1, "已发出的提交次数必须保留")
        self.assertEqual(res.stop_reason, "unconfirmed",
                         "有未决请求时不能说成名额已满/报名失败")
        self.assertEqual(res.code, 2)
        self.assertIn("未确认", "\n".join(res.logs))


if __name__ == "__main__":
    unittest.main()
