# -*- coding: utf-8 -*-
"""进程级生命周期持久化适配器：ui.json 心跳 / 启动冻结 / 正常关闭记账。

生命周期字段只写 ui.json，绝不写 session.json / storage_state.json；F5 重载
只是页面重取 bootstrap，不会重建这里的任何状态。心跳与界面 save_prefs 共用
prefs_store 的同一把锁，做原子读-合并-写，互不覆盖。

偏好路径由调用方注入（生产传 PREFS，测试传临时目录）：心跳线程绝不会
写回真实数据根。

@author LiXinwen
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from domain.lifecycle_rules import compute_lifecycle, compute_lifecycle_v2
from domain.time_text import format_local_text
from infrastructure.prefs_store import (
    PrefsLockTimeout,
    atomic_write,
    load_from,
    prefs_transaction,
)


def _now_text() -> str:
    """本地墙钟文本：墙钟读取在 infrastructure 适配层，domain 只做纯格式化。"""
    return format_local_text(datetime.now())


class AppLifecycle:
    """进程级生命周期：启动冻结上一周期 → 每分钟心跳 → 正常关闭写 last_closed_at。"""

    INTERVAL_S = 60

    def __init__(self, prefs_file: Path, now_fn=None) -> None:
        # now_fn = 业务时钟：v2 记账用可信 epoch；None 时只走旧文本路径（旧测试）
        self._now_fn = now_fn
        # 创建时定格偏好路径：测试传入临时目录后，心跳线程绝不会写回真实数据根
        self._prefs = Path(prefs_file)
        # 本进程归属与本地经过时长起点：首校时迟到时据此倒推本次启动 epoch，
        # 而不是把「校准时刻」当成启动时刻（也不是永久放弃 v2）
        self._run_id = uuid.uuid4().hex[:12]
        self._start_mono = time.monotonic()
        self._v2_lock = threading.Lock()
        self._foreign_owner = False   # 看到更新的 run_id 后，本实例停止写生命周期
        # 时钟读数在事务外完成：绝不持偏好锁联网或等待授时
        self._started_epoch = None
        if self._now_fn is not None:
            try:
                self._started_epoch = self._now_fn()
            except Exception:
                self._started_epoch = None   # 保留 now_fn：迟到校准仍可恢复
        self._started_text = _now_text()
        self._prev_v2 = None
        try:
            with prefs_transaction(self._prefs):
                self._load_and_claim(load_from(self._prefs))
        except PrefsLockTimeout:
            # 跨进程锁拿不到：本次启动元数据写入整体放弃。界面仍可用，下一次
            # 心跳或启动会重新读取；宁可少记一次，也不覆盖另一个实例的周期。
            self._load_and_claim(load_from(self._prefs), write=False)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._heartbeat, daemon=True,
                                        name="lifecycle-heartbeat")
        self._thread.start()

    def _load_and_claim(self, raw: dict, *, write: bool = True) -> None:
        """在事务内冻结上一周期并写入本次周期；调用方保证排他。

        started + 首个心跳 + run_open=true 三者一起落盘——短启动就崩溃的周期
        也有「最后存活时刻」，不会回退拿更早周期的关闭时间当精确值。
        """
        self._prev = compute_lifecycle(raw, datetime.now())
        self._prev_v2 = raw.get("lifecycle_v2")
        if self._now_fn is not None:
            # 生产时钟路径：缺 v2 或质量不足一律 unknown，绝不退回旧墙钟差值。
            if self._started_epoch is None:
                v2 = {"closed_for_s": None, "close_quality": "unknown",
                      "close_source": "clock_unavailable"}
            else:
                v2 = compute_lifecycle_v2(self._prev_v2, self._started_epoch)
                if v2 is None:
                    v2 = {"closed_for_s": None, "close_quality": "unknown",
                          "close_source": "v2_missing"}
            self._prev = dict(self._prev, **v2)
        if not write:
            return
        merged = dict(raw)
        merged["last_started_at"] = self._started_text
        merged["last_heartbeat_at"] = self._started_text
        merged["run_open"] = True
        if self._now_fn is not None:
            merged["lifecycle_v2"] = {
                "run_id": self._run_id,
                "run_open": True,
                "started_epoch": self._started_epoch,
                "heartbeat_epoch": self._started_epoch,
                "quality": "ready" if self._started_epoch is not None else "unknown"}
        self._atomic_write(merged)

    def _atomic_write(self, prefs: dict) -> None:
        atomic_write(self._prefs, prefs)

    def _write_merged(self, patch: dict, force: bool = False) -> bool:
        """与 update_prefs 同一把锁的读-合并-写，但钉住创建时的偏好文件。

        force=False（心跳路径）：锁内先核停止标记——finalize 已开始收尾时，
        迟到的心跳直接放弃，绝不把 last_heartbeat_at 写到关闭时刻之后。
        归属核验与写回在同一事务内：文件里的 lifecycle_v2 已属于别的 run_id 时
        整份放弃（返回 False），不先清空再写成自己的记录。
        """
        try:
            with prefs_transaction(self._prefs):
                if not force and self._stop.is_set():
                    return False
                prefs = load_from(self._prefs)
                incoming = patch or {}
                if "lifecycle_v2" in incoming:
                    stored = prefs.get("lifecycle_v2")
                    if isinstance(stored, dict) and \
                            stored.get("run_id") not in (None, self._run_id):
                        self._foreign_owner = True
                        return False
                prefs.update(incoming)
                self._atomic_write(prefs)
                return True
        except PrefsLockTimeout:
            return False

    def _heartbeat(self) -> None:
        while not self._stop.wait(self.INTERVAL_S):
            try:
                self._beat_once()
            except Exception:
                pass   # 单次心跳失败不影响界面，下一分钟再试

    def _beat_once(self) -> bool:
        """一次心跳：心跳时刻在锁外读取，读-核验-合并-写在偏好事务内完成。"""
        patch = {"last_heartbeat_at": _now_text()}
        record = None
        if self._now_fn is not None:
            epoch = None
            try:
                epoch = self._now_fn()
            except Exception:
                epoch = None
            with self._v2_lock:
                if self._foreign_owner:
                    return False
                if epoch is not None:
                    self._recover_started_epoch(epoch)
                # 校时失效时保持本周期归属、质量降为 unknown，不编心跳
                record = self._v2_record(run_open=True, epoch=epoch)
            if record is None:
                return False
            patch["lifecycle_v2"] = record
        return self._write_merged(patch)

    def _recover_started_epoch(self, epoch: float) -> None:
        """首校时迟到：用本进程经过时长倒推本次启动 epoch，并重算上一周期。

        只在仍缺起点时执行一次；调用方必须持有 _v2_lock。生产时钟路径下
        没有 v2 旧记录时同样只能 unknown，不退回旧墙钟文本的精确/约值。
        """
        if self._started_epoch is not None or self._now_fn is None:
            return
        elapsed = max(0.0, time.monotonic() - self._start_mono)
        self._started_epoch = float(epoch) - elapsed
        v2 = compute_lifecycle_v2(self._prev_v2, self._started_epoch)
        if v2 is None:
            v2 = {"closed_for_s": None, "close_quality": "unknown",
                  "close_source": "v2_missing"}
        self._prev = dict(self._prev, **v2)

    def _v2_record(self, *, run_open: bool, epoch=None, closed_epoch=None) -> dict:
        """本周期 v2 记录：缺本次可信时刻就记 unknown，不沿用旧心跳凑可信事实。

        run_id 归属隔离：文件里的记录属于别的进程实例时返回 None，由调用方
        整体放弃本次生命周期写入，免得迟到的旧写覆盖更新的周期事实。
        调用方必须持有 _v2_lock。
        """
        stored = self._read_v2() or {}
        if stored and stored.get("run_id") not in (None, self._run_id):
            self._foreign_owner = True
            return None
        record = dict(stored)
        record.update({"run_id": self._run_id, "run_open": bool(run_open),
                       "started_epoch": self._started_epoch})
        # 「有起点」不等于「此刻可信」：本次心跳/关闭取时失败时必须 unknown，
        # 否则下一次启动会把旧 heartbeat 解释成约值、把旧 closed 当精确值。
        trusted_now = epoch is not None or closed_epoch is not None
        if self._started_epoch is None or not trusted_now:
            record["quality"] = "unknown"
            return record
        record["quality"] = "ready"
        if run_open and epoch is not None:
            record["heartbeat_epoch"] = float(epoch)
        if closed_epoch is not None:
            record["closed_epoch"] = float(closed_epoch)
        return record

    def _read_v2(self) -> "dict | None":
        try:
            v2 = (load_from(self._prefs) or {}).get("lifecycle_v2")
            return v2 if isinstance(v2, dict) else None
        except Exception:
            return None

    def snapshot(self) -> dict:
        """bootstrap 的 lifecycle 载荷：上一周期关闭时长/质量（只读，只作提示）。

        当前凭证的登录估算不在这里——bootstrap.login 与 check 事件统一带
        login_state() 的同一份锚点，界面只认那一个来源，避免冻结快照与
        重新登录后的实际剩余自相矛盾。
        """
        return {
            "last_started_at": self._started_text,
            "last_closed_at": self._prev["last_closed_at"],
            "last_heartbeat_at": self._prev["last_heartbeat_at"],
            "closed_for_s": self._prev["closed_for_s"],
            "close_quality": self._prev["close_quality"],
        }

    def finalize(self) -> None:
        """窗口正常关闭：先置停（拦住后续心跳），再在同一把锁内原子写
        run_open=false 与关闭时刻——顺序反了会留下晚于关闭的心跳。
        不持锁 join 心跳线程：持锁 join 会和心跳的锁内写入互相等死。
        取 epoch 失败也必须落「正常关闭」文本事实，v2 质量记 unknown，不跳过收尾；
        但若文件已被另一个 run_id 接管，本实例整体让位，不改写新周期的记录。"""
        self._stop.set()
        patch = {"last_closed_at": _now_text(), "run_open": False}
        write = True
        if self._now_fn is not None:
            epoch = None
            try:
                epoch = self._now_fn()
            except Exception:
                epoch = None
            try:
                with self._v2_lock:
                    if self._foreign_owner:
                        return
                    if epoch is not None:
                        self._recover_started_epoch(epoch)
                    record = self._v2_record(
                        run_open=False, epoch=epoch, closed_epoch=epoch)
                    if record is None:
                        return       # 归属已被新实例接管：不写回旧周期
                    patch["lifecycle_v2"] = record
            except Exception:
                patch["lifecycle_v2"] = {
                    "run_id": self._run_id, "run_open": False,
                    "started_epoch": self._started_epoch,
                    "quality": "unknown"}
        try:
            self._write_merged(patch, force=True)
        except Exception:
            pass
