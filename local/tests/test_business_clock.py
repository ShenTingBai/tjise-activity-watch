#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立业务时钟的行为回归：网络样本经替换 socket/DNS 边界驱动真实 sync。

覆盖：系统日期无关性（±30 天/跳变）、样本校验（nonce/LI/stratum/era/重放/根延迟/
处理耗时）、一致簇（两源合格/单源不足/分歧拒绝）、断网有界、跨进程快照、
旧 owner/坏 JSON/过期、holdover 漂移与 900 秒上限、代次单调与迟到结果丢弃、
关闭后不写快照。全部网络替换，不真正修改系统时钟。

@author LiXinwen
"""
from __future__ import annotations

import itertools
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL = os.path.dirname(HERE)
if LOCAL not in sys.path:
    sys.path.insert(0, LOCAL)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from business_clock import (  # noqa: E402
    BusinessClock, ClockClient, ClockUnavailable, SNTPPacketError,
    build_client_packet, parse_server_packet, _elapsed_tick,
)
from fixtures.business_clock import ntp_response  # noqa: E402

TARGET = 1793100000.0          # 假服务器时间（2026 年，与系统日期无关）
SOURCES = ("src-a", "src-b", "src-c")


class Tick:
    """可控经过时长：模拟 GetTickCount64 域（含睡眠推进）。"""

    def __init__(self, value=1000.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += float(seconds)


def echo_transport(tick, base_epoch=TARGET, bad=None, delay=0.0, rtt=0.08,
                   live_sources=SOURCES):
    """注入传输：回显 originate，按 base_epoch 造合法响应；bad[source] 可注入缺陷。
    delay>0 时响应耗时超过任何期限（黑洞）。返回真实发/收 tick，与生产同形。"""
    def transport(source, packet, timeout_s):
        if delay:
            time.sleep(delay)
        if source not in live_sources:
            raise OSError("源不可用")
        nonce = packet[40:48]          # 完整 64 位本轮标识
        send_tick = tick()
        recv_epoch = base_epoch - rtt / 2.0
        xmit_epoch = base_epoch
        kw = dict(root_delay=0.05, dispersion=0.01)
        if bad and source in bad:
            kw.update(bad[source])
        resp = ntp_response(nonce, recv_epoch, xmit_epoch, **kw)
        tick.advance(rtt)
        return resp, send_tick, send_tick + rtt, True
    return transport


class WallClockIndependenceTests(unittest.TestCase):
    """业务时刻来自网络锚点+经过时长，与系统墙钟日期无关。"""

    def test_sync_ready_with_two_consistent_sources(self):
        tick = Tick(1000.0)
        clock = BusinessClock(sources=SOURCES, tick_fn=tick,
                              transport=echo_transport(tick),
                              snapshot_dir=tempfile.mkdtemp(prefix="tjise-clock-t-"))
        state = clock.sync()
        self.assertEqual(state, "ready")
        self.assertLess(abs(clock.now() - TARGET), 0.3,
                        "业务时刻必须等于假服务器时间，而非本机 time.time()")
        lower, upper = clock.bounds()
        self.assertLessEqual(lower, clock.now() + 1e-9)
        self.assertGreaterEqual(upper, clock.now() - 1e-9)
        self.assertLess(upper - lower, 1.0)
        clock.close()

    def test_os_wall_clock_days_off_does_not_change_business_time(self):
        tick = Tick(1000.0)
        clock = BusinessClock(sources=SOURCES, tick_fn=tick,
                              transport=echo_transport(tick),
                              snapshot_dir=tempfile.mkdtemp(prefix="tjise-clock-t-"))
        clock.sync()
        before = clock.now()
        # 系统墙钟错误 ±30 天/跨年：业务时钟绝不跟跳（实现不得读 time.time 参与关系）
        for offset in (30 * 86400.0, -30 * 86400.0, 366 * 86400.0):
            with mock.patch("time.time", return_value=TARGET + offset):
                self.assertLess(abs(clock.now() - before), 1.0,
                                "OS 墙钟跳 %d 天不得改变业务时刻" % (offset / 86400))
        tick.advance(600.0)     # 真实经过 600 秒：业务时刻推进 600 秒（未超持有上限）
        self.assertLess(abs(clock.now() - before - 600.0), 1.0)
        clock.close()

    def test_epoch_decoded_in_2020_2099_era(self):
        resp = ntp_response(123456, TARGET - 0.01, TARGET)
        parsed = parse_server_packet(resp, 123456, "x")
        self.assertEqual(parsed["server_xmit"], TARGET)
        with self.assertRaises(SNTPPacketError):
            parse_server_packet(b"\x00" * 40, 123456, "x")   # 畸形长度


class SampleValidationTests(unittest.TestCase):
    """不合格样本一律拒绝；无唯一一致簇则不建立锚点。"""

    def _sync_state(self, bad=None, live=SOURCES, base=TARGET):
        tick = Tick(2000.0)
        clock = BusinessClock(sources=SOURCES, tick_fn=tick,
                              transport=echo_transport(tick, base_epoch=base,
                                                       bad=bad, live_sources=live),
                              snapshot_dir=tempfile.mkdtemp(prefix="tjise-clock-t-"))
        state = clock.sync()
        clock.close()
        return state, clock

    def test_bad_samples_rejected(self):
        """任一字段缺陷在全部源上出现时，不得形成锚点（样本全部拒绝）。"""
        tick = Tick(2000.0)

        def make_transport(kw=None, nonce_delta=0):
            def transport(source, packet, timeout_s):
                nonce = packet[40:48]
                if nonce_delta:
                    # 只改小数部分：秒的高 32 位相同，正是「同秒另一个请求」的重放
                    nonce = nonce[:7] + bytes([(nonce[7] + nonce_delta) & 0xFF])
                resp = ntp_response(nonce, TARGET - 0.01, TARGET,
                                    **(kw or {}))
                tick.advance(0.08)
                return resp, tick()
            return transport

        cases = {
            "wrong_nonce": dict(nonce_delta=1),
            "li_unsync": dict(kw={"li": 3}),
            "stratum_zero": dict(kw={"stratum": 0}),
            "stratum_kod": dict(kw={"stratum": 16}),
            "version3": dict(kw={"vn": 3}),
            "client_mode": dict(kw={"mode": 3}),
        }
        for name, spec in cases.items():
            with self.subTest(case=name):
                clock = BusinessClock(sources=SOURCES, tick_fn=Tick(2000.0),
                                      transport=make_transport(**spec),
                                      snapshot_dir=tempfile.mkdtemp(prefix="tjise-clock-t-"))
                clock.sync()
                self.assertEqual(clock.generation, 0, name)
                with self.assertRaises(ClockUnavailable):
                    clock.now()
                clock.close()

    def test_server_processing_time_bounds(self):
        # 服务器处理耗时（xmit-recv）超过本地 RTT：拒绝
        tick = Tick(2000.0)

        def transport(source, packet, timeout_s):
            nonce = packet[40:48]
            resp = ntp_response(nonce, TARGET - 3.0, TARGET)   # 处理 3 秒
            tick.advance(0.08)
            return resp, tick()

        clock = BusinessClock(sources=SOURCES, tick_fn=tick, transport=transport,
                              snapshot_dir=tempfile.mkdtemp(prefix="tjise-clock-t-"))
        clock.sync()
        self.assertEqual(clock.generation, 0)
        clock.close()

    def test_single_source_cannot_form_cluster(self):
        state, clock = self._sync_state(live=("src-a",))
        self.assertEqual(clock.generation, 0, "单源不足，不能任选一个样本")
        with self.assertRaises(ClockUnavailable):
            clock.now()

    def test_disagreement_between_sources_rejected(self):
        """三源两两相差 5 秒：没有任何一致对，必须拒绝而非任选。"""
        tick = Tick(2000.0)

        def transport(source, packet, timeout_s):
            nonce = packet[40:48]
            base = TARGET + {"src-a": 0.0, "src-b": 5.0, "src-c": 10.0}[source]
            resp = ntp_response(nonce, base - 0.01, base)
            tick.advance(0.08)
            return resp, tick()

        clock = BusinessClock(sources=SOURCES, tick_fn=tick, transport=transport,
                              snapshot_dir=tempfile.mkdtemp(prefix="tjise-clock-t-"))
        clock.sync()
        self.assertEqual(clock.generation, 0, "分歧簇必须拒绝，不能任选")
        clock.close()


class BoundedFailureTests(unittest.TestCase):
    """断网/DNS 不回：调用方期限到即返回，失败有原因、不回退墙钟。"""

    def test_total_blackhole_returns_within_deadline(self):
        tick = Tick(1000.0)
        clock = BusinessClock(sources=SOURCES, tick_fn=tick,
                              transport=echo_transport(tick, live_sources=()),
                              snapshot_dir=tempfile.mkdtemp(prefix="tjise-clock-t-"))
        started = time.monotonic()
        state = clock.sync()
        elapsed = time.monotonic() - started
        self.assertEqual(state, "unavailable")
        self.assertLess(elapsed, 6.0, "全部黑洞也必须 5 秒期限内返回")
        with self.assertRaises(ClockUnavailable):
            clock.now()
        self.assertTrue(clock.reason)
        clock.close()

    def test_blocking_transport_is_bounded(self):
        tick = Tick(1000.0)
        block = threading.Event()

        def transport(source, packet, timeout_s):
            block.wait(30)            # 模拟永久阻塞的 DNS/接收
            raise OSError("取消")

        clock = BusinessClock(sources=SOURCES, tick_fn=tick, transport=transport,
                              snapshot_dir=tempfile.mkdtemp(prefix="tjise-clock-t-"))
        started = time.monotonic()
        clock.sync()
        self.assertLess(time.monotonic() - started, 6.0)
        clock.close()
        block.set()

    def test_late_response_after_deadline_is_dropped(self):
        tick = Tick(1000.0)
        gate = threading.Event()

        def transport(source, packet, timeout_s):
            gate.wait(8)              # 第一轮全部迟到（8 秒 > 5 秒期限，消除竞态）
            tick.advance(0.08)
            nonce = packet[40:48]
            return ntp_response(nonce, TARGET - 0.01, TARGET), tick()

        clock = BusinessClock(sources=SOURCES, tick_fn=tick, transport=transport,
                              snapshot_dir=tempfile.mkdtemp(prefix="tjise-clock-t-"))
        state = clock.sync()
        self.assertEqual(state, "unavailable")
        self.assertEqual(clock.generation, 0, "迟到响应不得成为样本")
        gate.set()
        clock.close()


class SnapshotAndClientTests(unittest.TestCase):
    """跨进程快照：同实例允许，跨会话/坏数据一律不可用。"""

    def _ready_clock(self, tick=None, snapshot_dir=None):
        tick = tick or Tick(1000.0)
        clock = BusinessClock(sources=SOURCES, tick_fn=tick,
                              transport=echo_transport(tick),
                              snapshot_dir=snapshot_dir
                              or tempfile.mkdtemp(prefix="tjise-clock-t-"))
        clock.sync()
        return clock, tick

    def test_snapshot_fields_and_file(self):
        clock, tick = self._ready_clock()
        snap = clock.snapshot()
        self.assertEqual(set(snap), {"schema", "owner_id", "generation", "state",
                                     "epoch_s", "tick_s", "sampled_tick_s",
                                     "uncertainty_s", "source", "age_s", "reason", "sync_status", "sync_round"})
        self.assertEqual(snap["sync_status"], "succeeded")
        self.assertEqual(snap["schema"], 1)
        path = clock._snapshot_path()
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["owner_id"], clock.owner_id)
        self.assertNotIn("token", json.dumps(data))   # 无凭证字段
        clock.close()

    def test_client_reads_same_instance_snapshot(self):
        clock, tick = self._ready_clock()
        client = ClockClient(snapshot_path=clock._snapshot_path(),
                             owner_id=clock.owner_id, tick_fn=tick)
        self.assertLess(abs(client.now() - clock.now()), 0.5)
        lower, upper = client.bounds()
        self.assertLessEqual(lower, upper)
        clock.close()

    def test_client_rejects_bad_snapshots(self):
        clock, tick = self._ready_clock()
        path = clock._snapshot_path()
        # 旧 owner
        with self.assertRaises(ClockUnavailable):
            ClockClient(snapshot_path=path, owner_id="someone-else",
                        tick_fn=tick).now()
        # 坏 JSON
        bad_dir = tempfile.mkdtemp(prefix="tjise-clock-t-")
        bad_path = os.path.join(bad_dir, "broken.json")
        with open(bad_path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        with self.assertRaises(ClockUnavailable):
            ClockClient(snapshot_path=bad_path, tick_fn=tick).now()
        # NaN / 无限字段
        nan_path = os.path.join(bad_dir, "nan.json")
        with open(nan_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"schema": 1, "state": "ready",
                                 "epoch_s": float("inf"),
                                 "sampled_tick_s": 1.0,
                                 "uncertainty_s": 0.1,
                                 "owner_id": clock.owner_id}))
        with self.assertRaises(ClockUnavailable):
            ClockClient(snapshot_path=nan_path, tick_fn=tick).now()
        # 未知状态
        unk = dict(clock.snapshot(), state="syncing")
        unk_path = os.path.join(bad_dir, "unk.json")
        with open(unk_path, "w", encoding="utf-8") as fh:
            json.dump(unk, fh)
        with self.assertRaises(ClockUnavailable):
            ClockClient(snapshot_path=unk_path, tick_fn=tick).now()
        clock.close()

    def test_cross_process_snapshot_consumption(self):
        """两个真实临时 Python 进程共用无凭证快照：读数一致且等于父实例权威值。

        只比较两个子进程互相相等是不够的——同一错误实现会得到相同的错值。
        父 BusinessClock 的 now 才是权威参照。
        """
        clock, tick = self._ready_clock()
        path = clock._snapshot_path()
        authority = clock.now()
        worker_src = (
            "import sys, json;"
            "sys.path.insert(0, %r); sys.path.insert(0, %r);"
            "from business_clock import ClockClient;"
            "c = ClockClient(snapshot_path=%r, owner_id=%r, tick_fn=lambda: %r);"
            "print(json.dumps({'now': c.now(), 'bounds': list(c.bounds())}))"
            % (LOCAL, HERE, path, clock.owner_id, tick.value)
        )
        env = dict(os.environ, TJISE_CLOCK_SNAPSHOT=path,
                   TJISE_CLOCK_OWNER=clock.owner_id,
                   PYTHONPATH=LOCAL + os.pathsep + HERE,
                   PYTHONDONTWRITEBYTECODE="1")
        env.pop("PYTHONPYCACHEPREFIX", None)
        for _ in range(2):
            done = subprocess.run([sys.executable, "-X", "utf8", "-c", worker_src],
                                  capture_output=True, text=True, timeout=30,
                                  env=env, cwd=tempfile.gettempdir())
            self.assertEqual(done.returncode, 0, done.stderr[-400:])
            data = json.loads(done.stdout.strip().splitlines()[-1])
            self.assertLess(abs(data["now"] - authority), 0.5,
                            "子进程读数必须等于父 BusinessClock 的权威时刻")
        clock.close()


def _snapshot_doc(**over) -> dict:
    """最小合法快照文档：anchor=tick 100、生成于 sampled 105，供客户端合同测试。"""
    doc = {"schema": 1, "owner_id": "own-1", "generation": 3, "state": "ready",
           "epoch_s": TARGET, "tick_s": 100.0, "sampled_tick_s": 105.0,
           "uncertainty_s": 0.15, "source": "NTP:src-a+2源", "age_s": 5.0,
           "reason": "北京时间已同步"}
    doc.update(over)
    return doc


class ClockClientContractTests(unittest.TestCase):
    """ClockClient 与生产 RetryBudget 的接口合同（C01/C02 红例）。

    anchor tick 与 snapshot 生成 tick 必须分开：epoch_s 只对应 tick_s；
    延迟样本必须得到 epoch + 10，而不是 epoch + 5。
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tjise-clock-client-")
        self.path = os.path.join(self.dir, "snap.json")

    def _write(self, doc):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False)
        return self.path

    def test_delayed_sample_uses_anchor_tick_not_sampled_tick(self):
        # anchor=100、采样=105、读取=110 → 正确 epoch+10；用 sampled 会错成 +5
        path = self._write(_snapshot_doc())
        client = ClockClient(snapshot_path=path, owner_id="own-1",
                             tick_fn=lambda: 110.0)
        self.assertAlmostEqual(client.now(), TARGET + 10.0, places=6,
                               msg="now 必须以 tick_s 为锚点，不能用 sampled_tick_s")

    def test_client_satisfies_retry_budget_contract(self):
        """真实 ClockClient 必须能作为生产预算的时钟：elapsed_s/bounds/state 齐全。"""
        path = self._write(_snapshot_doc())
        client = ClockClient(snapshot_path=path, owner_id="own-1",
                             tick_fn=lambda: 110.0)
        for name in ("now", "bounds", "elapsed_s", "state", "snapshot"):
            self.assertTrue(callable(getattr(client, name, None)), name)
        self.assertIsInstance(client.reason, str)
        self.assertTrue(client.reason)
        self.assertEqual(client.owner_id, "own-1")
        self.assertEqual(client.generation, 3)
        self.assertIsInstance(client.elapsed_s(), float)
        from time_contract import RetryBudget, BudgetExhausted
        budget = RetryBudget(TARGET + 60.0, business_clock=client)
        self.assertLessEqual(budget.remaining_s(), 60.0)
        with mock.patch.object(client, "elapsed_s", return_value=170.0):
            self.assertEqual(budget.remaining_s(), 0.0)
            with self.assertRaises(BudgetExhausted):
                budget.check()

    def test_rewrite_at_899s_does_not_extend_past_900s(self):
        # 锚点 tick=0，主程序在 899 秒重写快照（sampled=899）；901 秒读取仍须拒绝
        path = self._write(_snapshot_doc(tick_s=0.0, sampled_tick_s=899.0))
        client = ClockClient(snapshot_path=path, owner_id="own-1",
                             tick_fn=lambda: 901.0)
        with self.assertRaises(ClockUnavailable):
            client.now()
        with self.assertRaises(ClockUnavailable):
            client.bounds()

    def test_uncertainty_drift_exceeding_limit_rejected(self):
        # 快照时刻误差 0.45 秒；采样后漂移 0.02 秒仍可读，漂移 0.06 秒后超 0.5 秒拒绝
        path = self._write(_snapshot_doc(uncertainty_s=0.45))
        ok = ClockClient(snapshot_path=path, owner_id="own-1", tick_fn=lambda: 305.0)
        self.assertLess(ok.bounds()[1] - ok.now(), 0.5)
        bad = ClockClient(snapshot_path=path, owner_id="own-1", tick_fn=lambda: 705.0)
        with self.assertRaises(ClockUnavailable):
            bad.now()
        with self.assertRaises(ClockUnavailable):
            bad.bounds()

    def test_negative_tick_delta_rejected(self):
        path = self._write(_snapshot_doc(tick_s=200.0, sampled_tick_s=200.0))
        client = ClockClient(snapshot_path=path, owner_id="own-1",
                             tick_fn=lambda: 110.0)
        with self.assertRaises(ClockUnavailable):
            client.now()

    def test_negative_uncertainty_and_nan_rejected(self):
        for over in ({"uncertainty_s": -0.1}, {"uncertainty_s": float("nan")},
                     {"epoch_s": float("inf")}, {"tick_s": None}):
            with self.subTest(over=over):
                path = self._write(_snapshot_doc(**over))
                client = ClockClient(snapshot_path=path, owner_id="own-1",
                                     tick_fn=lambda: 110.0)
                with self.assertRaises(ClockUnavailable):
                    client.now()

    def test_owner_mismatch_and_missing_owner_rejected(self):
        path = self._write(_snapshot_doc(owner_id=""))
        client = ClockClient(snapshot_path=path, owner_id="own-1",
                             tick_fn=lambda: 110.0)
        with self.assertRaises(ClockUnavailable):
            client.now()


