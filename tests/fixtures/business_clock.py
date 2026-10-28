#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试夹具：共享 FakeBusinessClock 与临时快照工厂。

显式 epoch/tick/quality，支持 wall_jump/advance/resync/expire；
不做「所有方法均成功」的万能桩，未知配置抛错，默认零网络零真实数据。

@author LiXinwen
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

_FIXTURES = os.path.dirname(os.path.abspath(__file__))      # local/tests/fixtures
_TESTS = os.path.dirname(_FIXTURES)                          # local/tests
LOCAL = os.path.dirname(_TESTS)                              # local
for _path in (LOCAL, _TESTS):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from business_clock import BusinessClock, ClockClient, ClockUnavailable  # noqa: E402,F401


class FakeBusinessClock:
    """生产 BusinessClock 的受控替身：显式锚点与经过时长，可驱动延迟与过期。

    与生产同形的快照合同（决策 012）：
    - ``epoch_s`` 只对应 ``tick_s``（授时锚点）；``sampled_tick_s`` 是快照
      生成时刻；``age_s = sampled_tick_s - tick_s``；
    - ``now() = epoch_s + elapsed_s() - tick_s``，advance 只推进经过时长，
      不重写锚点——锚点随时间变「陈旧」才能测出 holdover/过期；
    - 超过 300 秒未重校 → holdover；超过 900 秒或漂移超 0.5 秒 → unavailable；
      不确定度含 100ppm 保守漂移，与生产 _effective_state_locked 同款。
    quality 传显式值时强制状态（测试边界用），默认按年龄推算。
    """

    HOLDOVER_AFTER_S = 300.0     # 与生产一致：超过 300 秒未重校标 holdover
    EXPIRE_AFTER_S = 900.0       # 与生产一致：锚点超过 900 秒不可用
    DRIFT_PPM = 100              # 与生产一致：holdover 保守漂移
    MAX_UNCERTAINTY_S = 0.5      # 与生产一致：总误差上限

    def __init__(self, epoch=1793100000.0, tick=1000.0, quality=None,
                 uncertainty=0.2, source="fake:ntp", owner_id="fake-owner"):
        self._anchor_epoch = float(epoch)    # epoch_s 对应的授时时刻
        self._anchor_tick = float(tick)      # 授时锚点对应的经过时长读数
        self._now_tick = float(tick)         # 当前经过时长（advance 只推进它）
        self._uncertainty = float(uncertainty)   # 采样时刻的测量误差
        self._source = source
        self._owner = owner_id
        self._gen = 1
        self._closed = False
        self._forced = quality
        if quality not in (None, "ready", "holdover", "unavailable"):
            raise ValueError("未知质量: %s" % quality)
        self._reason = "北京时间已同步"

    def _effective(self):
        """按年龄推算当前状态与有效不确定度（生产 _effective_state_locked 同形）。"""
        if self._closed:
            return "unavailable", 0.0, "时钟已关闭"
        if self._anchor_epoch is None:
            return "unavailable", 0.0, "正在获取北京时间"
        if self._forced == "unavailable":
            return "unavailable", 0.0, self._reason or "失去授时（测试注入）"
        age = self._now_tick - self._anchor_tick
        if age < 0:
            age = 0.0
        uncertainty = self._uncertainty + self.DRIFT_PPM * 1e-6 * age
        if age > self.EXPIRE_AFTER_S:
            return "unavailable", uncertainty, "锚点超过 %g 秒未重校" % self.EXPIRE_AFTER_S
        if uncertainty > self.MAX_UNCERTAINTY_S:
            return "unavailable", uncertainty, "不确定度漂移超过 %g 秒" % self.MAX_UNCERTAINTY_S
        if self._forced in ("ready", "holdover"):
            reason = {"ready": "北京时间已同步",
                      "holdover": "沿用上次校时"}[self._forced]
            return self._forced, uncertainty, reason
        if age > self.HOLDOVER_AFTER_S:
            return "holdover", uncertainty, "沿用上次校时（%d 秒未重校）" % int(age)
        return "ready", uncertainty, "北京时间已同步"

    def elapsed_s(self):
        return self._now_tick

    def state(self):
        return self._effective()[0]

    def now(self):
        state, _, reason = self._effective()
        if state == "unavailable":
            raise ClockUnavailable(reason)
        return self._anchor_epoch + (self._now_tick - self._anchor_tick)

    def bounds(self):
        state, uncertainty, reason = self._effective()
        if state == "unavailable":
            raise ClockUnavailable(reason)
        now = self._anchor_epoch + (self._now_tick - self._anchor_tick)
        return now - uncertainty, now + uncertainty

    def advance(self, seconds):
        """只推进经过时长：锚点不动，快照随时间陈旧（等价真实时间流逝）。"""
        self._now_tick += float(seconds)

    def wall_jump(self, new_epoch):
        """模拟系统墙钟变化：只改诊断对照值，业务时刻不变（Fake 不读墙钟）。"""
        self._wall = float(new_epoch)
        return self.now()

    def resync(self, new_epoch=None, quality="ready", uncertainty=0.2):
        """模拟一次重校：锚点换成当前经过时长处的新授时，代次 +1。"""
        self._anchor_epoch = float(new_epoch if new_epoch is not None else self.now())
        self._anchor_tick = self._now_tick
        self._uncertainty = float(uncertainty)
        self._forced = quality
        self._gen += 1
        self._reason = {"ready": "北京时间已同步",
                        "holdover": "沿用上次校时",
                        "unavailable": "未同步"}.get(quality, "未知质量")

    def expire(self):
        """模拟锚点过期/失去授时（显式终态，优先于年龄推算）。"""
        self._forced = "unavailable"
        self._gen += 1
        self._reason = "锚点超过持有上限未重校"

    def snapshot(self):
        state, uncertainty, reason = self._effective()
        age = self._now_tick - self._anchor_tick if self._anchor_tick is not None else None
        return {"schema": 1, "owner_id": self._owner, "generation": self._gen,
                "state": state, "epoch_s": self._anchor_epoch,
                "tick_s": self._anchor_tick, "sampled_tick_s": self._now_tick,
                "uncertainty_s": uncertainty, "source": self._source,
                "age_s": age, "reason": reason}

    @property
    def owner_id(self):
        return self._owner

    @property
    def generation(self):
        return self._gen

    @property
    def reason(self):
        return self._effective()[2]


