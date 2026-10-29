"""直发传输离线测试：物理并发上限、无隐藏队列、关闭 / 过期 / HTTP 语义。

全部用替身 opener，封锁真实 urlopen；用 threading.Event 控制在途，验证 started /
completed / failed 事件归属、超时上限与事件不含 token / 完整 URL。
"""
from __future__ import annotations

import io
import queue
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
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


def make_ticket(request_id, *, run_id="run-1", attempt_no=1, deadline=None,
                request_deadline=None, timeout_s=3.0):
    return {
        "run_id": run_id,
        "request_id": request_id,
        "attempt_no": attempt_no,
        "url": "https://school.example/rjyHdCyr/signUp/?token=SECRET_TOKEN",
        "headers": {"Accept": "application/json, text/plain, */*",
                    "token": "test-token",
                    "Referer": "https://school.example/stu/",
                    "User-Agent": "test-agent"},
        "dispatch_deadline": deadline if deadline is not None else 10_000.0,
        "request_deadline": request_deadline if request_deadline is not None else 20_000.0,
        "timeout_s": timeout_s,
    }


class NoNetworkGuard:
    """封锁真实 urlopen 与 socket 连接：所有请求必须走替身 opener。"""

    def setUp(self):
        super().setUp()
        for target in ("urllib.request.urlopen", "socket.create_connection"):
            patcher = mock.patch(target, side_effect=AssertionError("真实网络被封锁"))
            patcher.start()
            self.addCleanup(patcher.stop)


class AdvanceLock:
    """锁包装器：首次 __enter__ 把虚拟 now 推到指定值，模拟等锁期间时间流逝。"""

    def __init__(self, real, clock, new_now):
        self._real = real
        self._clock = clock
        self._new_now = new_now
        self._first = True
        self._mutex = threading.Lock()

    def __enter__(self):
        with self._mutex:
            first, self._first = self._first, False
        if first:
            self._clock["now"] = self._new_now
        return self._real.__enter__()

    def __exit__(self, *args):
        return self._real.__exit__(*args)


class GateLock:
    """锁包装器：首次 __enter__ 挂起在获取锁之前，由测试显式放行。"""

    def __init__(self, real):
        self._real = real
        self.gate = threading.Event()
        self.arrived = threading.Event()
        self._first = True
        self._mutex = threading.Lock()

    def __enter__(self):
        with self._mutex:
            first, self._first = self._first, False
        if first:
            self.arrived.set()
            self.gate.wait(timeout=5)
        return self._real.__enter__()

    def __exit__(self, *args):
        return self._real.__exit__(*args)


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