def _sample(epoch, tick, source="src-a", rtt=0.05, err=0.09, round_id=1):
    """直接构造一个已解析样本（Fencing 测试只验证发布规则，不重复网络解码）。"""
    return {"source": source, "epoch": epoch, "tick": tick,
            "rtt": rtt, "err": err, "round": round_id}


class NetworkRoundFenceTests(unittest.TestCase):
    """C09/C10：完整请求标识、来源校验、测量误差、代次发布与并发写。"""

    def _clock(self, tick, transport):
        return BusinessClock(sources=SOURCES, tick_fn=tick, transport=transport,
                             snapshot_dir=tempfile.mkdtemp(prefix="tjise-clock-f-"))

    def test_request_identifier_is_full_64_bit_and_nonzero(self):
        packet_a, nonce_a = build_client_packet()
        packet_b, nonce_b = build_client_packet()
        self.assertEqual(len(nonce_a), 8)
        self.assertNotEqual(nonce_a, b"\x00" * 8, "冷启动请求不得全零")
        self.assertNotEqual(nonce_a, nonce_b, "同一秒的两次请求必须可区分")
        self.assertEqual(packet_a[40:48], nonce_a)

    def test_same_second_different_request_rejected(self):
        """originate 只有秒相同、小数不同：必须拒绝（旧实现只比高 32 位会误收）。"""
        nonce = b"\x01\x02\x03\x04\x05\x06\x07\x08"
        resp = ntp_response(b"\x01\x02\x03\x04\x05\x06\x07\x09", TARGET - 0.01, TARGET)
        with self.assertRaises(SNTPPacketError):
            parse_server_packet(resp, nonce, "src-a")

    def test_wrong_peer_response_rejected(self):
        tick = Tick(2000.0)

        def transport(source, packet, timeout_s):
            nonce = packet[40:48]
            tick.advance(0.08)
            # 第三、四个元素：真实发/收 tick 与「来源不符」标记
            return (ntp_response(nonce, TARGET - 0.05, TARGET),
                    1999.9, tick(), False)

        clock = self._clock(tick, transport)
        clock.sync()
        self.assertEqual(clock.generation, 0, "错来源响应不得进入样本")
        clock.close()

    def test_server_processing_not_added_twice(self):
        """服务器处理 20ms 已经包含在 xmit 里，不能再当一次网络半程加到 epoch。

        T3=TARGET、RTT=80ms、T3-T2=20ms：网络往返 = 80-20 = 60ms，
        收包中心估计 = TARGET + 30ms。1ms 容差才能暴露多加的 10ms。
        """
        tick = Tick(2000.0)
        processing = 0.02

        def transport(source, packet, timeout_s):
            nonce = packet[40:48]
            resp = ntp_response(nonce, TARGET - processing, TARGET)
            # 发包 1999.92、收包 2000.0：rtt=80ms，其中 20ms 是服务器处理
            return resp, 1999.92, 2000.0, True

        clock = self._clock(tick, transport)
        self.assertEqual(clock.sync(), "ready")
        self.assertAlmostEqual(clock.now(), TARGET + (0.08 - processing) / 2.0,
                               delta=0.001,
                               msg="收包 epoch 只能加网络半程，不能再加处理耗时")
        clock.close()

    def test_zero_processing_uses_half_rtt(self):
        """处理耗时为 0：网络往返就是本地 RTT，收包中心 = T3 + RTT/2。"""
        tick = Tick(2000.0)

        def transport(source, packet, timeout_s):
            resp = ntp_response(packet[40:48], TARGET, TARGET)
            return resp, 1999.92, 2000.0, True

        clock = self._clock(tick, transport)
        self.assertEqual(clock.sync(), "ready")
        self.assertAlmostEqual(clock.now(), TARGET + 0.04, delta=0.001)
        clock.close()

    def test_processing_near_rtt_leaves_tiny_network_half(self):
        """处理 79ms 接近 RTT 80ms：网络半程只剩 0.5ms，仍按公式给中心。"""
        tick = Tick(2000.0)

        def transport(source, packet, timeout_s):
            resp = ntp_response(packet[40:48], TARGET - 0.079, TARGET)
            return resp, 1999.92, 2000.0, True

        clock = self._clock(tick, transport)
        self.assertEqual(clock.sync(), "ready")
        self.assertAlmostEqual(clock.now(), TARGET + 0.0005, delta=0.001)
        clock.close()

    def test_negative_network_within_tolerance_is_clamped(self):
        """容差内负网络时长：按 0 保守处理（不早于 server_xmit），误差界不缩小。

        处理 100ms 大于本地 RTT 80ms，但未超过 rtt+50ms 容差。
        中心估计不能倒推成 T3 - 10ms，也不能因为扣减处理耗时把 rtt/2 误差抹掉。
        """
        tick = Tick(2000.0)

        def transport(source, packet, timeout_s):
            resp = ntp_response(packet[40:48], TARGET - 0.10, TARGET,
                                root_delay=0.0, dispersion=0.0)
            return resp, 1999.92, 2000.0, True

        clock = self._clock(tick, transport)
        self.assertEqual(clock.sync(), "ready")
        self.assertAlmostEqual(clock.now(), TARGET, delta=1e-6,
                               msg="负网络半程必须夹到 0，不得倒推")
        self.assertGreaterEqual(clock.snapshot()["uncertainty_s"], 0.04,
                                "误差界仍须保留 rtt/2，不能因扣处理耗时缩小")
        clock.close()

    def test_processing_beyond_tolerance_rejected(self):
        """处理耗时超过 rtt+50ms 容差：无法解释的样本必须拒绝，不作保守猜测。"""
        tick = Tick(2000.0)

        def transport(source, packet, timeout_s):
            resp = ntp_response(packet[40:48], TARGET - 0.14, TARGET)
            return resp, 1999.92, 2000.0, True

        clock = self._clock(tick, transport)
        clock.sync()
        self.assertEqual(clock.generation, 0)
        with self.assertRaises(ClockUnavailable):
            clock.now()
        clock.close()

    def test_timer_resolution_is_counted(self):
        """GetTickCount64 常见 15.6ms 分辨率；误差界不得固定按 1ms 低报。"""
        tick = Tick(2000.0)

        def transport(source, packet, timeout_s):
            return (ntp_response(packet[40:48], TARGET, TARGET,
                                 root_delay=0.0, dispersion=0.0),
                    2000.0, 2000.0, True)

        clock = self._clock(tick, transport)
        self.assertEqual(clock.sync(), "ready")
        self.assertGreaterEqual(clock.snapshot()["uncertainty_s"], 0.015,
                                "误差界必须计入真实计时分辨率")
        clock.close()

    def test_two_equal_support_contradictory_groups_rejected(self):
        """A-B 与 B-C 各自一致、A-C 矛盾：两组支持数相同，不得任选首个。"""
        tick = Tick(2000.0)
        offsets = {"src-a": 0.0, "src-b": 0.25, "src-c": 0.5}

        def transport(source, packet, timeout_s):
            base = TARGET + offsets[source]
            resp = ntp_response(packet[40:48], base - 0.01, base)
            return resp, 2000.0, 2000.08, True

        clock = self._clock(tick, transport)
        clock.sync()
        self.assertEqual(clock.generation, 0, "矛盾且同支持数的组必须拒绝")
        with self.assertRaises(ClockUnavailable):
            clock.now()
        clock.close()

    def test_stale_round_never_publishes(self):
        """新轮已发布后，旧轮结果即使更晚拿到也不得覆盖。"""
        tick = Tick(2000.0)
        clock = self._clock(tick, echo_transport(tick))
        self.assertEqual(clock.sync(), "ready")
        old_round = clock._round_seq
        with clock._lock:
            clock._round_seq += 1
            new_round = clock._round_seq
        newer = [_sample(TARGET + 5.0, 2000.0, "src-a", round_id=new_round),
                 _sample(TARGET + 5.0, 2000.0, "src-b", round_id=new_round)]
        self.assertTrue(clock._accept_cluster(newer, new_round))
        clock._publish_snapshot(new_round)
        generation = clock.generation
        accepted = clock.now()
        stale = [_sample(TARGET - 60.0, 2000.0, "src-a", round_id=old_round),
                 _sample(TARGET - 60.0, 2000.0, "src-b", round_id=old_round)]
        self.assertFalse(clock._accept_cluster(stale, old_round))
        self.assertEqual(clock.generation, generation, "旧轮不得再推进代次")
        self.assertAlmostEqual(clock.now(), accepted, places=6)
        clock.close()

    def test_concurrent_snapshot_writes_stay_atomic(self):
        tick = Tick(2000.0)
        clock = self._clock(tick, echo_transport(tick))
        clock.sync()
        path = clock._snapshot_path()
        errors = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    with open(path, encoding="utf-8") as fh:
                        data = json.load(fh)
                except FileNotFoundError:
                    continue
                except (OSError, ValueError) as exc:
                    errors.append(repr(exc))
                    return
                if not isinstance(data, dict) or data.get("schema") != 1:
                    errors.append("字段损坏")
                    return

        def writer():
            with clock._lock:
                clock._round_seq += 1
                round_id = clock._round_seq
            clock._publish_snapshot(round_id)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        writers = [threading.Thread(target=writer) for _ in range(6)]
        for item in writers:
            item.start()
        for item in writers:
            item.join(5)
        stop.set()
        thread.join(5)
        self.assertEqual(errors, [], "并发替换期间快照必须是完整 JSON")
        leftovers = [name for name in os.listdir(clock._snapshot_dir)
                     if ".tmp-" in name]
        self.assertEqual(leftovers, [], "发布完成后不得留下临时文件")
        clock.close()

    def test_close_does_not_wait_for_permanent_dns(self):
        tick = Tick(2000.0)
        gate = threading.Event()

        def transport(source, packet, timeout_s):
            gate.wait(20)             # 模拟永久阻塞的 DNS/接收
            raise OSError("取消")

        clock = self._clock(tick, transport)
        worker = threading.Thread(target=lambda: clock.sync(), daemon=True)
        started = time.monotonic()
        worker.start()
        time.sleep(0.1)
        clock.close()
        self.assertLess(time.monotonic() - started, 2.0, "close 不得等永久 DNS")
        gate.set()
        worker.join(5)
        self.assertEqual(os.listdir(clock._snapshot_dir), [],
                         "关闭后不留下临时文件，也不得再写快照")


