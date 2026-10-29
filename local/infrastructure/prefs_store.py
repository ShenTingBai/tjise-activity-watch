# -*- coding: utf-8 -*-
"""ui.json 偏好存储适配器：跨进程短时锁 + 原子读-合并-写。

路径全部显式传入（调用方持有 PREFS 绑定），锁与写入原语可整体替换成测试桩。
「读-核验-写」必须串行，否则生命周期心跳的整份快照会覆盖用户刚存的筛选
（反之亦然）；拿不到跨进程锁宁可放弃本次写入，也不写回未核验的旧快照。

@author LiXinwen
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
from pathlib import Path

# 进程内锁：同一进程里的生命周期线程与界面调用互斥
PREFS_LOCK = threading.Lock()
LOCK_TIMEOUT_S = 1.0      # 生命周期元数据：拿不到就放弃本次写入
SAVE_TIMEOUT_S = 3.0      # 用户偏好：给正在收尾的另一个进程留出时间


class PrefsLockTimeout(RuntimeError):
    """同一偏好文件的跨进程短时锁在期限内没有拿到。"""


def load_from(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def atomic_write(path: Path, prefs: dict) -> None:
    """临时文件 + os.replace 原子落盘：断电/崩溃不留半个 ui.json。

    目录随写入确保（测试里 path 指向临时目录，不会碰到真实数据根）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(prefs, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def acquire_file_lock(handle, timeout_s: float) -> None:
    """给已打开的锁文件加排他锁（标准库：Windows msvcrt / POSIX fcntl）。

    只用 1 字节区域锁表达「正在读-核验-写」；不写入业务内容，也不依赖锁文件
    内容本身。轮询而非阻塞，才能在窗口关闭路径上有界放弃。
    """
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    if sys.platform == "win32":
        import msvcrt
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")        # 区域锁需要文件里至少有一个字节
            handle.flush()
        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise PrefsLockTimeout("偏好文件正被另一个进程写入")
                time.sleep(0.005)
    else:
        import fcntl
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise PrefsLockTimeout("偏好文件正被另一个进程写入")
                time.sleep(0.005)


def release_file_lock(handle) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextlib.contextmanager
def prefs_transaction(path: Path, *, timeout_s: float = LOCK_TIMEOUT_S):
    """同一 prefs 路径的进程内 + 跨进程短时排他事务。

    进程内沿用 PREFS_LOCK；跨进程用与 ui.json 同目录的 ``ui.json.lock`` 区域锁。
    事务只覆盖「读-核验-合并-原子写」，调用方不得持锁联网或等待子进程。
    拿不到跨进程锁时抛 PrefsLockTimeout：宁可放弃这次元数据写入，也不写回
    一份未核验归属的旧快照。
    """
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    if not PREFS_LOCK.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise PrefsLockTimeout("偏好文件正在写入")
    try:
        lock_path = Path(str(path) + ".lock")
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(lock_path, "a+b")
        except OSError:
            raise PrefsLockTimeout("偏好锁文件不可创建")
        try:
            acquire_file_lock(handle, max(0.0, deadline - time.monotonic()))
            try:
                yield
            finally:
                release_file_lock(handle)
        finally:
            handle.close()
    finally:
        PREFS_LOCK.release()


def update(path: Path, patch: dict) -> bool:
    """统一的原子读-合并-写；拿不到跨进程锁就返回失败。

    绕过文件锁写入会覆盖另一个实例刚保存的周期。调用方可重试用户偏好；
    可选登录估算落盘失败也不影响真实登录成功。
    """
    try:
        with prefs_transaction(path, timeout_s=SAVE_TIMEOUT_S):
            prefs = load_from(path)
            prefs.update(patch or {})
            atomic_write(path, prefs)
        return True
    except PrefsLockTimeout:
        return False
