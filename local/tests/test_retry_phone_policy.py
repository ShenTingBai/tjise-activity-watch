#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重试三模式与手机受限链路的具名契约测试（离线）。

覆盖决策 1/2 的行为边界：显式时长不扩窗、自动延长（-1）初始 120 与
cap 1500、父监督按「候选 − 已接纳」增量平移单调截止、伪造/重复/非法/过界
事件 0 增量、取消立即唤醒看门狗、三种模式 worker/父/桌面投影同值、
0 缺截止 2 分钟兜底。全部离线：fake worker 脚本、假时钟、127.0.0.1 不开，
真实学校/浏览器/凭据一律不用。

@author LiXinwen
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

LOCAL = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
if str(LOCAL) not in sys.path:
    sys.path.insert(0, str(LOCAL))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from time_contract import configure_time_source  # noqa: E402
configure_time_source(time.time)

from domain.retry_window import (  # noqa: E402
    ADAPTIVE_RETRY, ADAPTIVE_SECONDS, DEFAULT_RETRY_SECONDS,
    extension_cap, extension_delta, initial_deadline,
)
from domain.time_text import task_deadline  # noqa: E402
from grab import BurstPolicy, compute_retry_window  # noqa: E402
from signup_plan import TaskSpec  # noqa: E402
from fixtures.business_clock import TestWallClock  # noqa: E402


# ---- 学校时间文本换算（与 ui/state/time.js 的 toTs 同一公式）------------
def school_text(epoch: float) -> str:
    """epoch 秒 → 学校时间文本；toTs 是它的逆变换。"""
    dt = datetime.fromtimestamp(float(epoch) + 8 * 3600, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def school_epoch(text) -> "float | None":
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})$",
                     str(text or ""))
    if not match:
        return None
    y, mo, d, h, mi, s = (int(part) for part in match.groups())
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp() - 8 * 3600


# ---- fake worker 脚本（临时根落地，零真实 worker/浏览器）----------------
WORKER_FIXTURE = r'''
import os
import sys
import time

scenario = sys.argv[1] if len(sys.argv) > 1 else "silent"
deadline = os.environ.get("TJISE_TEST_DEADLINE", "0")

if scenario == "silent":
    time.sleep(60)
elif scenario == "quick":
    # 只为拿 worker 命令行：立刻正常退出，不占监督窗口
    print("[i] quick", flush=True)
    sys.exit(0)
elif scenario == "extend_then_wait":
    # 先宣布一次扩窗再挂住：父层必须按新截止收树，旧截止不得杀树
    print("[retry] kind=window_extended retry_deadline=%s sign_end=0" % deadline, flush=True)
    time.sleep(60)
elif scenario == "extend_then_done":
    print("[retry] kind=window_extended retry_deadline=%s sign_end=0" % deadline, flush=True)
    print("[retry] kind=done attempts=1 stop_reason=success retry_deadline=%s "
          "sign_end=0 last_reason=" % deadline, flush=True)
    sys.exit(0)
elif scenario == "terminal_then_extend":
    # 终态之后的扩窗必须被忽略：旧截止照常收树
    print("[retry] kind=done attempts=1 stop_reason=success retry_deadline=0 "
          "sign_end=0 last_reason=", flush=True)
    print("[retry] kind=window_extended retry_deadline=%s sign_end=0" % deadline, flush=True)
    time.sleep(60)
elif scenario == "double_extend":
    # 同一候选重复宣布一次：只有第一次有增量
    print("[retry] kind=window_extended retry_deadline=%s sign_end=0" % deadline, flush=True)
    print("[retry] kind=window_extended retry_deadline=%s sign_end=0" % deadline, flush=True)
    time.sleep(60)
else:
    raise SystemExit("unknown scenario: %s" % scenario)
'''


