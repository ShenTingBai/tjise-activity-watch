#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立业务时钟：标准北京时间锚点 + 不受系统墙钟修改影响的经过时长。

设计要点（决策 012）：
- 业务当前时刻 = 网络授时锚点(epoch) + 应用内经过时长(elapsed) - 锚点时刻的 tick，
  系统 time.time 只用于诊断偏移，不参与该关系、有效期或授时日期选择。
- 公共 NTP 源（UDP 123）：ntp.aliyun.com / ntp1.aliyun.com / time.cloudflare.com；
  一轮三源共享 5 秒期限，每源至多一次请求；DNS/接收在有界后台槽位，调用方到期即返回。
- 样本校验：长度/版本/服务器模式/originate nonce/来源/LI/stratum/收发时间/处理耗时/era；
  至少两个源在同一 tick 上一致（差 ≤ 双方误差和 + 0.25s）才形成唯一簇，取最小 RTT。
- 不确定度含 RTT 半程、服务器根延迟/分散、计时分辨率与簇内差异；≤0.5s 才进 ready。
- 300 秒后台重校；未重校按 holdover 推进并计 100ppm 保守漂移；>900 秒或超阈值 → unavailable。
  任何失败都有原因，绝不回退系统墙钟。
- 快照：每实例独立临时目录原子写（不含凭证），worker 经 TJISE_CLOCK_SNAPSHOT/
  TJISE_CLOCK_OWNER 读取；同实例跨进程允许，不允许跨主程序会话复活。
- Windows 经过时长用 Win32 GetTickCount64（跨进程同源、含睡眠）；其他平台注入
  time.monotonic 兜底，禁止把进程 CPU 时间当经过时长。

