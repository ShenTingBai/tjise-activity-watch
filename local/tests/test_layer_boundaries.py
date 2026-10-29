# -*- coding: utf-8 -*-
"""分层边界契约测试：domain/application 不得直接依赖混合具体模块。

被禁的是「实现混杂的具体模块」（signup_plan 含协调器/线程、business_clock 含
SNTP/系统时钟、time_contract 含全局时间源、updater/update_check 含网络/进程）：
所需纯函数、时钟函数与异常类型一律由 composition root（local/app.py）注入。
本文件同时用内存替身验证注入缝真实生效。

@author LiXinwen
"""
from __future__ import annotations

import ast
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

LOCAL = Path(__file__).resolve().parents[1]
if str(LOCAL) not in sys.path:
    sys.path.insert(0, str(LOCAL))

# 混合具体模块：domain/application 只准依赖 stdlib 与 domain 自身
BANNED_MODULES = {
    "signup_plan", "business_clock", "time_contract", "grab", "updater",
    "update_check", "runtime_paths", "webview", "app", "gui",
}


def _module_sources():
    for pkg in ("domain", "application"):
        for path in sorted((LOCAL / pkg).glob("*.py")):
            yield pkg, path


class ImportBoundaryTests(unittest.TestCase):
    def test_domain_and_application_import_no_mixed_concrete_modules(self):
        for pkg, path in _module_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [(node.module or "").split(".")[0]]
                else:
                    continue
                for name in names:
                    self.assertNotIn(
                        name, BANNED_MODULES,
                        "%s/%s 不得直接依赖混合具体模块 %r（应由 app.py 注入）"
                        % (pkg, path.name, name))

    def test_domain_reads_no_wall_clock(self):
        """domain 只做纯换算/格式化：不得直接读墙钟，此刻值一律由调用方传入。"""
        banned = ("datetime.now(", "datetime.today(", "datetime.utcnow(",
                  "time.time(", "time.monotonic(", "perf_counter(")
        for pkg, path in _module_sources():
            if pkg != "domain":
                continue
            text = path.read_text(encoding="utf-8")
            for token in banned:
                self.assertNotIn(token, text,
                                 "%s 不得直接读取墙钟：%s" % (path.name, token))


