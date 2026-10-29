"""直发传输离线测试：物理并发上限、无隐藏队列、关闭 / 过期 / HTTP 语义。

全部用替身 opener，封锁真实 urlopen；用 threading.Event 控制在途，验证 started /
completed / failed 事件归属、超时上限与事件不含 token / 完整 URL。
"""
from __future__ import annotations

import queue
import sys
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

LOCAL = Path(__file__).resolve().parents[1]
if str(LOCAL) not in sys.path:
    sys.path.insert(0, str(LOCAL))

import signup_transport
from signup_transport import MAX_PHYSICAL_INFLIGHT, DirectTransport


class FakeResponse:
    def __init__(self, status=200, body=b"{}"):
        self.status = status
        self.code = status
        self._body = body
        self.closed = False

    def read(self):
        return self._body

    def close(self):
        self.closed = True


def make_ticket(request_id, *, run_id="run-1", attempt_no=1, deadline=None, timeout_s=3.0):
    return {
        "run_id": run_id,
        "request_id": request_id,
        "attempt_no": attempt_no,
        "url": "https://school.example/rjyHdCyr/signUp/?token=SECRET_TOKEN",
        "headers": {"Accept": "application/json", "Referer": "https://school.example/"},
        "dispatch_deadline": deadline if deadline is not None else 10_000.0,
        "timeout_s": timeout_s,
    }


class ImmediateOpener:
    """按预置结果立即返回 / 抛错，记录每次调用参数。"""

    def __init__(self, results=None):
        self.results = list(results or [FakeResponse()])
        self.calls = []

    def open(self, request, timeout=None):
        self.calls.append({
            "url": request.full_url,
            "method": request.get_method(),
            "headers": {k.lower(): v for k, v in request.header_items()},
            "timeout": timeout,
        })
        kind, val = self.results.pop(0) if self.results else ("resp", FakeResponse())
        if kind == "exc":
            raise val
        return val


def drain(q):
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


def wait_events(q, timeout=3.0):
    """收集事件直到出现终态（completed / failed / not_started）；started 先于终态已入列。"""
    import time

    events = []
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            ev = q.get(timeout=0.05)
        except queue.Empty:
            continue
        events.append(ev)
        if ev["kind"] in ("completed", "failed", "not_started"):
            break
    return events


class TransportContractTests(unittest.TestCase):
    def test_completed_event_carries_run_request_attempt(self):
        q = queue.Queue()
        opener = ImmediateOpener([("resp", FakeResponse(200, b'{"code":1}'))])
        t = DirectTransport(q, opener=opener, monotonic_fn=lambda: 0.0)
        self.assertTrue(t.try_submit(make_ticket("req-1", attempt_no=7)))
        events = wait_events(q)
        done = [e for e in events if e["kind"] == "completed"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["run_id"], "run-1")
        self.assertEqual(done[0]["request_id"], "req-1")
        self.assertEqual(done[0]["attempt_no"], 7)
        self.assertEqual(done[0]["http_status"], 200)
        self.assertEqual(done[0]["body_bytes"], b'{"code":1}')

    def test_get_method_and_headers(self):
        q = queue.Queue()
        opener = ImmediateOpener([("resp", FakeResponse())])
        t = DirectTransport(q, opener=opener, monotonic_fn=lambda: 0.0)
        t.try_submit(make_ticket("req-1"))
        wait_events(q)
        self.assertEqual(opener.calls[0]["method"], "GET")
        self.assertIn("accept", opener.calls[0]["headers"])

    def test_event_does_not_leak_token_or_full_url(self):
        q = queue.Queue()
        opener = ImmediateOpener([("resp", FakeResponse())])
        t = DirectTransport(q, opener=opener, monotonic_fn=lambda: 0.0)
        t.try_submit(make_ticket("req-1"))
        events = wait_events(q)
        for e in events:
            self.assertNotIn("SECRET_TOKEN", str(e))
            self.assertNotIn("signUp", str(e))

    def test_http_error_completes_with_status_and_body(self):
        q = queue.Queue()
        err = urllib.error.HTTPError("https://x/", 500, "boom", {}, None)
        err.read = lambda: b"<html>err</html>"
        opener = ImmediateOpener([("exc", err)])
        t = DirectTransport(q, opener=opener, monotonic_fn=lambda: 0.0)
        t.try_submit(make_ticket("req-1"))
        events = wait_events(q)
        done = [e for e in events if e["kind"] == "completed"]
        self.assertEqual(done[0]["http_status"], 500)
        self.assertEqual(done[0]["body_bytes"], b"<html>err</html>")

    def test_redirect_302_returned_as_is(self):
        q = queue.Queue()
        opener = ImmediateOpener([("resp", FakeResponse(302, b""))])
        t = DirectTransport(q, opener=opener, monotonic_fn=lambda: 0.0)
        t.try_submit(make_ticket("req-1"))
        events = wait_events(q)
        done = [e for e in events if e["kind"] == "completed"]
        self.assertEqual(done[0]["http_status"], 302)  # 不跳转，原样

    def test_network_exception_reports_failed_with_class(self):
        q = queue.Queue()
        opener = ImmediateOpener([("exc", ConnectionError("reset"))])
        t = DirectTransport(q, opener=opener, monotonic_fn=lambda: 0.0)
        t.try_submit(make_ticket("req-1"))
        events = wait_events(q)
        failed = [e for e in events if e["kind"] == "failed"]
        self.assertEqual(failed[0]["error_class"], "ConnectionError")

    def test_timeout_capped_by_budget_and_hard_limit(self):
        q = queue.Queue()
        opener = ImmediateOpener([("resp", FakeResponse())])
        t = DirectTransport(q, opener=opener, monotonic_fn=lambda: 0.0)
        # 剩余预算 1 秒 -> timeout<=1；timeout_s=5 与 8 硬上限都不越
        t.try_submit(make_ticket("req-1", deadline=1.0, timeout_s=5.0))
        wait_events(q)
        self.assertLessEqual(opener.calls[0]["timeout"], 1.0)

    def test_duplicate_request_id_rejected(self):
        q = queue.Queue()
        opener = ImmediateOpener([("resp", FakeResponse())] * 2)
        t = DirectTransport(q, opener=opener, monotonic_fn=lambda: 0.0)
        self.assertTrue(t.try_submit(make_ticket("req-1")))
        self.assertFalse(t.try_submit(make_ticket("req-1")))  # 重复拒绝
        events = wait_events(q)
        # 只有 req-1 的 started/completed，没有 req-2 的任何事件
        self.assertTrue(events)
        self.assertEqual({e["request_id"] for e in events}, {"req-1"})


