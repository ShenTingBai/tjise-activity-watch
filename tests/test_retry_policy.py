#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重试策略契约测试：窗口计算、次数上限、结构化解析与 fake worker 全链路。

时间用例对应 D:/@Temp/tjise-retry-policy-20261024/timeline-cases.json；
fake worker 场景不连学校站点，只验证 worker 输出 → task 事件的载荷链路。

@author LiXinwen
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

LOCAL = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
# grab.py / signup_plan.py 内部是裸导入（from runtime_paths import ...）：
# 先把 local/ 加进 sys.path，按包路径导入时裸导入才不会断
if str(LOCAL) not in sys.path:
    sys.path.insert(0, str(LOCAL))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from time_contract import configure_time_source  # noqa: E402
configure_time_source(time.time)   # 旧用例显式注入本机时钟；新用例改注 FakeBusinessClock

from grab import attempts_allowed, compute_retry_window, retry_anchor
from signup_plan import TaskSpec
from fixtures.business_clock import TestWallClock  # noqa: E402

NOW = 1000.0


def activity(aid, sign, start, end, sign_end=""):
    return {"id": aid, "name": aid, "sign_start": sign, "sign_end": sign_end,
            "act_start": start, "act_end": end, "signed": False}


# ---- 条款同意夹具（计划 repair-terms-test-fixtures）----
# start_plan、login、check、list 都先过 app._terms_gate，而门禁只读 app.PREFS：
# 不把偏好钉到临时文件，这些用例就会去读开发者真实的 ui.json，门禁结论随机器上
# 是否点过同意而漂移。同意状态只写在本轮临时 ui.json 的 terms_version /
# terms_accepted_at 两个键上，模块跑完即删；未同意状态、旧版本拒绝与 demo 豁免
# 的专测在 test_terms_consent.py 里照旧构造，不受这里影响。
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


class RetryWindowTests(unittest.TestCase):
    """compute_retry_window：三种起算点与 sign_end 硬上限。"""

    def test_timeline_cases_table_driven(self):
        """用例与 timeline-cases.json 一一对应：未来/已开放/直到截止/缺截止回退/截止截断。"""
        cases = [
            # (at_ts, sign_end_ts, retry_for_s, 期望 start/deadline/binding)
            (4600.0, 8200.0, 600, (4600.0, 5200.0, "duration")),      # 未来活动从 sign_start 起算
            (900.0, 8200.0, 600, (NOW, NOW + 600, "duration")),        # 已开放从启动起算
            (900.0, 8200.0, 0, (NOW, 8200.0, "sign_end")),             # 直到报名截止
            (900.0, None, 0, (NOW, NOW + 120, "fallback")),            # 缺 sign_end 回退 120 秒
            (1100.0, 1300.0, 3600, (1100.0, 1300.0, "sign_end")),      # 截止更早：sign_end 截断
            (1100.0, 9999.0, 3600, (1100.0, 4700.0, "duration")),      # 时长更早：duration
        ]
        for at, sign_end, retry_for, want in cases:
            with self.subTest(at=at, sign_end=sign_end, retry_for=retry_for):
                start, deadline, binding = compute_retry_window(at, sign_end, retry_for, now=NOW)
                self.assertEqual((start, deadline, binding), want)

    def test_deadline_never_passes_sign_end(self):
        """任何策略下有效截止都不得晚于 sign_end。"""
        for retry_for in (0, 1, 600, 86400, 10 ** 6):
            _, deadline, _ = compute_retry_window(4600.0, 5000.0, retry_for, now=NOW)
            self.assertLessEqual(deadline, 5000.0,
                                 "retry_for=%r 时截止越过了 sign_end" % retry_for)

    def test_fallback_never_runs_forever(self):
        """「直到截止」但缺 sign_end：必须回退到有限窗口。"""
        _, deadline, binding = compute_retry_window(900.0, None, 0, now=NOW)
        self.assertEqual(binding, "fallback")
        self.assertLessEqual(deadline, NOW + 120.001)


class AttemptsCountingTests(unittest.TestCase):
    """attempts_allowed：0 表示不限；否则等于上限时不得再点。"""

    def test_max_attempts_semantics(self):
        self.assertTrue(attempts_allowed(0, 0))
        self.assertTrue(attempts_allowed(99, 0), "0=不限")
        self.assertTrue(attempts_allowed(2, 3))
        self.assertFalse(attempts_allowed(3, 3), "第 4 次点击前必须拦下")
        self.assertFalse(attempts_allowed(9, 3))