class _ServiceCase(unittest.TestCase):
    """api._run_plan_task 的公共装置：假 worker 脚本 + 短监督余量。"""

    GRACE = 0.4

    def setUp(self):
        sys.path.insert(0, str(LOCAL))
        try:
            import app as local_app
        except Exception as exc:
            self.skipTest("无法导入 local/app.py：%s" % exc)
        finally:
            sys.path.pop(0)
        self.app = local_app
        self.root = Path(tempfile.mkdtemp(prefix="tjise-retry-phone-"))
        self.addCleanup(shutil.rmtree, str(self.root), True)
        self._old_grace = local_app.SUPERVISE_GRACE_S
        local_app.SUPERVISE_GRACE_S = self.GRACE
        self.addCleanup(setattr, local_app, "SUPERVISE_GRACE_S", self._old_grace)
        self.script = self.root / "worker_fixture.py"
        self.script.write_text(WORKER_FIXTURE, encoding="utf-8")

    def _spec(self, retry_for, *, anchor_offset: float = 1.0,
              sign_end: str = "") -> TaskSpec:
        """anchor_offset 是「当前时刻 − 冻结起点」：越大，初始截止越近。"""
        return TaskSpec(task_id="t1", activity_id="a1", name="重试模式实验",
                        sign_start="2026-10-30 09:00:00", act_start="", act_end="",
                        sign_end=sign_end, retry_for_s=retry_for, max_attempts=0,
                        signup_mode="instant_signup", lead_s=0,
                        retry_started_at=time.time() - anchor_offset)

    def _run(self, scenario: str, *, retry_for, anchor_offset: float = 1.0,
             sign_end: str = "", cancel_after: float | None = None,
             announce_deadline: "float | None" = None, clock_spy=None):
        """跑一场 fake worker；返回 (result, elapsed, events, captured_args)。"""
        api = self.app.Api(clock=TestWallClock())
        events = []
        api._emit = lambda kind, payload: events.append((kind, payload))
        cancel = threading.Event()
        captured = []
        original_env = self.app._env

        def fake_env():
            env = dict(original_env())
            if announce_deadline is not None:
                env["TJISE_TEST_DEADLINE"] = "%.3f" % float(announce_deadline)
            return env

        def fake_command(*args):
            # worker_command 返回的 argv 会被 plan_service 用 += 追加
            # --window/--retry-for/--sign-end 等：记下最终 argv 才是证据
            cmd = [sys.executable, str(self.script), scenario]
            captured.append(cmd)
            return cmd

        timer = None
        started = time.time()
        result = None
        with mock.patch.object(self.app, "worker_command", fake_command), \
                mock.patch.object(self.app, "_env", fake_env):
            if clock_spy is not None:
                with mock.patch.object(api, "_clock_now", clock_spy):
                    if cancel_after is not None:
                        timer = threading.Timer(cancel_after, cancel.set)
                        timer.start()
                    try:
                        result = api._run_plan_task(
                            self._spec(retry_for, anchor_offset=anchor_offset,
                                       sign_end=sign_end), cancel, lambda *a: None)
                    finally:
                        if timer is not None:
                            timer.cancel()
            else:
                if cancel_after is not None:
                    timer = threading.Timer(cancel_after, cancel.set)
                    timer.start()
                try:
                    result = api._run_plan_task(
                        self._spec(retry_for, anchor_offset=anchor_offset,
                                   sign_end=sign_end), cancel, lambda *a: None)
                finally:
                    if timer is not None:
                        timer.cancel()
        return result, time.time() - started, events, captured


def _watchdog_leaks() -> list:
    return [t for t in threading.enumerate()
            if t.name == "signup-watchdog" and t.is_alive()]