class ConcurrencyCapTests(unittest.TestCase):
    def _controlled_opener(self):
        class Controlled:
            def __init__(self):
                self.gate = threading.Event()
                self.entered = 0
                self.lock = threading.Lock()
                self.entered_evt = threading.Event()

            def open(self, request, timeout=None):
                with self.lock:
                    self.entered += 1
                self.entered_evt.set()
                self.gate.wait(timeout=5)
                return FakeResponse()

        return Controlled()

    def test_third_submit_rejected_when_two_in_flight(self):
        q = queue.Queue()
        opener = self._controlled_opener()
        t = DirectTransport(q, opener=opener, monotonic_fn=lambda: 0.0)
        self.assertTrue(t.try_submit(make_ticket("req-1")))
        self.assertTrue(t.try_submit(make_ticket("req-2")))
        # 等两笔都在 open 中
        import time
        end = time.monotonic() + 3.0
        while opener.entered < 2 and time.monotonic() < end:
            time.sleep(0.01)
        self.assertEqual(opener.entered, 2)
        self.assertEqual(t.active_count(), MAX_PHYSICAL_INFLIGHT)
        # 第三笔立即拒绝，无隐藏队列
        self.assertFalse(t.try_submit(make_ticket("req-3")))
        self.assertEqual(t.active_count(), MAX_PHYSICAL_INFLIGHT)
        # 释放阻塞：两笔完成，无多余排队（只有 req-1/req-2 的 started/completed）
        opener.gate.set()
        events = self._wait(q, 4)
        ids = {e["request_id"] for e in events}
        self.assertEqual(ids, {"req-1", "req-2"})
        self.assertEqual(t.active_count(), 0)
        # 释放后可再提交
        self.assertTrue(t.try_submit(make_ticket("req-4")))

    def test_close_returns_promptly_and_blocks_new(self):
        q = queue.Queue()
        opener = self._controlled_opener()
        t = DirectTransport(q, opener=opener, monotonic_fn=lambda: 0.0)
        t.try_submit(make_ticket("req-1"))
        import time
        end = time.monotonic() + 3.0
        while opener.entered < 1 and time.monotonic() < end:
            time.sleep(0.01)
        # close 不等待在途线程，立即返回
        start = time.monotonic()
        t.close()
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertFalse(t.try_submit(make_ticket("req-2")))  # 关闭后不接纳
        # 在途那笔仍会完成，只影响旧队列
        opener.gate.set()
        events = self._wait(q, 2)
        self.assertIn("req-1", {e["request_id"] for e in events})

    def _wait(self, q, n, timeout=3.0):
        import time
        events = []
        end = time.monotonic() + timeout
        while len(events) < n and time.monotonic() < end:
            try:
                events.append(q.get(timeout=0.05))
            except queue.Empty:
                pass
        return events


class LifecycleGuardTests(unittest.TestCase):
    def test_expired_ticket_not_started(self):
        q = queue.Queue()
        opener = ImmediateOpener([("resp", FakeResponse())])
        t = DirectTransport(q, opener=opener, monotonic_fn=lambda: 100.0)
        # 截止在过去 -> 过期
        self.assertTrue(t.try_submit(make_ticket("req-1", deadline=50.0)))
        events = self._wait(q, 1)
        self.assertEqual(events[0]["kind"], "not_started")
        self.assertEqual(opener.calls, [])  # 未真正 open

    def test_thread_start_failure_produces_no_started(self):
        q = queue.Queue()
        opener = ImmediateOpener([("resp", FakeResponse())])
        t = DirectTransport(q, opener=opener, monotonic_fn=lambda: 0.0)
        with mock.patch("threading.Thread.start", side_effect=RuntimeError("no thread")):
            self.assertFalse(t.try_submit(make_ticket("req-1")))
        self.assertEqual(drain(q), [])  # 无 started，不耗 attempts
        self.assertEqual(t.active_count(), 0)  # 名额已回收

    def _wait(self, q, n, timeout=3.0):
        import time
        events = []
        end = time.monotonic() + timeout
        while len(events) < n and time.monotonic() < end:
            try:
                events.append(q.get(timeout=0.05))
            except queue.Empty:
                pass
        return events


if __name__ == "__main__":
    unittest.main()