class RetryLineParsingTests(unittest.TestCase):
    """[retry] 结构化行与 telemetry 载荷。"""

    def _app(self):
        sys.path.insert(0, str(LOCAL))
        try:
            import app as local_app
        except Exception as exc:
            self.skipTest("无法导入 local/app.py：%s" % exc)
        finally:
            sys.path.pop(0)
        return local_app

    def test_parse_retry_line_keeps_reason_with_spaces(self):
        app = self._app()
        parsed = app._parse_retry_line(
            "[retry] attempts=3 stop_reason=max_attempts retry_deadline=1792900000 "
            "sign_end=1792810000 last_reason=code:0 报名人数已满")
        self.assertEqual(parsed["attempts"], "3")
        self.assertEqual(parsed["stop_reason"], "max_attempts")
        self.assertEqual(parsed["retry_deadline"], "1792900000")
        self.assertEqual(parsed["last_reason"], "code:0 报名人数已满")

    def test_parse_retry_line_rejects_plain_lines(self):
        app = self._app()
        self.assertIsNone(app._parse_retry_line("[i] 第 3 次提交：code:0 名额已满"))
        self.assertIsNone(app._parse_retry_line(""))

    def test_retry_payload_defaults_and_blank_epochs(self):
        app = self._app()
        spec = TaskSpec("t", "a", "a", "2026-10-25 10:00:00", "2026-10-25 12:00:00",
                        "2026-10-25 13:00:00", sign_end="2026-10-26 10:00:00")
        payload = app._retry_payload({}, spec, stop_reason="process_error")
        self.assertEqual(payload["stop_reason"], "process_error")
        self.assertEqual(payload["attempts"], 0)
        self.assertEqual(payload["retry_deadline"], "")
        self.assertEqual(payload["sign_end"], "2026-10-26 10:00:00", "结构化行缺失时用活动自身 sign_end")
        payload2 = app._retry_payload(
            {"attempts": "5", "stop_reason": "sign_end", "retry_deadline": "0",
             "sign_end": "1792810000", "last_reason": "x"}, spec, stop_reason="fallback")
        self.assertEqual(payload2["attempts"], 5)
        self.assertEqual(payload2["retry_deadline"], "", "epoch=0 表示没有，落空串")
        self.assertEqual(payload2["sign_end"], "1792810000")