# ===================================================================== 公式层
class InitialDeadlineTests(unittest.TestCase):
    """-1 初始 120、cap 1500 且 sign_end 硬截；三种模式同一公式。"""

    def test_auto_mode_initial_deadline_is_120_not_sign_end(self):
        anchor = 1792000000.0
        sign_end = anchor + 4000.0
        self.assertEqual(initial_deadline(anchor, ADAPTIVE_RETRY, sign_end),
                         anchor + DEFAULT_RETRY_SECONDS)
        self.assertEqual(initial_deadline(anchor, ADAPTIVE_RETRY, None),
                         anchor + DEFAULT_RETRY_SECONDS)

    def test_auto_mode_cap_is_anchor_plus_1500_cut_by_sign_end(self):
        anchor = 1792000000.0
        self.assertEqual(extension_cap(anchor, None), anchor + ADAPTIVE_SECONDS)
        self.assertEqual(extension_cap(anchor, anchor + 300.0), anchor + 300.0)
        self.assertEqual(extension_cap(anchor, anchor + 99999.0),
                         anchor + ADAPTIVE_SECONDS)

    def test_fixed_and_until_deadline_keep_existing_contract(self):
        anchor = 1792000000.0
        sign_end = anchor + 30.0
        # 正数硬时长：sign_end 更早时被截断
        self.assertEqual(initial_deadline(anchor, 600, sign_end), sign_end)
        self.assertEqual(initial_deadline(anchor, 600, anchor + 900.0), anchor + 600)
        # 0：有截止直达，缺截止回退 120
        self.assertEqual(initial_deadline(anchor, 0, anchor + 5000.0), anchor + 5000.0)
        self.assertEqual(initial_deadline(anchor, 0, None), anchor + DEFAULT_RETRY_SECONDS)

    def test_zero_without_sign_end_falls_back_to_two_minutes(self):
        """0 缺截止明确兜底 2 分钟，绝不无限运行。"""
        anchor = 1792000000.0
        deadline, binding = compute_retry_window(anchor, None, 0, now=anchor)[1:]
        self.assertEqual(deadline, anchor + DEFAULT_RETRY_SECONDS)
        self.assertEqual(binding, "fallback")
        self.assertEqual(initial_deadline(anchor, 0, None), anchor + 120.0)

    def test_worker_and_parent_formula_agree_for_three_modes(self):
        anchor = 1792000000.0
        sign_end = anchor + 4000.0
        cases = ((120, anchor + 120.0), (ADAPTIVE_RETRY, anchor + 120.0),
                 (0, sign_end))
        for mode, want in cases:
            with self.subTest(mode=mode):
                _, worker_deadline, _ = compute_retry_window(
                    anchor, sign_end, mode, now=anchor)
                spec = TaskSpec(task_id="t1", activity_id="a1", name="对照",
                                sign_start="", act_start="", act_end="",
                                sign_end=school_text(sign_end),
                                retry_for_s=mode, max_attempts=0,
                                signup_mode="instant_signup", lead_s=0,
                                retry_started_at=anchor)
                parent_deadline = task_deadline(spec, school_epoch)
                self.assertEqual(worker_deadline, want)
                self.assertEqual(parent_deadline, want)


class ExtensionDeltaTests(unittest.TestCase):
    """伪造/重复/非法/过界事件 0 增量；合法扩窗只给严格增长的差值。"""

    CAP = 2500.0

    def test_valid_extension_returns_strict_growth_delta(self):
        self.assertEqual(extension_delta(1000.0, 1380.0, self.CAP, True), 380.0)

    def test_fixed_mode_and_disabled_never_extend(self):
        self.assertEqual(extension_delta(1000.0, 1380.0, self.CAP, False), 0.0)
        self.assertEqual(extension_delta(1000.0, 1380.0, None, True), 0.0)

    def test_duplicate_shrink_out_of_bounds_are_zero(self):
        self.assertEqual(extension_delta(1000.0, 1000.0, self.CAP, True), 0.0)
        self.assertEqual(extension_delta(1000.0, 900.0, self.CAP, True), 0.0)
        self.assertEqual(extension_delta(1000.0, self.CAP + 1.0, self.CAP, True), 0.0)
        self.assertEqual(extension_delta(1000.0, self.CAP, self.CAP, True),
                         self.CAP - 1000.0)

    def test_illegal_values_are_zero(self):
        for candidate in ("abc", None, "", float("nan"), float("inf"), [1]):
            with self.subTest(candidate=candidate):
                self.assertEqual(extension_delta(1000.0, candidate, self.CAP, True), 0.0)

    def test_delta_does_not_depend_on_wall_clock(self):
        """同一对数值的增量恒定：时钟跳变不影响 extension_delta 的结果。"""
        before = extension_delta(1000.0, 1380.0, self.CAP, True)
        time.sleep(0.05)
        self.assertEqual(extension_delta(1000.0, 1380.0, self.CAP, True), before)