@author LiXinwen
"""
from __future__ import annotations

import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import uuid

__all__ = ["ClockUnavailable", "BusinessClock", "ClockClient", "SNTPPacketError"]

SNTP_PORT = 123
NTP_UNIX_DELTA = 2208988800          # 1900-1970 秒
ERA_CANDIDATES = 4                   # 2^32 秒 ≈ 136 年，取 4 个候选足够覆盖
ERA_MIN_YEAR, ERA_MAX_YEAR = 2020, 2099
MAX_ROOT_DELAY_S = 0.5               # 服务器根延迟上限（秒）
CLUSTER_TOLERANCE_S = 0.25           # 簇内允许差异 = 双方误差和 + 0.25
READY_UNCERTAINTY_S = 0.5            # 应用策略阈值（非网络精度保证）
HOLDOVER_DRIFT_PPM = 100             # holdover 保守漂移
# GetTickCount64 文档说明分辨率通常 10–16ms（默认计时器周期 15.625ms）。
# 固定 1ms 会低报误差；取一个完整周期覆盖「发包」「收包」两次取样。
TICK_RESOLUTION_S = 0.015625


class ClockUnavailable(Exception):
    """业务时钟不可信：无锚点/快照缺失/过期/损坏。调用方必须显式处理。"""


class SNTPPacketError(Exception):
    """NTP 响应未通过校验：畸形、重放、未同步、来源不符等。"""


def _elapsed_tick() -> float:
    """系统级经过毫秒→秒：Windows 用 GetTickCount64（正确声明 restype），
    其余平台用 time.monotonic；两者都是系统级单调时钟，跨进程同源。"""
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.GetTickCount64.restype = ctypes.c_ulonglong   # ULONGLONG，不是默认 int
        kernel32.GetTickCount64.argtypes = []
        return float(kernel32.GetTickCount64()) / 1000.0
    return time.monotonic()


def _default_owner_id() -> str:
    return uuid.uuid4().hex[:16]


def _beijing_year_candidates(seconds_u32: int):
    """NTP 秒是 32 位模数：枚举 era 候选，落在 2020..2099 年的必须唯一。
    发包与解码都不依赖系统年份，避免错误 OS 日期选错 era。"""
    out = []
    for era in range(-1, ERA_CANDIDATES):
        value = seconds_u32 + era * (1 << 32)
        ntp_epoch = value - NTP_UNIX_DELTA
        year = time.gmtime(ntp_epoch).tm_year if ntp_epoch > 0 else -1
        if ERA_MIN_YEAR <= year <= ERA_MAX_YEAR:
            out.append(ntp_epoch)
    return out


def _new_nonce() -> bytes:
    """每轮每源一个完整 64 位标识：冷启动也不得用全零（全零会被当成重放）。"""
    while True:
        raw = os.urandom(8)
        if raw != b"\x00" * 8:
            return raw


def build_client_packet(nonce: bytes | None = None) -> tuple[bytes, bytes]:
    """48 字节客户端请求 + 本轮完整 64 位标识（放在 transmit 位置，服务器回显到 originate）。

    LI=0/VN=4/Mode=3；标识用密码学随机数，同一秒内的两个请求也互不相同，
    不再用业务时钟估计值（同秒重发会得到相同高位，无法区分本轮）。
    布局：0 头部 | 4 根延迟 | 8 根分散 | 12 参考ID | 16/24/32/40 四个 64 位时间戳。
    """
    nonce = _new_nonce() if nonce is None else bytes(nonce)
    if len(nonce) != 8 or nonce == b"\x00" * 8:
        raise ValueError("时钟请求标识必须是 8 字节且非全零")
    secs, frac = struct.unpack("!II", nonce)
    li_vn_mode = (0 << 6) | (4 << 3) | 3
    packet = struct.pack("!BBBb9I2I", li_vn_mode, 0, 6, -21,
                         0, 0, 0,        # 根延迟/分散/参考ID
                         0, 0,           # 参考时间戳
                         0, 0,           # originate 由客户端置零
                         0, 0,           # receive 时间戳（客户端为零）
                         secs, frac)     # transmit（本轮标识）
    return packet, nonce


def _ntp_timestamp(data: bytes, offset: int, what: str) -> float:
    """64 位 NTP 时间戳（32 秒 + 32 小数）→ 业务 UTC epoch 秒。
    era 以整数秒唯一落在 2020..2099 判定；小数部分保留，不引入 1 秒量化误差。"""
    secs, frac = struct.unpack("!II", data[offset:offset + 8])
    if secs == 0 and frac == 0:
        raise SNTPPacketError("%s 时间戳为零" % what)
    cands = _beijing_year_candidates(secs)
    if len(cands) != 1:
        raise SNTPPacketError("%s era 候选不唯一，无法确定日期" % what)
    return cands[0] + frac / (1 << 32)


def parse_server_packet(data: bytes, nonce, source: str) -> dict:
    """校验并解析服务器响应；返回
    {source, server_recv, server_xmit, root_delay, root_delay_raw, dispersion}。

    originate 必须 8 字节全等：只比秒的高 32 位会把「同一秒的另一个请求」
    当成自己的响应。任何不符合都抛 SNTPPacketError，不作为样本。
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) < 48:
        raise SNTPPacketError("响应长度不足 48 字节")
    li_vn_mode = data[0]
    li, vn, mode = li_vn_mode >> 6, (li_vn_mode >> 3) & 0x7, li_vn_mode & 0x7
    if vn != 4:
        raise SNTPPacketError("版本不是 4")
    if mode != 4:
        raise SNTPPacketError("不是服务器模式响应")
    if li == 3:
        raise SNTPPacketError("服务器报告未同步（LI=3）")
    stratum = data[1]
    if not 1 <= stratum <= 15:
        raise SNTPPacketError("stratum 越界（0/KoD 或 >15）")
    # 根延迟是有符号 16.16 定点数；负值不能用来抵消误差，只按绝对值计入
    root_delay_raw = struct.unpack("!i", data[4:8])[0] / 65536.0
    if abs(root_delay_raw) > MAX_ROOT_DELAY_S:
        raise SNTPPacketError("根延迟越界")
    root_delay = abs(root_delay_raw)
    dispersion = struct.unpack("!I", data[8:12])[0] / 65536.0
    expected = (struct.pack("!II", int(nonce) & 0xFFFFFFFF, 0)
                if isinstance(nonce, int) else bytes(nonce))
    if len(expected) != 8:
        raise SNTPPacketError("本轮请求标识长度不对")
    if bytes(data[24:32]) != expected:
        raise SNTPPacketError("originate 与本次请求不符（重放或错配）")
    server_recv = _ntp_timestamp(data, 32, "接收")
    server_xmit = _ntp_timestamp(data, 40, "发送")
    if server_xmit < server_recv:
        raise SNTPPacketError("服务器处理时间为负")
    return {"source": source, "server_recv": server_recv, "server_xmit": server_xmit,
            "root_delay": root_delay, "root_delay_raw": root_delay_raw,
            "dispersion": dispersion}