class WorkerTelemetryFlowTests(unittest.TestCase):
    """fake worker：[i] 第 N 次提交 / [retry] 行 → task 事件与终态载荷。"""

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

    def _run_worker(self, api, source, policy=None, sign_end=""):
        """启动单任务计划等到 plan_done，返回 (事件, plan_id, task_id, worker 参数)。"""
        import tempfile
        from unittest import mock

        local_app = self._app_module
        fake = Path(tempfile.mkdtemp()) / "fake_worker.py"
        fake.write_text(source, encoding="utf-8")
        captured = {}
        events = []
        api._emit = lambda kind, payload: events.append((kind, payload))
        # 本类只验证重试策略链路：先给一个 valid 的登录结论，越过登录门禁
        api._last_check = {"result": "valid", "checked_at": 0.0}

        original = local_app.worker_command

        def capture(*args):
            # 模拟真实 worker_command：返回 base+args 的新列表；
            # _run_plan_task 随后的 += 会落在这张表上，captured 因此能看到完整命令
            cmd = [sys.executable, str(fake)] + list(args)
            captured["args"] = cmd
            return cmd

        at = (datetime.now() - timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S")
        row = activity("a", at, "2026-10-23 12:00:00", "2026-10-23 13:00:00", sign_end=sign_end)
        # 计划接纳以本进程缓存为权威（R05）：本类验证遥测链路，先把同源快照
        # 登记进缓存再开始；接纳门禁本身由 test_signup_plan 的专门用例覆盖
        api._activities = {str(row["id"]): dict(row)}
        patcher = mock.patch.object(local_app, "worker_command", capture)
        with patcher:
            res = api.start_plan([row], False, policy)
            self.assertTrue(res["ok"], res)
            budget = time.time() + 10
            while time.time() < budget and not any(k == "plan_done" for k, _ in events):
                time.sleep(0.05)
        return events, res["plan_id"], captured.get("args")

    def test_max_attempts_full_is_retryable_then_stops(self):
        """人数已满可重试：第 N 次提交行推 telemetry，次数到达给 max_attempts 终态。"""
        api = self._api()
        source = (
            "import sys, time\n"
            "time.sleep(0.1)\n"
            "print('[i] 第 1 次提交：code:0 报名人数已满', flush=True)\n"
            "print('[i] 第 2 次提交：code:0 报名人数已满', flush=True)\n"
            "print('[i] 第 3 次提交：code:0 报名人数已满', flush=True)\n"
            "print('[X] 已提交 3 次仍没成功：code:0 报名人数已满', flush=True)\n"
            "print('[retry] attempts=3 stop_reason=max_attempts retry_deadline=1792900000 "
            "sign_end=1792810000 last_reason=code:0 报名人数已满', flush=True)\n"
            "sys.exit(2)\n"
        )
        sign_end = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        events, plan_id, worker_args = self._run_worker(
            api, source, policy={"retry_for_s": 600, "max_attempts": 3}, sign_end=sign_end)

        task_events = [p for kind, p in events if kind == "task"]
        submitting = [p for p in task_events
                      if p.get("status") == "submitting" and "attempts" in p]
        # task 事件是自包含快照（重试截止到达等后续推送也会重报当前次数）；
        # 计数契约看「值只在真正 signUp 的行上递增」，不看事件条数
        pushes = [p["attempts"] for p in submitting]
        increments = [b for a, b in zip([0] + pushes, pushes) if b != a]
        self.assertEqual(increments, [1, 2, 3],
                         "attempts 只在真正 signUp 的行上递增（刷新/查询/截止推送不计）")
        self.assertEqual(pushes[-1], 3)
        self.assertTrue(all("报名人数已满" in p["last_reason"] for p in submitting))
        final = task_events[-1]
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["code"], 2)
        self.assertEqual(final["stop_reason"], "max_attempts")
        self.assertEqual(final["attempts"], 3)
        self.assertEqual(final["retry_deadline"], "1792900000")
        done = [p for kind, p in events if kind == "plan_done"][-1]
        self.assertEqual(done["status"], "failed")
        # worker 参数链路：策略与截止必须传到命令行
        self.assertIn("--retry-for", worker_args)
        self.assertEqual(worker_args[worker_args.index("--retry-for") + 1], "600")
        self.assertEqual(worker_args[worker_args.index("--max-attempts") + 1], "3")
        self.assertEqual(worker_args[worker_args.index("--sign-end") + 1], sign_end)

    def test_success_and_login_invalid_terminal_payloads(self):
        """成功带 attempts；登录失效（退出码 3）带 login_invalid 终态。"""
        api = self._api()
        ok_source = (
            "import sys, time\n"
            "time.sleep(0.1)\n"
            "print('[i] 第 2 次提交：code:1 成功', flush=True)\n"
            "print('[OK] 报名成功（尝试 2 次）', flush=True)\n"
            "print('[retry] attempts=2 stop_reason=success retry_deadline=1792900000 sign_end=0 last_reason=', flush=True)\n"
            "sys.exit(0)\n"
        )
        events, _, _ = self._run_worker(api, ok_source)
        final = [p for kind, p in events if kind == "task"][-1]
        self.assertEqual(final["status"], "success")
        self.assertEqual(final["stop_reason"], "success")
        self.assertEqual(final["attempts"], 2)

        login_source = (
            "import sys, time\n"
            "time.sleep(0.1)\n"
            "print('[X] 登录失效：学校要求重新登录', flush=True)\n"
            "print('[retry] attempts=1 stop_reason=login_invalid retry_deadline=1792900000 sign_end=0 last_reason=code:4001 登录失效', flush=True)\n"
            "sys.exit(3)\n"
        )
        events2, _, _ = self._run_worker(api, login_source)
        final2 = [p for kind, p in events2 if kind == "task"][-1]
        self.assertEqual(final2["status"], "failed")
        self.assertEqual(final2["code"], 3)
        self.assertEqual(final2["stop_reason"], "login_invalid")

    def test_invalid_policy_falls_back_to_defaults(self):
        """脏策略值回落默认 120 秒 / 不限，不得把 worker 挂成异常形态。"""
        api = self._api()
        source = ("import sys, time\n"
                  "time.sleep(0.1)\n"
                  "print('[X] 没抢到：名额已满', flush=True)\n"
                  "print('[retry] attempts=1 stop_reason=retry_duration retry_deadline=1792900000 sign_end=0 last_reason=名额已满', flush=True)\n"
                  "sys.exit(2)\n")
        events, _, worker_args = self._run_worker(
            api, source, policy={"retry_for_s": -5, "max_attempts": "abc"})
        self.assertEqual(worker_args[worker_args.index("--retry-for") + 1], "120")
        self.assertEqual(worker_args[worker_args.index("--max-attempts") + 1], "0")
        self.assertNotIn("--sign-end", worker_args, "活动没有 sign_end 就不传")

    def test_checkpoint_uses_source_time_not_receipt(self):
        """R10：节点时间来自子进程记录的发生时刻，父层转发慢 2 秒也不漂。"""
        api = self._api()
        source = (
            "import json, sys, time\n"
            "at = 1792000000.0\n"
            "print('[checkpoint] ' + json.dumps({'kind': 'worker_started', 'at': at}), flush=True)\n"
            "time.sleep(1.2)\n"
            "print('[checkpoint] ' + json.dumps({'kind': 'page_opened', 'at': at + 0.5}), flush=True)\n"
            "time.sleep(0.3)\n"
            "print('[i] 详情页已加载', flush=True)\n"
            "print('[retry] kind=done attempts=0 stop_reason=retry_duration "
            "retry_deadline=0 sign_end=0 last_reason=', flush=True)\n"
            "print('[X] 没抢到：名额已满', flush=True)\n"
            "sys.exit(2)\n"
        )
        events, _, _ = self._run_worker(api, source)
        task_events = [p for kind, p in events if kind == "task"]
        opened = [p["page_opened_at"] for p in task_events if "page_opened_at" in p]
        self.assertTrue(opened, "结构化 checkpoint 必须变成任务节点")
        self.assertEqual(opened[0], 1792000000.5,
                         "节点时间必须是源事件时间，不是读到日志的接收时刻")
        self.assertEqual(task_events[-1].get("run_started_at"), 1792000000.0)

    def test_progress_retry_line_does_not_fake_terminal(self):
        """进度 [retry] 行（无 stop_reason）不得被当成终态。"""
        api = self._api()
        source = (
            "import sys, time\n"
            "print('[retry] kind=window retry_deadline=1792900300 sign_end=1792900000', flush=True)\n"
            "print('[i] 第 1 次提交：已发起', flush=True)\n"
            "time.sleep(0.1)\n"
            "print('[X] 没抢到：名额已满', flush=True)\n"
            "print('[retry] kind=done attempts=1 stop_reason=retry_duration "
            "retry_deadline=1792900300 sign_end=1792900000 last_reason=名额已满', flush=True)\n"
            "sys.exit(2)\n"
        )
        events, _, _ = self._run_worker(api, source)
        task_events = [p for kind, p in events if kind == "task"]
        final = task_events[-1]
        self.assertEqual(final["stop_reason"], "retry_duration")
        self.assertEqual(final["attempts"], 1)
        # 进度行在启动阶段就把截止推上去了，不必等终态
        early = [p for p in task_events
                 if p.get("status") == "submitting" and p.get("retry_deadline") == "1792900300"]
        self.assertTrue(early, "窗口确定的进度行必须让截止尽早上屏")

    def test_plan_snapshot_carries_policy(self):
        """计划快照保留策略：页面重载后策略不丢。"""
        api = self._api()
        sign_end = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        source = ("import time\n"
                  "print('[i] worker started', flush=True)\n"
                  "time.sleep(300)\n")
        threading.Timer(0.3, lambda: api.stop_plan(api._plan.plan_id)).start()
        events, plan_id, _ = self._run_worker(
            api, source, policy={"retry_for_s": 1800, "max_attempts": 5}, sign_end=sign_end)
        snapshots = [p for kind, p in events if kind == "plan"]
        task = snapshots[0]["tasks"][0]
        self.assertEqual(task["retry_for_s"], 1800)
        self.assertEqual(task["max_attempts"], 5)
        self.assertEqual(task["sign_end"], sign_end)