class _FakeClock:
    """BurstPolicy 的最小业务时钟替身：wall 是业务 epoch，mono 是单调秒。"""

    def __init__(self, wall, mono=1000.0):
        self.wall = float(wall)
        self.mono = float(mono)

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def elapsed_s(self):
        return self.mono

    def now(self):
        return self.wall

    def sleep(self, seconds):
        step = max(0.0, float(seconds))
        self.wall += step
        self.mono += step


class WorkerExtensionPolicyTests(unittest.TestCase):
    """worker 侧扩窗：固定 120 连 404 不扩；自动模式扩到 cap 且 sign_end 硬截。"""

    START = 1792000000.0

    def _policy(self, *, allow_extend, base_deadline=None, sign_end_ts=None):
        clock = _FakeClock(wall=self.START, mono=1000.0)
        policy = BurstPolicy(
            open_ts=self.START,
            base_deadline=self.START + 120.0 if base_deadline is None else base_deadline,
            sign_end_ts=sign_end_ts, max_attempts=0,
            clock=clock, retry_start=self.START,
            allow_extend=allow_extend)
        return policy, clock

    def test_fixed_120_with_consecutive_404_does_not_extend(self):
        """固定 120（显式 --retry-for）连 404 不扩窗：用户给的秒数是硬承诺。"""
        policy, clock = self._policy(allow_extend=False)
        clock.wall = self.START + 5.0
        for _ in range(10):
            policy.settle(policy.register_request(), "404")
        self.assertEqual(policy.effective_deadline, self.START + 120.0)
        self.assertGreater(policy.streak_404, 3, "连击照常计数，只是不拉窗口")

    def test_auto_mode_extends_to_cap_and_sign_end_stays_hard(self):
        """自动延长（allow_extend）连 404 扩到起点+1500；sign_end 更早时被硬截。"""
        policy, clock = self._policy(allow_extend=True, base_deadline=self.START + 120.0)
        clock.wall = self.START + 5.0
        for _ in range(10):
            policy.settle(policy.register_request(), "404")
        self.assertEqual(policy.effective_deadline, self.START + ADAPTIVE_SECONDS)

        cut, clock2 = self._policy(allow_extend=True, base_deadline=self.START + 120.0,
                                   sign_end_ts=self.START + 300.0)
        clock2.wall = self.START + 5.0
        for _ in range(10):
            cut.settle(cut.register_request(), "404")
        self.assertEqual(cut.effective_deadline, self.START + 300.0,
                         "sign_end 永远硬截止，扩窗不得越过")


