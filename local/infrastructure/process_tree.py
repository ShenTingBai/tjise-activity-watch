# -*- coding: utf-8 -*-
"""worker 进程树适配器：Windows Job Object 归属 + 整树终止。

只在父进程还活着时 taskkill 才可靠；父进程先退出时必须靠 Job Object
（KILL_ON_JOB_CLOSE）收走 one-file 引导进程留下的真实 worker 与浏览器——
「停止不了、还在无限重试」就是没有这层归属的形态。

关键顺序：CREATE_SUSPENDED 建进程 → 建 Job → 归属 → ResumeThread。
先跑起来再 Assign 存在真实窗口：worker 在这几毫秒里就能派生 Playwright
子进程，那些后代不会自动进入 job，父进程先退出时照样脱管。

kernel32 通过模块级 win_kernel32()/KERNEL32 缓存提供，测试可整体替换
（替身接口），不必起真进程制造归属失败。

@author LiXinwen
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import threading

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_INFORMATION = 0x0400
_CREATE_SUSPENDED = 0x00000004
_TH32CS_SNAPTHREAD = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002
_INVALID_HANDLE_VALUE = (1 << 64) - 1
_RESUME_FAILED = 0xFFFFFFFF


class SpawnFailure(RuntimeError):
    """worker 创建/归属/恢复失败：本场没有启动。

    失败不降级成「父进程活着才 taskkill」——那种运行方式在父进程被杀后
    会留下脱管的后代，正是「停止不了」的来源。宁可这一场明确失败。
    """


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ThreadID", ctypes.c_uint32),
        ("th32OwnerProcessID", ctypes.c_uint32),
        ("tpBasePri", ctypes.c_long),
        ("tpDeltaPri", ctypes.c_long),
        ("dwFlags", ctypes.c_uint32),
    ]


_KERNEL32 = None


def win_kernel32():
    """Windows kernel32 句柄与函数原型（只初始化一次）。

    必须在同一个句柄上设置 argtypes：否则 64 位下 ctypes 会把 Python int
    当 32 位 c_int 传，job/进程/线程句柄被截断，调用静默失败。
    """
    global _KERNEL32
    if _KERNEL32 is not None:
        return _KERNEL32
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.SetInformationJobObject.restype = ctypes.c_int
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    kernel32.AssignProcessToJobObject.restype = ctypes.c_int
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.TerminateJobObject.restype = ctypes.c_int
    kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenThread.restype = ctypes.c_void_p
    kernel32.OpenThread.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.ResumeThread.restype = ctypes.c_uint32
    kernel32.ResumeThread.argtypes = [ctypes.c_void_p]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    kernel32.Thread32First.restype = ctypes.c_int
    kernel32.Thread32First.argtypes = [ctypes.c_void_p, ctypes.POINTER(_THREADENTRY32)]
    kernel32.Thread32Next.restype = ctypes.c_int
    kernel32.Thread32Next.argtypes = [ctypes.c_void_p, ctypes.POINTER(_THREADENTRY32)]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    _KERNEL32 = kernel32
    return kernel32


def close_handle(handle) -> None:
    """关闭自有句柄；0/None 直接跳过，失败不抛（清理路径不制造新异常）。"""
    if not handle:
        return
    try:
        win_kernel32().CloseHandle(handle)
    except Exception:
        pass


def open_primary_thread(pid: int):
    """打开刚创建的挂起进程的主线程。

    挂起进程此刻只有一个线程，所以「按拥有者 PID 找到的第一个线程」
    就是主线程。找不到或打不开返回 None，由调用方按启动失败处理。
    """
    kernel32 = win_kernel32()
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if not snapshot or snapshot == _INVALID_HANDLE_VALUE:
        return None
    try:
        entry = _THREADENTRY32()
        entry.dwSize = ctypes.sizeof(_THREADENTRY32)
        found = None
        ok = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while ok:
            if entry.th32OwnerProcessID == int(pid):
                found = int(entry.th32ThreadID)
                break
            ok = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        if not found:
            return None
        return kernel32.OpenThread(_THREAD_SUSPEND_RESUME, False, found) or None
    finally:
        close_handle(snapshot)


def create_job_for(proc: subprocess.Popen):
    """建一个 KILL_ON_JOB_CLOSE 的 Job 并把进程挂进去；失败返回 None。

    返回 None 由调用方升级成 SpawnFailure：不再有「没拿到所有权也继续跑」
    的降级路径。失败时自己关掉已建的 job 与进程句柄，不留泄漏。
    """
    if os.name != "nt":
        return None
    kernel32 = win_kernel32()
    job = None
    handle = None
    try:
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
                job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info), ctypes.sizeof(info)):
            close_handle(job)
            return None
        handle = kernel32.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _PROCESS_QUERY_INFORMATION,
            False, proc.pid)
        if not handle:
            close_handle(job)
            return None
        if not kernel32.AssignProcessToJobObject(job, handle):
            close_handle(job)
            return None
        return job
    except Exception:
        close_handle(job)
        return None
    finally:
        close_handle(handle)


class ProcessTree:
    """本场 worker 的进程树所有权：收走时不依赖一个还活着的父 PID。"""

    def __init__(self, proc: subprocess.Popen, job):
        self.proc = proc
        self._job = job
        self._lock = threading.RLock()
        self._ended = False
        self._closed = False

    @property
    def owned(self) -> bool:
        """是否拿到了跨父进程生命周期的所有权（Job Object）。"""
        return self._job is not None

    def terminate(self) -> bool:
        """终止整棵树，返回是否确认已终止。

        失败不置「已成功」标志：watchdog 失败后 finally 还能再试一次。
        加锁串行化 watchdog 与 finally 的并发调用，避免一个正在
        TerminateJobObject 时另一个已经走进了 close()。
        """
        with self._lock:
            if self._ended:
                return True
            ok = True
            if self._job is not None:
                try:
                    if not win_kernel32().TerminateJobObject(self._job, 1):
                        ok = False
                except Exception:
                    ok = False
            if self.proc.poll() is None:
                if not kill_tree(self.proc):
                    ok = False
            if ok:
                self._ended = True
            return ok

    def close(self) -> None:
        """释放 job 句柄；KILL_ON_JOB_CLOSE 保证残留后代随句柄一起结束。

        与 terminate 互斥：绝不在另一次终止还在用句柄时关掉它。
        """
        with self._lock:
            if self._job is None or self._closed:
                return
            self._closed = True
            close_handle(self._job)
            self._job = None


def kill_tree(proc: subprocess.Popen) -> bool:
    """按进程树终止 worker，返回是否已确认终止。

    只 terminate 直接子进程的话，PyInstaller onefile 的引导进程死后，
    解压出的真实 worker 会带着浏览器继续按重试节奏跑——就是
    「停止不了、还在无限重试」的那个形态。收尸（wait）交给调用方
    的监控循环，这里只负责让它死透。

    注意：本函数只在父进程还活着时有效；父进程已退出时调用方必须用
    :class:`ProcessTree`（Job Object）收走它留下的后代。返回值是
    「终止是否成功」，调用方据此决定能否置「已收走」标志。
    """
    if proc.poll() is not None:
        return True
    killed = False
    if os.name == "nt":
        try:
            done = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=3,
                creationflags=CREATE_NO_WINDOW)
            killed = done.returncode == 0
        except Exception:
            killed = False
    if proc.poll() is None:
        # taskkill 不可用/失败时的兜底：至少终止直接子进程
        try:
            proc.kill()
            killed = True
        except Exception:
            pass
    return killed or proc.poll() is not None


def spawn_owned_worker(args: list[str], *, cwd: str, env: dict) -> ProcessTree:
    """创建报名 worker：先挂起，取得进程树所有权后再让它跑。

    任何一步（挂起创建 / 建 Job / 归属 / 恢复线程）失败，都先把仍挂起的
    自有进程结束掉、关掉所有自有句柄，再抛 :class:`SpawnFailure`；
    调用方据此把这一场记成「本机没能启动」，不产生任何网络或浏览器动作。
    """
    if os.name != "nt":
        raise SpawnFailure("非 Windows 环境无法取得进程树所有权")
    proc = subprocess.Popen(
        args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        env=env, creationflags=CREATE_NO_WINDOW | _CREATE_SUSPENDED)
    job = None
    try:
        thread = open_primary_thread(proc.pid)
        if not thread:
            raise SpawnFailure("找不到刚创建进程的主线程")
        try:
            job = create_job_for(proc)
            if not job:
                raise SpawnFailure("worker 没能加入进程树（Job 创建或归属失败）")
            if win_kernel32().ResumeThread(thread) == _RESUME_FAILED:
                raise SpawnFailure("worker 主线程没能恢复执行")
        finally:
            close_handle(thread)
    except BaseException:
        # 失败路径：结束仍挂起的自有进程（此刻它还没有任何后代），
        # 管道与句柄一并关闭，绝不留下半个 worker。
        close_handle(job)
        job = None
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        if proc.stdout:
            try:
                proc.stdout.close()
            except Exception:
                pass
        raise
    return ProcessTree(proc, job)