class InstantModeRetryWindowTests(unittest.TestCase):
    """已开放场次（立即报名/抢名额）的重试窗口：从本次启动计时，sign_end 硬截断。

    时间全部注入 now，不依赖真实时钟；三种策略（2 分钟/1 小时/直到截止）
    与路线、任务卡、LED 共用同一套 deadline 口径。
    """

    NOW = 1792000000.0

    def test_open_activity_starts_from_launch_not_history(self):
        """已开放场次从本次启动（now）起算：历史 sign_start 不再充当重试起点。"""
        at_ts = self.NOW - 3 * 86400      # 三天前开放的场次
        sign_end_ts = self.NOW + 86400
        retry_start, deadline, binding = compute_retry_window(
            at_ts, sign_end_ts, 3600, now=self.NOW)
        self.assertEqual(retry_start, self.NOW, "重试起点必须是本次启动时刻")
        self.assertEqual(deadline, self.NOW + 3600)
        self.assertEqual(binding, "duration")

    def test_long_policy_truncated_by_sign_end(self):
        """1 小时策略撞上更早的报名截止：deadline=sign_end，binding=sign_end。"""
        at_ts = self.NOW - 600
        sign_end_ts = self.NOW + 1200
        retry_start, deadline, binding = compute_retry_window(
            at_ts, sign_end_ts, 3600, now=self.NOW)
        self.assertEqual(retry_start, self.NOW)
        self.assertEqual(deadline, sign_end_ts)
        self.assertEqual(binding, "sign_end")

    def test_until_sign_end_policy_uses_sign_end(self):
        """直到截止（0）：deadline=sign_end；缺 sign_end 回退 120 秒，绝不无限运行。"""
        at_ts = self.NOW - 600
        sign_end_ts = self.NOW + 5400
        _, deadline, binding = compute_retry_window(at_ts, sign_end_ts, 0, now=self.NOW)
        self.assertEqual((deadline, binding), (sign_end_ts, "sign_end"))
        _, fallback_deadline, fallback_binding = compute_retry_window(
            at_ts, None, 0, now=self.NOW)
        self.assertEqual((fallback_deadline, fallback_binding),
                         (self.NOW + 120, "fallback"))

    def test_future_activity_still_waits_for_sign_start(self):
        """预报名场次保持原口径：从 sign_start 起算，已开放口径不得外溢。"""
        future_at = self.NOW + 1800
        sign_end_ts = self.NOW + 86400
        retry_start, deadline, binding = compute_retry_window(
            future_at, sign_end_ts, 600, now=self.NOW)
        self.assertEqual(retry_start, future_at)
        self.assertEqual(deadline, future_at + 600)
        self.assertEqual(binding, "duration")


class RetryAnchorTests(unittest.TestCase):
    """重试起算点锚定（retry_anchor）：页面加载延迟不得后移 deadline。

    时间全部注入，不依赖真实时钟；对照旧行为（加载完成后才取 now）证明修复。
    """

    NOW = 1792000000.0

    def test_open_activity_deadline_pinned_to_launch(self):
        """已开放场次：anchor=worker 启动时刻；慢 20 秒的页面不改 deadline。"""
        past_at = self.NOW - 600
        sign_end_ts = self.NOW + 86400
        anchor = retry_anchor(past_at, now=self.NOW)
        self.assertEqual(anchor, self.NOW, "已开放场次的起算点固定为 worker 启动时刻")
        # 旧口径（页面加载完成后才取 now）：deadline 被慢页面整体后移 20 秒
        _, late_deadline, _ = compute_retry_window(past_at, sign_end_ts, 600, now=self.NOW + 20)
        self.assertEqual(late_deadline, self.NOW + 620)
        # 新口径（anchor 在 page.goto 前固定）：deadline 钉在启动时刻
        _, deadline, binding = compute_retry_window(anchor, sign_end_ts, 600, now=anchor)
        self.assertEqual((deadline, binding), (self.NOW + 600, "duration"))

    def test_future_activity_anchor_is_sign_start(self):
        """未来场次：anchor 归一到原定 sign_start，deadline 从开放起算。"""
        future_at = self.NOW + 1800
        sign_end_ts = self.NOW + 86400
        anchor = retry_anchor(future_at, now=self.NOW)
        self.assertEqual(anchor, future_at, "未来场次的起算点仍是原定开放时刻")
        _, deadline, binding = compute_retry_window(anchor, sign_end_ts, 600, now=anchor)
        self.assertEqual((deadline, binding), (future_at + 600, "duration"))

    def test_sign_end_truncation_and_fallback_unchanged(self):
        """anchor 口径下 sign_end 截断与缺截止回退 120 秒保持不变。"""
        anchor = self.NOW
        sign_end_ts = self.NOW + 1200
        _, deadline, binding = compute_retry_window(anchor, sign_end_ts, 3600, now=anchor)
        self.assertEqual((deadline, binding), (sign_end_ts, "sign_end"))
        _, fallback_deadline, fallback_binding = compute_retry_window(anchor, None, 0, now=anchor)
        self.assertEqual((fallback_deadline, fallback_binding),
                         (self.NOW + 120, "fallback"))


