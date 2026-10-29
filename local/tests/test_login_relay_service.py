#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手机重登中继父进程侧测试：凭据预备、singleflight、状态投影与收口。

替身子进程/管道/时钟；不启动真实浏览器、不读实际 session、不写任何凭据文件。

@author LiXinwen
"""
from __future__ import annotations

import io
import json
import queue
import sys
import threading
import time
import unittest
from pathlib import Path

LOCAL = Path(__file__).resolve().parents[1]
if str(LOCAL) not in sys.path:
    sys.path.insert(0, str(LOCAL))

from application.login_relay_service import (  # noqa: E402
    LoginRelayService, STATE_NEEDS_LOCAL_SETUP, STATE_PLAN_ACTIVE,
    public_state,
)


class FakeStdout:
    """worker stdout 替身：行由测试写入，写完置 EOF。"""

    def __init__(self):
        self._lines = queue.Queue()

    def feed(self, text):
        self._lines.put(str(text))

    def end(self):
        self._lines.put(None)

    def __iter__(self):
        return self

    def __next__(self):
        item = self._lines.get()
        if item is None:
            raise StopIteration
        return item


class FakeStdin:
    def __init__(self):
        self.written = []
        self.fail = False

    def write(self, text):
        if self.fail:
            raise OSError("pipe closed")
        self.written.append(str(text))

    def flush(self):
        pass


class FakeProc:
    def __init__(self):
        self.stdin = FakeStdin()
        self.stdout = FakeStdout()
        self.killed = False

    def poll(self):
        return -9 if self.killed else None


class FakeTree:
    def __init__(self):
        self.proc = FakeProc()
        self.terminated = 0

    def terminate(self):
        self.terminated += 1
        self.proc.killed = True
        self.proc.stdout.end()


class RelayServiceBase(unittest.TestCase):
    def setUp(self):
        self.trees = []
        self.clock = {"now": 0.0}
        self.events = []
        self.success_calls = []
        self.finish_calls = []
        self.spawn_error = None

        def spawn(args):
            if self.spawn_error is not None:
                raise self.spawn_error
            tree = FakeTree()
            tree.args = list(args)
            self.trees.append(tree)
            return tree

        self.service = LoginRelayService(
            spawn=spawn, now=lambda: self.clock["now"],
            emit=lambda kind, payload: self.events.append((kind, payload)),
            on_success=lambda: self.success_calls.append(time.time()),
            on_finish=lambda: self.finish_calls.append(time.time()))

    def tearDown(self):
        self.service.shutdown()


class PrepareTests(RelayServiceBase):
    def test_prepare_only_returns_ready_boolean(self):
        result = self.service.prepare("user-x", "secret-y")
        self.assertEqual(result, {"ok": True, "ready": True})
        self.assertTrue(self.service.prepared())
        self.assertNotIn("secret-y", json.dumps(result))

    def test_prepare_is_memory_only(self):
        """凭据绝不落盘：预备前后临时目录/工作区都不得出现明文副本。"""
        self.service.prepare("user-x", "secret-y")
        self.assertEqual(self.service.state()["safe_error"], "")
        text = json.dumps(self.service.state(), ensure_ascii=False)
        self.assertNotIn("secret-y", text)
        self.assertNotIn("user-x", text)

    def test_empty_prepare_clears_staging(self):
        self.service.prepare("user-x", "secret-y")
        result = self.service.prepare("", "")
        self.assertFalse(result["ok"])
        self.assertFalse(self.service.prepared())


class BeginTests(RelayServiceBase):
    def test_begin_without_prepare_is_needs_local_setup(self):
        result = self.service.begin()
        self.assertEqual(result["error"], STATE_NEEDS_LOCAL_SETUP)
        self.assertEqual(self.trees, [])

    def test_begin_with_active_plan_is_plan_active(self):
        self.service.prepare("u", "p")
        result = self.service.begin(plan_active=True)
        self.assertEqual(result["error"], STATE_PLAN_ACTIVE)
        self.assertEqual(self.trees, [], "计划活跃不得动会话/起 worker")

    def test_demo_never_starts_worker(self):
        self.service.prepare("u", "p")
        result = self.service.begin(demo=True)
        self.assertEqual(result["error"], "demo")
        self.assertEqual(self.trees, [])

    def test_terms_required_blocks_start(self):
        self.service.prepare("u", "p")
        result = self.service.begin(terms_ok=False)
        self.assertEqual(result["error"], "terms_required")
        self.assertEqual(self.trees, [])

    def test_begin_spawns_and_sends_credentials_once(self):
        self.service.prepare("user-x", "secret-y")
        result = self.service.begin(worker_command=["python", "grab.py", "login"])
        self.assertTrue(result["ok"])
        tree = self.trees[0]
        self.assertEqual(tree.args, ["python", "grab.py", "login", "--relay"])
        sent = json.loads(tree.proc.stdin.written[0])
        self.assertEqual(sent["kind"], "credentials")
        self.assertEqual(sent["username"], "user-x")
        self.assertEqual(sent["password"], "secret-y")
        self.assertTrue(sent["flow_id"])
        # 下发后不再留第二份；预备库保留到显式清除/关闭
        self.assertEqual(len(tree.proc.stdin.written), 1)
        self.assertTrue(self.service.prepared())

    def test_singleflight_concurrent_begin(self):
        self.service.prepare("u", "p")
        results = []
        barrier = threading.Barrier(3)

        def begin():
            barrier.wait()
            results.append(self.service.begin(worker_command=["x"]))

        threads = [threading.Thread(target=begin) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        ok = [r for r in results if r.get("ok")]
        busy = [r for r in results if r.get("error") == "busy"]
        self.assertEqual(len(ok), 1, "singleflight：并发 begin 只能有一场")
        self.assertEqual(len(busy), 2)

    def test_close_versus_create_race(self):
        """关闭与创建竞态：取消后状态留终态快照，不留下孤儿 worker。"""
        self.service.prepare("u", "p")
        self.service.begin(worker_command=["x"])
        self.service.cancel()
        self.assertEqual(self.trees[0].terminated, 1)
        self.assertEqual(self.service.state()["state"], "canceled")


class FlowTests(RelayServiceBase):
    def _begin(self):
        self.service.prepare("u", "p")
        result = self.service.begin(worker_command=["x"])
        self.assertTrue(result["ok"])
        return self.trees[-1], result["flow_id"]

    def _emit(self, tree, payload):
        tree.proc.stdout.feed("[login] " + json.dumps(payload, ensure_ascii=False))

    def test_state_has_no_credentials(self):
        tree, flow_id = self._begin()
        self._emit(tree, {"kind": "challenge", "flow_id": flow_id,
                          "challenge_id": "chg-1",
                          "image": "data:image/png;base64,AAAA", "expires_in": 60,
                          "attempts": 0, "max_attempts": 3})
        tree.proc.stdout.end()
        self.service._pump_thread.join(timeout=5)
        state = self.service.state()
        text = json.dumps(state, ensure_ascii=False)
        self.assertNotIn("password", text)
        self.assertNotIn("secret", text)
        self.assertEqual(state["challenge_id"], "chg-1")

    def test_old_flow_lines_are_ignored(self):
        tree, flow_id = self._begin()
        self._emit(tree, {"kind": "challenge", "flow_id": "stale",
                          "challenge_id": "chg-old",
                          "image": "data:image/png;base64,AAAA", "expires_in": 60})
        self._emit(tree, {"kind": "success", "flow_id": "stale"})
        tree.proc.stdout.end()
        self.service._pump_thread.join(timeout=5)
        self.assertEqual(self.success_calls, [], "旧 flow 不得触发锚点")
        self.assertEqual(self.service.state()["challenge_id"], "",
                         "旧 flow 的挑战不得写进当前会话")

    def test_success_fires_anchor_exactly_once(self):
        tree, flow_id = self._begin()
        self._emit(tree, {"kind": "success", "flow_id": flow_id})
        self._emit(tree, {"kind": "success", "flow_id": flow_id})
        tree.proc.stdout.end()
        self.service._pump_thread.join(timeout=5)
        self.assertEqual(len(self.success_calls), 1, "锚点只能建一次")
        self.assertEqual(len(self.finish_calls), 1)

    def test_submit_rejects_stale_flow(self):
        tree, flow_id = self._begin()
        result = self.service.submit("other-flow", "chg-1", "Ab12")
        self.assertEqual(result["error"], "stale_flow")
        self.assertEqual(tree.proc.stdin.written[1:], [])

    def test_submit_forwards_to_worker(self):
        tree, flow_id = self._begin()
        result = self.service.submit(flow_id, "chg-1", "Ab12")
        self.assertTrue(result["ok"])
        sent = json.loads(tree.proc.stdin.written[1])
        self.assertEqual(sent["kind"], "submit")
        self.assertEqual(sent["code"], "Ab12")

    def test_cancel_and_shutdown_clear_everything(self):
        tree, flow_id = self._begin()
        self.service.cancel()
        self.assertTrue(tree.terminated >= 1)
        self.assertEqual(self.service.state()["state"], "canceled")
        self.service.shutdown()
        self.assertFalse(self.service.prepared(), "关闭必须清除预备凭据")

    def test_eof_marks_failed_and_clears_busy(self):
        tree, flow_id = self._begin()
        self._emit(tree, {"kind": "challenge", "flow_id": flow_id,
                          "challenge_id": "chg-1",
                          "image": "data:image/png;base64,AAAA", "expires_in": 60})
        tree.proc.stdout.end()
        self.service._pump_thread.join(timeout=5)
        state = self.service.state()
        self.assertEqual(state["state"], "failed", "半路 EOF 必须如实记失败")
        self.assertEqual(state["safe_error"], "eof")
        self.assertEqual(len(self.finish_calls), 1, "收口必须通知 busy 位释放")
        self.assertEqual(len(tree.proc.stdin.written), 1,
                         "收口后不再向 worker 写任何内容")

    def test_public_state_shape(self):
        state = public_state(None)
        self.assertFalse(state["ready"])
        for key in ("flow_id", "state", "challenge_id", "image", "expires_in",
                    "attempts", "max_attempts", "safe_error"):
            self.assertIn(key, state)