def _offset_transport(offsets, *, root_delay=0.04, dispersion=0.0):
    """按源偏移构造同 tick 样本：server_recv=server_xmit=TARGET+offset。

    rtt=0、err=root_delay/2+计时分辨率≈0.0356；一致阈值≈0.321，
    因此 0/0.20/0.40 恰好让 A-B、B-C 成立而 A-C 不成立。
    """
    def transport(source, packet, timeout_s):
        base = TARGET + float(offsets[source])
        resp = ntp_response(packet[40:48], base, base,
                            root_delay=root_delay, dispersion=dispersion)
        return resp, 2000.0, 2000.0, True
    return transport


class ConsistentClusterTests(unittest.TestCase):
    """CB01：簇必须是组内两两一致，不能把「都与中间人一致」当全组一致。"""

    def _sync(self, offsets, order=SOURCES):
        tick = Tick(2000.0)
        clock = BusinessClock(sources=order, tick_fn=tick,
                              transport=_offset_transport(offsets),
                              snapshot_dir=tempfile.mkdtemp(prefix="tjise-clock-c-"))
        state = clock.sync()
        return clock, state

    def test_ab_bc_consistent_ac_conflict_rejected_all_orders(self):
        """A-B、B-C 成立而 A-C 矛盾：两个同支持数极大组，必须拒绝发布。

        0/0.20/0.40、err≈0.0356：ABC 的总误差≈0.436 仍小于 0.5，
        因此不会靠「误差超限」碰巧拒绝，能真正暴露星形邻接合并缺陷。
        """
        base = {"src-a": 0.0, "src-b": 0.20, "src-c": 0.40}
        for order in itertools.permutations(SOURCES):
            with self.subTest(order=order):
                clock, state = self._sync(dict(base), order)
                self.assertEqual(state, "unavailable")
                self.assertEqual(clock.generation, 0,
                                 "AB/BC 同支持且 AC 冲突时不得按 RTT 任选")
                with self.assertRaises(ClockUnavailable):
                    clock.now()
                clock.close()

    def test_unique_pair_with_outlier_publishes(self):
        """正对照：唯一一致的 AB 加一个离群 C，正常发布 AB（2 源支持）。"""
        clock, state = self._sync({"src-a": 0.0, "src-b": 0.20, "src-c": 10.0})
        self.assertEqual(state, "ready")
        self.assertEqual(clock.generation, 1)
        self.assertIn("2源", clock.snapshot()["source"])
        clock.close()

    def test_all_three_consistent_publishes_with_three_sources(self):
        """正对照：三源两两一致时取三源极大组，支持数 3。"""
        clock, state = self._sync({"src-a": 0.0, "src-b": 0.10, "src-c": 0.20})
        self.assertEqual(state, "ready")
        self.assertIn("3源", clock.snapshot()["source"])
        clock.close()

    def test_two_sources_consistent_publishes(self):
        """正对照：只有两源可用且一致时仍可发布。"""
        tick = Tick(2000.0)
        clock = BusinessClock(sources=("src-a", "src-b"), tick_fn=tick,
                              transport=_offset_transport({"src-a": 0.0,
                                                           "src-b": 0.05}),
                              snapshot_dir=tempfile.mkdtemp(prefix="tjise-clock-c-"))
        self.assertEqual(clock.sync(), "ready")
        self.assertEqual(clock.generation, 1)
        clock.close()