WORKER_FIXTURE = r'''
import os
import subprocess
import sys
import time

scenario = sys.argv[1] if len(sys.argv) > 1 else "silent"
pid_file = os.environ.get("TJISE_LIFECYCLE_PIDS", "")

if scenario == "silent":
    time.sleep(60)
elif scenario == "slow_output":
    for i in range(2000):
        print("[i] tick %d" % i, flush=True)
        time.sleep(0.02)
elif scenario == "parent_exit":
    # 孙进程继承 stdout：父进程立刻退出，管道仍被握着，EOF 永远不会来
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    if pid_file:
        with open(pid_file, "w", encoding="utf-8") as f:
            f.write(str(child.pid))
    print("[i] parent exiting, grandchild holds stdout", flush=True)
    sys.exit(0)
elif scenario == "structured_only":
    # 只有结构化提交行、没有人话行：次数不能因此归零
    print("[retry] kind=submit attempts=3", flush=True)
    time.sleep(60)
elif scenario == "job_marker":
    # 主体第一条指令就问「我现在属于某个 Job 吗」：
    # 若归属发生在进程已经跑起来之后，这里只会看到 0。
    import ctypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.IsProcessInJob.restype = ctypes.c_int
    k.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.POINTER(ctypes.c_int)]
    inside = ctypes.c_int(0)
    ok = k.IsProcessInJob(ctypes.c_void_p(-1), None, ctypes.byref(inside))
    marker = os.environ.get("TJISE_LIFECYCLE_MARK", "")
    if marker:
        with open(marker, "w", encoding="utf-8") as f:
            # 第三个字段是主体第一条指令的时刻，用来和父层「归属成功」的时刻比先后
            f.write("%d %d %.6f" % (1 if ok else 0, inside.value, time.time()))
    time.sleep(60)
elif scenario == "submitted_then_wait":
    # 已发起 3 次提交、没有终态：随后被用户停止
    print("[i] 第 3 次提交：已发起", flush=True)
    print("[retry] kind=submit attempts=3", flush=True)
    time.sleep(60)
elif scenario == "success_then_wait":
    # 成功终态已到，但进程还没退出：此时停止不得把成功改写成取消
    print("[i] 第 1 次提交：code:1 成功", flush=True)
    print("[OK] 报名成功（尝试 1 次）", flush=True)
    print("[retry] kind=done attempts=1 stop_reason=success retry_deadline=0 "
          "sign_end=0 last_reason=", flush=True)
    time.sleep(60)
elif scenario == "already_then_wait":
    print("[i] 你已经报过这个活动", flush=True)
    print("[retry] kind=done attempts=0 stop_reason=already_signed "
          "retry_deadline=0 sign_end=0 last_reason=", flush=True)
    time.sleep(60)
elif scenario == "definite_fail_then_wait":
    # 学校明确失败：停止晚到时按这个确定结论记，不降级成「待核对」
    print("[i] 第 2 次提交：报名人数已满", flush=True)
    print("[retry] kind=done attempts=2 stop_reason=max_attempts "
          "retry_deadline=0 sign_end=0 last_reason=报名人数已满", flush=True)
    time.sleep(60)
elif scenario == "terminal":
    print("[i] 第 2 次提交：code:1 成功", flush=True)
    print("[OK] 报名成功（尝试 2 次）", flush=True)
    print("[retry] kind=done attempts=2 stop_reason=success retry_deadline=0 "
          "sign_end=0 last_reason=", flush=True)
    sys.exit(0)
else:
    raise SystemExit("unknown scenario: %s" % scenario)
'''


def _pid_alive(pid: int) -> bool:
    """进程是否还活着；Windows 用 tasklist 避免 os.kill 的终止语义。"""
    if os.name == "nt":
        # tasklist 输出是控制台代码页（GBK），不能让 reader 线程按 UTF-8 解码
        done = subprocess.run(["tasklist", "/FI", "PID eq %d" % int(pid), "/NH"],
                              capture_output=True, timeout=5)
        out = (done.stdout or b"").decode("mbcs", "replace")
        return str(int(pid)) in out
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True