class PlanServiceModeTests(_ServiceCase):
    """父层：自动模式 worker 参数、扩窗平移单调截止、取消立即唤醒。"""

    def test_auto_mode_worker_arguments_use_window_without_retry_for(self):
        """自动延长（-1）发 --window 120 且不传 --retry-for；正数/0 仍显式。"""
        _, _, _, captured = self._run("quick", retry_for=ADAPTIVE_RETRY)
        args = captured[0]
        self.assertIn("--window", args)
        self.assertEqual(args[args.index("--window") + 1], "120")
        self.assertNotIn("--retry-for", args)

        _, _, _, captured = self._run("quick", retry_for=120)
        args = captured[0]
        self.assertIn("--retry-for", args)
        self.assertEqual(args[args.index("--retry-for") + 1], "120")
        self.assertNotIn("--window", args)

        _, _, _, captured = self._run("quick", retry_for=0)
        args = captured[0]
        self.assertEqual(args[args.index("--retry-for") + 1], "0")

    def test_extension_moves_hard_stop_and_old_deadline_does_not_kill(self):
        """延期后旧截止不杀树、新截止会收树（关键算式按增量平移）。"""
        now = time.time()
        anchor = now - (DEFAULT_RETRY_SECONDS - 1.0)   # 初始截止 ≈ 1 秒后
        candidate = anchor + DEFAULT_RETRY_SECONDS + 4.0
        api = self.app.Api(clock=TestWallClock())
        api._emit = lambda kind, payload: None
        cancel = threading.Event()
        original_env = self.app._env

        def fake_env():
            env = dict(original_env())
            env["TJISE_TEST_DEADLINE"] = "%.3f" % candidate
            return env

        def fake_command(*args):
            return [sys.executable, str(self.script), "extend_then_wait"]

        started = time.time()
        with mock.patch.object(self.app, "worker_command", fake_command), \
                mock.patch.object(self.app, "_env", fake_env):
            result = api._run_plan_task(
                self._spec(ADAPTIVE_RETRY, anchor_offset=DEFAULT_RETRY_SECONDS - 1.0),
                cancel, lambda *a: None)
        elapsed = time.time() - started
        self.assertEqual(result["stop_reason"], "retry_duration")
        self.assertGreaterEqual(elapsed, 2.5,
                                "旧截止（约 %.1f 秒）不得杀树" % (0.5 + self.GRACE))
        self.assertLessEqual(elapsed, 4.0 + self.GRACE + 2.0,
                             "新截止必须把树收走，不能挂住不退")
        self.assertEqual(_watchdog_leaks(), [])

    def test_fixed_mode_ignores_window_extended_events(self):
        """固定模式的扩窗事件一律 0 增量：旧截止照常收树。"""
        now = time.time()
        anchor = now - (DEFAULT_RETRY_SECONDS - 1.0)
        candidate = anchor + DEFAULT_RETRY_SECONDS + 8.0
        api = self.app.Api(clock=TestWallClock())
        api._emit = lambda kind, payload: None
        cancel = threading.Event()
        original_env = self.app._env

        def fake_env():
            env = dict(original_env())
            env["TJISE_TEST_DEADLINE"] = "%.3f" % candidate
            return env

        def fake_command(*args):
            return [sys.executable, str(self.script), "extend_then_wait"]

        started = time.time()
        with mock.patch.object(self.app, "worker_command", fake_command), \
                mock.patch.object(self.app, "_env", fake_env):
            result = api._run_plan_task(
                self._spec(120, anchor_offset=DEFAULT_RETRY_SECONDS - 1.0),
                cancel, lambda *a: None)
        elapsed = time.time() - started
        self.assertEqual(result["stop_reason"], "retry_duration")
        self.assertLess(elapsed, 3.0, "固定 120 不得被 window_extended 拉长")
        self.assertEqual(_watchdog_leaks(), [])

    def test_terminal_then_extend_is_ignored(self):
        """终态之后的扩窗事件忽略：仍按旧截止收树，不延期。"""
        now = time.time()
        anchor = now - (DEFAULT_RETRY_SECONDS - 1.0)
        candidate = anchor + DEFAULT_RETRY_SECONDS + 8.0
        api = self.app.Api(clock=TestWallClock())
        api._emit = lambda kind, payload: None
        cancel = threading.Event()
        original_env = self.app._env

        def fake_env():
            env = dict(original_env())
            env["TJISE_TEST_DEADLINE"] = "%.3f" % candidate
            return env

        def fake_command(*args):
            return [sys.executable, str(self.script), "terminal_then_extend"]

        started = time.time()
        with mock.patch.object(self.app, "worker_command", fake_command), \
                mock.patch.object(self.app, "_env", fake_env):
            result = api._run_plan_task(
                self._spec(ADAPTIVE_RETRY, anchor_offset=DEFAULT_RETRY_SECONDS - 1.0),
                cancel, lambda *a: None)
        elapsed = time.time() - started
        self.assertLess(elapsed, 3.0, "终态后的扩窗不得延期")
        self.assertEqual(result["status"], "success")

    def test_duplicate_extension_event_adds_only_once(self):
        """同一候选重复宣布：只有第一次有增量。"""
        now = time.time()
        anchor = now - (DEFAULT_RETRY_SECONDS - 1.0)
        candidate = anchor + DEFAULT_RETRY_SECONDS + 4.0
        api = self.app.Api(clock=TestWallClock())
        api._emit = lambda kind, payload: None
        cancel = threading.Event()
        original_env = self.app._env

        def fake_env():
            env = dict(original_env())
            env["TJISE_TEST_DEADLINE"] = "%.3f" % candidate
            return env

        def fake_command(*args):
            return [sys.executable, str(self.script), "double_extend"]

        started = time.time()
        with mock.patch.object(self.app, "worker_command", fake_command), \
                mock.patch.object(self.app, "_env", fake_env):
            result = api._run_plan_task(
                self._spec(ADAPTIVE_RETRY, anchor_offset=DEFAULT_RETRY_SECONDS - 1.0),
                cancel, lambda *a: None)
        elapsed = time.time() - started
        self.assertEqual(result["stop_reason"], "retry_duration")
        # 只扩一次：new = old + (candidate - accepted)；重复事件不得再加一份
        self.assertLess(elapsed, 7.5, "重复扩窗事件不得叠加第二次增量")
        self.assertEqual(_watchdog_leaks(), [])

    def test_cancel_wakes_watchdog_immediately(self):
        """取消立即唤醒看门狗线程退出：不许睡到远截止才放手。"""
        result, _, _, _ = self._run("silent", retry_for=3600, cancel_after=0.3)
        self.assertIn(result["status"], {"canceled", "failed"})
        self.assertEqual(_watchdog_leaks(), [],
                         "取消后 signup-watchdog 线程必须已被唤醒退出")

    def test_backward_clock_recalibration_cannot_extend_monotonic_duration(self):
        """扩窗不重新读业务时钟：clock_now 只在入口取一次。"""
        now = time.time()
        anchor = now - (DEFAULT_RETRY_SECONDS - 1.0)
        candidate = anchor + DEFAULT_RETRY_SECONDS + 3.0
        calls = {"n": 0}

        def spy():
            calls["n"] += 1
            return now

        result, _, _, _ = self._run(
            "extend_then_done", retry_for=ADAPTIVE_RETRY,
            anchor_offset=DEFAULT_RETRY_SECONDS - 1.0,
            announce_deadline=candidate, clock_spy=spy)
        self.assertEqual(result["status"], "success")
        self.assertEqual(calls["n"], 1,
                         "扩窗路径不得重新读业务时钟（重校向后修正不得延期）")