class InjectionBoundaryTests(unittest.TestCase):
    """注入缝的内存替身：证明纯函数/异常类型确经参数进入，不走模块直连。"""

    def test_plan_rules_classify_and_validate_are_injected(self):
        from domain import plan_rules

        seen = []

        def classify_mode(sign_start, sign_end, now):
            seen.append(("classify", sign_start, sign_end, now))
            return "closed"

        error = plan_rules.closed_items_error(
            [{"id": "a1", "name": "甲", "sign_end": "2026-01-01 09:00:00"}],
            123.0, classify_mode)
        self.assertIsNotNone(error, "注入的分类函数判 closed 时必须拒绝")
        self.assertEqual(error["errors"][0]["kind"], "sign_end_passed")
        self.assertTrue(seen, "必须调用注入的分类函数而不是模块直连")

        def validate_fn(items, now=None, signed_activities=None):
            seen.append(("validate", tuple(items), now))
            return {"ok": False, "conflicts": ["c1"], "errors": ["e1"]}

        out = plan_rules.check_conflicts([{"id": "a1"}], 1.0, [], validate_fn)
        self.assertEqual(out["conflicts"], ["c1"])
        self.assertEqual(out["errors"], ["e1"])

    def test_plan_rules_no_error_when_injected_classifier_passes(self):
        from domain import plan_rules

        error = plan_rules.closed_items_error(
            [{"id": "a1", "name": "甲", "sign_end": ""}], 1.0,
            lambda s, e, n: "instant_signup")
        self.assertIsNone(error, "注入分类函数不判 closed 时不得拒绝")

    def test_task_stop_binding_uses_injected_epoch_fn(self):
        from domain import plan_rules

        spec = SimpleNamespace(sign_end="2026-01-01 09:00:00",
                               retry_started_at=100.0, retry_for_s=50)
        # epoch_of(sign_end)=50；task_deadline=min(100+50, 50)=50 → 绑 sign_end
        binding = plan_rules.task_stop_reason_binding(
            spec, True, lambda ts: 50.0)
        self.assertEqual(binding, "sign_end")
        self.assertEqual(
            plan_rules.task_stop_reason_binding(spec, False, lambda ts: 50.0), "")

    def test_plan_service_catches_only_injected_clock_unavailable(self):
        """业务时钟读不到 → 保持原定开放时刻：异常类型经参数注入。"""
        from application import plan_service

        class FakeClockErr(Exception):
            pass

        captured = {}

        def worker_command(*args):
            captured["args"] = list(args)
            return ["fake-worker"]

        def spawn(args, cwd=None, env=None):
            raise RuntimeError("stop-before-threads")

        spec = SimpleNamespace(
            task_id="t1", activity_id="a1", name="活动",
            sign_start="2026-01-01 09:00:00", sign_end="",
            lead_s=0, retry_for_s=5, max_attempts=0,
            headed=False, signup_mode="", retry_started_at=0)

        def boom():
            raise FakeClockErr("no clock")

        with self.assertRaises(RuntimeError):
            plan_service.run_plan_task(
                spec, threading.Event(), None,
                emit=lambda *a: None, lock=threading.Lock(), plan_procs={},
                clock_now=boom, is_closing=lambda: False,
                worker_command=worker_command, env=lambda: {},
                spawn=spawn, cwd=".", grace_s=0.1,
                epoch_of=lambda ts: None, time_text_of=lambda epoch=None: "",
                clock_unavailable=FakeClockErr)
        self.assertEqual(captured["args"][0], "grab", "worker 命令已装配")
        self.assertIn("--at", captured["args"])
        at_index = captured["args"].index("--at")
        self.assertEqual(captured["args"][at_index + 1], "2026-01-01 09:00:00",
                         "注入的时钟异常被识别为「时钟不可用」：保持原定开放时刻")

    def test_plan_service_closing_refusal_uses_domain_error(self):
        from application import plan_service
        from domain.errors import ClosingError

        called = []

        with self.assertRaises(ClosingError):
            plan_service.run_plan_task(
                SimpleNamespace(
                    task_id="t1", activity_id="a1", name="活动",
                    sign_start="", sign_end="", lead_s=0, retry_for_s=5,
                    max_attempts=0, headed=False, signup_mode="",
                    retry_started_at=0),
                threading.Event(), None,
                emit=lambda *a: None, lock=threading.Lock(), plan_procs={},
                clock_now=lambda: 1.0, is_closing=lambda: True,
                worker_command=lambda *a: ["fake"], env=lambda: {},
                spawn=lambda *a, **k: called.append("spawn"),
                cwd=".", grace_s=0.1,
                epoch_of=lambda ts: None, time_text_of=lambda epoch=None: "",
                clock_unavailable=Exception)
        self.assertEqual(called, [], "关闭拒绝必须发生在创建之前（零 spawn）")

    def test_demo_items_uses_injected_time_text(self):
        from domain import demo_data

        items = demo_data.demo_items(1000.0, lambda epoch=None: "T%s" % epoch)
        self.assertEqual(items[0]["sign_start"], "T997.0",
                         "演示时间必须走注入的格式化函数")

    def test_format_local_text_is_pure_formatting(self):
        """ui.json 时间文本格式冻结：只接受 datetime 值并格式化，不取时。"""
        from datetime import datetime

        from domain.time_text import format_local_text

        self.assertEqual(format_local_text(datetime(2026, 1, 2, 3, 4, 5)),
                         "2026-01-02 03:04:05")


if __name__ == "__main__":
    unittest.main()