class _WorkerCase(unittest.TestCase):
    """真实临时 worker 的公共装置（脚本生成在临时根，零真实 worker/浏览器）。

    短测试把 SUPERVISE_GRACE_S 换成 0.4 秒并在 cleanup 恢复；子类只写用例。
    """

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
        self.root = Path(tempfile.mkdtemp(prefix="tjise-lifecycle-"))
        self.addCleanup(shutil.rmtree, str(self.root), True)
        self._old_grace = local_app.SUPERVISE_GRACE_S
        local_app.SUPERVISE_GRACE_S = self.GRACE
        self.addCleanup(setattr, local_app, "SUPERVISE_GRACE_S", self._old_grace)
        self.script = self.root / "worker_fixture.py"
        self.script.write_text(WORKER_FIXTURE, encoding="utf-8")

    def _spec(self, retry_for: int, *, deadline_past: bool = True):
        # retry_for_s 是整数字段。默认把起点再往前推 0.5 秒，让有效截止略早于现在，
        # 监督延迟就固定为 GRACE，不依赖用例执行速度；取消类用例要一个「远未到点」
        # 的窗口，否则监督会先于取消收尾，测到的就不是取消语义了。
        offset = (retry_for + 0.5) if deadline_past else 1.0
        return TaskSpec(task_id="t1", activity_id="a1", name="生命周期实验",
                        sign_start="2026-10-26 09:00:00", act_start="", act_end="",
                        sign_end="", retry_for_s=retry_for, max_attempts=0,
                        signup_mode="instant_signup", lead_s=0,
                        retry_started_at=time.time() - offset)

    def _run_task(self, scenario: str, *, retry_for: int = 5,
                  cancel_after: float | None = None, cancel_on_attempts: int | None = None,
                  expect_spawn_failure: bool = False, deadline_past: bool = True):
        api = self.app.Api(clock=TestWallClock())
        events = []
        api._emit = lambda kind, payload: events.append((kind, payload))
        cancel = threading.Event()
        pid_file = self.root / ("pid-%d.txt" % time.monotonic_ns())
        mark_file = self.root / ("mark-%d.txt" % time.monotonic_ns())
        seen = {"attempts": 0, "spawned_at": []}

        def status(state, detail):
            """生产 status 回调的替身：只记录，不改行为。

            「父确认已消费该行再取消」靠它证明——收到 attempts>=N 的那次回调
            一定发生在 feed() 已经解析完该行之后。
            """
            if state != "submitting" or not isinstance(detail, dict):
                return
            if "worker_spawned_at" in detail:
                seen["spawned_at"].append(detail["worker_spawned_at"])
            if "attempts" in detail:
                seen["attempts"] = max(seen["attempts"], int(detail["attempts"]))
                if cancel_on_attempts and seen["attempts"] >= cancel_on_attempts:
                    cancel.set()

        def fake_command(*args):
            return [sys.executable, str(self.script), scenario]

        original_env = self.app._env

        def fake_env():
            env = dict(original_env())
            env["TJISE_LIFECYCLE_PIDS"] = str(pid_file)
            env["TJISE_LIFECYCLE_MARK"] = str(mark_file)
            return env

        timer = None
        started = time.time()
        result = None
        error = None
        with mock.patch.object(self.app, "worker_command", fake_command), \
                mock.patch.object(self.app, "_env", fake_env):
            if cancel_after is not None:
                timer = threading.Timer(cancel_after, cancel.set)
                timer.start()
            try:
                result = api._run_plan_task(
                    self._spec(retry_for, deadline_past=deadline_past), cancel, status)
            except self.app._SpawnFailure as exc:
                error = exc
            finally:
                if timer is not None:
                    timer.cancel()
        if expect_spawn_failure:
            self.assertIsNotNone(error, "归属失败必须以 _SpawnFailure 结束这一场")
        elif error is not None:
            raise error
        return api, result, events, time.time() - started, pid_file, mark_file, seen