# ===================================================================== 投影对照
def _run_js(snippet: str):
    """node vm 执行真实 UI 脚本（复用项目既有行为测试装置）；无 node 时 skip。"""
    try:
        from test_ui_contract import run_js
    except Exception as exc:                       # pragma: no cover - 装置缺失
        raise unittest.SkipTest("UI 行为装置不可用：%s" % exc)
    return run_js(snippet)


class DesktopProjectionTests(unittest.TestCase):
    """三种模式 worker / 父监督 / 桌面投影同值对照（node vm 跑真实 board.js）。"""

    ANCHOR = 1792000000.0
    SIGN_END = ANCHOR + 4000.0

    def _desktop(self, retry_for, sign_end_text, fallback_now=None,
                 with_deadline=None):
        import json as _json
        task = {"retry_for_s": retry_for, "retry_started_at": self.ANCHOR,
                "sign_end": sign_end_text}
        if with_deadline is not None:
            task["retry_deadline"] = with_deadline
        now = self.ANCHOR if fallback_now is None else fallback_now
        snippet = ("const task = %s;\n"
                   "out.push(effectiveRetryDeadline(task, %s));\n"
                   % (_json.dumps(task), repr(float(now))))
        values = _run_js(snippet)
        self.assertTrue(values, "桌面投影没有输出")
        return values[0]

    def _worker_parent(self, mode, sign_end_epoch):
        _, worker_deadline, _ = compute_retry_window(
            self.ANCHOR, sign_end_epoch, mode, now=self.ANCHOR)
        spec = TaskSpec(task_id="t1", activity_id="a1", name="投影对照",
                        sign_start="", act_start="", act_end="",
                        sign_end=school_text(sign_end_epoch) if sign_end_epoch else "",
                        retry_for_s=mode, max_attempts=0,
                        signup_mode="instant_signup", lead_s=0,
                        retry_started_at=self.ANCHOR)
        return worker_deadline, task_deadline(spec, school_epoch)

    def test_three_modes_projection_same_value_across_layers(self):
        """固定 120 / 自动 -1 / 直到截止 0：三层推导同值（含 0 缺截止兜底）。"""
        cases = (
            (120, self.SIGN_END, self.ANCHOR + 120.0),
            (ADAPTIVE_RETRY, self.SIGN_END, self.ANCHOR + 120.0),
            (0, self.SIGN_END, self.SIGN_END),
            (0, None, self.ANCHOR + DEFAULT_RETRY_SECONDS),
            (120, self.ANCHOR + 30.0, self.ANCHOR + 30.0),
        )
        for mode, sign_end_epoch, want in cases:
            with self.subTest(mode=mode, sign_end=sign_end_epoch):
                worker_deadline, parent_deadline = self._worker_parent(
                    mode, sign_end_epoch)
                desktop = self._desktop(
                    mode, school_text(sign_end_epoch) if sign_end_epoch else "")
                self.assertEqual(worker_deadline, want)
                self.assertEqual(parent_deadline, want)
                self.assertEqual(float(desktop), want)

    def test_auto_mode_matches_explicit_120_worker_formula(self):
        """自动模式的 worker 参数是 --window 120：两条公式初始截止必须同值。"""
        _, via_auto, _ = compute_retry_window(
            self.ANCHOR, self.SIGN_END, ADAPTIVE_RETRY, now=self.ANCHOR)
        _, via_window, _ = compute_retry_window(
            self.ANCHOR, self.SIGN_END, DEFAULT_RETRY_SECONDS, now=self.ANCHOR)
        self.assertEqual(via_auto, via_window)

    def test_desktop_prefers_backend_accepted_deadline(self):
        """倒计时优先后端已接纳 deadline（合法扩窗后的权威截止同此入口）。"""
        desktop = self._desktop(ADAPTIVE_RETRY, school_text(self.SIGN_END),
                                with_deadline=self.ANCHOR + 777.0)
        self.assertEqual(float(desktop), self.ANCHOR + 777.0)

    def test_desktop_zero_without_sign_end_shows_two_minute_fallback(self):
        desktop = self._desktop(0, "")
        self.assertEqual(float(desktop), self.ANCHOR + DEFAULT_RETRY_SECONDS)


