#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""报名计划核心的本地单元测试，不连接学校站点。

断言以「状态 + 事件载荷」为准，不只断言布尔值；覆盖跨批冲突、空/倒置时间、
重复 id、已报名项、同批并发、失败隔离、取消等待、停止后终态和 busy 锁。

@author LiXinwen
"""

from __future__ import annotations

import io
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

LOCAL = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
# 裸导入（time_contract / grab）依赖 local/ 在 sys.path 上：必须早于这些 import
if str(LOCAL) not in sys.path:
    sys.path.insert(0, str(LOCAL))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from time_contract import configure_time_source, school_time_text  # noqa: E402
configure_time_source(time.time)   # 旧用例显式注入本机时钟；新用例改注 FakeBusinessClock
from unittest import mock

from local.signup_plan import (
    PlanCoordinator,
    TaskSpec,
    classify_signup_mode,
    group_by_sign_start,
    intervals_overlap,
    parse_timestamp,
    validate_plan,
)
from fixtures.business_clock import TestWallClock  # noqa: E402
from business_clock import ClockUnavailable  # noqa: E402


def activity(aid, sign, start, end, name=None, signed=False):
    return {
        "id": aid,
        "name": name or aid,
        "sign_start": sign,
        "act_start": start,
        "act_end": end,
        "signed": signed,
    }


# ---- 条款同意夹具（计划 repair-terms-test-fixtures）----
# login、start_plan、check、list 都先过 app._terms_gate，而门禁只读 app.PREFS：
# 不把偏好钉到临时文件，这些用例就会去读开发者真实的 ui.json，门禁结论随机器上
# 是否点过同意而漂移。同意状态只写在本轮临时 ui.json 的 terms_version /
# terms_accepted_at 两个键上，模块跑完即删；未同意状态的门禁专测在
# test_terms_consent.py 里照旧构造，不受这里影响。
_PREFS_PATCHER = None
_PREFS_TMP = None


def setUpModule():
    """把本模块的偏好文件钉到临时 ui.json，并写入当前版本的条款同意。"""
    global _PREFS_PATCHER, _PREFS_TMP
    sys.path.insert(0, str(LOCAL))
    try:
        import app as local_app
    except Exception:
        # 导入失败时由各用例自己 skipTest，保持原有跳过行为，不替它们下结论
        return
    finally:
        sys.path.pop(0)
    _PREFS_TMP = tempfile.TemporaryDirectory(prefix="tjise-terms-prefs-")
    _PREFS_PATCHER = mock.patch.object(local_app, "PREFS",
                                       Path(_PREFS_TMP.name) / "ui.json")
    _PREFS_PATCHER.start()
    if not local_app.update_prefs({"terms_version": local_app.TERMS_VERSION,
                                   "terms_accepted_at": "2026-10-27 12:00:00"}):
        tearDownModule()
        raise RuntimeError("条款同意偏好夹具写入失败")


def tearDownModule():
    """还原真实的 app.PREFS，并删除本轮临时偏好目录。"""
    global _PREFS_PATCHER, _PREFS_TMP
    if _PREFS_PATCHER is not None:
        _PREFS_PATCHER.stop()
        _PREFS_PATCHER = None
    if _PREFS_TMP is not None:
        _PREFS_TMP.cleanup()
        _PREFS_TMP = None


class SignupPlanValidationTests(unittest.TestCase):
    def test_same_open_non_overlapping_can_share_batch(self):
        rows = [
            activity("a", "2026-10-23 10:00:00", "2026-10-23 12:00:00", "2026-10-23 13:00:00"),
            activity("b", "2026-10-23 10:00:00", "2026-10-23 14:00:00", "2026-10-23 15:00:00"),
        ]
        result = validate_plan(rows)
        self.assertTrue(result["ok"])
        self.assertFalse(result["conflicts"])
        self.assertEqual(len(group_by_sign_start(result["specs"])), 1)

    def test_same_open_overlapping_is_conflict(self):
        rows = [
            activity("a", "2026-10-23 10:00:00", "2026-10-23 12:00:00", "2026-10-23 13:00:00"),
            activity("b", "2026-10-23 10:00:00", "2026-10-23 12:30:00", "2026-10-23 14:00:00"),
        ]
        result = validate_plan(rows)
        self.assertFalse(result["ok"])
        self.assertEqual(result["conflicts"][0]["left"]["activity_id"], "a")

    def test_different_open_overlapping_is_also_conflict(self):
        rows = [
            activity("a", "2026-10-23 10:00:00", "2026-10-23 12:00:00", "2026-10-23 13:00:00"),
            activity("b", "2026-10-23 11:00:00", "2026-10-23 12:30:00", "2026-10-23 14:00:00"),
        ]
        result = validate_plan(rows)
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["conflicts"]), 1)

    def test_touching_boundaries_do_not_overlap(self):
        left_start = parse_timestamp("2026-10-23 12:00:00")
        left_end = parse_timestamp("2026-10-23 13:00:00")
        right_start = parse_timestamp("2026-10-23 13:00:00")
        right_end = parse_timestamp("2026-10-23 14:00:00")
        self.assertFalse(intervals_overlap(left_start, left_end, right_start, right_end))

    def test_missing_invalid_and_duplicate_items_are_rejected(self):
        rows = [
            activity("a", "2026-10-23 10:00:00", "", ""),
            activity("a", "2026-10-23 10:00:00", "2026-10-23 12:00:00", "2026-10-23 13:00:00"),
        ]
        result = validate_plan(rows)
        kinds = {item["kind"] for item in result["errors"]}
        self.assertFalse(result["ok"])
        self.assertIn("activity_time_unknown", kinds)
        self.assertIn("duplicate_id", kinds)

    def test_empty_and_inverted_time_edges_are_rejected_table_driven(self):
        """表驱动：sign_start 缺失/非法、act 缺失、act 倒置，各自给出明确错误。"""
        cases = [
            # (sign_start, act_start, act_end, 期望错误 kind)
            ("", "2026-10-23 12:00:00", "2026-10-23 13:00:00", "invalid_sign_start"),
            ("2026/10/23 10:00", "2026-10-23 12:00:00", "2026-10-23 13:00:00", "invalid_sign_start"),
            ("2026-10-23 10:00:00", "", "2026-10-23 13:00:00", "activity_time_unknown"),
            ("2026-10-23 10:00:00", "2026-10-23 12:00:00", "", "activity_time_unknown"),
            ("2026-10-23 10:00:00", "2026-10-23 13:00:00", "2026-10-23 12:00:00", "activity_time_invalid"),
            ("2026-10-23 10:00:00", "2026-10-23 12:00:00", "2026-10-23 12:00:00", "activity_time_invalid"),
        ]
        for sign, start, end, want in cases:
            with self.subTest(want=want, sign=sign, start=start, end=end):
                result = validate_plan([activity("a", sign, start, end)])
                self.assertFalse(result["ok"], "非法时间不得放行：%r" % ((sign, start, end),))
                kinds = [item["kind"] for item in result["errors"]]
                self.assertIn(want, kinds, "错误归类应为 %s，实际 %r" % (want, kinds))
                # 错误载荷要带活动定位，前端才能逐条翻译
                hit = next(item for item in result["errors"] if item["kind"] == want)
                self.assertEqual(hit.get("activity_id"), "a")

    def test_signed_activity_is_rejected_with_payload(self):
        result = validate_plan([
            activity("a", "2026-10-23 10:00:00", "2026-10-23 12:00:00", "2026-10-23 13:00:00", signed=True),
        ])
        self.assertFalse(result["ok"])
        hit = next(item for item in result["errors"] if item["kind"] == "already_signed")
        self.assertEqual(hit["name"], "a")

    def test_same_sign_start_grouping_is_batched_by_full_timestamp(self):
        """同一 sign_start 一批；不同 sign_start 分批且从早到晚。"""
        specs = [
            TaskSpec("t-late", "late", "late", "2026-10-23 11:00:00", "2026-10-23 15:00:00", "2026-10-23 16:00:00"),
            TaskSpec("t-a", "a", "a", "2026-10-23 10:00:00", "2026-10-23 12:00:00", "2026-10-23 13:00:00"),
            TaskSpec("t-b", "b", "b", "2026-10-23 10:00:00", "2026-10-23 14:00:00", "2026-10-23 15:00:00"),
        ]
        groups = group_by_sign_start(specs)
        self.assertEqual([[s.task_id for s in g] for g in groups],
                         [["t-a", "t-b"], ["t-late"]])


class SignupPlanCoordinatorTests(unittest.TestCase):
    def _wait(self, coordinator):
        thread = coordinator._thread
        self.assertIsNotNone(thread)
        thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_same_batch_starts_in_parallel_and_failure_is_isolated(self):
        specs = [
            TaskSpec("task-a", "a", "a", "2026-10-22 10:00:00", "2026-10-23 12:00:00", "2026-10-23 13:00:00"),
            TaskSpec("task-b", "b", "b", "2026-10-22 10:00:00", "2026-10-23 14:00:00", "2026-10-23 15:00:00"),
        ]
        events = []
        barrier = threading.Barrier(2)

        def emit(kind, payload):
            events.append((kind, payload))

        def runner(spec, cancel_event, status):
            barrier.wait(1)
            if spec.activity_id == "b":
                return {"status": "failed", "reason": "测试失败"}
            return {"status": "success", "code": 0}

        coordinator = PlanCoordinator("plan-1", specs, runner, emit)
        coordinator.start()
        self._wait(coordinator)
        task_states = {p["task_id"]: p["status"] for kind, p in events if kind == "task"}
        self.assertEqual(task_states["task-a"], "success")
        self.assertEqual(task_states["task-b"], "failed")
        done = [p for kind, p in events if kind == "plan_done"][-1]
        self.assertEqual(done["status"], "partial")

    def test_cancel_waiting_task_does_not_cancel_other_task(self):
        specs = [
            TaskSpec("task-a", "a", "a", "2099-10-22 10:00:00", "2099-10-23 12:00:00", "2099-10-23 13:00:00"),
            TaskSpec("task-b", "b", "b", "2099-10-22 10:00:00", "2099-10-23 14:00:00", "2099-10-23 15:00:00"),
        ]
        events = []

        def emit(kind, payload):
            events.append((kind, payload))

        def runner(spec, cancel_event, status):
            return {"status": "success"}

        coordinator = PlanCoordinator("plan-2", specs, runner, emit)
        coordinator.start()
        time.sleep(0.02)
        self.assertTrue(coordinator.cancel_task("task-a"))
        coordinator.stop()
        self._wait(coordinator)
        task_states = {p["task_id"]: p["status"] for kind, p in events if kind == "task"}
        self.assertEqual(task_states["task-a"], "canceled")


class SignupPlanOrderingTests(unittest.TestCase):
    """跨批次顺序等待与 plan_done 载荷。"""

    def _wait(self, coordinator):
        thread = coordinator._thread
        self.assertIsNotNone(thread)
        thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_batches_run_in_time_order_and_payloads_are_complete(self):
        """不同开放批次按时间顺序执行；task 事件带 plan_id/task_id/sign_start，
        plan_done 带按状态计数的 summary，而不是一句布尔。"""
        now = datetime.now().replace(microsecond=0)
        first_at = (now - timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
        second_at = (now - timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S")
        specs = [
            TaskSpec("task-late", "late", "晚批", second_at, "2026-10-23 15:00:00", "2026-10-23 16:00:00"),
            TaskSpec("task-early", "early", "早批", first_at, "2026-10-23 12:00:00", "2026-10-23 13:00:00"),
        ]
        events = []
        order = []
        started = threading.Event()

        def emit(kind, payload):
            events.append((kind, payload))
            if kind == "task" and payload.get("status") == "submitting":
                order.append(payload["task_id"])

        def runner(spec, cancel_event, status):
            started.set()
            # 早批失败、晚批成功：单项失败不得覆盖其他结果
            return {"status": "failed", "reason": "测试失败"} if spec.task_id == "task-early" \
                else {"status": "success", "code": 0}

        coordinator = PlanCoordinator("plan-3", specs, runner, emit)
        coordinator.start()
        self._wait(coordinator)
        # 独立调度后两批都立即并发启动，不再保证提交先后（旧实现 join 整批，
        # 会用第一批的重试阻塞第二批——这正是被移除的行为）；
        # 这里断言两批都被提交且结果/载荷完整，顺序交给批次内保证。
        self.assertEqual(sorted(order), ["task-early", "task-late"],
                         "两个批次都必须被提交")
        for kind, payload in events:
            if kind != "task":
                continue
            self.assertEqual(payload["plan_id"], "plan-3")
            self.assertIn("task_id", payload)
            self.assertIn("sign_start", payload)
            self.assertIn("status", payload)
        done = [p for kind, p in events if kind == "plan_done"][-1]
        self.assertEqual(done["plan_id"], "plan-3")
        self.assertEqual(done["status"], "partial")
        self.assertFalse(done["ok"])
        self.assertEqual(done["summary"]["total"], 2)
        self.assertEqual(done["summary"]["counts"], {"failed": 1, "success": 1})

    def test_stop_keeps_terminal_states_terminal(self):
        """停止/取消之后，终态任务不会再回到等待或提交中。"""
        spec_a = TaskSpec("task-a", "a", "a", "2099-10-22 10:00:00", "2099-10-23 12:00:00", "2099-10-23 13:00:00")
        spec_b = TaskSpec("task-b", "b", "b", "2099-10-22 10:00:00", "2099-10-23 14:00:00", "2099-10-23 15:00:00")
        events = []

        def emit(kind, payload):
            events.append((kind, payload))

        def runner(spec, cancel_event, status):
            return {"status": "canceled", "reason": "用户取消了这一场"}

        coordinator = PlanCoordinator("plan-4", [spec_a, spec_b], runner, emit)
        coordinator.start()
        time.sleep(0.02)
        self.assertTrue(coordinator.cancel_task("task-a"))
        coordinator.stop()
        self._wait(coordinator)
        a_events = [p["status"] for kind, p in events
                    if kind == "task" and p["task_id"] == "task-a"]
        self.assertEqual(a_events, ["waiting", "canceled"], "终态之后不得再有状态事件")
        # 终态上重复取消/停止都是幂等拒绝
        self.assertFalse(coordinator.cancel_task("task-a"))
        snapshot = coordinator.snapshot()
        states = {t["task_id"]: t["status"] for t in snapshot["tasks"]}
        self.assertEqual(states["task-a"], "canceled")
        self.assertEqual(states["task-b"], "canceled")
        self.assertEqual(snapshot["status"], "stopped")


class ApiBusyLockTests(unittest.TestCase):
    """同一时间只有一个计划占用协调器：停止后才能登录或启动新计划。"""

    def _api(self):
        sys.path.insert(0, str(LOCAL))
        try:
            import app as local_app
        except Exception as exc:  # 缺 pywebview 等依赖时跳过，不影响核心测试
            self.skipTest("无法导入 local/app.py：%s" % exc)
        finally:
            sys.path.pop(0)
        self._app_module = local_app
        return local_app.Api(clock=TestWallClock())

    def test_busy_lock_blocks_login_and_start_plan_until_stopped(self):
        api = self._api()

        class FakePlan:
            plan_id = "plan-x"

            def __init__(self, status):
                self._status = status

            def snapshot(self):
                return {"status": self._status}

            def published_status(self):
                # 生产 Api 的 busy 判定只读这个不可变状态，不取协调器锁
                return self._status

        api._plan = FakePlan("running")
        self.assertTrue(api._is_busy_locked())
        login_res = api.login()
        self.assertFalse(login_res["ok"])
        self.assertIn("先停止", login_res["text"])
        plan_res = api.start_plan([activity("a", "2099-10-23 10:00:00",
                                            "2099-10-23 12:00:00", "2099-10-23 13:00:00")])
        self.assertFalse(plan_res["ok"])
        self.assertIn("先停止", plan_res["text"])

        api._plan = FakePlan("stopped")
        api._proc = None
        api._plan_procs = {}
        self.assertFalse(api._is_busy_locked(), "计划停止后必须解除占用")

    def test_run_plan_task_rewrites_past_open_time_to_now(self):
        """已开放场次的 at 必须换成现在，否则 grab.py 的重试窗口一次都不会进。"""
        api = self._api()  # 导入失败时跳过，不拖垮其他测试
        local_app = self._app_module
        from unittest import mock

        captured = {}

        def fake_worker(*args):
            captured["args"] = list(args)
            return ["python", "grab.py"]

        def no_spawn(*a, **k):
            captured["spawned"] = True
            raise RuntimeError("本测试不真正起子进程")

        spec = TaskSpec("task-a", "a", "已经开放的活动",
                        "2026-10-20 00:00:00", "2026-10-23 12:00:00", "2026-10-23 13:00:00")
        with mock.patch.object(local_app, "worker_command", fake_worker), \
                mock.patch.object(local_app.subprocess, "Popen", no_spawn):
            with self.assertRaises(RuntimeError):
                api._run_plan_task(spec, threading.Event(), lambda s, d=None: None)
        self.assertIn("spawned", captured)
        args = captured["args"]
        at_value = args[args.index("--at") + 1]
        self.assertNotEqual(at_value, "2026-10-20 00:00:00", "过期的开放时间不得原样传给脚本")
        rewritten = parse_timestamp(at_value)  # 必须仍是 grab.py 认的完整格式
        self.assertIsNotNone(rewritten)
        # 学校时间统一为带东八区时区的 datetime：比较也必须用同一口径，
        # 否则 naive/aware 混比会抛 TypeError（不是放宽断言）
        self.assertLessEqual(rewritten, datetime.now(rewritten.tzinfo).replace(microsecond=0))

    def test_run_plan_task_keeps_future_start_when_os_date_is_30_days_off(self):
        """未来场次必须原样传真实 sign_start：错误 OS 日期不得改写成「现在」。"""
        from unittest import mock
        from fixtures.business_clock import FakeBusinessClock
        from time_contract import school_time_text

        self._api()                 # 完成导入与跳过判定，并留下 _app_module
        local_app = self._app_module
        start = 1793100000.0
        clock = FakeBusinessClock(epoch=start, tick=0.0)
        api = local_app.Api(clock=clock)
        captured, statuses = {}, []

        def fake_worker(*args):
            captured["args"] = list(args)
            return ["python", "grab.py"]

        class FakeProc:
            pid = 4242
            returncode = 2

            def __init__(self):
                self.stdout = io.StringIO("")

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

        class FakeTree:
            proc = FakeProc()

            def terminate(self):
                return True

            def close(self):
                pass

        def fake_spawn(*a, **k):
            captured["spawned"] = True
            return FakeTree()

        real_start = school_time_text(start + 600.0)
        spec = TaskSpec("task-f", "f", "未来开放的活动",
                        real_start, "2026-10-23 12:00:00", "2026-10-23 13:00:00")
        # OS 墙钟快 30 天：业务时刻才是判断依据
        with mock.patch.object(local_app, "worker_command", fake_worker), \
                mock.patch.object(local_app, "spawn_owned_worker", fake_spawn), \
                mock.patch("time.time", return_value=start + 30 * 86400):
            api._run_plan_task(spec, threading.Event(),
                               lambda s, d=None: statuses.append(dict(d or {})))
        args = captured["args"]
        self.assertEqual(args[args.index("--at") + 1], real_start,
                         "未来场次不得被系统日期改写成现在")
        spawned = [item.get("worker_spawned_at") for item in statuses
                   if item.get("worker_spawned_at") is not None]
        self.assertEqual(spawned, [start], "worker 节点时间必须是业务北京时间")


class CoordinatorClockLossTests(unittest.TestCase):
    """C04：等待期间校时失效必须收成唯一 plan_done，不把线程永久挂在等待里。"""

    def test_clock_loss_while_waiting_emits_single_plan_done(self):
        start = 1793100000.0
        row = {"id": "a", "name": "未来场",
               "sign_start": school_time_text(start + 3600),
               "sign_end": school_time_text(start + 7200),
               "act_start": "2099-01-03 09:00:00", "act_end": "2099-01-03 11:00:00",
               "signed": False}
        spec = TaskSpec.from_activity(row, "t-a", now=start)
        events, runner_calls = [], []
        calls = {"n": 0}

        def flaky_now():
            calls["n"] += 1
            if calls["n"] > 1:
                raise ClockUnavailable("锚点超过持有上限未重校")
            return start

        def runner(spec, cancel_event, status):
            runner_calls.append(spec.task_id)
            return {"status": "success"}

        coord = PlanCoordinator("p-clock", [spec], runner,
                                lambda kind, payload: events.append((kind, payload)),
                                now=flaky_now, sleep=lambda seconds: None)
        coord.start()
        coord._thread.join(5)
        self.assertFalse(coord._thread.is_alive(), "调度线程不得因校时失效永久挂起")
        dones = [payload for kind, payload in events if kind == "plan_done"]
        self.assertEqual(len(dones), 1, "plan_done 必须恰一次")
        self.assertEqual(runner_calls, [], "校时失效后不得启动新 worker")
        tasks = [payload for kind, payload in events if kind == "task"]
        self.assertEqual(tasks[-1]["status"], "canceled")
        self.assertIn("北京时间", tasks[-1].get("reason", ""))
        self.assertEqual(dones[0]["summary"]["counts"].get("canceled"), 1)


class WorkerStopLifecycleTests(unittest.TestCase):
    """停止计划 → 按进程树终止 worker → canceled → 单次 plan_done 的生命周期。

    用会 spawn 孙进程的假 worker 模拟 PyInstaller onefile 形态：
    只 terminate 直接子进程时孙进程会活下来（界面「停不掉、还在重试」）。
    """

    # 假 worker：引导进程先拉一个孙进程（pid 落盘供断言），再自己阻塞。
    FAKE_WORKER = (
        "import os, subprocess, sys, time\n"
        "here = os.path.dirname(os.path.abspath(__file__))\n"
        "if os.environ.get('AUDIT_GRANDCHILD'):\n"
        "    time.sleep(300)\n"
        "else:\n"
        "    child = subprocess.Popen([sys.executable, os.path.abspath(__file__)],\n"
        "                             env=dict(os.environ, AUDIT_GRANDCHILD='1'))\n"
        "    open(os.path.join(here, 'child.pid'), 'w').write(str(child.pid))\n"
        "    print('[i] worker started', flush=True)\n"
        "    time.sleep(300)\n"
    )

    def _api(self):
        sys.path.insert(0, str(LOCAL))
        try:
            import app as local_app
        except Exception as exc:
            self.skipTest("无法导入 local/app.py：%s" % exc)
        finally:
            sys.path.pop(0)
        self._app_module = local_app
        return local_app.Api(clock=TestWallClock())

    def _start_and_wait_worker(self, api, source, needs_grandchild=True):
        """启动单任务计划并等到 worker 进程（含孙进程）真的跑起来。"""
        import tempfile
        from unittest import mock

        local_app = self._app_module
        fake = Path(tempfile.mkdtemp()) / "fake_worker.py"
        fake.write_text(source, encoding="utf-8")
        pidfile = fake.parent / "child.pid"
        pidfile.unlink(missing_ok=True)
        captured = []
        api._emit = lambda kind, payload: captured.append((kind, payload))
        # 本测试只验证停止生命周期：先给一个 valid 的登录结论，越过登录门禁
        api._last_check = {"result": "valid", "checked_at": 0.0}

        at = (datetime.now() - timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S")
        row = activity("a", at, "2026-10-23 12:00:00", "2026-10-23 13:00:00")
        # 计划接纳以本进程缓存为权威（R05）：本类验证停止生命周期，
        # 先把同源快照登记进缓存；接纳门禁由专门用例覆盖
        api._activities = {str(row["id"]): dict(row)}
        patcher = mock.patch.object(local_app, "worker_command",
                                    lambda *args: [sys.executable, str(fake)])
        with patcher:
            res = api.start_plan([row])
            self.assertTrue(res["ok"], res)
            plan_id = res["plan_id"]
            budget = time.time() + 10
            while time.time() < budget and (not api._plan_procs
                                            or (needs_grandchild and not pidfile.exists())):
                time.sleep(0.02)
            self.assertTrue(api._plan_procs, "worker 进程必须先跑起来")
            if needs_grandchild:
                self.assertTrue(pidfile.exists(), "孙进程 pid 文件必须出现")
            task_id = next(iter(api._plan_procs))
            grandchild_pid = int(pidfile.read_text().strip()) if needs_grandchild else 0
        return patcher, captured, plan_id, task_id, grandchild_pid

    @staticmethod
    def _pid_alive(pid):
        import subprocess
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid],
                             capture_output=True).stdout
        return str(pid).encode() in out

    def test_stop_kills_tree_cancels_task_and_emits_single_plan_done(self):
        api = self._api()
        patcher, captured, plan_id, task_id, grandchild_pid = \
            self._start_and_wait_worker(api, self.FAKE_WORKER)

        proc = api._plan_procs[task_id]
        t0 = time.time()
        with patcher:
            first = api.stop_plan(plan_id)
            second = api.stop_plan(plan_id)      # 幂等：重复停止不得报错或挂起
        while proc.poll() is None and time.time() - t0 < 2.0:
            time.sleep(0.02)
        elapsed = time.time() - t0

        self.assertTrue(first["ok"], first)
        # 第二次调用要么仍 ok，要么计划已收尾（人话提示），绝不能是异常/挂起
        self.assertTrue(second["ok"] or "没有这个计划" in second["text"], second)
        self.assertIsNotNone(proc.poll(), "worker 直接进程必须死掉")
        self.assertLessEqual(elapsed, 1.0,
                             "确认停止到 worker 终止必须 ≤1 秒（实测 %.0fms）" % (elapsed * 1000))
        # 孙进程也必须被树终止收掉——这就是 onefile 形态下「停不掉」的根因
        budget = time.time() + 3
        while time.time() < budget and self._pid_alive(grandchild_pid):
            time.sleep(0.05)
        self.assertFalse(self._pid_alive(grandchild_pid), "孙进程必须随进程树终止")

        done = [p for kind, p in captured if kind == "plan_done"]
        self.assertEqual(len(done), 1, "plan_done 只能发一次：%r" % done)
        self.assertEqual(done[0]["status"], "stopped")
        task_states = {p["task_id"]: p["status"] for kind, p in captured if kind == "task"}
        self.assertEqual(task_states[task_id], "canceled")
        self.assertEqual(api._plan_procs, {}, "计划结束后进程表必须清空")
        self.assertFalse(api._is_busy_locked(), "结束后必须解除 busy 锁")

    def test_worker_failure_exit_code_is_preserved(self):
        """worker 报名失败退出码 2 必须原样保留为任务 code，不得被收尾掩盖。"""
        api = self._api()
        source = ("import sys, time\n"
                  "time.sleep(0.5)\n"
                  "print('[X] 没抢到：名额已满', flush=True)\n"
                  "sys.exit(2)\n")
        patcher, captured, plan_id, task_id, _ = \
            self._start_and_wait_worker(api, source, needs_grandchild=False)

        budget = time.time() + 10
        while time.time() < budget and not any(k == "plan_done" for k, _ in captured):
            time.sleep(0.05)
        done = [p for kind, p in captured if kind == "plan_done"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["status"], "failed")
        self.assertFalse(done[0]["ok"])
        task = [p for kind, p in captured
                if kind == "task" and p["task_id"] == task_id][-1]
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["code"], 2, "失败退出码 2 必须透传给界面")
        self.assertIn("名额已满", task["reason"])
        self.assertEqual(api._plan_procs, {})
        self.assertFalse(api._is_busy_locked())


class SignupModeClassificationTests(unittest.TestCase):
    """双模式契约：预报名 / 立即报名（抢名额）/ 已截止三分类与门禁。"""

    NOW = 1792000000.0  # 固定「当前时间」，分类必须可注入，不偷跑真实时钟

    def test_three_modes_table_driven(self):
        """sign_end 已过→closed；sign_start 在未来→pre_register；其余→instant_signup。"""
        cases = [
            # (sign_start, sign_end, 期望模式, 说明)
            ("2099-01-01 09:00:00", "2099-01-02 09:00:00", "pre_register", "未来开放=预报名"),
            ("2099-01-01 09:00:00", "", "pre_register", "缺截止不挡预报名"),
            ("2000-01-01 09:00:00", "2099-01-02 09:00:00", "instant_signup", "已开放未截止=立即报名"),
            ("2000-01-01 09:00:00", "", "instant_signup", "已开放且缺截止：安全回退不当已截止"),
            ("2000-01-01 09:00:00", "2000-01-02 09:00:00", "closed", "报名截止已过=只读"),
            ("", "2000-01-02 09:00:00", "closed", "开放时间缺失但截止已过仍是截止"),
            ("", "", "instant_signup", "两项都缺失：按已开放安全回退"),
        ]
        for sign_start, sign_end, want, note in cases:
            with self.subTest(note=note):
                self.assertEqual(
                    classify_signup_mode(sign_start, sign_end, now=self.NOW), want)

    def test_task_spec_carries_signup_mode(self):
        """TaskSpec 在计划创建时定格模式，as_dict 随快照输出（重载不丢）。"""
        future = TaskSpec.from_activity(
            {"id": "f", "name": "未来场", "sign_start": "2099-01-01 09:00:00",
             "sign_end": "2099-01-02 09:00:00", "act_start": "2099-01-03 09:00:00",
             "act_end": "2099-01-03 11:00:00"}, "task-f", now=self.NOW)
        open_now = TaskSpec.from_activity(
            {"id": "o", "name": "开放场", "sign_start": "2000-01-01 09:00:00",
             "sign_end": "2099-01-02 09:00:00", "act_start": "2099-01-03 09:00:00",
             "act_end": "2099-01-03 11:00:00"}, "task-o", now=self.NOW)
        closed = TaskSpec.from_activity(
            {"id": "c", "name": "截止场", "sign_start": "2000-01-01 09:00:00",
             "sign_end": "2000-01-02 09:00:00", "act_start": "2099-01-03 09:00:00",
             "act_end": "2099-01-03 11:00:00"}, "task-c", now=self.NOW)
        self.assertEqual(future.as_dict()["signup_mode"], "pre_register")
        self.assertEqual(open_now.as_dict()["signup_mode"], "instant_signup")
        self.assertEqual(closed.as_dict()["signup_mode"], "closed")

    def test_validate_plan_rejects_sign_end_passed_only(self):
        """报名截止是资格门禁；举办时间过去不得拦截（举办时间只管冲突）。"""
        # 报名截止已过：sign_end_passed，且错误带活动定位与截止时间
        closed_row = dict(activity("c", "2000-01-01 09:00:00",
                                   "2099-01-03 09:00:00", "2099-01-03 11:00:00"),
                          sign_end="2000-01-02 09:00:00")
        result = validate_plan([closed_row], now=self.NOW)
        self.assertFalse(result["ok"])
        hit = next(item for item in result["errors"] if item["kind"] == "sign_end_passed")
        self.assertEqual(hit["activity_id"], "c")
        self.assertEqual(hit["sign_end"], "2000-01-02 09:00:00")
        # 举办时间早已结束但报名窗口有效：不得因举办时间拦截
        open_row = dict(activity("o", "2000-01-01 09:00:00",
                                 "2000-01-01 10:00:00", "2000-01-01 11:00:00",
                                 signed=False),
                        sign_end="2099-01-02 09:00:00")
        result = validate_plan([open_row], now=self.NOW)
        self.assertTrue(result["ok"], "举办时间只用于冲突，不能当报名资格截止")
        self.assertEqual([e for e in result["errors"] if e["kind"] == "sign_end_passed"], [])

    def test_mixed_plan_groups_open_batch_first(self):
        """混合计划：已开放批次排最前立即执行，未来批次按原定时刻等待。"""
        future_at = datetime.fromtimestamp(self.NOW + 3600).strftime("%Y-%m-%d %H:%M:%S")
        past_at = datetime.fromtimestamp(self.NOW - 3600).strftime("%Y-%m-%d %H:%M:%S")
        act_s, act_e = "2099-01-03 09:00:00", "2099-01-03 11:00:00"
        specs = [
            TaskSpec.from_activity({"id": "f", "name": "未来场", "sign_start": future_at,
                                    "sign_end": "", "act_start": act_s, "act_end": act_e},
                                   "task-f", now=self.NOW),
            TaskSpec.from_activity({"id": "o", "name": "开放场", "sign_start": past_at,
                                    "sign_end": "", "act_start": act_s, "act_end": act_e},
                                   "task-o", now=self.NOW),
        ]
        groups = group_by_sign_start(specs)
        self.assertEqual([[s.task_id for s in g] for g in groups],
                         [["task-o"], ["task-f"]],
                         "已开放批次必须先于未来批次执行（开放优先）")
        self.assertEqual(specs[0].signup_mode, "pre_register")
        self.assertEqual(specs[1].signup_mode, "instant_signup")


class PlanRuntimeDetailTests(unittest.TestCase):
    """plan snapshot 保留运行时 detail；晚到事件不覆盖已收到的 checkpoint。"""

    def _wait(self, coordinator):
        thread = coordinator._thread
        self.assertIsNotNone(thread)
        thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_snapshot_keeps_runtime_checkpoints_and_mode(self):
        """重载恢复的快照必须带真实 checkpoint、模式与策略字段。"""
        at = (datetime.now() - timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S")
        spec = TaskSpec.from_activity(
            {"id": "o", "name": "开放场", "sign_start": at, "sign_end": "2099-01-02 09:00:00",
             "act_start": "2099-01-03 09:00:00", "act_end": "2099-01-03 11:00:00"},
            "task-o", retry_for_s=600, max_attempts=3)
        events = []

        def emit(kind, payload):
            events.append((kind, payload))

        def runner(spec, cancel_event, status):
            status("submitting", {"run_started_at": 1000.0})
            status("submitting", {"page_opened_at": 1002.5})
            status("submitting", {"watch_started_at": 1003.0})
            status("submitting", {"retry_deadline": "1792900000"})
            return {"status": "failed", "code": 2, "reason": "没抢到：名额已满",
                    "stop_reason": "retry_duration", "attempts": 2}

        coordinator = PlanCoordinator("plan-snap", [spec], runner, emit)
        coordinator.start()
        self._wait(coordinator)
        snap = coordinator.snapshot()
        task = snap["tasks"][0]
        self.assertEqual(task["run_started_at"], 1000.0)
        self.assertEqual(task["page_opened_at"], 1002.5)
        self.assertEqual(task["watch_started_at"], 1003.0)
        self.assertEqual(task["retry_deadline"], "1792900000")
        self.assertEqual(task["signup_mode"], "instant_signup")
        self.assertEqual(task["sign_end"], "2099-01-02 09:00:00")
        self.assertEqual(task["retry_for_s"], 600)
        self.assertEqual(task["attempts"], 2)
        # task 事件与快照同构：界面实时/恢复走同一份字段
        final = [p for kind, p in events if kind == "task"][-1]
        self.assertEqual(final["page_opened_at"], 1002.5)
        self.assertEqual(final["signup_mode"], "instant_signup")
        self.assertIn("sign_end", final)
        self.assertIn("retry_for_s", final)

    def test_late_events_do_not_overwrite_checkpoints(self):
        """checkpoint 先到先得：重放/迟到的同名字段不得改写已发生的节点。"""
        at = (datetime.now() - timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S")
        spec = TaskSpec.from_activity(
            {"id": "o", "name": "开放场", "sign_start": at, "sign_end": "",
             "act_start": "2099-01-03 09:00:00", "act_end": "2099-01-03 11:00:00"},
            "task-o")

        def runner(spec, cancel_event, status):
            status("submitting", {"run_started_at": 1000.0, "page_opened_at": 1002.5})
            # 迟到/重放：checkpoint 重复值与空值都不得覆盖
            status("submitting", {"run_started_at": 9999.0, "page_opened_at": None})
            # 进度字段跟随最新；晚到的纯状态事件不得抹掉 checkpoint
            status("submitting", {"attempts": 3, "last_reason": "code:0 报名人数已满"})
            return {"status": "failed", "code": 2, "reason": "没抢到：名额已满"}

        coordinator = PlanCoordinator("plan-late", [spec], runner, lambda *a: None)
        coordinator.start()
        self._wait(coordinator)
        task = coordinator.snapshot()["tasks"][0]
        self.assertEqual(task["run_started_at"], 1000.0, "checkpoint 不得被迟到同名字段覆盖")
        self.assertEqual(task["page_opened_at"], 1002.5, "checkpoint 空值不得抹掉已收到的事件")
        self.assertEqual(task["attempts"], 3, "进度字段应跟随最新一次提交")
        self.assertEqual(task["last_reason"], "code:0 报名人数已满")


    def test_mixed_plan_runs_open_first_and_future_waits(self):
        """混合计划：已开放场次立即执行到终态，未来场次保持等待；两场模式与时间线独立。"""
        past_at = (datetime.now() - timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S")
        act_s, act_e = "2099-01-03 09:00:00", "2099-01-03 11:00:00"
        specs = [
            TaskSpec.from_activity({"id": "f", "name": "未来场", "sign_start": "2099-01-01 09:00:00",
                                    "sign_end": "", "act_start": act_s, "act_end": act_e},
                                   "task-f"),
            TaskSpec.from_activity({"id": "o", "name": "开放场", "sign_start": past_at,
                                    "sign_end": "", "act_start": act_s, "act_end": act_e},
                                   "task-o"),
        ]
        events = []
        open_done = threading.Event()

        def emit(kind, payload):
            events.append((kind, payload))
            if kind == "task" and payload.get("task_id") == "task-o" \
                    and payload.get("status") == "success":
                open_done.set()

        def runner(spec, cancel_event, status):
            status("submitting", {"run_started_at": 1000.0})
            return {"status": "success", "code": 0}

        coordinator = PlanCoordinator("plan-mixed", specs, runner, emit)
        coordinator.start()
        self.assertTrue(open_done.wait(2), "已开放场次应立即执行完成，不被未来批次阻塞")
        snap = coordinator.snapshot()
        states = {t["task_id"]: t["status"] for t in snap["tasks"]}
        self.assertEqual(states["task-o"], "success", "已开放场次立即执行")
        self.assertEqual(states["task-f"], "waiting", "未来场次保持等待原定开放")
        by_id = {t["task_id"]: t for t in snap["tasks"]}
        self.assertEqual(by_id["task-o"]["signup_mode"], "instant_signup")
        self.assertEqual(by_id["task-f"]["signup_mode"], "pre_register")
        self.assertIn("run_started_at", by_id["task-o"], "已开放场次的执行 checkpoint 已落")
        self.assertNotIn("run_started_at", by_id["task-f"], "未来场次尚未启动，无执行 checkpoint")
        # 收尾：停止计划，未来场次取消
        self.assertTrue(coordinator.stop())
        self._wait(coordinator)
        done = [p for kind, p in events if kind == "plan_done"][-1]
        self.assertEqual(done["status"], "stopped")


class PreopenSchedulerTests(unittest.TestCase):
    """预报名提前准备与独立调度（fake clock 注入，不起真实 worker）。

    fake clock：now() 读虚拟时刻，sleep(s) 推进虚拟时刻——调度线程进入
    join 后时钟冻结，因此批内 runner 读到的时钟值就是该批的准备时刻；
    旧实现（等到 sign_start 才启动）在本组断言下会读到 sign_start 而失败。
    """

    T0 = 1792000000.0

    def _clock(self):
        return {"t": self.T0}

    @staticmethod
    def _fmt(ts):
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

    def _activity_row(self, aid, sign_start_ts):
        return {"id": aid, "name": aid, "sign_start": self._fmt(sign_start_ts),
                "sign_end": "", "act_start": "2099-01-03 09:00:00",
                "act_end": "2099-01-03 11:00:00"}

    def test_future_task_launches_at_prepare_time_not_sign_start(self):
        """未来场次在 sign_start-100 秒启动 worker，而不是等到 sign_start。"""
        clock = self._clock()
        sign_start_ts = clock["t"] + 1000

        def fake_now():
            return clock["t"]

        def fake_sleep(seconds):
            clock["t"] += seconds
            time.sleep(0)   # 让出 GIL：纯推进虚拟时钟的自旋会在套件高负载下饿死主线程（取消/断言），偶发假失败

        spec = TaskSpec.from_activity(self._activity_row("f", sign_start_ts),
                                      "task-f", lead_s=100, now=clock["t"])
        launches = []

        def runner(spec, cancel_event, status):
            launches.append((spec.task_id, fake_now()))
            return {"status": "success", "code": 0}

        coordinator = PlanCoordinator("plan-lead", [spec], runner, lambda *a: None,
                                      now=fake_now, sleep=fake_sleep)
        coordinator.start()
        thread = coordinator._thread
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(launches, [("task-f", sign_start_ts - 100)],
                         "未来场次必须在准备时刻（sign_start-lead）启动 worker")

    def test_same_sign_start_tasks_launch_together(self):
        """同一开放时间的两场在同一批次同时启动（同一准备时刻）。"""
        clock = self._clock()
        sign_start_ts = clock["t"] + 500

        def fake_now():
            return clock["t"]

        def fake_sleep(seconds):
            clock["t"] += seconds
            time.sleep(0)   # 让出 GIL：纯推进虚拟时钟的自旋会在套件高负载下饿死主线程（取消/断言），偶发假失败

        specs = [
            TaskSpec.from_activity(self._activity_row("f1", sign_start_ts), "task-f1", lead_s=100, now=clock["t"]),
            TaskSpec.from_activity(self._activity_row("f2", sign_start_ts), "task-f2", lead_s=100, now=clock["t"]),
        ]
        launches = []

        def runner(spec, cancel_event, status):
            launches.append((spec.task_id, fake_now()))
            return {"status": "success", "code": 0}

        coordinator = PlanCoordinator("plan-same", specs, runner, lambda *a: None,
                                      now=fake_now, sleep=fake_sleep)
        coordinator.start()
        thread = coordinator._thread
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual([t for t, _ in launches], ["task-f1", "task-f2"])
        self.assertEqual({ts for _, ts in launches}, {sign_start_ts - 100},
                         "同刻开放的两场必须同批同时启动")

    def test_retrying_instant_does_not_block_future_prepare(self):
        """立即场持续重试时，未来场仍能在自己的准备时刻启动（互不阻塞）。"""
        clock = self._clock()
        sign_start_ts = clock["t"] + 300
        past_at = clock["t"] - 10

        def fake_now():
            return clock["t"]

        def fake_sleep(seconds):
            clock["t"] += seconds
            time.sleep(0)   # 让出 GIL：纯推进虚拟时钟的自旋会在套件高负载下饿死主线程（取消/断言），偶发假失败

        specs = [
            TaskSpec.from_activity(self._activity_row("f", sign_start_ts), "task-f", lead_s=100, now=clock["t"]),
            TaskSpec.from_activity(self._activity_row("o", past_at), "task-o", now=clock["t"]),
        ]
        gate = threading.Event()
        launches = []

        def runner(spec, cancel_event, status):
            if spec.task_id == "task-o":
                gate.wait(3)          # 模拟立即场长时间重试：占住第一批
                return {"status": "canceled", "reason": "测试收尾"}
            launches.append((spec.task_id, fake_now()))
            return {"status": "success", "code": 0}

        coordinator = PlanCoordinator("plan-mix", specs, runner, lambda *a: None,
                                      now=fake_now, sleep=fake_sleep)
        coordinator.start()
        # 未来场必须在准备时刻启动；旧实现会在批次 join 处被立即场阻塞
        budget = time.time() + 5
        while time.time() < budget and not launches:
            time.sleep(0.02)
        self.assertEqual([t for t, _ in launches], ["task-f"],
                         "立即场重试期间，未来场必须在自己的准备时刻启动")
        self.assertEqual(launches[0][1], sign_start_ts - 100)
        states = {t["task_id"]: t["status"] for t in coordinator.snapshot()["tasks"]}
        self.assertEqual(states["task-o"], "submitting", "立即场仍在重试中，不受影响")
        gate.set()
        thread = coordinator._thread
        thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_cancel_before_prepare_skips_launch(self):
        """等待中的未来场被取消后：不再启动 worker，状态保持 canceled。

        用事件确定次序：fake_sleep 首次到达即发 entered 并停住，主线程确认
        「已进等待且未到准备时刻」后再取消，最后放行时钟推进——不再依赖
        线程调度竞争（礼让只是减轻自旋，不构成时序保证）。"""
        clock = self._clock()
        sign_start_ts = clock["t"] + 600
        entered, advance = threading.Event(), threading.Event()

        def fake_now():
            return clock["t"]

        def fake_sleep(seconds):
            clock["t"] += seconds
            time.sleep(0)   # 保留礼让：避免放行后的等待循环饿死主线程断言
            entered.set()
            advance.wait(5)

        spec = TaskSpec.from_activity(self._activity_row("f", sign_start_ts),
                                      "task-f", lead_s=100, now=clock["t"])
        launches = []

        def runner(spec, cancel_event, status):
            launches.append((spec.task_id, fake_now()))
            return {"status": "success", "code": 0}

        coordinator = PlanCoordinator("plan-cancel", [spec], runner, lambda *a: None,
                                      now=fake_now, sleep=fake_sleep)
        coordinator.start()
        try:
            self.assertTrue(entered.wait(5), "调度线程必须先进入等待循环")
            self.assertLess(fake_now(), sign_start_ts - 100,
                            "取消必须发生在准备时刻之前")
            self.assertTrue(coordinator.cancel_task("task-f"))
            advance.set()
        finally:
            advance.set()            # 失败路径也放行，避免测试线程遗留
            thread = coordinator._thread
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(launches, [], "已取消的场次不得再启动 worker")
        snap = coordinator.snapshot()
        self.assertEqual(snap["tasks"][0]["status"], "canceled")
        # 既有 _plan_status 语义：唯一任务被取消（未停止）时计划按 failed 收尾
        self.assertEqual(snap["status"], "failed")


class RetryPolicyContractTests(unittest.TestCase):
    """TaskSpec 重试策略字段、计划快照与 task 事件 telemetry 透传。"""

    def test_task_spec_defaults_and_policy_roundtrip(self):
        """默认 120 秒/不限次数保持老调用兼容；显式策略进 as_dict。"""
        default = TaskSpec("t", "a", "n", "2026-10-23 10:00:00",
                           "2026-10-23 12:00:00", "2026-10-23 13:00:00")
        self.assertEqual(default.sign_end, "")
        self.assertEqual(default.retry_for_s, 120)
        self.assertEqual(default.max_attempts, 0)

        spec = TaskSpec.from_activity(
            {"id": "a", "name": "a", "sign_start": "2026-10-23 10:00:00",
             "sign_end": "2026-10-24 10:00:00",
             "act_start": "2026-10-23 12:00:00", "act_end": "2026-10-23 13:00:00"},
            "task-a", headed=True, retry_for_s=1800, max_attempts=5)
        payload = spec.as_dict()
        self.assertEqual(payload["sign_end"], "2026-10-24 10:00:00")
        self.assertEqual(payload["retry_for_s"], 1800)
        self.assertEqual(payload["max_attempts"], 5)
        self.assertTrue(payload["headed"])

    def test_snapshot_and_task_events_carry_policy_and_telemetry(self):
        """快照保留策略（重载不丢）；submitting telemetry 与终态 stop_reason 透传。"""
        now = datetime.now().replace(microsecond=0)
        at = (now - timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S")
        spec = TaskSpec("task-a", "a", "a", at, "2026-10-23 12:00:00",
                        "2026-10-23 13:00:00",
                        sign_end="2026-10-24 10:00:00", retry_for_s=600, max_attempts=3)
        events = []

        def emit(kind, payload):
            events.append((kind, payload))

        def runner(spec, cancel_event, status):
            status("submitting", {"attempts": 1, "last_reason": "code:0 报名人数已满"})
            status("submitting", {"attempts": 2, "last_reason": "code:0 报名人数已满"})
            return {"status": "failed", "code": 2, "reason": "没抢到：名额已满",
                    "stop_reason": "max_attempts", "attempts": 2,
                    "retry_deadline": "1792900000", "sign_end": spec.sign_end}

        coordinator = PlanCoordinator("plan-retry", [spec], runner, emit)
        snap = coordinator.snapshot()
        self.assertEqual(snap["tasks"][0]["retry_for_s"], 600, "快照必须带策略")
        self.assertEqual(snap["tasks"][0]["max_attempts"], 3)
        self.assertEqual(snap["tasks"][0]["sign_end"], "2026-10-24 10:00:00")

        coordinator.start()
        thread = coordinator._thread
        thread.join(2)
        self.assertFalse(thread.is_alive())

        task_events = [p for kind, p in events if kind == "task"]
        submitting = [p for p in task_events
                      if p["status"] == "submitting" and "attempts" in p]
        self.assertEqual([p["attempts"] for p in submitting], [1, 2],
                         "每次 signUp 推一次 attempts")
        final = task_events[-1]
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["stop_reason"], "max_attempts")
        self.assertEqual(final["attempts"], 2)
        self.assertEqual(final["retry_deadline"], "1792900000")
        self.assertEqual(final["sign_end"], "2026-10-24 10:00:00")


class BurstPolicyFreezeTests(unittest.TestCase):
    """黄金风暴自适应扩窗不得改写计划冻结字段（R04/R10 边界）。"""

    def test_task_spec_frozen_fields_survive_adaptive_extension(self):
        """retry_started_at/retry_for_s/sign_end/max_attempts 接纳时冻结，扩窗不改写。"""
        now = datetime.now().replace(microsecond=0)
        at = (now - timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S")
        spec = TaskSpec("task-a", "a", "a", at, "2026-10-23 12:00:00",
                        "2026-10-23 13:00:00",
                        sign_end="2026-10-24 10:00:00", retry_for_s=600,
                        max_attempts=3)
        events = []

        def emit(kind, payload):
            events.append((kind, payload))

        def runner(spec, cancel_event, status):
            # 模拟 worker 上报自适应扩窗后的更大 retry_deadline
            status("submitting", {"attempts": 1,
                                  "retry_deadline": "1792900500",
                                  "last_reason": "404 接口未就绪/限流"})
            return {"status": "failed", "code": 2,
                    "stop_reason": "retry_duration", "attempts": 1,
                    "retry_deadline": "1792900500", "sign_end": spec.sign_end}

        coordinator = PlanCoordinator("plan-freeze", [spec], runner, emit)
        snap = coordinator.snapshot()
        frozen = snap["tasks"][0]
        self.assertEqual(frozen["retry_for_s"], 600)
        self.assertEqual(frozen["max_attempts"], 3)
        self.assertEqual(frozen["sign_end"], "2026-10-24 10:00:00")

        coordinator.start()
        coordinator._thread.join(2)
        self.assertFalse(coordinator._thread.is_alive())

        # 扩窗只改运行时 detail，不改快照冻结字段
        final_snap = coordinator.snapshot()["tasks"][0]
        self.assertEqual(final_snap["retry_for_s"], 600,
                         "自适应扩窗不得改写计划冻结的 retry_for_s")
        self.assertEqual(final_snap["max_attempts"], 3,
                         "自适应扩窗不得改写计划冻结的 max_attempts")
        self.assertEqual(final_snap["sign_end"], "2026-10-24 10:00:00",
                         "自适应扩窗不得改写计划冻结的 sign_end")

    def test_retry_deadline_can_grow_but_not_shrink(self):
        """扩窗允许 deadline 变大；迟到/重放的更小值不得改小。"""
        now = datetime.now().replace(microsecond=0)
        at = (now - timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S")
        spec = TaskSpec("task-b", "b", "b", at, "2026-10-23 12:00:00",
                        "2026-10-23 13:00:00",
                        sign_end="2026-10-24 10:00:00", retry_for_s=600,
                        max_attempts=0)
        events = []

        def emit(kind, payload):
            events.append((kind, payload))

        def runner(spec, cancel_event, status):
            status("submitting", {"attempts": 1, "retry_deadline": "1792900000"})
            # 自适应扩窗：deadline 变大（允许）
            status("submitting", {"attempts": 2, "retry_deadline": "1792900500"})
            # 迟到/重放的更小值（拒绝）
            status("submitting", {"attempts": 3, "retry_deadline": "1792899900"})
            return {"status": "failed", "code": 2,
                    "stop_reason": "retry_duration", "attempts": 3,
                    "retry_deadline": "1792899900"}

        coordinator = PlanCoordinator("plan-grow", [spec], runner, emit)
        coordinator.start()
        coordinator._thread.join(2)
        self.assertFalse(coordinator._thread.is_alive())

        task_events = [p for kind, p in events if kind == "task"]
        final = task_events[-1]
        self.assertEqual(str(final.get("retry_deadline")), "1792900500",
                         "deadline 只能单调变大，迟到的更小值不得改小")


class PlanAdmissionTests(unittest.TestCase):
    """R05/R11：计划接纳只信本进程缓存，并且同一 request_id 只接纳一次。"""

    def _api(self):
        sys.path.insert(0, str(LOCAL))
        try:
            import app as local_app
        except Exception as exc:
            self.skipTest("无法导入 local/app.py：%s" % exc)
        finally:
            sys.path.pop(0)
        self._app = local_app
        return local_app.Api(clock=TestWallClock())

    def _seed(self, api, **override):
        item = {"id": "a1", "name": "活动", "sign_start": "2026-01-01 09:00:00",
                "sign_end": "2099-01-02 09:00:00",
                "act_start": "2099-01-03 09:00:00", "act_end": "2099-01-03 11:00:00",
                "signed": False}
        item.update(override)
        api._activities = {item["id"]: dict(item)}
        api._last_check = {"result": "valid", "checked_at": 0.0}
        return item

    def _fake_coordinator(self, app):
        from unittest import mock
        inst = mock.Mock()
        inst.plan_id = "plan-1"
        inst.snapshot = mock.Mock(return_value={"status": "running"})
        return inst

    def test_cached_signed_beats_stale_frontend(self):
        """缓存已报名、前端说未报名：拒绝，且不创建协调器。"""
        api = self._api()
        app = self._app
        self._seed(api, signed=True)
        with mock.patch.object(app, "PlanCoordinator") as coordinator:
            res = api.start_plan([{"id": "a1", "signed": False}])
        self.assertFalse(res["ok"])
        self.assertIn("已变化", res["text"])
        self.assertEqual(res["errors"][0]["kind"], "activity_changed")
        coordinator.assert_not_called()
        self.assertIsNone(api._plan)

    def test_cached_closed_beats_stale_frontend_end(self):
        """缓存已截止、前端给未来截止：拒绝，不能拿旧快照开始。"""
        api = self._api()
        app = self._app
        self._seed(api, sign_end="2020-01-02 09:00:00")
        with mock.patch.object(app, "PlanCoordinator") as coordinator:
            res = api.start_plan([{"id": "a1", "sign_end": "2099-01-02 09:00:00"}])
        self.assertFalse(res["ok"])
        self.assertIn("已变化", res["text"])
        coordinator.assert_not_called()

    def test_unknown_and_malformed_items_are_recoverable(self):
        api = self._api()
        app = self._app
        self._seed(api)
        cases = [
            ("unknown_activity", [{"id": "nope"}]),
            ("bad_item", ["not-a-dict"]),
            ("missing_id", [{"name": "没有 id"}]),
            ("duplicate_id", [{"id": "a1"}, {"id": "a1"}]),
        ]
        for kind, items in cases:
            with self.subTest(kind=kind):
                with mock.patch.object(app, "PlanCoordinator") as coordinator:
                    res = api.start_plan(items)
                self.assertFalse(res["ok"], "坏输入必须拒绝")
                self.assertIn(kind, [error.get("kind") for error in res["errors"]])
                coordinator.assert_not_called()
                self.assertIsNone(api._plan, "拒绝路径不得创建计划")

    def test_valid_snapshot_is_accepted_once(self):
        api = self._api()
        app = self._app
        item = self._seed(api)
        inst = self._fake_coordinator(app)
        with mock.patch.object(app, "PlanCoordinator", return_value=inst):
            res = api.start_plan([{"id": item["id"]}])
        self.assertTrue(res["ok"], res)
        inst.start.assert_called_once_with()
        self.assertEqual(api._plan, inst)

    def test_same_request_id_starts_only_once(self):
        """R11：同 request_id 并发只启动一次；重放返回原 plan_id。"""
        api = self._api()
        app = self._app
        item = self._seed(api)
        inst = self._fake_coordinator(app)

        def factory(plan_id, *args, **kwargs):
            # 真实协调器持有传进来的 plan_id：对账时按它判断「这个计划还在跑」
            inst.plan_id = plan_id
            return inst

        with mock.patch.object(app, "PlanCoordinator", side_effect=factory):
            first = api.start_plan([{"id": item["id"]}], request_id="rid-1")
            second = api.start_plan([{"id": item["id"]}], request_id="rid-1")
        self.assertTrue(first["ok"])
        self.assertEqual(second.get("plan_id"), first["plan_id"])
        self.assertTrue(second.get("duplicate"))
        inst.start.assert_called_once_with()
        state = api.start_request_state("rid-1")
        self.assertEqual(state["state"], "accepted")
        self.assertEqual(state["plan_id"], first["plan_id"])
        self.assertTrue(state["running"])

    def test_cancel_pending_prevents_acceptance(self):
        """取消先到：最终预留必须拒绝，零 worker、零计划。"""
        api = self._api()
        app = self._app
        item = self._seed(api)
        api._remember_start("rid-2", "pending")
        self.assertTrue(api.cancel_start_request("rid-2")["ok"])
        with mock.patch.object(app, "PlanCoordinator") as coordinator:
            res = api.start_plan([{"id": item["id"]}], request_id="rid-2")
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("canceled"))
        coordinator.assert_not_called()
        self.assertIsNone(api._plan)

    def test_cancel_after_acceptance_is_refused(self):
        api = self._api()
        app = self._app
        item = self._seed(api)
        inst = self._fake_coordinator(app)
        with mock.patch.object(app, "PlanCoordinator", return_value=inst):
            api.start_plan([{"id": item["id"]}], request_id="rid-3")
        res = api.cancel_start_request("rid-3")
        self.assertFalse(res["ok"])
        self.assertEqual(res["state"], "accepted")



class StartAdmissionConcurrencyTests(unittest.TestCase):
    """C04：同 ID 查重与占位原子，取消与接纳有唯一线性化点。"""

    def _api(self):
        sys.path.insert(0, str(LOCAL))
        try:
            import app as local_app
        except Exception as exc:
            self.skipTest("无法导入 local/app.py：%s" % exc)
        finally:
            sys.path.pop(0)
        self._app = local_app
        return local_app.Api(clock=TestWallClock())

    def _seed(self, api):
        item = {"id": "a1", "name": "活动", "sign_start": "2026-01-01 09:00:00",
                "sign_end": "2099-01-02 09:00:00",
                "act_start": "2099-01-03 09:00:00", "act_end": "2099-01-03 11:00:00",
                "signed": False}
        api._activities = {"a1": dict(item)}
        api._last_check = {"result": "valid", "checked_at": 0.0}
        return item

    def _inst(self, plan_id):
        inst = mock.Mock()
        inst.plan_id = plan_id
        inst.snapshot = mock.Mock(return_value={"status": "running", "tasks": []})
        return inst

    def test_concurrent_same_id_runs_inner_once(self):
        """两个真正并发的同 ID 请求：只有一个执行 inner。"""
        api = self._api()
        app = self._app
        item = self._seed(api)
        real_inner = api._start_plan_inner
        entered = threading.Barrier(2, timeout=0.5)
        calls = []

        def slow_inner(items, headed, policy, rid):
            calls.append(rid)
            try:
                entered.wait()
            except threading.BrokenBarrierError:
                pass
            return real_inner(items, headed, policy, rid)

        inst = self._inst("p1")
        results = [None, None]

        def starter(index):
            results[index] = api.start_plan([{"id": item["id"]}], request_id="rid-c")

        with mock.patch.object(api, "_start_plan_inner", side_effect=slow_inner), \
                mock.patch.object(app, "PlanCoordinator", return_value=inst):
            threads = [threading.Thread(target=starter, args=(i,), daemon=True) for i in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
            self.assertFalse(any(thread.is_alive() for thread in threads), "并发接纳不得死锁")
        self.assertEqual(calls, ["rid-c"], "只有占位者能执行 inner")
        self.assertEqual(inst.start.call_count, 1, "协调器只能启动一次")
        states = [r and (r.get("pending") and "pending" or ("accepted" if r.get("ok") else "rejected"))
                  for r in results]
        self.assertIn("accepted", states)
        self.assertIn("pending", states, "后到请求应拿到在途答复")
        record = api.start_request_state("rid-c")
        self.assertEqual(record["state"], "accepted")
        accepted = next(r for r in results if r and r.get("ok") and not r.get("pending"))
        self.assertEqual(record["plan_id"], accepted["plan_id"],
                         "登记的计划号必须与实际返回的一致")

    def test_cancel_before_reservation_wins(self):
        """取消停在预留前：放行后必须拒绝，零启动。"""
        api = self._api()
        app = self._app
        item = self._seed(api)
        entered, release = threading.Event(), threading.Event()
        made = []

        def gate(plan_id, specs, runner, emit, now=None):
            entered.set()
            release.wait(5)
            inst = self._inst(plan_id)
            made.append(inst)
            return inst

        holder = {}

        def starter():
            holder["res"] = api.start_plan([{"id": item["id"]}], request_id="rid-d")

        with mock.patch.object(app, "PlanCoordinator", side_effect=gate):
            worker = threading.Thread(target=starter, daemon=True)
            worker.start()
            self.assertTrue(entered.wait(5))
            cancel = api.cancel_start_request("rid-d")
            self.assertTrue(cancel["ok"], "pending 阶段可以取消")
            release.set()
            worker.join(5)
        self.assertFalse(holder["res"].get("ok"))
        self.assertTrue(holder["res"].get("canceled"))
        self.assertFalse(made and made[0].start.called, "取消先到则零启动")
        self.assertIsNone(api._plan)

    def test_cancel_after_acceptance_reports_real_plan(self):
        """接纳先赢：取消不得谎称成功，必须给真实 plan_id。"""
        api = self._api()
        app = self._app
        item = self._seed(api)
        inst = self._inst("p2")
        with mock.patch.object(app, "PlanCoordinator", return_value=inst):
            res = api.start_plan([{"id": item["id"]}], request_id="rid-e")
        self.assertTrue(res["ok"])
        cancel = api.cancel_start_request("rid-e")
        self.assertFalse(cancel["ok"])
        self.assertEqual(cancel["state"], "accepted")
        self.assertEqual(cancel["plan_id"], res["plan_id"])

    def test_inner_exception_leaves_real_conclusion(self):
        api = self._api()
        item = self._seed(api)
        with mock.patch.object(api, "_start_plan_inner", side_effect=RuntimeError("boom")):
            res = api.start_plan([{"id": item["id"]}], request_id="rid-f")
        self.assertFalse(res["ok"])
        self.assertIn("本机异常", res["text"])
        record = api.start_request_state("rid-f")
        self.assertEqual(record["state"], "rejected", "异常也必须留真实结论，不能永久 pending")

    def test_coordinator_start_failure_records_rejected(self):
        api = self._api()
        app = self._app
        item = self._seed(api)
        inst = self._inst("p3")
        inst.start.side_effect = RuntimeError("启动失败")
        with mock.patch.object(app, "PlanCoordinator", return_value=inst):
            res = api.start_plan([{"id": item["id"]}], request_id="rid-g")
        self.assertFalse(res["ok"])
        self.assertIsNone(api._plan)
        record = api.start_request_state("rid-g")
        self.assertEqual(record["state"], "rejected")


class SignedBaselineValidationTests(unittest.TestCase):
    """真实证据：学校列表 signed=true 的活动必须参与冲突基线。

    冲突只看举办时段 act_start/act_end；报名开放时间不参与。
    """

    NOW = TestWallClock().now()

    def test_selected_overlapping_signed_activity_is_rejected(self):
        rows = [activity("new", "2026-10-23 10:00:00",
                         "2099-01-03 19:00:00", "2099-01-03 20:00:00")]
        signed = [dict(activity("old", "2026-10-20 10:00:00",
                                "2099-01-03 18:30:00", "2099-01-03 19:30:00"),
                       signed=True)]
        result = validate_plan(rows, now=self.NOW, signed_activities=signed)
        self.assertFalse(result["ok"])
        kinds = [error["kind"] for error in result["errors"]]
        self.assertIn("signed_activity_overlap", kinds)
        overlap = next(item for item in result["errors"]
                       if item["kind"] == "signed_activity_overlap")
        self.assertEqual(overlap["activity_id"], "new")
        self.assertEqual(overlap["signed"]["activity_id"], "old")
        self.assertEqual(result["signed_overlaps"][0]["activity_id"], "new")

    def test_signed_activity_without_window_blocks_plan(self):
        rows = [activity("new", "2026-10-23 10:00:00",
                         "2099-01-03 19:00:00", "2099-01-03 20:00:00")]
        signed = [dict(activity("old", "2026-10-20 10:00:00", "", ""), signed=True)]
        result = validate_plan(rows, now=self.NOW, signed_activities=signed)
        self.assertFalse(result["ok"])
        kinds = [error["kind"] for error in result["errors"]]
        self.assertIn("signed_activity_time_unknown", kinds)
        hit = next(item for item in result["errors"]
                   if item["kind"] == "signed_activity_time_unknown")
        self.assertEqual(hit["activity_id"], "old")

    def test_signed_non_overlapping_keeps_plan_ok(self):
        rows = [activity("new", "2026-10-23 10:00:00",
                         "2099-01-03 19:00:00", "2099-01-03 20:00:00")]
        signed = [dict(activity("old", "2026-10-20 10:00:00",
                                "2099-01-03 17:00:00", "2099-01-03 18:00:00"),
                       signed=True)]
        result = validate_plan(rows, now=self.NOW, signed_activities=signed)
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["signed_overlaps"], [])

    def test_touching_boundaries_are_not_overlap(self):
        rows = [activity("new", "2026-10-23 10:00:00",
                         "2099-01-03 19:00:00", "2099-01-03 20:00:00")]
        signed = [dict(activity("old", "2026-10-20 10:00:00",
                                "2099-01-03 18:00:00", "2099-01-03 19:00:00"),
                       signed=True)]
        result = validate_plan(rows, now=self.NOW, signed_activities=signed)
        self.assertTrue(result["ok"], "首尾相接不是冲突")


class SignedResultReconciliationTests(unittest.TestCase):
    """报名成功必须在生产路径上回写缓存，并进入下一次开始的冲突基线。"""

    def _api(self):
        sys.path.insert(0, str(LOCAL))
        try:
            import app as local_app
        except Exception as exc:
            self.skipTest("无法导入 local/app.py：%s" % exc)
        finally:
            sys.path.pop(0)
        self._app = local_app
        return local_app.Api(clock=TestWallClock())

    def _seed(self, api, aid="a1", start="2099-01-03 19:00:00",
              end="2099-01-03 20:00:00", **override):
        item = {"id": aid, "name": "活动" + aid,
                "sign_start": "2026-10-23 10:00:00", "sign_end": "2099-01-02 09:00:00",
                "act_start": start, "act_end": end, "signed": False}
        item.update(override)
        api._activities[aid] = dict(item)
        api._last_check = {"result": "valid", "checked_at": 0.0}
        return item

    class _FakePlan:
        """按生产顺序发事件的最小协调器：plan → task(终态) → plan_done。"""

        def __init__(self, app, plan_id, specs, status, emit, on_start=None):
            self.app = app
            self.plan_id = plan_id
            self.specs = list(specs)
            self.emit = emit
            self.status = status
            self.started = False
            self.on_start = on_start

        def start(self):
            self.started = True
            if self.on_start:
                self.on_start(self)
            self.emit("plan", {"plan_id": self.plan_id, "status": "ready",
                               "tasks": [spec.as_dict() for spec in self.specs]})
            for index, spec in enumerate(self.specs):
                self.emit("task", {"plan_id": self.plan_id,
                                   "task_id": spec.task_id,
                                   "activity_id": spec.activity_id,
                                   "name": spec.name,
                                   "status": self.status,
                                   "attempts": index + 1})
            self.emit("plan_done", {"plan_id": self.plan_id, "status": "completed",
                                    "ok": True, "summary": {}})

        def snapshot(self):
            return {"plan_id": self.plan_id, "status": "completed", "tasks": [],
                    "summary": {}}

        def published_status(self):
            return "completed" if self.started else "ready"

        def stop(self):
            return True

        def cancel_task(self, task_id):
            return True

    def _run_plan(self, api, status="success"):
        app = self._app
        specs = [TaskSpec.from_activity(dict(item), "task-01-%s" % item["id"])
                 for item in api._activities.values()]
        holder = {}

        def factory(plan_id, specs_arg, runner, emit, now=None):
            holder["plan"] = self._FakePlan(app, plan_id, specs_arg, status, emit)
            return holder["plan"]

        with mock.patch.object(app, "PlanCoordinator", side_effect=factory):
            res = api.start_plan([{"id": item["id"]} for item in api._activities.values()])
        self.assertTrue(res["ok"], res)
        return holder["plan"]

    def test_success_marks_cache_signed_and_blocks_second_start(self):
        api = self._api()
        self._seed(api)
        self._run_plan(api, "success")
        cached = api._activities["a1"]
        self.assertTrue(cached["signed"], "success 必须回写列表缓存")
        self.assertEqual(cached["signed_source"], "plan_success")
        with mock.patch.object(self._app, "PlanCoordinator") as coordinator:
            res = api.start_plan([{"id": "a1", "signed": False}])
        self.assertFalse(res["ok"], "已确认成功不得被前端旧快照重新接纳")
        coordinator.assert_not_called()

    def test_already_signed_marks_cache_signed(self):
        api = self._api()
        self._seed(api)
        self._run_plan(api, "already_signed")
        self.assertTrue(api._activities["a1"]["signed"])
        self.assertEqual(api._activities["a1"]["signed_source"], "plan_already_signed")

    def test_unconfirmed_and_failed_do_not_mark_signed(self):
        for status in ("failed", "canceled"):
            with self.subTest(status=status):
                api = self._api()
                self._seed(api)
                self._run_plan(api, status)
                self.assertFalse(api._activities["a1"]["signed"],
                                 "%s 不得被猜成已报名" % status)

    def test_list_refresh_keeps_confirmed_success_within_sync_window(self):
        api = self._api()
        self._seed(api)
        self._run_plan(api, "success")
        school = [dict(api._activities["a1"], signed=False)]
        merged = api._merge_list_activities(school)
        self.assertTrue(merged[0]["signed"], "同步窗口内不得撤销已确认成功")
        self.assertTrue(merged[0].get("sync_pending"))
        self.assertEqual(merged[0]["signed_source"], "plan_success")
        api._activities["a1"]["signed_confirmed_at"] -= 10_000
        stale = api._merge_list_activities([dict(school[0])])
        self.assertFalse(stale[0]["signed"], "超过同步窗口以学校列表为最终权威")

    def test_list_refresh_true_clears_sync_pending(self):
        api = self._api()
        self._seed(api)
        self._run_plan(api, "success")
        merged = api._merge_list_activities([dict(api._activities["a1"], signed=True)])
        self.assertTrue(merged[0]["signed"])
        self.assertFalse(merged[0].get("sync_pending"))

    def test_start_plan_rejects_signed_baseline_overlap(self):
        api = self._api()
        self._seed(api, aid="old", start="2099-01-03 18:30:00",
                   end="2099-01-03 19:30:00", signed=True)
        self._seed(api, aid="new", start="2099-01-03 19:00:00",
                   end="2099-01-03 20:00:00")
        with mock.patch.object(self._app, "PlanCoordinator") as coordinator:
            res = api.start_plan([{"id": "new"}])
        self.assertFalse(res["ok"])
        self.assertIn("signed_activity_overlap",
                      [error.get("kind") for error in res["errors"]])
        coordinator.assert_not_called()

    def test_start_plan_rejects_signed_without_activity_window(self):
        api = self._api()
        self._seed(api, aid="old", start="", end="", signed=True)
        self._seed(api, aid="new")
        with mock.patch.object(self._app, "PlanCoordinator") as coordinator:
            res = api.start_plan([{"id": "new"}])
        self.assertFalse(res["ok"])
        self.assertIn("signed_activity_time_unknown",
                      [error.get("kind") for error in res["errors"]])
        coordinator.assert_not_called()


class CoordinatorLockOrderTests(unittest.TestCase):
    """P2-1：Api 锁与协调器锁不得构成 AB-BA；emit 异常不得杀死任务线程。"""

    def _api(self):
        sys.path.insert(0, str(LOCAL))
        try:
            import app as local_app
        except Exception as exc:
            self.skipTest("无法导入 local/app.py：%s" % exc)
        finally:
            sys.path.pop(0)
        self._app = local_app
        return local_app.Api(clock=TestWallClock())

    def _start_real_plan(self, api):
        app = self._app
        api._activities = {"a1": {
            "id": "a1", "name": "活动", "sign_start": "2026-10-23 10:00:00",
            "sign_end": "2099-01-02 09:00:00",
            "act_start": "2099-01-03 09:00:00", "act_end": "2099-01-03 11:00:00",
            "signed": False}}
        api._last_check = {"result": "valid", "checked_at": 0.0}
        holder = {}

        def runner(spec, cancel_event, status):
            # 先发一条进度事件，再返回成功：覆盖「任务事件与 bootstrap 并发」
            status("submitting", {"attempts": 1})
            return {"status": "success", "reason": "ok"}

        def factory(plan_id, specs, runner_fn, emit, now=None):
            coord = PlanCoordinator(plan_id, specs, runner_fn, emit, now=now)
            holder["coord"] = coord
            return coord

        with mock.patch.object(app, "PlanCoordinator", side_effect=factory):
            res = api.start_plan([{"id": "a1"}])
        self.assertTrue(res["ok"], res)
        return holder["coord"]

    def test_bootstrap_and_task_events_close_within_one_second(self):
        api = self._api()
        coord = self._start_real_plan(api)
        original = coord.snapshot
        entered = threading.Event()
        proceed = threading.Event()

        def slow_snapshot():
            # 旧实现：bootstrap 已持 Api 锁，正等协调器锁；
            # 新实现：调用方不持 Api 锁，任务事件仍能发布。
            entered.set()
            proceed.wait(2.0)
            return original()

        coord.snapshot = slow_snapshot
        boot_done = threading.Event()
        boot_error = []

        def boot():
            try:
                api.bootstrap()
            except Exception as exc:      # noqa: BLE001 - 测试只关心是否收口
                boot_error.append(exc)
            finally:
                boot_done.set()

        thread = threading.Thread(target=boot, daemon=True)
        thread.start()
        self.assertTrue(entered.wait(1.0), "bootstrap 必须进入快照读取")
        # 另一条路径：协调器持锁发 task 事件（会反向请求 Api 锁）
        emitter = threading.Thread(
            target=lambda: coord._set_state("task-01-a1", "submitting", {"attempts": 2}),
            daemon=True)
        emitter.start()
        time.sleep(0.1)
        proceed.set()
        self.assertTrue(boot_done.wait(1.0),
                        "bootstrap 与任务事件并发时必须在 1 秒内收口（P2-1）")
        emitter.join(1.0)
        self.assertFalse(emitter.is_alive(), "任务事件发布不得被 bootstrap 卡死")
        self.assertFalse(boot_error)

    def test_emit_exception_is_isolated_and_plan_done_still_sent(self):
        events = []
        errors = []

        def emit(kind, payload):
            if kind == "task" and not errors:
                errors.append(kind)
                raise RuntimeError("bridge down")
            events.append(kind)

        spec = TaskSpec.from_activity({
            "id": "a1", "name": "活动", "sign_start": "2026-10-23 10:00:00",
            "sign_end": "2099-01-02 09:00:00",
            "act_start": "2099-01-03 09:00:00", "act_end": "2099-01-03 11:00:00"},
            "task-01-a1", now=TestWallClock().now())
        coord = PlanCoordinator("p1", [spec],
                                lambda s, e, status: {"status": "success"},
                                emit)
        coord.start()
        coord._thread.join(2.0)
        self.assertFalse(coord._thread.is_alive(), "emit 异常不能卡住协调线程")
        self.assertEqual(events.count("plan_done"), 1, "恰好一次 plan_done")
        self.assertEqual(coord.published_status(), "completed")
        self.assertTrue(coord._emit_errors, "回调异常必须留下可审阅记录")


if __name__ == "__main__":
    unittest.main()