class TestWallClock:
    """测试专用本机时钟：由测试显式注入，生产代码里不再有任何墙钟替身。

    语义刻意保持简单：now 就是本机 time.time()，bounds 零误差，
    elapsed 用单调钟。只服务「与时钟语义无关」的旧用例，不冒充北京时间。
    """

    owner_id = "test-wall"
    generation = 0
    source = "system-wall（测试显式注入）"

    def __init__(self, quality="ready"):
        if quality not in ("ready", "holdover"):
            raise ValueError("TestWallClock 只支持 ready/holdover")
        self._quality = quality

    def now(self) -> float:
        return time.time()

    def bounds(self):
        n = self.now()
        return n, n

    def elapsed_s(self) -> float:
        return time.monotonic()

    def state(self) -> str:
        return self._quality

    @property
    def reason(self) -> str:
        return "测试显式注入的本机时钟"

    def request_resync(self) -> None:
        pass

    def snapshot(self) -> dict:
        return {"schema": 1, "owner_id": self.owner_id, "generation": 0,
                "state": self._quality, "epoch_s": self.now(), "tick_s": 0.0,
                "sampled_tick_s": self.elapsed_s(), "uncertainty_s": 0.0,
                "source": self.source, "age_s": 0.0, "reason": self.reason}


def write_snapshot(snapshot: dict, directory: str | None = None) -> str:
    """把快照原子写入临时目录，返回路径（供 ClockClient 跨进程用）。"""
    directory = directory or tempfile.mkdtemp(prefix="tjise-clock-fixture-")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "tjise-clock-%s.json" % snapshot.get("owner_id", "x"))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def fake_transport(samples: dict):
    """构造注入式传输：{source: (response_bytes, recv_tick)}，未列出的源抛 OSError。
    response_bytes 可传 None 表示 UDP 黑洞（超时）。"""
    def transport(source, packet, timeout_s):
        if source not in samples:
            raise OSError("无此源")
        item = samples[source]
        if item is None:
            raise OSError("黑洞")
        return item
    return transport


import struct


def ntp_response(originate, server_recv_epoch: float,
                 server_xmit_epoch: float, root_delay: float = 0.05,
                 dispersion: float = 0.01, li: int = 0, stratum: int = 2,
                 vn: int = 4, mode: int = 4) -> bytes:
    """构造合法（或指定字段非法的）48 字节服务器响应。

    originate 可给完整 8 字节（生产协议：整轮标识原样回显），
    也可给整数秒（旧装置：小数部分为零）——两者都按服务器语义原样回显。
    布局：0 头部 | 4 根延迟 | 8 根分散 | 12 参考ID | 16 参考ts |
    24 originate | 32 recv | 40 xmit。"""
    NTP_DELTA = 2208988800

    def ntp64(epoch):
        ntp = float(epoch) + NTP_DELTA
        return int(ntp) & 0xFFFFFFFF, int((ntp - int(ntp)) * (1 << 32)) & 0xFFFFFFFF

    recv_s, recv_f = ntp64(server_recv_epoch)
    xmit_s, xmit_f = ntp64(server_xmit_epoch)
    if isinstance(originate, (bytes, bytearray)):
        if len(originate) != 8:
            raise ValueError("originate 标识必须是 8 字节")
        orig_s, orig_f = struct.unpack("!II", bytes(originate))
    else:
        orig_s, orig_f = int(originate) & 0xFFFFFFFF, 0
    header = struct.pack("!BBBb", ((li & 0x3) << 6) | ((vn & 0x7) << 3) | (mode & 0x7),
                         stratum & 0xFF, 6, -21)
    # 根延迟按有符号 16.16 打包：负值能被测试构造出来，生产按绝对值计入误差
    return header \
        + struct.pack("!iII", int(root_delay * 65536), int(dispersion * 65536), 0) \
        + b"\x00" * 8 \
        + struct.pack("!II", orig_s, orig_f) \
        + struct.pack("!II", recv_s, recv_f) \
        + struct.pack("!II", xmit_s, xmit_f)
