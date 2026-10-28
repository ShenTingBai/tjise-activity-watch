#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立北京时间贯穿执行链的生产行为回归（决策 012）。

用共享 FakeBusinessClock 驱动生产函数，系统墙钟任意跳变时：
调度分类、接纳时刻、预算截止与时长上限、协调器准备点全部按业务时钟走。
OS 墙钟只作诊断，绝不参与业务关系。

@author LiXinwen
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL = os.path.dirname(HERE)
ROOT = os.path.dirname(LOCAL)
for p in (LOCAL, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from fixtures.business_clock import FakeBusinessClock  # noqa: E402
from time_contract import RetryBudget, BudgetExhausted, school_time_text  # noqa: E402
import grab  # noqa: E402
import signup_plan  # noqa: E402
import time_contract  # noqa: E402

START = 1793100000.0     # 假「北京时间」锚点（与真实系统日期无关）


class RetryBudgetBusinessTests(unittest.TestCase):
    """RetryBudget 业务模式：绝对截止按上界、时长按经过时长，二者取早。"""

    def _clock(self, epoch=START, tick=1000.0):
        clock = FakeBusinessClock(epoch=epoch, tick=tick)
        clock._elapsed0 = clock.elapsed_s()
        return clock

    def test_deadline_and_duration_both_bounded(self):
        clock = self._clock(epoch=START, tick=1000.0)
        # 截止 60 秒后：剩余按「业务上界」与「冻结的经过时长上限」取早；
        # 经过 61 秒后必须到点，即使业务重校把锚点向后纠正
        budget = RetryBudget(START + 60.0, business_clock=clock)
        self.assertLessEqual(budget.remaining_s(), 60.0)
        clock.advance(61.0)
        self.assertEqual(budget.remaining_s(), 0.0)
        with self.assertRaises(BudgetExhausted):
            budget.check()

    def test_absolute_deadline_uses_upper_bound(self):
        clock = self._clock(epoch=START, tick=1000.0)
        clock._uncertainty = 0.4
        budget = RetryBudget(START + 10.0, business_clock=clock)
        # 上界 = now + 0.4 → 剩余按上界收紧，不会高估
        self.assertLessEqual(budget.remaining_s(), 10.0 - 0.4 + 0.01)

    def test_resync_forward_does_not_extend_duration(self):
        clock = self._clock(epoch=START, tick=1000.0)
        budget = RetryBudget(START + 120.0, business_clock=clock)
        clock.advance(10.0)
        before = budget.remaining_s()
        # 业务时钟向前纠正：绝对截止立即收紧，时长上限不得延长
        clock.resync(START + 300.0)
        self.assertLessEqual(budget.remaining_s(), before + 0.01,
                             "向前重校不得延长已接纳的时长上限")

    def test_resync_backward_does_not_extend_duration(self):
        clock = self._clock(epoch=START, tick=1000.0)
        budget = RetryBudget(START + 120.0, business_clock=clock)
        clock.advance(30.0)
        before = budget.remaining_s()
        # resync 到更小 epoch 才是回拨：绝对截止看起来更远，但冻结的时长上限仍到点
        clock.resync(START - 300.0)
        after = budget.remaining_s()
        self.assertLessEqual(after, before + 0.01,
                             "回拨不得延长已冻结的时长上限")
        self.assertAlmostEqual(after, before, delta=0.01,
                               msg="时长上限仍是唯一收紧边界")

    def test_os_wall_jump_does_not_change_business_budget(self):
        clock = self._clock(epoch=START, tick=1000.0)
        budget = RetryBudget(START + 120.0, business_clock=clock)
        clock.advance(10.0)
        before = budget.remaining_s()
        # OS 墙钟前移/回拨都不参与业务预算；只作诊断
        with mock.patch("time.time", return_value=0.0):
            self.assertAlmostEqual(budget.remaining_s(), before, delta=0.01,
                                   msg="OS 回拨不得改变业务预算")
        forward = time.time() + 8 * 3600
        with mock.patch("time.time", return_value=forward):
            self.assertAlmostEqual(budget.remaining_s(), before, delta=0.01,
                                   msg="OS 前移不得改变业务预算")


class ScheduleBusinessClockTests(unittest.TestCase):
    """分类/校验/协调器的缺省 now 全部走注册的业务时钟。"""

    def setUp(self):
        self._prev = time_contract._BUSINESS_NOW
        self.clock = FakeBusinessClock(epoch=START, tick=500.0)
        signup_plan.configure = None
        time_contract.configure_time_source(self.clock.now)

    def tearDown(self):
        time_contract.configure_time_source(self._prev or (lambda: __import__("time").time()))

    def test_classify_signup_mode_follows_business_clock(self):
        # 开放时间在业务时刻 +10 秒：还没开放；OS 墙钟无论怎么错都一样
        start = school_time_text(START + 10.0)
        end = school_time_text(START + 7200.0)
        self.assertEqual(signup_plan.classify_signup_mode(start, end), "pre_register")
        self.clock.advance(11.0)
        self.assertEqual(signup_plan.classify_signup_mode(start, end), "instant_signup")

    def test_validate_plan_uses_business_now(self):
        rows = [{"id": "a", "name": "A", "sign_start": school_time_text(START + 5.0),
                 "sign_end": school_time_text(START + 7200.0),
                 "act_start": school_time_text(START + 86400.0),
                 "act_end": school_time_text(START + 90000.0), "signed": False}]
        self.assertEqual(signup_plan.validate_plan(rows)["ok"], True)
        self.clock.advance(6.0)     # 业务时间推进：同一份活动变成已开放
        self.assertEqual(signup_plan.validate_plan(rows)["ok"], True)

    def test_unconfigured_default_now_raises(self):
        saved = time_contract._BUSINESS_NOW
        try:
            time_contract.configure_time_source(None)
            with self.assertRaises(RuntimeError):
                signup_plan.classify_signup_mode(
                    school_time_text(START), school_time_text(START + 60.0))
        finally:
            time_contract.configure_time_source(saved)

    def test_school_time_text_requires_explicit_source(self):
        with self.assertRaises(ValueError):
            school_time_text()    # 不再暗用 OS 当前时间


class CoordinatorBusinessClockTests(unittest.TestCase):
    """协调器准备点与业务时钟同源；父层据此提前 20 秒请求重校。"""

    def test_next_prepare_epoch_is_min_pending(self):
        rows = [
            {"id": "a", "name": "A", "sign_start": school_time_text(START + 300.0),
             "sign_end": school_time_text(START + 7200.0),
             "act_start": school_time_text(START + 86400.0),
             "act_end": school_time_text(START + 90000.0), "signed": False},
            {"id": "b", "name": "B", "sign_start": school_time_text(START + 100.0),
             "sign_end": school_time_text(START + 7200.0),
             "act_start": school_time_text(START + 86400.0),
             "act_end": school_time_text(START + 90000.0), "signed": False},
        ]
        clock = FakeBusinessClock(epoch=START, tick=0.0)
        specs = [signup_plan.TaskSpec.from_activity(r, "t-%s" % r["id"], False,
                                                    now=START)
                 for r in rows]
        coord = signup_plan.PlanCoordinator("p1", specs, lambda *a: {}, lambda *a: None,
                                            now=clock.now)
        # B 场 sign_start=START+100、lead 100 秒 → 准备点 = START（最早）
        self.assertAlmostEqual(coord.next_prepare_epoch(), START, delta=1.5,
                               msg="最早准备点 = 各未终态场次的最小准备时刻")
        # 全部终态后返回 None
        for spec in specs:
            coord._states[spec.task_id] = "canceled"
        self.assertIsNone(coord.next_prepare_epoch())


class RealClientWorkerTests(unittest.TestCase):
    """生产 cmd_grab 通过真实 ClockClient 进入 RetryBudget（C01/C02 生产路径红例）。

    只替换网络与浏览器边界，不替换时钟本身：env 指向临时快照，setup_business_clock
    走真实 ClockClient 分支。预算已过时必须收成唯一结构化终态，零鉴权零浏览器。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tjise-client-worker-")
        self._env = {key: os.environ.get(key) for key in
                     ("TJISE_CLOCK_SNAPSHOT", "TJISE_CLOCK_OWNER")}
        self._biz = time_contract._BUSINESS_NOW
        self._clock_state = dict(grab._CLOCK)

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        time_contract.configure_time_source(self._biz)
        grab._CLOCK.update(self._clock_state)

    def _prepare_snapshot(self):
        """写一份真实 tick 域的无凭证快照并把 env 指过去。"""
        from business_clock import _elapsed_tick
        from fixtures.business_clock import write_snapshot

        tick = _elapsed_tick()
        epoch = 1793100000.0
        path = write_snapshot({"schema": 1, "owner_id": "worker-owner",
                               "generation": 1, "state": "ready", "epoch_s": epoch,
                               "tick_s": tick, "sampled_tick_s": tick,
                               "uncertainty_s": 0.1, "source": "NTP:test",
                               "age_s": 0.0, "reason": "北京时间已同步"}, self.tmp)
        os.environ["TJISE_CLOCK_SNAPSHOT"] = path
        os.environ["TJISE_CLOCK_OWNER"] = "worker-owner"
        return epoch

    def _run(self, *, expired=True):
        import grab as grab_module

        epoch = self._prepare_snapshot()

        calls = {"probe": 0, "post": 0, "browser": 0}
        logs = []

        def fake_probe(budget=None):
            calls["probe"] += 1
            return "valid", "ok"

        def fake_post(path, payload=None, tok=None, timeout=None, budget=None):
            calls["post"] += 1
            return {"code": 1, "data": {}}

        def fake_launch(pw, headless):
            calls["browser"] += 1
            raise AssertionError("过期预算不得创建浏览器")

        at_epoch = epoch - 600.0 if expired else epoch + 600.0
        args = SimpleNamespace(
            dry_run=False, headed=False, lead=100.0, window=120.0,
            retry_for=120.0, max_attempts=0, sign_end="",
            retry_start=at_epoch, activity_id="act-1",
            at=time_contract.school_time_text(at_epoch), name="生产路径活动")

        patches = {"log": lambda msg: logs.append(str(msg)),
                   "probe_token": fake_probe, "_post": fake_post,
                   "launch_browser": fake_launch,
                   "notify": lambda *a, **k: False}
        originals = {name: getattr(grab_module, name, None) for name in patches}
        restore_pw = _install_fake_playwright()
        grab_module.time = time
        time_contract.time = time
        try:
            for name, value in patches.items():
                setattr(grab_module, name, value)
            with contextlib.redirect_stdout(io.StringIO()):
                code = grab_module.cmd_grab(args)
        finally:
            for name, value in originals.items():
                if value is None:
                    try:
                        delattr(grab_module, name)
                    except AttributeError:
                        pass
                else:
                    setattr(grab_module, name, value)
            grab_module.time = time
            time_contract.time = time
            restore_pw()
        return code, logs, calls

    def test_expired_client_budget_yields_single_terminal_without_io(self):
        code, logs, calls = self._run(expired=True)
        terminal = [line for line in logs
                    if "[retry] " in line and "kind=done" in line and "stop_reason=" in line]
        self.assertEqual(len(terminal), 1, "必须恰一条结构化终态：%r" % (logs[-6:],))
        self.assertIn("stop_reason=retry_duration", terminal[0])
        self.assertEqual(code, 2)
        self.assertEqual(calls, {"probe": 0, "post": 0, "browser": 0},
                         "过期预算既不得鉴权也不得开浏览器")
        self.assertFalse(any("AttributeError" in line for line in logs),
                         "真实 ClockClient 不得因缺 elapsed_s 崩溃")

    def test_client_is_what_production_grab_uses(self):
        from business_clock import ClockClient
        self._prepare_snapshot()
        clock = grab.setup_business_clock(allow_sync=True)
        self.assertIsInstance(clock, ClockClient, "worker 模式必须走 ClockClient")
        self.assertGreater(clock.now(), 0)


def _install_fake_playwright():
    """只替换 playwright 模块边界：cmd_grab 顶部 import 不因测试环境缺包失败。"""
    import types
    pkg = types.ModuleType("playwright")
    sync_api = types.ModuleType("playwright.sync_api")

    class _Pw:
        def __enter__(self):
            return object()

        def __exit__(self, exc_type, exc, tb):
            return False

    class _Timeout(Exception):
        pass

    sync_api.sync_playwright = _Pw
    sync_api.TimeoutError = _Timeout
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


class CliListWithoutClockTests(unittest.TestCase):
    """C03：CLI list 没有可信北京时间时保留全部活动，不按本机时钟筛选/排行。"""

    def setUp(self):
        self._clock_state = dict(grab._CLOCK)
        grab._CLOCK.update({"now": None, "bounds": None, "meta": None})
        self._patched = {}
        rows = [{"id": "old", "name": "早已截止", "sign_start": "2020-01-01 09:00:00",
                 "sign_end": "2020-01-02 09:00:00", "act_start": "2020-01-03 09:00:00",
                 "act_end": "2020-01-03 11:00:00", "place": "A", "signed": False,
                 "checkin_required": None, "checkout_required": None}]
        for name in ("check_token", "fetch_activities", "log"):
            self._patched[name] = getattr(grab, name)
        grab.check_token = lambda budget=None: (True, "ok")
        grab.fetch_activities = lambda budget=None, with_details=False: rows
        grab.log = lambda msg: None

    def tearDown(self):
        for name, value in self._patched.items():
            setattr(grab, name, value)
        grab._CLOCK.update(self._clock_state)

    def test_list_keeps_rows_and_marks_state_unknown(self):
        args = SimpleNamespace(all=False, json=True)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = grab.cmd_list(args)
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue().strip())
        self.assertEqual([row["id"] for row in data], ["old"],
                         "没有可信时钟时不得按本机日期过滤活动")
        self.assertEqual(data[0]["state"], "待确认",
                         "开放状态未知时显示待确认，不做本机推断")


if __name__ == "__main__":
    unittest.main()