class TransportContractTests(NoNetworkGuard, unittest.TestCase):
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
        headers = opener.calls[0]["headers"]
        self.assertEqual(headers.get("accept"), "application/json, text/plain, */*",
                         "Accept 与 _get 契约一致")
        self.assertEqual(headers.get("token"), "test-token", "token 必须传到 Request")
        self.assertEqual(headers.get("referer"), "https://school.example/stu/")
        self.assertEqual(headers.get("user-agent"), "test-agent")

    def test_event_does_not_leak_token_or_full_url(self):
        q = queue.Queue()
        opener = ImmediateOpener([("resp", FakeResponse())])
        t = DirectTransport(q, opener=opener, monotonic_fn=lambda: 0.0)
        t.try_submit(make_ticket("req-1"))
        events = wait_events(q)
        for e in events:
            self.assertNotIn("SECRET_TOKEN", str(e))
            self.assertNotIn("test-token", str(e))
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
        # 网络 timeout 受 request_deadline 剩余与 8 秒硬上限截断（F02 后预算由
        # request_deadline 承担，dispatch_deadline 只管最晚启动）。
        q = queue.Queue()
        opener = ImmediateOpener([("resp", FakeResponse())])
        t = DirectTransport(q, opener=opener, monotonic_fn=lambda: 0.0)
        # request_deadline 剩余 1 秒 -> timeout<=1；timeout_s=5 不越
        t.try_submit(make_ticket("req-1", deadline=10_000.0, request_deadline=1.0,
                                 timeout_s=5.0))
        wait_events(q)
        self.assertLessEqual(opener.calls[0]["timeout"], 1.0)
        # 硬上限 8 秒：timeout_s=20 与宽裕预算都不越
        q2 = queue.Queue()
        opener2 = ImmediateOpener([("resp", FakeResponse())])
        t2 = DirectTransport(q2, opener=opener2, monotonic_fn=lambda: 0.0)
        t2.try_submit(make_ticket("req-2", deadline=10_000.0, request_deadline=100.0,
                                  timeout_s=20.0))
        wait_events(q2)
        self.assertLessEqual(opener2.calls[0]["timeout"], 8.0)

    def test_network_timeout_not_truncated_by_dispatch_window(self):
        # F02：50ms 发射窗不得误截网络 timeout。now=100、dispatch=100.05、
        # request_deadline=103、timeout_s=3 -> opener 收到接近 3 秒；
        # now 前进 0.02 后 <= 2.98（request_deadline 剩余说了算）。
        q = queue.Queue()
        opener = ImmediateOpener([("resp", FakeResponse())])
        t = DirectTransport(q, opener=opener, monotonic_fn=lambda: 100.0)
        t.try_submit(make_ticket("req-1", deadline=100.05, request_deadline=103.0,
                                 timeout_s=3.0))
        wait_events(q)
        self.assertGreater(opener.calls[0]["timeout"], 2.9,
                           "网络 timeout 不得被 50ms 发射窗截断")
        self.assertLessEqual(opener.calls[0]["timeout"], 3.0)
        q2 = queue.Queue()
        opener2 = ImmediateOpener([("resp", FakeResponse())])
        t2 = DirectTransport(q2, opener=opener2, monotonic_fn=lambda: 100.02)
        t2.try_submit(make_ticket("req-2", deadline=100.05, request_deadline=103.0,
                                  timeout_s=3.0))
        wait_events(q2)
        self.assertLessEqual(opener2.calls[0]["timeout"], 2.98 + 1e-6)
        self.assertGreater(opener2.calls[0]["timeout"], 2.9)

    def test_deadline_boundary_not_started_no_open(self):
        # dispatch_deadline 恰好到点 / request_deadline 到点 / 预算不足 / 非有限：
        # 只发 not_started、不调用 opener、不把 timeout=0 传入网络。
        cases = (
            ("dispatch 到点", dict(deadline=100.0, request_deadline=103.0, timeout_s=3.0)),
            ("request 到点", dict(deadline=105.0, request_deadline=100.0, timeout_s=3.0)),
            ("request 已过", dict(deadline=105.0, request_deadline=99.0, timeout_s=3.0)),
            ("timeout 为零", dict(deadline=105.0, request_deadline=103.0, timeout_s=0.0)),
            ("timeout 非有限", dict(deadline=105.0, request_deadline=103.0,
                                    timeout_s=float("nan"))),
        )
        for name, kwargs in cases:
            with self.subTest(name):
                q = queue.Queue()
                opener = ImmediateOpener([("resp", FakeResponse())])
                t = DirectTransport(q, opener=opener, monotonic_fn=lambda: 100.0)
                self.assertTrue(t.try_submit(make_ticket("req-1", **kwargs)))
                events = wait_events(q)
                self.assertEqual([e["kind"] for e in events], ["not_started"],
                                 "%s：只发 not_started" % name)
                self.assertEqual(opener.calls, [], "%s：不得调用 opener" % name)

    def test_real_default_opener_redirect_handler_does_not_follow(self):
        # 真实 _build_default_opener 的 redirect handler：离线构造 302 响应
        # 验证 redirect_request 返回 None（不跟随），不用替身返回 302 代替验证。
        opener = signup_transport._build_default_opener()
        handlers = [h for h in opener.handlers
                    if isinstance(h, urllib.request.HTTPRedirectHandler)]
        self.assertTrue(handlers, "默认 opener 必须安装 redirect handler")
        req = urllib.request.Request("https://school.example/rjyHdCyr/signUp/x",
                                     method="GET")
        fp = io.BytesIO(b"")
        headers = {"Location": "https://school.example/elsewhere"}
        result = handlers[0].redirect_request(
            req, fp, 302, "Found", headers, headers["Location"])
        self.assertIsNone(result, "3xx 必须原样返回：redirect_request 不产生新请求")

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