class _SourceSlot:
    """单源有界槽位：DNS/接收在后台线程，主调到期即返回；迟到的结果按代次丢弃。"""

    def __init__(self, source: str):
        self.source = source
        self.inflight = False
        self.last_attempt_tick = -1e18
        self.lock = threading.Lock()

    def can_attempt(self, now_tick: float, cooldown: float) -> bool:
        with self.lock:
            return not self.inflight and (now_tick - self.last_attempt_tick) >= cooldown


class BusinessClock:
    """应用时钟：sync 后 ready，断网/分歧进入 holdover 或 unavailable，绝不回退墙钟。"""

    def __init__(self, *, sources=None, tick_fn=None, transport=None,
                 snapshot_dir=None, owner_id=None, sync_timeout_s: float = 5.0,
                 resync_interval_s: float = 300.0, holdover_max_s: float = 900.0,
                 per_source_cooldown_s: float = 64.0,
                 background: bool = False) -> None:
        self._sources = list(sources or ("ntp.aliyun.com", "ntp1.aliyun.com",
                                         "time.cloudflare.com"))
        self._tick = tick_fn or _elapsed_tick
        self._transport = transport          # 测试注入；生产为 _udp_transport 闭包
        self._sync_timeout_s = float(sync_timeout_s)
        self._resync_interval_s = float(resync_interval_s)
        self._holdover_max_s = float(holdover_max_s)
        self._cooldown_s = float(per_source_cooldown_s)
        self._owner_id = owner_id or _default_owner_id()
        # RLock：_publish_snapshot 持锁调用 snapshot() 这类内部读取需要可重入
        self._lock = threading.RLock()
        # 同步单飞锁：同一实例同一时刻只跑一轮采样；并发的第二次调用只回报现状
        self._sync_lock = threading.Lock()
        # 快照发布锁：唯一临时文件 + os.replace 必须串行，关闭后不再有新写
        self._snapshot_lock = threading.Lock()
        self._round_seq = 0                  # 本轮采样序号（只增）
        self._published_round = 0            # 已发布过的最新采样序号
        self._anchor_epoch = None            # 收到响应那刻的 UTC 估计
        self._anchor_tick = None             # 同一时刻的经过 tick
        self._anchor_uncertainty = None
        self._anchor_source = None
        self._generation = 0
        self._state = "unavailable"          # syncing/ready/holdover/unavailable
        self._reason = "尚未校时"
        self._last_sync_tick = None
        self._closed = False
        self._slots = {s: _SourceSlot(s) for s in self._sources}
        self._wakeup = threading.Event()
        if snapshot_dir is None:
            base = os.environ.get("TEMP") or tempfile.gettempdir()
            snapshot_dir = os.path.join(base, "tjise-clock-%s" % self._owner_id)
        self._snapshot_dir = str(snapshot_dir)
        os.makedirs(self._snapshot_dir, exist_ok=True)
        self._sync_status = "pending"        # 首轮未开始不等于校准失败
        if background:
            self._bg = threading.Thread(target=self._background_loop,
                                        name="business-clock", daemon=True)
            self._bg.start()
        else:
            self._bg = None

    # ---- 经过时长 ----
    def elapsed_s(self) -> float:
        """应用内经过时长（秒）：睡眠期间继续累计，系统回拨不影响。"""
        return self._tick()

    # ---- 状态与读取 ----
    def _effective_state_locked(self) -> tuple[str, float, str]:
        """按年龄推算当前状态与有效不确定度（ready/holdover 都可读）。"""
        if self._anchor_epoch is None:
            return "unavailable", 0.0, self._reason
        age = self.elapsed_s() - self._anchor_tick
        if age < 0:
            age = 0.0
        if age > self._holdover_max_s:
            return "unavailable", 0.0, "锚点超过 %g 秒未重校" % self._holdover_max_s
        drift = HOLDOVER_DRIFT_PPM * 1e-6 * age
        uncertainty = self._anchor_uncertainty + drift
        if uncertainty > READY_UNCERTAINTY_S:
            return "unavailable", 0.0, "不确定度漂移超过 %g 秒" % READY_UNCERTAINTY_S
        if age > self._resync_interval_s:
            return "holdover", uncertainty, "沿用上次校时（%d 秒未重校）" % int(age)
        return "ready", uncertainty, "北京时间已同步"

    def state(self) -> str:
        with self._lock:
            return self._effective_state_locked()[0]

    def now(self) -> float:
        """业务 UTC epoch 秒。不可信时抛 ClockUnavailable，绝不返回系统时间。"""
        with self._lock:
            state, _, reason = self._effective_state_locked()
            if state == "unavailable":
                raise ClockUnavailable(reason)
            return self._anchor_epoch + (self.elapsed_s() - self._anchor_tick)

    def bounds(self) -> tuple[float, float]:
        """业务时刻区间：(下界, 上界)，含测量误差、簇内差异与 holdover 漂移。"""
        with self._lock:
            state, uncertainty, reason = self._effective_state_locked()
            if state == "unavailable":
                raise ClockUnavailable(reason)
            now = self._anchor_epoch + (self.elapsed_s() - self._anchor_tick)
            return now - uncertainty, now + uncertainty

    def snapshot(self) -> dict:
        """无凭证时钟快照（schema=1）。字段固定，供 worker 跨进程读取。"""
        with self._lock:
            state, uncertainty, reason = self._effective_state_locked()
            return {"schema": 1, "owner_id": self._owner_id,
                    "generation": self._generation, "state": state,
                    "epoch_s": self._anchor_epoch, "tick_s": self._anchor_tick,
                    "sampled_tick_s": self._tick(),
                    "uncertainty_s": uncertainty, "source": self._anchor_source,
                    "age_s": (self.elapsed_s() - self._anchor_tick)
                             if self._anchor_tick is not None else None,
                    "reason": reason, "sync_status": self._sync_status,
                    "sync_round": self._round_seq}

    @property
    def owner_id(self) -> str:
        return self._owner_id

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def reason(self) -> str:
        with self._lock:
            return self._effective_state_locked()[2]

    # ---- 同步 ----
    def _publish_locked(self, epoch_at_recv: float, tick_at_recv: float,
                        uncertainty: float, source: str) -> None:
        self._anchor_epoch = epoch_at_recv
        self._anchor_tick = tick_at_recv
        self._anchor_uncertainty = uncertainty
        self._anchor_source = source
        self._generation += 1
        self._state = "ready"
        self._reason = "北京时间已同步"
        self._last_sync_tick = tick_at_recv

    def sync(self, timeout_s: float | None = None) -> str:
        """一轮有界同步：三源各至多一次请求，共享期限；返回结束时的状态。

        单飞：已有其它轮在跑时不再叠加第二轮，直接回报当前有效状态——
        手动重校与后台重校并发不会产生两批互相覆盖的样本。
        期限按真实单调钟记账（join/收尾）；业务 tick 只承担经过时长，不承担墙钟。
        不形成唯一一致簇或超时 → 保持/进入 holdover 或 unavailable，不回退墙钟。"""
        if not self._sync_lock.acquire(blocking=False):
            return self.state()
        with self._lock:
            previous_generation = self._generation
            self._sync_status = "syncing"
        try:
            return self._sync_round(timeout_s)
        finally:
            # 即使传输失败或关闭抛错，界面也能区分正在尝试与已经失败。
            with self._lock:
                self._sync_status = ("succeeded" if self._generation > previous_generation
                                     else "failed")
            try:
                self._publish_snapshot()
            finally:
                self._sync_lock.release()

    def _sync_round(self, timeout_s: float | None = None) -> str:
        real_deadline = time.monotonic() + float(timeout_s or self._sync_timeout_s)
        with self._lock:
            if self._closed:
                raise ClockUnavailable("时钟已关闭")
            self._round_seq += 1
            round_id = self._round_seq
            if self._anchor_epoch is None:
                self._reason = "正在获取北京时间"
        attempt_tick = self._tick()      # 只用于每源冷却记账，不参与 RTT
        results = []
        threads = []
        for source in self._sources:
            slot = self._slots[source]
            if not slot.can_attempt(attempt_tick, self._cooldown_s):
                continue
            with slot.lock:
                if slot.inflight:
                    continue
                slot.inflight = True
                slot.last_attempt_tick = attempt_tick
            thread = threading.Thread(
                target=self._one_source, name="clock-%s" % source,
                args=(slot, real_deadline, round_id, results),
                daemon=True)
            threads.append(thread)
            thread.start()
        for thread in threads:
            remaining = real_deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)
        self._accept_cluster(results, round_id)
        # sync() 的 finally 在标记本轮完成后统一发布，避免文件仍写着 syncing。
        with self._lock:
            return self._effective_state_locked()[0]

    def _one_source(self, slot: _SourceSlot, real_deadline: float,
                    round_id: int, results: list) -> None:
        """单源槽位：真实传输走已连接 UDP（DNS 在本线程内），测试注入 transport。
        实际发包/收包 tick 由传输层返回：DNS 耗时不算 RTT，也不共用别源的发送时刻。
        迟到返回/关闭后：results 只在本次 sync 代次内被消费，天然丢弃。"""
        try:
            transport = self._transport or self._udp_transport
            remaining = real_deadline - time.monotonic()
            if remaining <= 0:
                return
            packet, nonce = build_client_packet()
            call_tick = self._tick()      # 旧式二元素传输的发包时刻兜底
            out = transport(slot.source, bytes(packet), remaining)
            data, send_tick, recv_tick, peer_ok = self._timing_of(out, call_tick)
            if not peer_ok:
                return          # 来源不是已解析的 endpoint：不当作本轮样本
            if self._closed or time.monotonic() > real_deadline:
                return          # 迟到的响应：不得进入本轮样本，更不得覆盖新代次
            parsed = parse_server_packet(data, nonce, slot.source)
            rtt = recv_tick - send_tick
            if rtt < 0:
                return
            if parsed["root_delay"] > MAX_ROOT_DELAY_S:
                return                      # 根延迟过大：不作样本
            server_delta = parsed["server_xmit"] - parsed["server_recv"]
            if server_delta > rtt + 0.05:
                return                      # 服务器处理耗时超过本地 RTT 界
            # 网络往返 = 本地 RTT - 服务器处理耗时（RFC 5905 §8）。
            # 处理耗时已经包含在 server_xmit 里，只再加网络半程；
            # 计时分辨率可能让网络时长出现 -50ms 内的负值：夹到 0（不早于
            # server_xmit）保守处理，但不因此缩小 rtt/2 的误差界。
            network_round_trip = rtt - server_delta
            network_half = max(0.0, network_round_trip / 2.0)
            epoch_at_recv = parsed["server_xmit"] + network_half
            err = rtt / 2.0 + parsed["root_delay"] / 2.0 + parsed["dispersion"] \
                + TICK_RESOLUTION_S
            results.append({"source": slot.source, "epoch": epoch_at_recv,
                            "tick": recv_tick, "rtt": rtt, "err": err,
                            "round": round_id})
        except (SNTPPacketError, OSError, socket.error, ValueError, Exception):
            # 单源失败只少一个候选；原因不外泄为登录失效或回退墙钟
            import traceback
            if os.environ.get("TJISE_CLOCK_DEBUG"):
                traceback.print_exc()
            return
        finally:
            with slot.lock:
                slot.inflight = False

    def _timing_of(self, out, call_tick: float) -> tuple:
        """从传输层结果取实际发/收 tick 与来源校验结论。

        生产传输返回 (data, send_tick, recv_tick, peer_ok)：DNS 之后的真实时刻，
        peer_ok=False 表示响应不是来自已解析的 endpoint；
        旧式二元素结果 (data, recv_tick) 用调用前 tick 兜底，只服务注入式装置。
        """
        if not isinstance(out, (tuple, list)) or len(out) < 2:
            raise OSError("时钟传输结果格式不对")
        if len(out) >= 4:
            return out[0], float(out[1]), float(out[2]), bool(out[3])
        return out[0], float(call_tick), float(out[1]), True

    def _udp_transport(self, source: str, packet: bytes, timeout_s: float):
        """真实传输：DNS 结束后才取发包 tick，并显式核对响应来源。

        返回 (data, send_tick, recv_tick, peer_ok)；DNS 阻塞由本线程承载，
        close() 不 join 该线程，所以永久 DNS 也不会拖住关闭。
        """
        infos = socket.getaddrinfo(source, SNTP_PORT, family=socket.AF_INET,
                                   type=socket.SOCK_DGRAM)
        if not infos:
            raise OSError("解析不到时间服务地址")
        family, socktype, proto, _canon, sockaddr = infos[0]
        with socket.socket(family, socktype, proto) as sock:
            sock.settimeout(max(0.2, timeout_s))
            send_tick = self._tick()        # DNS 已完成，这里才是真实发包时刻
            sock.sendto(packet, sockaddr)
            data, peer = sock.recvfrom(1024)
            recv_tick = self._tick()
        return data, send_tick, recv_tick, peer == sockaddr

    def _accept_cluster(self, results: list, round_id: int) -> bool:
        """枚举两两一致的子集，仅比较极大组；同支持数的矛盾组一律拒绝。

        旧实现按「与某样本接近」收集星形邻接：A-B、B-C 成立但 A-C 矛盾时，
        中间人 B 会把三方合并成支持数 3，等于把不同意见算成共同支持。
        这里显式检查组内任意两两一致（最多三源，子集枚举开销可忽略），
        再去掉真子集，只比较极大一致组；若最高支持数对应多个不同组，
        说明存在彼此矛盾且证据等量的结论，不能按 RTT 或枚举顺序任选。
        """
        aligned = []
        if results:
            ref_tick = max(item["tick"] for item in results)
        for item in results:
            # 把各样本推进到同一比较时刻，消除收包先后差
            aligned.append(dict(item,
                                epoch_at=item["epoch"] + (ref_tick - item["tick"])))
        size = len(aligned)
        consistent = [[False] * size for _ in range(size)]
        for i in range(size):
            for j in range(i + 1, size):
                ok = abs(aligned[i]["epoch_at"] - aligned[j]["epoch_at"]) <= \
                    aligned[i]["err"] + aligned[j]["err"] + CLUSTER_TOLERANCE_S
                consistent[i][j] = consistent[j][i] = ok
        groups = []
        for mask in range(1, 1 << size):
            members = [k for k in range(size) if mask & (1 << k)]
            if len(members) < 2:
                continue
            if all(consistent[a][b] for idx, a in enumerate(members)
                   for b in members[idx + 1:]):
                groups.append(frozenset(members))
        maximal = [group for group in groups
                   if not any(group < other for other in groups)]

        candidates = []
        for group in maximal:
            members = sorted(group)
            best = min((aligned[k] for k in members), key=lambda x: x["rtt"])
            spread = max(abs(aligned[k]["epoch_at"] - best["epoch_at"]) for k in members)
            uncertainty = best["err"] + spread
            if uncertainty > READY_UNCERTAINTY_S:
                continue
            candidates.append({"sample": best, "support": len(members),
                               "uncertainty": uncertainty})
        if not candidates:
            with self._lock:
                if self._anchor_epoch is None:
                    self._reason = ("未能形成一致时间簇" if results
                                    else "本轮没有可用的时间源响应")
            return False
        top = max(c["support"] for c in candidates)
        highest = [c for c in candidates if c["support"] == top]
        if len(highest) > 1:
            # 两个同等支持数、成员不同的极大组（如 A-B 与 B-C 各自成立但 A-C 矛盾）：
            # 没有唯一答案，不能按枚举顺序任选第一个。
            with self._lock:
                if self._anchor_epoch is None:
                    self._reason = "存在互相矛盾且支持数相同的时间组"
            return False
        best = min(highest, key=lambda c: c["sample"]["rtt"])
        with self._lock:
            if self._closed:
                return False
            if round_id < self._published_round:
                return False        # 新轮已发布：旧轮结果永不再写
            # 对齐后的 epoch 对应 ref_tick，不是样本自己的收包 tick：
            # 两者混用会把「对齐推进的时长」再加一次。
            self._publish_locked(best["sample"]["epoch_at"], ref_tick,
                                 best["uncertainty"],
                                 "NTP:%s+%d源" % (best["sample"]["source"],
                                                  best["support"]))
        return True

    # ---- 快照与后台 ----
    def _snapshot_path(self) -> str:
        return os.path.join(self._snapshot_dir, "tjise-clock-%s.json" % self._owner_id)

    def _publish_snapshot(self, round_id: int | None = None) -> None:
        """原子写不含凭证的快照；唯一临时文件 + 发布锁，关闭后不得再写。

        旧轮（round_id 小于已发布轮次）不得覆盖新结果——不能只靠事后加代次
        把旧采样伪装成新采样。临时文件带 pid+随机串，避免两个写者互相踩。
        """
        with self._snapshot_lock:
            with self._lock:
                if self._closed:
                    return
                if round_id is not None and round_id < self._published_round:
                    return
                snap = self.snapshot()
                path = self._snapshot_path()
                if round_id is not None:
                    self._published_round = max(self._published_round, round_id)
            tmp = "%s.tmp-%d-%s" % (path, os.getpid(), uuid.uuid4().hex[:8])
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(snap, fh, ensure_ascii=False)
                os.replace(tmp, path)
            except OSError:
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def request_resync(self) -> None:
        """有未来任务时提前 20 秒请求一次后台重校；尊重每源冷却。"""
        self._wakeup.set()

    def _background_loop(self) -> None:
        # 启动即先同步一次：不能让界面在 300 秒内一直「正在获取北京时间」
        try:
            self.sync()
        except Exception:
            pass
        while True:
            self._wakeup.wait(timeout=self._resync_interval_s)
            self._wakeup.clear()
            if self._closed:
                return
            try:
                self.sync()
            except Exception:
                pass
            with self._lock:
                if self._closed:
                    return

    def close(self) -> None:
        """停止后台重校；不等永久阻塞的 DNS，关闭后不再写快照。

        先置关闭标记，再等一次发布锁：正在写的快照会写完，之后不会再有新写；
        最后只清理本实例自己留下的临时文件（不删除快照本身，worker 可能仍在读）。
        """
        with self._lock:
            self._closed = True
        self._wakeup.set()
        with self._snapshot_lock:
            pass
        self._cleanup_temps()

    def _cleanup_temps(self) -> None:
        """只清理本实例（owner_id 命名）创建的临时文件；目录与他人文件不动。"""
        prefix = os.path.basename(self._snapshot_path()) + ".tmp-"
        try:
            names = os.listdir(self._snapshot_dir)
        except OSError:
            return
        for name in names:
            if name.startswith(prefix):
                try:
                    os.remove(os.path.join(self._snapshot_dir, name))
                except OSError:
                    pass