class WorkerLifecycleTests(_WorkerCase):
    """F03：父先退出、孙持管道时，监督仍能在有界时间内结束并清干净。"""

    def test_silent_worker_is_supervised(self):
        api, result, _, elapsed, _, _, _ = self._run_task("silent", retry_for=1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stop_reason"], "retry_duration")
        self.assertLessEqual(elapsed, 0.5 + self.GRACE + 2.0,
                             "静默不退出的 worker 必须被监督收走")
        self.assertEqual(api._plan_procs, {})

    def test_slow_output_does_not_block_supervision(self):
        api, result, events, elapsed, _, _, _ = self._run_task("slow_output", retry_for=1)
        self.assertEqual(result["status"], "failed")
        self.assertLessEqual(elapsed, 0.5 + self.GRACE + 2.0)
        self.assertTrue(any("tick" in str(payload) for kind, payload in events),
                        "慢输出仍要被读线程消费，不能卡住监督")
        self.assertEqual(api._plan_procs, {})

    def test_parent_exit_with_grandchild_holding_pipe_ends_bounded(self):
        api, result, _, elapsed, pid_file, _, _ = self._run_task("parent_exit", retry_for=1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stop_reason"], "retry_duration",
                         "父已退出且无终态、也没有提交过：按重试时长收尾")
        self.assertLessEqual(elapsed, 0.5 + self.GRACE + 2.0,
                             "孙进程持管道不得把监控拖到永久等待")
        self.assertEqual(api._plan_procs, {}, "进程表必须清空，否则 busy 永远不释放")
        self.assertTrue(pid_file.exists(), "装置应记录孙进程 PID")
        grandchild = int(pid_file.read_text(encoding="utf-8").strip())
        deadline = time.time() + 2.0
        while time.time() < deadline and _pid_alive(grandchild):
            time.sleep(0.05)
        self.assertFalse(_pid_alive(grandchild), "孙进程必须被本场所有权收走")

    def test_structured_attempts_survive_supervision(self):
        _, result, _, elapsed, _, _, _ = self._run_task("structured_only", retry_for=1)
        self.assertEqual(result["attempts"], 3,
                         "只有结构化提交行时次数也不能归零")
        self.assertEqual(result["stop_reason"], "unconfirmed",
                         "发过请求但没有终态：必须说结果待核对")
        self.assertLessEqual(elapsed, 0.5 + self.GRACE + 2.0)

    def test_normal_terminal_cancels_watchdog(self):
        api, result, _, elapsed, _, _, _ = self._run_task("terminal", retry_for=30)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["attempts"], 2)
        self.assertLess(elapsed, 3.0, "正常完成必须立刻撤销监督，不等 30 秒截止")
        self.assertEqual(api._plan_procs, {})

    def test_cancel_and_deadline_together_return_once(self):
        api, result, _, elapsed, _, _, _ = self._run_task(
            "silent", retry_for=1, cancel_after=0.5)
        self.assertIn(result["status"], {"canceled", "failed"},
                      "取消与截止同时到达只能有一个终态")
        self.assertLessEqual(elapsed, 0.5 + self.GRACE + 2.0)
        self.assertEqual(api._plan_procs, {})

    def test_plan_done_once_and_busy_released(self):
        """整计划跑一遍：监督收走后 plan_done 恰一次、busy 解除。"""
        api = self.app.Api(clock=TestWallClock())
        events = []
        api._emit = lambda kind, payload: events.append((kind, payload))
        api._last_check = {"result": "valid", "checked_at": 0.0}
        at = (datetime.now() - timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S")
        sign_end = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        row = activity("a1", at, "2026-10-23 12:00:00", "2026-10-23 13:00:00",
                       sign_end=sign_end)
        api._activities = {"a1": dict(row)}

        def fake_command(*args):
            return [sys.executable, str(self.script), "parent_exit"]

        with mock.patch.object(self.app, "worker_command", fake_command):
            res = api.start_plan([row], False, {"retry_for_s": 1, "max_attempts": 0})
            self.assertTrue(res["ok"], res)
            deadline = time.time() + 1 + self.GRACE + 2.0
            while time.time() < deadline and not any(k == "plan_done" for k, _ in events):
                time.sleep(0.05)
        done = [p for kind, p in events if kind == "plan_done"]
        self.assertEqual(len(done), 1, "plan_done 必须恰一次")
        terminals = [p for kind, p in events
                     if kind == "task" and p.get("status") in
                     {"success", "already_signed", "failed", "canceled"}]
        self.assertEqual(len(terminals), 1, "本场终态必须恰一次")
        self.assertFalse(api._is_busy(), "监督收走后 busy 必须解除")
        self.assertEqual(api._plan_procs, {})
        # F5/bootstrap 要能恢复最后一个计划的结论：另存一份终态快照
        self.assertIsNone(api._plan, "当前计划仍按旧契约清空")
        self.assertIsNotNone(api._last_plan, "plan_done 后必须留下终态快照")
        self.assertIn(api._last_plan.get("status"), {"failed", "partial", "completed"})


class WorkerOwnershipTests(_WorkerCase):
    """FR02：所有权必须在 worker 主体执行之前建立，失败不得静默降级。

    全部使用真实临时子进程；Job 侧失败用「只替换 Windows API 返回值」的方式
    制造（那是外部 I/O 边界，不是被测的生产函数）。
    """

    def setUp(self):
        if os.name != "nt":
            self.skipTest("Job Object 归属只在 Windows 上成立")
        super().setUp()

    def _patch_kernel(self, **overrides):
        """只改 Windows API 的返回值，保留其余真实实现。"""
        app = self.app
        real = app._win_kernel32()
        saved = app._KERNEL32

        class Wrap:
            def __getattr__(self, name):
                if name in overrides:
                    return overrides[name]
                return getattr(real, name)

        app._KERNEL32 = Wrap()
        self.addCleanup(setattr, app, "_KERNEL32", saved)

    def test_worker_body_starts_only_after_job_ownership(self):
        """归属成功的时刻必须早于 worker 主体的第一条指令。

        不能只看 IsProcessInJob：测试进程自己也可能在某个 Job 里，
        子进程会继承它，那样「是否在 Job 里」永远为真。这里比的是时刻——
        挂起创建的意义就是主体在归属成功之前一步都跑不了。
        """
        app = self.app
        real = app._win_kernel32()
        stamp = {}

        def recording_assign(job, handle):
            ok = real.AssignProcessToJobObject(job, handle)
            if ok:
                stamp["at"] = time.time()
            return ok

        self._patch_kernel(AssignProcessToJobObject=recording_assign)
        _, result, _, _, _, mark, _ = self._run_task("job_marker", retry_for=1)
        self.assertTrue(mark.exists(), "装置应写出归属探测结果")
        ok, inside, first_instruction = mark.read_text(encoding="utf-8").split()
        self.assertEqual(ok, "1", "IsProcessInJob 调用失败")
        self.assertIn("at", stamp, "装置应记录归属成功时刻")
        self.assertGreaterEqual(
            float(first_instruction), stamp["at"] - 0.02,
            "worker 主体在归属成功之前就跑了：创建与归属之间存在未受控窗口")
        self.assertEqual(inside, "1")
        self.assertEqual(result["stop_reason"], "retry_duration")

    def test_spawn_reports_ownership_failure_and_runs_no_body(self):
        """归属失败必须报错、零主体动作、零进程残留。"""
        for name, override in (
                ("创建 Job 失败", {"CreateJobObjectW": lambda *a: None}),
                ("设置 Job 失败", {"SetInformationJobObject": lambda *a: 0}),
                ("归属 Job 失败", {"AssignProcessToJobObject": lambda *a: 0}),
                ("恢复线程失败", {"ResumeThread": lambda *h: 0xFFFFFFFF}),
                ("找不到主线程", {"OpenThread": lambda *a: None}),
        ):
            with self.subTest(阶段=name):
                self._patch_kernel(**override)
                api, _result, _events, _elapsed, _pid, mark, _seen = self._run_task(
                    "job_marker", retry_for=1, expect_spawn_failure=True)
                self.assertFalse(mark.exists(),
                                 "归属失败时 worker 主体不得执行（%s）" % name)
                self.assertEqual(api._plan_procs, {}, "失败后进程表必须为空")
                self.assertFalse(api._is_busy(), "失败后不得留下忙状态")

    def test_cancel_before_spawn_completes_returns_single_terminal(self):
        """取消碰到挂起创建：仍只有一个终态、进程表清空。"""
        api, result, events, elapsed, _, _, _ = self._run_task(
            "silent", retry_for=1, cancel_after=0.0)
        self.assertIn(result["status"], {"canceled", "failed"})
        self.assertLessEqual(elapsed, 0.5 + self.GRACE + 3.0)
        self.assertEqual(api._plan_procs, {})
        self.assertEqual(result["attempts"], 0, "没有提交过就不能记次数")

    def test_deadline_already_passed_supervises_immediately(self):
        """截止已过：看门狗与主循环同一上限，只收一次、不死锁。"""
        api, result, _, elapsed, _, _, _ = self._run_task("silent", retry_for=1)
        self.assertEqual(result["stop_reason"], "retry_duration")
        self.assertLessEqual(elapsed, 0.5 + self.GRACE + 2.0)
        self.assertEqual(api._plan_procs, {})


class StopPreservesFactsTests(_WorkerCase):
    """FR03：停止只终止本地执行，不覆盖次数与已确认结果。"""

    def test_cancel_after_submissions_keeps_attempts_and_marks_unconfirmed(self):
        _, result, _, _, _, _, seen = self._run_task(
            "submitted_then_wait", retry_for=30, deadline_past=False, cancel_on_attempts=3)
        self.assertGreaterEqual(seen["attempts"], 3, "父层必须已消费该提交行")
        self.assertEqual(result["attempts"], 3, "停止不得把已提交次数清零")
        self.assertEqual(result["stop_reason"], "unconfirmed",
                         "已发出请求又没确定回应：结果待核对，不是普通取消")
        self.assertTrue(result.get("locally_stopped"))
        self.assertEqual(result["status"], "failed")

    def test_cancel_after_confirmed_success_keeps_success(self):
        _, result, _, _, _, _, _ = self._run_task(
            "success_then_wait", retry_for=30, deadline_past=False, cancel_after=0.8)
        self.assertEqual(result["status"], "success",
                         "停止晚到不得把已确认成功改写为取消")
        self.assertEqual(result["attempts"], 1)

    def test_cancel_after_already_signed_keeps_already(self):
        _, result, _, _, _, _, _ = self._run_task(
            "already_then_wait", retry_for=30, deadline_past=False, cancel_after=0.8)
        self.assertEqual(result["status"], "already_signed")

    def test_cancel_after_definite_failure_keeps_reason(self):
        _, result, _, _, _, _, _ = self._run_task(
            "definite_fail_then_wait", retry_for=30, deadline_past=False, cancel_after=0.8)
        self.assertEqual(result["stop_reason"], "max_attempts",
                         "确定失败晚于停止时不能被改写成「待核对」")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["status"], "failed")

    def test_cancel_without_submission_is_plain_canceled(self):
        _, result, _, _, _, _, _ = self._run_task(
            "silent", retry_for=30, deadline_past=False, cancel_after=0.5)
        self.assertEqual(result["status"], "canceled",
                         "一次都没提交时仍是普通取消")
        self.assertEqual(result["attempts"], 0)