class ConcurrencyCapTests(NoNetworkGuard, unittest.TestCase):
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

    def test_close_before_admission_cancelled_not_started(self):
        # F04：线程挂在接纳锁门外（获取锁之前，不持锁）。close 后放行，
        # 必须 not_started、open=0、无 started——close 返回后不再产生新 started。
        # 受控线程入口直接跑 _run，避免 try_submit 自身的锁入口抢占 gate。
        q = queue.Queue()
        opener = ImmediateOpener([("resp", FakeResponse())])
        t = DirectTransport(q, opener=opener, monotonic_fn=lambda: 100.0)
        gate = GateLock(t._lock)
        t._lock = gate
        th = threading.Thread(
            target=t._run,
            args=(make_ticket("req-1", deadline=200.0, request_deadline=300.0),),
            daemon=True)
        th.start()
        self.assertTrue(gate.arrived.wait(timeout=3), "线程应已挂在提交点之前")
        t.close()
        gate.gate.set()
        th.join(timeout=3)
        events = wait_events(q)
        self.assertEqual([e["kind"] for e in events], ["not_started"],
                         "未跨接纳点由 close 取消为 not_started")
        self.assertEqual(opener.calls, [], "取消的 ticket 不得 open")

    def test_close_after_admission_started_committed_no_wait(self):
        # F04：已跨接纳点者 close 返回前 started 已入队、只开一次；
        # close 不等待阻塞 open/read，不假称撤销，不补 not_started。
        q = queue.Queue()
        release = threading.Event()
        entered_open = threading.Event()
        calls = []

        class BlockingOpener:
            def open(self, request, timeout=None):
                calls.append(timeout)
                entered_open.set()
                release.wait(timeout=5)
                return FakeResponse()

        t = DirectTransport(q, opener=BlockingOpener(), monotonic_fn=lambda: 100.0)
        self.assertTrue(t.try_submit(make_ticket("req-1", deadline=200.0,
                                                 request_deadline=300.0)))
        self.assertTrue(entered_open.wait(timeout=3), "线程应已跨接纳点并进入 open")
        # 提交点先于 open：此时 started 必已入队
        started = [e for e in drain(q) if e["kind"] == "started"]
        self.assertEqual(len(started), 1, "started 必须在 close 前入队")
        start = time.monotonic()
        t.close()
        self.assertLess(time.monotonic() - start, 0.5,
                        "close 不等待阻塞 open/read")
        self.assertFalse(release.is_set(), "open 仍应处于阻塞")
        release.set()
        events = wait_events(q)
        self.assertEqual([e["kind"] for e in events], ["completed"],
                         "close 不补 not_started、不重复 started")
        self.assertEqual(len(calls), 1, "只开一次")

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


class LifecycleGuardTests(NoNetworkGuard, unittest.TestCase):
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


class AdmissionLockTests(NoNetworkGuard, unittest.TestCase):
    """F02/F04 返修：时间读数必须在锁内接纳提交点完成。

    线程等锁期间越过 dispatch/request 截止时，依旧使用旧 now 发射是缺陷：
    检查与 started 入队必须共用同一把锁内的新鲜读数。
    """

    def test_lock_wait_past_dispatch_deadline_not_started(self):
        # 复现 deadline-lock-race：进锁时 now 已越过 dispatch_deadline，
        # 预期只有 not_started、open=0（不得用旧 now 发射）。
        q = queue.Queue()
        opener = ImmediateOpener([("resp", FakeResponse())])
        clock = {"now": 100.0}
        t = DirectTransport(q, opener=opener, monotonic_fn=lambda: clock["now"])
        t._lock = AdvanceLock(t._lock, clock, 100.06)
        t._run(make_ticket("req-1", deadline=100.05, request_deadline=103.0,
                           timeout_s=3.0))
        events = drain(q)
        self.assertEqual([e["kind"] for e in events], ["not_started"],
                         "等锁越过 dispatch_deadline 不得发射")
        self.assertEqual(opener.calls, [], "到点不得调用 opener")

    def test_lock_wait_past_request_deadline_not_started(self):
        # 同类红例：进锁时 now 已越过 request_deadline（网络预算截止）。
        q = queue.Queue()
        opener = ImmediateOpener([("resp", FakeResponse())])
        clock = {"now": 100.0}
        t = DirectTransport(q, opener=opener, monotonic_fn=lambda: clock["now"])
        t._lock = AdvanceLock(t._lock, clock, 100.06)
        t._run(make_ticket("req-1", deadline=105.0, request_deadline=100.05,
                           timeout_s=3.0))
        events = drain(q)
        self.assertEqual([e["kind"] for e in events], ["not_started"],
                         "等锁越过 request_deadline 不得发射")
        self.assertEqual(opener.calls, [], "到点不得调用 opener")

    def test_timeout_uses_in_lock_now_when_not_expired(self):
        # 未过期时 timeout 必须用锁内新 now 计算：now 100→100.02 后
        # timeout=min(3,8,103-100.02)=2.98，不得仍是旧 now 算出的 3.0。
        q = queue.Queue()
        opener = ImmediateOpener([("resp", FakeResponse())])
        clock = {"now": 100.0}
        t = DirectTransport(q, opener=opener, monotonic_fn=lambda: clock["now"])
        t._lock = AdvanceLock(t._lock, clock, 100.02)
        t._run(make_ticket("req-1", deadline=105.0, request_deadline=103.0,
                           timeout_s=3.0))
        events = wait_events(q)
        self.assertEqual([e["kind"] for e in events], ["started", "completed"])
        self.assertLessEqual(opener.calls[0]["timeout"], 2.98 + 1e-6,
                             "timeout 必须按锁内新 now 截断")
        self.assertGreater(opener.calls[0]["timeout"], 2.9)


if __name__ == "__main__":
    unittest.main()