class ClockClient:
    """worker/只读端：按相同 tick 域消费父实例快照；无效一律 ClockUnavailable。

    快照合同（与 BusinessClock.snapshot 同源，一次读取只认一份文档）：
    - ``epoch_s`` 永远对应 ``tick_s``（授时锚点），不是快照生成时刻；
    - ``sampled_tick_s`` 只是该文档生成时刻，用来补采样之后的漂移；
    - 授时年龄按 ``current_tick - tick_s`` 判 900 秒上限，文件被重写不会续命；
    - ``uncertainty_s`` 是采样时的误差，客户端只补采样后漂移，避免重复计入。
    """

    def __init__(self, *, snapshot_path=None, owner_id=None, tick_fn=None,
                 holdover_max_s: float = 900.0,
                 resync_interval_s: float = 300.0) -> None:
        self._path = snapshot_path or os.environ.get("TJISE_CLOCK_SNAPSHOT") or ""
        self._owner = owner_id or os.environ.get("TJISE_CLOCK_OWNER") or ""
        self._tick = tick_fn or _elapsed_tick
        self._holdover_max_s = float(holdover_max_s)
        self._resync_interval_s = float(resync_interval_s)

    # ---- 读取与校验 ----
    def _load(self) -> dict:
        """一次读取内完成全部校验并算出同一代的值；字段缺失/矛盾一律拒绝。"""
        if not self._path:
            raise ClockUnavailable("没有配置时钟快照路径")
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                snap = json.load(fh)
        except (OSError, ValueError) as exc:
            raise ClockUnavailable("时钟快照不可读：%s" % exc)
        if not isinstance(snap, dict) or snap.get("schema") != 1:
            raise ClockUnavailable("时钟快照格式不符")
        owner = snap.get("owner_id")
        if not isinstance(owner, str) or not owner:
            raise ClockUnavailable("时钟快照缺少实例归属")
        if self._owner and owner != self._owner:
            raise ClockUnavailable("时钟快照属于其他实例")

        def number(name, value, *, minimum=None) -> float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ClockUnavailable("时钟快照字段无效：%s" % name)
            value = float(value)
            if value != value or value in (float("inf"), float("-inf")):
                raise ClockUnavailable("时钟快照字段无效：%s" % name)
            if minimum is not None and value < minimum:
                raise ClockUnavailable("时钟快照字段越界：%s" % name)
            return value

        epoch = number("epoch_s", snap.get("epoch_s"))
        anchor_tick = number("tick_s", snap.get("tick_s"))
        sampled_tick = number("sampled_tick_s", snap.get("sampled_tick_s"))
        uncertainty = number("uncertainty_s", snap.get("uncertainty_s"), minimum=0.0)
        state = snap.get("state")
        if state not in ("ready", "holdover"):
            raise ClockUnavailable("时钟快照状态不可用：%s" % state)

        now_tick = self._tick()
        age = now_tick - anchor_tick
        sample_age = now_tick - sampled_tick
        if age < 0 or sample_age < 0:
            raise ClockUnavailable("时钟快照锚点晚于本机经过时长")
        if age > self._holdover_max_s:
            raise ClockUnavailable("时钟快照超过 %g 秒未更新" % self._holdover_max_s)
        drift = HOLDOVER_DRIFT_PPM * 1e-6 * sample_age
        total = uncertainty + drift
        if total > READY_UNCERTAINTY_S:
            raise ClockUnavailable("时钟快照误差超过 %g 秒" % READY_UNCERTAINTY_S)
        effective = "holdover" if (state == "holdover"
                                   or age > self._resync_interval_s) else "ready"
        return {"snap": snap, "epoch": epoch, "anchor_tick": anchor_tick,
                "sampled_tick": sampled_tick, "uncertainty": uncertainty,
                "drift": drift, "total_uncertainty": total, "age": age,
                "now_tick": now_tick, "state": effective,
                "now": epoch + (now_tick - anchor_tick), "owner": owner}

    # ---- 与 BusinessClock 相同的消费接口（RetryBudget 需要）----
    def elapsed_s(self) -> float:
        """应用内经过时长：与父实例同一 tick 域，含睡眠。"""
        return float(self._tick())

    def now(self) -> float:
        """业务 UTC epoch 秒；锚点用 tick_s，不用 sampled_tick_s。"""
        return self._load()["now"]

    def bounds(self) -> tuple[float, float]:
        """业务时刻区间：采样误差 + 采样后保守漂移，同一份快照内取值。"""
        loaded = self._load()
        return (loaded["now"] - loaded["total_uncertainty"],
                loaded["now"] + loaded["total_uncertainty"])

    def state(self) -> str:
        return self._load()["state"]

    @property
    def reason(self) -> str:
        loaded = self._load()
        snap = loaded["snap"]
        return str(snap.get("reason") or ("沿用上次校时" if loaded["state"] == "holdover"
                                          else "北京时间已同步"))

    @property
    def owner_id(self) -> str:
        return self._load()["owner"]

    @property
    def generation(self) -> int:
        value = self._load()["snap"].get("generation")
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0

    def snapshot(self) -> dict:
        """把当前有效读数整理成快照形状（不写文件）。

        ``uncertainty_s`` 保留采样时记录值，另给 ``uncertainty_now_s`` 表示
        含采样后漂移的当前误差——调用方不该再从 sampled 时刻重算一次漂移。
        """
        loaded = self._load()
        snap = dict(loaded["snap"])
        snap["state"] = loaded["state"]
        snap["age_s"] = loaded["age"]
        snap["uncertainty_now_s"] = loaded["total_uncertainty"]
        return snap


def _probe_json() -> int:
    """--probe --json：只探公共 NTP 并退出；不加载 runtime_paths、不碰凭证。"""
    clock = BusinessClock(background=False)
    started = time.monotonic()
    try:
        state = clock.sync()
    except Exception as exc:
        state = "unavailable"
        clock._reason = "探针异常：%s" % exc
    result = {"ok": state == "ready", "state": state,
              "owner_id": clock.owner_id, "generation": clock.generation,
              "reason": clock.reason, "elapsed_probe_s":
              round(time.monotonic() - started, 3)}
    try:
        result["epoch_s"] = clock.now()
        lower, upper = clock.bounds()
        result["bounds"] = [lower, upper]
        result["uncertainty_s"] = round(upper - lower, 3) / 2
        result["os_offset_s"] = round(clock.now() - time.time(), 3)  # 仅诊断
    except ClockUnavailable:
        pass
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


def _main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--probe" in argv:
        return _probe_json()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
