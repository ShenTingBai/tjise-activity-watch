#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重试策略契约测试：窗口计算、次数上限、结构化解析与 fake worker 全链路。

时间用例对应 D:/@Temp/tjise-retry-policy-20261024/timeline-cases.json；
fake worker 场景不连学校站点，只验证 worker 输出 → task 事件的载荷链路。

@author LiXinwen
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

LOCAL = Path(__file__).resolve().parents[1]
# grab.py / signup_plan.py 内部是裸导入（from runtime_paths import ...）：
# 先把 local/ 加进 sys.path，按包路径导入时裸导入才不会断
sys.path.insert(0, str(LOCAL))

from grab import attempts_allowed, compute_retry_window, retry_anchor
from signup_plan import TaskSpec

NOW = 1000.0


def activity(aid, sign, start, end, sign_end=""):
    return {"id": aid, "name": aid, "sign_start": sign, "sign_end": sign_end,
            "act_start": start, "act_end": end, "signed": False}


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
        return local_app.Api()

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


if __name__ == "__main__":
    unittest.main()