class SubmitBackoffTests(unittest.TestCase):
    """RC-05：人数已满时提交间隔 2 秒起、1.5 倍退避、8 秒封顶且受截止钳制。"""

    def test_backoff_sequence_is_bounded(self):
        from grab import (SUBMIT_BACKOFF_MAX_S, SUBMIT_BACKOFF_START_S,
                          next_submit_gap)
        gaps = []
        gap = SUBMIT_BACKOFF_START_S
        for _ in range(8):
            gaps.append(round(gap, 3))
            gap = next_submit_gap(gap)
        self.assertEqual(gaps[:5], [2.0, 3.0, 4.5, 6.75, 8.0])
        self.assertEqual(set(gaps[4:]), {SUBMIT_BACKOFF_MAX_S},
                         "退避必须封顶在 8 秒，不能无限放大")

    def test_backoff_never_exceeds_remaining_window(self):
        from grab import wait_before_next_submit
        sleeps = []

        class _Budget:
            def remaining_s(self):
                return 5.0

            def sleep(self, seconds):
                sleeps.append(round(seconds, 3))

        next_gap = wait_before_next_submit(_Budget(), 8.0)
        self.assertEqual(sleeps, [5.0], "窗口只剩 5 秒时最多等 5 秒")
        self.assertEqual(next_gap, 8.0)

    def test_invalid_gap_restarts_from_two_seconds(self):
        from grab import next_submit_gap
        self.assertEqual(next_submit_gap(None), 3.0)
        self.assertEqual(next_submit_gap(0), 3.0)
        self.assertEqual(next_submit_gap(-10), 3.0)


if __name__ == "__main__":
    unittest.main()