class HoldoverAndGenerationTests(unittest.TestCase):
    """300 秒后 holdover、900 秒上限、100ppm 漂移、代次单调、重校纠正。"""

    def _clock(self, tick=None, base=TARGET):
        tick = tick or Tick(1000.0)
        clock = BusinessClock(sources=SOURCES, tick_fn=tick,
                              transport=echo_transport(tick, base_epoch=base),
                              snapshot_dir=tempfile.mkdtemp(prefix="tjise-clock-t-"))
        clock.sync()
        return clock, tick

    def test_holdover_after_resync_interval(self):
        clock, tick = self._clock()
        tick.advance(320.0)                    # 超过 300 秒未重校
        self.assertEqual(clock.state(), "holdover")
        self.assertLess(abs(clock.now() - TARGET - 320.0), 1.0)
        lower, upper = clock.bounds()
        self.assertLess(upper - lower, 1.2)    # 漂移加入但仍有界
        clock.close()

    def test_holdover_expiry_becomes_unavailable(self):
        clock, tick = self._clock()
        tick.advance(1000.0)
        with self.assertRaises(ClockUnavailable):
            clock.now()
        self.assertEqual(clock.state(), "unavailable")
        clock.close()

    def test_resync_corrects_anchor_and_generation_grows(self):
        tick = Tick(1000.0)
        base = {"epoch": TARGET}
        clock = BusinessClock(
            sources=SOURCES, tick_fn=tick,
            transport=echo_transport(tick, base_epoch=base["epoch"]),
            snapshot_dir=tempfile.mkdtemp(prefix="tjise-clock-t-"))
        clock.sync()
        gen1 = clock.generation
        tick.advance(320.0)
        base["epoch"] = TARGET + 5.0           # 服务器时间被纠正 5 秒
        clock._transport = echo_transport(tick, base_epoch=base["epoch"])
        clock.sync()
        self.assertEqual(clock.generation, gen1 + 1, "重校必须增加代次")
        self.assertLess(abs(clock.now() - (TARGET + 5.0 + 320.0 - 320.0)), 1.0)
        clock.close()

    def test_generation_never_decreases(self):
        clock, tick = self._clock()
        gen = clock.generation
        clock._transport = echo_transport(tick, live_sources=())   # 断网
        clock.sync()
        self.assertEqual(clock.generation, gen, "失败重校不得倒退代次")
        clock.close()


if __name__ == "__main__":
    unittest.main()