class CanonicalOptionsTests(unittest.TestCase):
    """bootstrap 的权威选项与桌面 select / store.js 兜底副本保持一致。"""

    def _bootstrap_retry_options(self):
        sys.path.insert(0, str(LOCAL))
        try:
            import app as local_app
        except Exception as exc:
            self.skipTest("无法导入 local/app.py：%s" % exc)
        finally:
            sys.path.pop(0)
        tmp = tempfile.mkdtemp(prefix="tjise-retry-opts-")
        self.addCleanup(shutil.rmtree, tmp, True)
        with mock.patch.object(local_app, "PREFS", Path(tmp) / "ui.json"), \
                mock.patch.object(local_app, "DEMO", True):
            payload = local_app.Api(clock=TestWallClock()).bootstrap()
        return payload["retry_options"]

    def test_desktop_select_options_match_bootstrap_canonical_choices(self):
        options = self._bootstrap_retry_options()
        html = (LOCAL / "ui" / "index.html").read_text(encoding="utf-8")
        match = re.search(r'id="retry-for".*?</select>', html, re.S)
        self.assertIsNotNone(match, "找不到 #retry-for 下拉框")
        values = re.findall(r'<option value="(-?\d+)">', match.group(0))
        self.assertEqual(values, [str(item["value"]) for item in options["choices"]],
                         "桌面选项必须与 bootstrap 权威清单同序同值")
        self.assertEqual(options["default"], 120)

    def test_store_default_choices_match_bootstrap_canonical(self):
        options = self._bootstrap_retry_options()
        snippet = "out.push(JSON.parse(JSON.stringify(S.retryOptions)));\n"
        values = _run_js(snippet)
        self.assertTrue(values, "store.js 没有导出 retryOptions")
        self.assertEqual(values[0]["choices"], options["choices"])
        self.assertEqual(values[0]["default"], options["default"])
