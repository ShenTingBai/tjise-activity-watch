#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校园活动自动报名 · 本机版（WebView2 外壳）。

界面在 ui/ 下用 HTML/CSS 写，这里只做三件事：

1. 起窗口、加载 ui/index.html；
2. 把 grab.py 的能力暴露成页面可调的方法（window.pywebview.api.*）；
3. 把子进程输出实时推给页面（window.onBackend.log / .state / .done）。

业务逻辑一律留在 grab.py：换界面不用碰它，界面出问题也能直接命令行复现。

@author LiXinwen
"""
from __future__ import annotations

import atexit
import contextlib
import ctypes
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

# 演示模式的数据根隔离必须在导入 runtime_paths 之前完成：
# DATA_DIR 是导入期常量，晚一步就已经指向真实 %LOCALAPPDATA%，bootstrap 会读真实凭证。
# 只认显式 --demo；不覆盖用户自己指定的 TJISE_DATA_DIR。
if "--demo" in sys.argv and not os.environ.get("TJISE_DATA_DIR", "").strip():
    _demo_root = tempfile.mkdtemp(prefix="tjise-demo-")
    os.environ["TJISE_DATA_DIR"] = _demo_root
    atexit.register(shutil.rmtree, _demo_root, True)
from datetime import datetime, timedelta
from pathlib import Path

import webview

from signup_plan import PlanCoordinator, TaskSpec, classify_signup_mode, validate_plan
from business_clock import BusinessClock, ClockUnavailable
from time_contract import school_epoch, school_time_text, configure_time_source
from update_check import check_latest_release

# 登录态/偏好统一数据根：开发用 local/data，冻结 EXE 用 %LOCALAPPDATA%
# （TJISE_DATA_DIR 可覆盖），与 grab-worker 解析出同一份登录文件
from runtime_paths import DATA_DIR, SESSION_FILE, UI_STATE_FILE, ensure_data_dir

HERE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", HERE))
UI = RESOURCE_ROOT / "ui" / "index.html"


def resolve_worker_path(frozen: bool, here: Path, resource_root: Path) -> Path:
    """worker 唯一来源（纯函数，供契约测试）：源码态跑 here/grab.py；
    冻结态只用打包进资源根的 grab-worker.exe——绝不退回 EXE 旁的旧文件，
    也不依赖启动 cwd：独立单 EXE 被拷到任何空目录都必须自足。"""
    if frozen:
        return resource_root / "grab-worker.exe"
    return here / "grab.py"


GRAB = resolve_worker_path(getattr(sys, "frozen", False), HERE, RESOURCE_ROOT)
PREFS = UI_STATE_FILE
TOKEN_LIFETIME_H = 17.5
# GitHub 最新版本页：用户点「GitHub 最新版本」时由系统默认浏览器打开，
# 启动时不自动联网检查，也不在 WebView 里跳转
RELEASES_LATEST_URL = "https://github.com/ShenTingBai/tjise-activity-watch/releases/latest"


def read_version() -> str:
    """读唯一的版本源 VERSION：源码从项目根、绿色版从脚本旁、冻结程序从打包资源根。

    版本格式只认 major.minor.patch；文件缺失或格式非法直接启动失败并给开发者
    可读错误——把版本读取失败静默成旧版本，会发布出 GUI、文件名、tag 对不上号的包。
    不能依赖启动 cwd：三种形态各有一个固定候选路径，按序取第一个存在的。
    """
    candidates = [
        RESOURCE_ROOT / "VERSION",   # 冻结程序：build_exe --add-data 打进资源根
        HERE / "VERSION",            # 绿色版：make_portable 复制到 app.py 旁
        HERE.parent / "VERSION",     # 源码：仓库根
    ]
    for path in candidates:
        if path.is_file():
            text = path.read_text(encoding="utf-8").strip()
            if re.fullmatch(r"\d+\.\d+\.\d+", text):
                return text
            raise SystemExit("[X] VERSION 内容不是 major.minor.patch：%r（%s）"
                             % (text, path))
    raise SystemExit("[X] 找不到 VERSION 文件（尝试过 %s）；版本号只有一个来源，不回退写死值"
                     % "、".join(str(p) for p in candidates))


APP_VERSION = read_version()

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 组件诊断：结构化错误码 → 人话主文案。技术细节只进运行记录（日志），
# 主提示保持一句话——用户要的是「该做什么」，不是 WinError 2。
WORKER_PROBLEM_TEXT = {
    "worker_missing": "程序组件缺失，请重新下载完整版本",
    "worker_permission": "程序组件无法读取，请重新下载完整版本",
    "spawn_failed": "程序组件启动失败，请重新下载完整版本",
}
# 离线自检（--runtime-check）给内置 worker --help 的时限
RUNTIME_CHECK_TIMEOUT_S = 60


def worker_problem() -> str:
    """启动 worker 前的组件自检：返回结构化错误码，组件齐备返回空串。

    源码态要求 grab.py 可读；冻结态要求资源根里的 grab-worker.exe 存在且可执行。
    只看文件事实，不发起任何进程/网络动作——调用方据此决定拒绝还是放行。
    """
    if getattr(sys, "frozen", False):
        if not GRAB.is_file():
            return "worker_missing"
        if not os.access(GRAB, os.R_OK | os.X_OK):
            return "worker_permission"
        return ""
    if not GRAB.is_file():
        return "worker_missing"
    if not os.access(GRAB, os.R_OK):
        return "worker_permission"
    return ""


def _component_error(exc: Exception) -> str:
    """把**进程创建**抛出的异常归类为结构化组件错误码（check/list/login 收尾共用）。

    只对进程启动阶段的 OSError 调用：文件/权限/其他 OS 层失败才有资格归组件码。
    数据/解析等普通异常不进这里——数据坏了不是组件坏了，不得诱导重新下载。
    """
    if isinstance(exc, FileNotFoundError):
        return "worker_missing"
    if isinstance(exc, PermissionError):
        return "worker_permission"
    return "spawn_failed"


class _ClosingError(RuntimeError):
    """Api 关闭屏障拒绝新 worker 创建：属「取消/正在关闭」语义。

    调用方必须把它与组件异常分开处理——关闭拒绝不得归类为 worker_* 组件码
    （重下/重检都解决不了「程序正在退出」），也不得冒充报名失败向上抛。
    """

# --demo：用假数据起界面，方便调样式、截图，不需要登录
DEMO = False


def demo_items() -> list[dict]:
    now = time.time()
    raw = [
        # lab 与 career 报名同时开放，但举办时段重叠；workshop 同批开放却不重叠。
        ("demo-security", "网络安全·数字时代大长城", "实践学习", "教学 A301", -3, 3600, 7200, 10800, False),
        ("demo-face", "我与学生面对面（天软信息化发展之路）", "讲座", "报告厅", -7200, 7200, -3600, 1800, True),
        ("demo-lab", "程序设计实验室开放日", "实践学习", "实训楼 B204", 1800, 5400, 7200, 10800, False),
        ("demo-career", "简历门诊：一对一改简历", "就业指导", "线上", 1800, 5400, 9000, 12600, False),
        ("demo-workshop", "创新作品夜场导览", "创新实践", "创新中心展厅", 1800, 5400, 14400, 16200, False),
        ("demo-volunteer", "图书馆志愿服务（第 12 期）", "志愿服务", "图书馆一层", 86400, 90000, 172800, 176400, False),
        # 活动举办时间缺失：演示「不能静默加入计划」的校验边界（act 两项传 None）
        ("demo-exhibit", "宿舍文化节摄影征集", "文化活动", "线上", 90000, 93600, None, None, False),
    ]
    flags = {
        "demo-security": (True, False),
        "demo-face": (False, False),
        "demo-lab": (True, True),
        "demo-career": (False, True),
        "demo-workshop": (True, False),
        "demo-volunteer": (None, None),
        "demo-exhibit": (None, True),
    }
    out = []
    for i, (aid, name, kind, place, s_off, e_off, act_s_off, act_e_off, signed) in enumerate(raw):
        # 样例时间也按学校东八区格式化：界面按 UTC+8 解析，用本机时区写会错位
        start = school_time_text(now + s_off)
        end = school_time_text(now + e_off)
        # 举办时间未公布时保持空串，和 grab.py list --json 的缺失形态一致
        act_start = "" if act_s_off is None else school_time_text(now + act_s_off)
        act_end = "" if act_e_off is None else school_time_text(now + act_e_off)
        checkin_required, checkout_required = flags.get(aid, (None, None))
        out.append({"index": i + 1, "id": aid, "name": name, "type": kind,
                    "place": place, "sign_start": start, "sign_end": end,
                    "act_start": act_start, "act_end": act_end, "signed": signed,
                    "checkin_required": checkin_required, "checkout_required": checkout_required,
                    # 状态文案与 grab.py list --json 同一套中文口径，避免演示与真实契约不一致
                    "state": "已报名" if signed else (
                        "开放中" if s_off <= 0 else "未开放"),
                    "seconds_to_open": s_off})
    return out


# main 建立 BusinessClock 后登记快照路径与 owner（_env 给 worker 用）
_CLOCK_ENV = {"path": "", "owner": ""}


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # 把主程序解析好的数据根传给 worker：即使 worker 所在目录不同，
    # 两侧也会读写同一份 session/storage_state
    env["TJISE_DATA_DIR"] = str(DATA_DIR)
    # 同实例时钟快照：worker 复用父进程校时结果，不各自另选源（决策 012）
    if _CLOCK_ENV.get("path"):
        env["TJISE_CLOCK_SNAPSHOT"] = _CLOCK_ENV["path"]
        env["TJISE_CLOCK_OWNER"] = _CLOCK_ENV["owner"]
    return env


def worker_command(*args: str) -> list[str]:
    """普通 Python 调 grab.py；冻结主程序改调资源根里内置的 worker exe。"""
    if getattr(sys, "frozen", False):
        return [str(GRAB)] + list(args)
    return [sys.executable, str(GRAB)] + list(args)


def load_prefs_from(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_prefs() -> dict:
    return load_prefs_from(PREFS)


def _atomic_write_prefs(path: Path, prefs: dict) -> None:
    """临时文件 + os.replace 原子落盘：断电/崩溃不留半个 ui.json。

    目录随写入确保（测试里 path 指向临时目录，不会碰到真实数据根）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(prefs, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# ---- 独立北京时间（决策 012）-----------------------------------------------
# 生产 main 恒注入 BusinessClock；未注入时这里是显式不可用，绝不回退本机墙钟。
# 测试需要可读时钟时必须显式注入 Fake（见 local/tests/fixtures/business_clock.py）。
class _UnavailableClock:
    """未配置时钟：所有业务读数显式失败，不提供任何墙钟替身。"""

    owner_id = ""
    generation = 0

    def now(self) -> float:
        raise ClockUnavailable("业务时钟未配置")

    def bounds(self):
        raise ClockUnavailable("业务时钟未配置")

    def elapsed_s(self) -> float:
        return time.monotonic()      # 仅用于本地监督，不当作业务时刻

    def state(self) -> str:
        return "unavailable"

    @property
    def reason(self) -> str:
        return "业务时钟未配置"

    def request_resync(self) -> None:
        pass

    def snapshot(self) -> dict:
        raise ClockUnavailable("业务时钟未配置")


def _credential_id(token: str) -> str:
    """本机内部凭证摘要：只用于把 v2 锚点和具体凭证绑定，不输出到日志。"""
    import hashlib
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()[:16]


def token_hours_left(clock=None) -> "float | None":
    """登录剩余时间估算（只显示，不参与判断）。

    只有 ui.json 里存在与当前凭证匹配的 session_timing_v2 锚点（新登录且有可信
    北京时间时建立）且本次读得到可信业务时刻才报数；旧 saved_at 是本机墙钟文本，
    无法反推真实 UTC，绝不拿系统当前时间减它冒充准确小时数。
    返回 None = 「暂无法估算」，登录有效性永远由学校真实校验决定。
    """
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        token = str(data.get("token") or "").strip()
    except Exception:
        return None
    if not token:
        return None
    timing = {}
    try:
        timing = (load_prefs() or {}).get("session_timing_v2") or {}
    except Exception:
        timing = {}
    if not isinstance(timing, dict) or timing.get("credential_id") != _credential_id(token):
        return None
    saved_epoch = timing.get("saved_epoch")
    if not isinstance(saved_epoch, (int, float)):
        return None
    if clock is None:
        return None          # 无业务时钟就不报数，不拿系统时间冒充北京时间
    try:
        now = clock.now()
    except Exception:
        return None
    used = (float(now) - float(saved_epoch)) / 3600.0
    return max(0.0, TOKEN_LIFETIME_H - used)


def save_session_timing_anchor(token: str, epoch: float, uncertainty_s: float = 0.0) -> None:
    """新登录成功且有可信北京时间时建立 v2 锚点；换凭证必须换新锚点。"""
    if not token:
        return
    update_prefs({"session_timing_v2": {
        "credential_id": _credential_id(token),
        "saved_epoch": float(epoch),
        "time_source": "ntp",
        "uncertainty_s": round(float(uncertainty_s), 3)}})


# 界面偏好与生命周期心跳共用的持久化锁：读-合并-写必须串行，
# 否则心跳的整份快照会覆盖用户刚存的筛选/引导状态（反之亦然）
_PREFS_LOCK = threading.Lock()
_PREFS_LOCK_TIMEOUT_S = 1.0      # 生命周期元数据：拿不到就放弃本次写入
_PREFS_SAVE_TIMEOUT_S = 3.0      # 用户偏好：给正在收尾的另一个进程留出时间


class _PrefsLockTimeout(RuntimeError):
    """同一偏好文件的跨进程短时锁在期限内没有拿到。"""


def _acquire_prefs_file_lock(handle, timeout_s: float) -> None:
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
                    raise _PrefsLockTimeout("偏好文件正被另一个进程写入")
                time.sleep(0.005)
    else:
        import fcntl
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise _PrefsLockTimeout("偏好文件正被另一个进程写入")
                time.sleep(0.005)


def _release_prefs_file_lock(handle) -> None:
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
def _prefs_transaction(path: Path, *, timeout_s: float = _PREFS_LOCK_TIMEOUT_S):
    """同一 prefs 路径的进程内 + 跨进程短时排他事务。

    进程内沿用 _PREFS_LOCK；跨进程用与 ui.json 同目录的 ``ui.json.lock`` 区域锁。
    事务只覆盖「读-核验-合并-原子写」，调用方不得持锁联网或等待子进程。
    拿不到跨进程锁时抛 _PrefsLockTimeout：宁可放弃这次元数据写入，也不写回
    一份未核验归属的旧快照。
    """
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    if not _PREFS_LOCK.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise _PrefsLockTimeout("偏好文件正在写入")
    try:
        lock_path = Path(str(path) + ".lock")
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(lock_path, "a+b")
        except OSError:
            raise _PrefsLockTimeout("偏好锁文件不可创建")
        try:
            _acquire_prefs_file_lock(handle, max(0.0, deadline - time.monotonic()))
            try:
                yield
            finally:
                _release_prefs_file_lock(handle)
        finally:
            handle.close()
    finally:
        _PREFS_LOCK.release()


def update_prefs(patch: dict) -> bool:
    """统一的原子读-合并-写（Api.save_prefs 与生命周期心跳共用同一把锁）。

    拿不到跨进程锁就返回失败：绕过文件锁写入会覆盖另一个实例刚保存的周期。
    调用方可重试用户偏好；可选登录估算落盘失败也不影响真实登录成功。
    """
    try:
        with _prefs_transaction(PREFS, timeout_s=_PREFS_SAVE_TIMEOUT_S):
            prefs = load_prefs()
            prefs.update(patch or {})
            _atomic_write_prefs(PREFS, prefs)
        return True
    except _PrefsLockTimeout:
        return False


def login_state(clock=None) -> dict:
    """登录三态快照：界面靠它区分「从没登录过」和「登录过期」。

    「没有登录」= session.json 不存在、无法解析或缺少 token；
    只有 token 还在但已过期才叫登录态失效。缺 token 时 hours_left
    必须是 None——按 saved_at 报一个假的小时数会把空壳会话显示成有效。
    saved_at / estimated_expires_at 只是估算提醒（saved_at + TOKEN_LIFETIME_H），
    登录有效性永远以学校真实校验为准，二者绝不参与放行或拦截。
    """
    state = {"session_present": False, "session_expired": False, "hours_left": None,
             "saved_at": None, "estimated_expires_at": None}
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        token = str(data.get("token") or "").strip()
        saved_text = str(data.get("saved_at") or "").strip()
    except Exception:
        return state
    if not token:
        return state
    state["session_present"] = True
    saved = _parse_local_time(saved_text)
    if saved is not None:
        state["saved_at"] = saved_text
    # 剩余时间只信 v2 锚点：旧 saved_at 是本机墙钟文本，宁可显示「暂无法估算」
    state["hours_left"] = token_hours_left(clock)
    if state["hours_left"] is not None and clock is not None:
        try:
            state["estimated_expires_at"] = clock.now() + state["hours_left"] * 3600.0
        except Exception:
            state["estimated_expires_at"] = None
    state["session_expired"] = state["hours_left"] is not None and state["hours_left"] <= 0
    return state


def _parse_local_time(text) -> "datetime | None":
    """ui.json / session.json 里的本地时间文本（YYYY-MM-DD HH:MM:SS）→ datetime。"""
    try:
        return datetime.strptime(str(text or "").strip(), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _latest_moment(*moments) -> "datetime | None":
    """取若干可解析时刻里最近的一个；全缺失返回 None（供生命周期参考点推导）。"""
    valid = [m for m in moments if m is not None]
    return max(valid) if valid else None


def compute_lifecycle(prev: dict, started_at: datetime) -> dict:
    """由上一运行周期的 ui.json 快照推导关闭时长与质量（纯函数，供契约测试）。

    周期以 run_open 标记区分（本周期启动写 true，正常关闭写 false）：
    - clean：上一周期 run_open=false，且关闭时刻不早于该周期启动、心跳不晚于关闭
      → 关闭记录确实属于最近这个周期，可给精确时长；
    - abnormal：上一周期 run_open=true（启动后没走正常关闭就没了）→ 只能拿
      「最近有效心跳（无则该周期启动时刻）」给「约」值，绝不能拿更早周期的
      关闭时间冒充本周期正常退出；
    - unknown：首次运行 / 时间不可解析，或系统时钟回拨（参考点晚于本次启动）。
    旧格式 ui.json 没有 run_open：按时间戳兼容推导，但关闭记录早于上一周期
    启动时刻时必须降级为 abnormal——它属于更早的周期，不是最近周期的正常关闭。
    """
    closed = _parse_local_time(prev.get("last_closed_at"))
    beat = _parse_local_time(prev.get("last_heartbeat_at"))
    started_prev = _parse_local_time(prev.get("last_started_at"))
    run_open = prev.get("run_open")
    result = {
        "last_closed_at": str(prev.get("last_closed_at") or ""),
        "last_heartbeat_at": str(prev.get("last_heartbeat_at") or ""),
        "closed_for_s": None,
        "close_quality": "unknown",
    }

    def finish(ref: datetime, quality: str) -> None:
        if started_at - ref < timedelta(0):
            return   # 系统时钟回拨/坏时间：精确与约值都不可信，退回未知
        result["closed_for_s"] = round((started_at - ref).total_seconds(), 1)
        result["close_quality"] = quality

    if run_open is True:
        # 显式存活标记仍在：上一周期没有正常关闭，只有存活参考点可依
        ref = _latest_moment(beat, started_prev)
        if ref is not None:
            finish(ref, "abnormal")
        return result
    if run_open is False:
        # 显式正常关闭。时间矛盾（关闭早于本周期启动，如系统回拨后关机）时
        # 精确值不可信，退 unknown——正常关闭标记不能覆盖时间顺序矛盾；
        # 同一秒关闭与启动允许等号。心跳晚于关闭同样按异常处理：正常关闭后
        # 不应再有心跳，出现即说明关闭记录可能被迟到写入污染。
        if closed is not None and started_prev is not None and closed < started_prev:
            return result
        if closed is not None and (beat is None or beat <= closed):
            finish(closed, "clean")
        elif beat is not None:
            finish(beat, "abnormal")
        return result
    # 旧格式（无 run_open 标记）：时间戳兼容推导
    if closed is not None and (started_prev is None or closed >= started_prev) \
            and (beat is None or beat <= closed):
        finish(closed, "clean")
        return result
    ref = _latest_moment(beat, started_prev)
    if ref is not None:
        finish(ref, "abnormal")
    return result


def compute_lifecycle_v2(prev_v2: "dict | None", started_epoch: float) -> "dict | None":
    """决策 012：以可信业务 epoch 记账的跨关闭时长（v2）。

    prev_v2 = {"run_open": bool, "started_epoch", "heartbeat_epoch", "closed_epoch",
               "quality": "ready"|"unknown"}
    全部由业务时钟写入：业务重校只影响单个读数的不确定度，不改变两次业务读数
    之差——系统墙钟跳变无法污染关闭时长。旧墙钟文本不迁移成 v2（不可反推）。
    quality=unknown（上一周期没有可信锚点）时不给任何精确/约值。
    返回 None = 无 v2 记账，调用方退回旧文本路径。
    """
    if not isinstance(prev_v2, dict) or "run_open" not in prev_v2:
        return None
    if prev_v2.get("quality") == "unknown":
        return {"closed_for_s": None, "close_quality": "unknown", "close_source": "v2"}
    started_prev = prev_v2.get("started_epoch")
    beat = prev_v2.get("heartbeat_epoch")
    closed = prev_v2.get("closed_epoch")
    result = {"closed_for_s": None, "close_quality": "unknown", "close_source": "v2"}

    def finish(ref, quality):
        if not isinstance(ref, (int, float)) or started_epoch - ref < 0:
            return
        result["closed_for_s"] = round(float(started_epoch) - float(ref), 1)
        result["close_quality"] = quality

    if prev_v2.get("run_open") is True:
        ref = beat if isinstance(beat, (int, float)) else started_prev
        finish(ref, "abnormal")
        return result
    # run_open False：正常关闭
    if isinstance(closed, (int, float)) and isinstance(started_prev, (int, float)) \
            and closed < started_prev:
        return result
    if isinstance(closed, (int, float)) and (beat is None or beat <= closed):
        finish(closed, "clean")
    elif isinstance(beat, (int, float)):
        finish(beat, "abnormal")
    return result


class AppLifecycle:
    """进程级生命周期：启动冻结上一周期 → 每分钟心跳 → 正常关闭写 last_closed_at。

    生命周期字段只写 ui.json，绝不写 session.json / storage_state.json；
    F5 重载只是页面重取 bootstrap，不会重建这里的任何状态。心跳与界面
    save_prefs 共用 _PREFS_LOCK，做原子读-合并-写，互不覆盖。
    """

    INTERVAL_S = 60

    def __init__(self, prefs_file: "Path | None" = None, now_fn=None) -> None:
        # now_fn = 业务时钟：v2 记账用可信 epoch；None 时只走旧文本路径（旧测试）
        self._now_fn = now_fn
        # 创建时定格偏好路径：测试传入临时目录后，心跳线程绝不会写回真实数据根
        self._prefs = prefs_file or PREFS
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
            with _prefs_transaction(self._prefs):
                self._load_and_claim(load_prefs_from(self._prefs))
        except _PrefsLockTimeout:
            # 跨进程锁拿不到：本次启动元数据写入整体放弃。界面仍可用，下一次
            # 心跳或启动会重新读取；宁可少记一次，也不覆盖另一个实例的周期。
            self._load_and_claim(load_prefs_from(self._prefs), write=False)
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
        _atomic_write_prefs(self._prefs, prefs)

    def _write_merged(self, patch: dict, force: bool = False) -> bool:
        """与 update_prefs 同一把锁的读-合并-写，但钉住创建时的偏好文件。

        force=False（心跳路径）：锁内先核停止标记——finalize 已开始收尾时，
        迟到的心跳直接放弃，绝不把 last_heartbeat_at 写到关闭时刻之后。
        归属核验与写回在同一事务内：文件里的 lifecycle_v2 已属于别的 run_id 时
        整份放弃（返回 False），不先清空再写成自己的记录。
        """
        try:
            with _prefs_transaction(self._prefs):
                if not force and self._stop.is_set():
                    return False
                prefs = load_prefs_from(self._prefs)
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
        except _PrefsLockTimeout:
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
            v2 = (load_prefs_from(self._prefs) or {}).get("lifecycle_v2")
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


_LIFECYCLE: "AppLifecycle | None" = None
_LIFECYCLE_LOCK = threading.Lock()


def get_lifecycle(now_fn=None) -> AppLifecycle:
    """进程级单例（懒创建）：main() 启动时先建，bootstrap 重复取同一实例。"""
    global _LIFECYCLE
    with _LIFECYCLE_LOCK:
        if _LIFECYCLE is None:
            _LIFECYCLE = AppLifecycle(now_fn=now_fn)
        return _LIFECYCLE


class Api:
    def __init__(self, clock=None) -> None:
        # 独立北京时间（决策 012）：生产 main 注入 BusinessClock；
        # 未注入时显式不可用，绝不回退本机时间。测试必须显式注入 Fake。
        self._clock = clock if clock is not None else _UnavailableClock()
        self._clock_syncing = False
        self._window: webview.Window | None = None
        self._proc: subprocess.Popen | None = None
        self._plan: PlanCoordinator | None = None
        self._plan_procs: dict[str, subprocess.Popen] = {}
        # 最近一个已结束计划的快照（只读展示用；_plan 只表示当前计划）
        self._last_plan: dict | None = None
        # check/list 这类短命 worker 的登记表：正常关闭时一并树终止，不留孤儿
        self._aux_procs: set = set()
        self._activities: dict[str, dict] = {}
        self._lock = threading.Lock()
        # 关闭屏障：shutdown 在同一把 _lock 内置位 _closing，而一切 worker 创建
        # （Popen）与登记也都在这把锁里先核它——关闭先发生则零创建；创建已开始
        # 则 shutdown 必等登记完成才能取快照，收走的进程不会漏。
        # _closing 表达「关闭已开始」；「收尾已完成」用 Event 表达（FB2）：后到
        # 的 shutdown 在锁外等待同一个 Event，返回即代表收尾真正完成。
        self._closing = False
        self._shutdown_done_event = threading.Event()
        # 刷新去重：连点「刷新」不该堆起多个 check/list 子进程
        self._checking = False
        self._listing = False
        # 在途请求 ID：去重跳过时界面才能对上账（R11）
        self._check_request_id = ""
        self._list_request_id = ""
        self._check_last = {"request_id": "", "payload": None}
        self._list_last = {"request_id": "", "payload": None}
        # 最近一次 check 的四态结论：start_plan 的放行/拒绝只信它
        self._last_check: dict = {}
        # 更新检查独立于登录/报名，只由帮助按钮触发；页面重载复用内存中的在途请求。
        self._updating = False
        self._update_request_id = ""
        self._update_result: dict | None = None
        # 开始请求登记（R11）：request_id → {state, plan_id, result}。
        # 只留最近有限条，活跃请求不驱逐；不存凭证、不写外部数据。
        self._start_requests: dict[str, dict] = {}
        self._start_order: list[str] = []
        # 计划接纳代次与最近接纳计划（P1）：plan/task/plan_done 事件与终态快照
        # 只归属「最近接纳的计划」。槽为空（_plan=None）不代表迟到回调属于最新
        # 计划——较新的计划可能已经结束；归属以接纳代次为准，不随 _plan 清空遗失。
        self._plan_generation = 0
        self._latest_plan_id = ""
        # 已提交过终态的计划（重复 plan_done 幂等：已定结论不被重复包改写）
        self._finished_plan_ids: dict[str, bool] = {}

    # ---------- 独立北京时间 ----------
    def _clock_now(self) -> float:
        if DEMO:
            # 演示离线：与 bootstrap 的「演示时间」快照同源（OS 时刻，仅演示）
            return time.time()
        return self._clock.now()

    def _clock_ready(self) -> bool:
        """开始门禁只认 ready/holdover 且 bounds 可读：syncing/未知不放行。"""
        try:
            if self._clock.state() not in ("ready", "holdover"):
                return False
            lower, upper = self._clock.bounds()
        except Exception:
            return False
        return bool(upper >= lower)

    def _clock_payload(self) -> dict:
        """bootstrap 的时钟载荷：demo 离线给显式标注的「演示时间」快照，
        不冒充真实授时；正常模式返回当前业务时钟快照。"""
        if DEMO:
            return {"schema": 1, "owner_id": "demo-clock", "generation": 1,
                    "state": "ready", "epoch_s": time.time(), "tick_s": 0,
                    "sampled_tick_s": 0, "uncertainty_s": None,
                    "source": "演示时间（离线，非真实授时）", "age_s": 0,
                    "reason": "演示模式不联网：顶栏时间仅供演示"}
        return self.clock_snapshot()

    def clock_snapshot(self) -> dict:
        """当前业务时钟快照（无凭证）：bootstrap 与「重新校时」共用。"""
        try:
            return self._clock.snapshot()
        except Exception as exc:
            return {"schema": 1, "state": "unavailable", "reason": str(exc)}

    def sync_clock(self) -> dict:
        """界面「重新校时」：去重后台重校，完成后推 business_clock 事件。"""
        with self._lock:
            if self._clock_syncing:
                return {"ok": True, "queued": True, "dedup": True}
            self._clock_syncing = True

        def run():
            try:
                # demo 完全离线：不发起 NTP，直接确认「演示时间」快照
                state = "ready" if DEMO else self._clock.sync()
            except Exception:
                state = "unavailable"
            with self._lock:
                self._clock_syncing = False
            self._emit("business_clock",
                       self._clock_payload() if DEMO else self.clock_snapshot())
            return state

        threading.Thread(target=run, name="clock-resync", daemon=True).start()
        return {"ok": True, "queued": True}

    def _start_clock_monitor(self) -> None:
        """有未来任务时在最早准备点前 20 秒请求一次后台重校（尊重源冷却）。"""
        if getattr(self, "_clock_monitor_started", False):
            return
        self._clock_monitor_started = True

        def monitor():
            requested_for = None
            while True:
                time.sleep(5)
                try:
                    with self._lock:
                        plan = self._plan
                    if plan is None:
                        requested_for = None
                        continue
                    nxt = plan.next_prepare_epoch()
                    if nxt is None:
                        continue
                    left = nxt - self._clock_now()
                    if 0 < left <= 20 and requested_for != plan.plan_id:
                        self._clock.request_resync()
                        requested_for = plan.plan_id
                except Exception:
                    continue

        threading.Thread(target=monitor, name="clock-monitor", daemon=True).start()

    # ---------- 事件推送 ----------
    def _emit(self, kind: str, payload) -> None:
        if not self._window:
            return
        # check/activities 统一带上本轮请求 ID：界面按 ID 丢弃迟到的旧轮事件，
        # 不靠「最后一次谁先到」猜（R11）
        if isinstance(payload, dict) and "request_id" not in payload:
            if kind == "check":
                payload = dict(payload, request_id=self._check_request_id)
            elif kind == "activities":
                payload = dict(payload, request_id=self._list_request_id)
        # 终态快照留档：去重跳过时若原操作已经结束，直接把结果交回去，
        # 不让界面等一个永远不会再发的旧 ID 事件（C06）
        if isinstance(payload, dict) and kind == "check":
            self._check_last = {"request_id": str(payload.get("request_id") or ""),
                                "payload": dict(payload)}
        elif isinstance(payload, dict) and kind == "activities":
            self._list_last = {"request_id": str(payload.get("request_id") or ""),
                               "payload": dict(payload)}
        js = "window.onBackend && window.onBackend.%s(%s)" % (
            kind, json.dumps(payload, ensure_ascii=False))
        try:
            self._window.evaluate_js(js)
        except Exception:
            pass

    # ---------- 读数据 ----------
    def bootstrap(self) -> dict:
        prefs = load_prefs()
        if DEMO:
            # 演示模式不读 session/storage_state，也不碰真实生命周期文件（C07）
            login_info = {"hours_left": None, "session_present": None,
                          "estimated_expires_at": None, "saved_at": None,
                          "demo": True}
            lifecycle_info = {"close_quality": "unknown", "closed_for_s": None,
                              "last_heartbeat_at": None, "demo": True}
        else:
            login_info = login_state(self._clock)
            lifecycle_info = get_lifecycle().snapshot()
        payload = {
            "version": APP_VERSION,
            "prefs": prefs,
            "business_clock": self._clock_payload(),
            "help": self.help(),
            "login": login_info,
            # 生命周期元数据（上一周期关闭时长/质量 + 登录估算）：只作提示，
            # 不参与任何放行/拦截；F5 重载重复调用拿到的是同一份进程级快照
            "lifecycle": lifecycle_info,
            # demo 标记：界面据此放行演示计划（真实模式仍要求登录校验 valid）
            "demo": DEMO,
        }
        # busy 与计划快照必须在同一临界区取：分两次拿锁会出现「busy=true 但
        # plan 是上一份终态快照」或反过来的错配，F5 恢复就会把已完成的计划
        # 画成运行中（FR05）。
        with self._lock:
            payload["busy"] = self._is_busy_locked()
            plan = self._plan
            last_plan = self._last_plan
            payload["update_check"] = {"checking": self._updating,
                                       "request_id": self._update_request_id,
                                       "result": self._update_result}
            if plan is not None:
                # 页面重载会丢掉 plan/task 事件：把计划快照一并交给 bootstrap，
                # 界面才能恢复任务列表和「停止」按钮；计划本身不会重复提交。
                payload["plan"] = plan.snapshot()
            elif last_plan is not None:
                # 计划已结束：仍要把最后一份终态快照交回去，F5 才能保留结论与次数
                payload["plan"] = last_plan
            if isinstance(payload.get("plan"), dict):
                # F5 权威快照携带接纳代次与请求 ID：界面据此建立归属，后到的
                # 旧计划事件不得覆盖恢复出来的计划（P1）
                boot_plan = payload["plan"]
                boot_plan["plan_generation"] = self._plan_generation
                boot_plan.setdefault("request_id",
                                     next((key for key, value in self._start_requests.items()
                                           if value.get("plan_id") == boot_plan.get("plan_id")), ""))
        return payload

    def help(self) -> dict:
        return {
            "title": "使用帮助",
            "subtitle": "使用说明、版本更新与本机数据",
            # 版本与最新版本入口：界面据此显示「版本 1.1.0」和 GitHub 外链按钮
            # （APP_VERSION 恒来自 read_version()，这里只是示例注释，不写死实现）
            "version": APP_VERSION,
            "latest_url": RELEASES_LATEST_URL,
            # 三步只讲「怎么操作」：选中不等于开始，开始按钮必须点，结果逐场看
            "steps": [
                {"no": 1, "title": "登录账号",
                 "text": "选择「登录」，在打开的浏览器中完成登录。下次启动会检查已保存的登录状态；"
                         "只有提示失效时才需要重新登录。"},
                {"no": 2, "title": "设置报名计划",
                 "text": "选择活动，设置尝试时长和最多提交次数，再选择「开始报名计划」。"
                         "预报名会在开放后尝试提交，已开放活动会在计划启动后尝试提交。"},
                {"no": 3, "title": "查看报名结果",
                 "text": "运行期间可最小化窗口，但需保持电脑唤醒与联网。结束后查看各活动的报名结果；"
                         "开始计划不代表报名成功。"},
            ],
            "note": "适用于天津市大学软件学院（天软）专升本学生使用的活动中心。"
                    "活动要求与报名结果以学校活动页为准。",
        }

    def front_ready(self) -> dict:
        # 仅排障：前端 boot 完成时打一声招呼，确认桥接真的通了
        self._trace("front_ready")
        return {"ok": True}

    def check_update(self, request_id: str) -> dict:
        """只登记轻量后台查询，不在桥接线程等待网络，也不改变报名门禁。"""
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_id):
            return {"ok": False, "queued": False, "text": "更新请求无效，请重试。"}
        with self._lock:
            if self._closing:
                return {"ok": False, "queued": False, "text": "程序正在关闭。"}
            if self._updating:
                return {"ok": True, "queued": True, "skipped": True,
                        "request_id": self._update_request_id}
            self._updating = True
            self._update_request_id = request_id
        try:
            threading.Thread(target=self._do_check_update, args=(request_id,), daemon=True).start()
        except Exception:
            with self._lock:
                self._updating = False
            return {"ok": False, "queued": False, "text": "检查没能启动，请重试。"}
        return {"ok": True, "queued": True, "request_id": request_id}

    def _do_check_update(self, request_id: str) -> None:
        result = {"state": "error", "current_version": APP_VERSION,
                  "latest_version": None, "url": RELEASES_LATEST_URL,
                  "text": "检查没能完成，请重试。", "error_code": "unexpected_error"}
        try:
            checked = check_latest_release(APP_VERSION)
            if isinstance(checked, dict):
                result = checked
        except Exception:
            # 最后兜底仍须解锁；普通更新错误不进入组件/登录错误通道。
            pass
        finally:
            result = dict(result, request_id=request_id)
            with self._lock:
                self._update_result = result
                self._updating = False
                closing = self._closing
            if not closing:
                self._emit("update", result)

    def check(self, request_id: str = "") -> dict:
        # js_api 的处理跑在 UI 线程上：一旦在这里跑 subprocess，
        # 窗口会直接"未响应"。所以只负责起线程，结果用事件推回去。
        with self._lock:
            if self._closing:
                # 关闭拒绝是取消语义：不给组件码，不再起线程
                return {"ok": False, "queued": False, "request_id": request_id,
                        "text": "程序正在关闭，已取消本次检查"}
            if self._checking:
                # 去重必须带上在途请求 ID：界面按 ID 对账，不能把旧轮当新轮
                answer = {"ok": True, "queued": True, "skipped": "已在检查登录态",
                          "request_id": self._check_request_id}
                last = self._check_last
                if last.get("payload") and last.get("request_id") == self._check_request_id:
                    # 原操作刚好已经结束：把终态直接带回去，界面不用干等
                    answer["result"] = last["payload"]
                return answer
            self._checking = True
            self._check_request_id = request_id or ""
        threading.Thread(target=self._do_check, daemon=True).start()
        return {"ok": True, "queued": True, "request_id": request_id}

    def list_activities(self, request_id: str = "") -> dict:
        with self._lock:
            if self._closing:
                return {"ok": False, "queued": False, "request_id": request_id,
                        "text": "程序正在关闭，已取消拉取活动列表"}
            if self._listing:
                answer = {"ok": True, "queued": True, "skipped": "已经在拉活动列表",
                          "request_id": self._list_request_id}
                last = self._list_last
                if last.get("payload") and last.get("request_id") == self._list_request_id:
                    answer["result"] = last["payload"]
                return answer
            self._listing = True
            self._list_request_id = request_id or ""
        threading.Thread(target=self._do_list, daemon=True).start()
        return {"ok": True, "queued": True, "request_id": request_id}

    def _do_check(self) -> None:
        try:
            self._do_check_inner()
        except Exception as exc:  # noqa: BLE001 - 兜底：结论构造中的意外也必须收尾
            # FB3：任何 check 终态必须先同步后端判定——结论构造阶段的意外不能
            # 让旧 valid 留作 start_plan 放行依据。先在锁内写 unknown/当前时刻
            # 并清旧组件结论，再锁外发唯一终态（状态写入先于事件）
            with self._lock:
                self._last_check = {"result": "unknown", "checked_at": time.time(),
                                    "runtime_error": ""}
            self._emit("check", {"ok": False, "result": "unknown",
                                 "text": "检查登录态没跑完，请点「刷新」再试一次"})
            self._emit("log", "[X] 检查收尾异常 %s: %s" % (type(exc).__name__, exc))
        finally:
            with self._lock:
                self._checking = False

    def _do_check_inner(self) -> None:
        if DEMO:
            # 演示模式完全离线：不读 session.json/storage_state、不起 worker、
            # 不碰学校。状态明确写「演示模式」，绝不伪装成真实登录有效（R20）
            self._last_check = {"result": "demo", "checked_at": time.time(),
                                "runtime_error": ""}
            self._emit("check", {
                "ok": True, "result": "demo", "demo": True,
                "text": "演示模式：使用内存样例，不检查真实登录",
                "hours_left": None, "session_present": None,
                "session_expired": False, "saved_at": None,
                "estimated_expires_at": None, "reason": "", "hint": "",
                "clock": {"source": "http_date", "state": "unknown",
                          "checked_at": None, "offset_s": None,
                          "uncertainty_s": None, "rtt_s": None,
                          "detail": "演示模式不做校时"},
            })
            return
        err_code = ""
        try:
            res = self._run(worker_command("check"), timeout=90)
            code, out = res["code"], res["out"]
        except _ClosingError:
            # 关闭取消：仍给恰一次终态，但属取消语义——不归类组件码、
            # 不触碰登录四态字段，技术细节没有需要落日志的内容
            self._emit("check", {"ok": False, "result": "unknown",
                                 "text": "程序正在关闭，本次检查已取消"})
            return
        except OSError as exc:
            # 进程创建失败（缺文件/权限/其他 OS 层）才归组件码：与登录四态分离，
            # 一律 unknown + runtime_error 附加字段——不清会话、不指导重登
            err_code = _component_error(exc)
            code = -1
            out = "[X] %s" % WORKER_PROBLEM_TEXT.get(err_code, "程序组件启动失败，请重新下载完整版本")
            # 技术细节只进运行记录（日志），主文案保持人话
            self._emit("log", "[X] 组件诊断 %s：%s: %s" % (err_code, type(exc).__name__, exc))
        except Exception as exc:
            # 数据/解析等普通异常：仍是检查失败终态，但不设 runtime_error、
            # 不诱导重新下载——数据坏了不是组件坏了
            code = -1
            out = "[X] 检查登录态没跑完，请点「刷新」再试一次"
            self._emit("log", "[X] 检查登录态异常 %s: %s" % (type(exc).__name__, exc))
        # 归因只信第一条 [X] 来源行：失败输出的末尾常常是「请重新登录」指引，
        # 拿最后一行分类会把网络错误误判成登录失效。末行仍作 text 供时钟展示。
        reason, text = _check_reason_line(out)
        # 三态字段跟结果一起发：check 失败可能是「没登录过」也可能是「过期」，
        # 界面只看 ok 会把首次使用误判成登录态失效。
        state = login_state(self._clock)
        result = _check_result(state, code, reason or text)
        if err_code:
            result = "unknown"
        self._last_check = {"result": result, "checked_at": time.time(),
                            "runtime_error": err_code}
        # 粗校时只作提示：没有样本时也给 unknown 字段，界面不会把上一轮的
        # warn 无限保留（R08）
        clock = _parse_clock_line(out) or {
            "source": "http_date", "checked_at": None, "state": "unknown",
            "offset_s": None, "uncertainty_s": None, "rtt_s": None,
            "detail": "本轮没有校时结论",
        }
        payload = {
            "ok": code == 0,
            "hours_left": state["hours_left"],
            "session_present": state["session_present"],
            "session_expired": state["session_expired"],
            # 当前凭证估算锚点（与 bootstrap.login 同一份 login_state 来源）：
            # 界面据此在重新登录后立即换新锚点；只作提醒，绝不参与放行/拦截
            "saved_at": state.get("saved_at"),
            "estimated_expires_at": state.get("estimated_expires_at"),
            "text": text,
            # 来源行（附加字段）：界面日志显示真实原因，而不是末尾的登录指引
            "reason": reason,
            # check 只回最后一行（R16）：给界面一个可区分的错误分类，
            # 免得把「没登录」和「断网」都笼统当失败。
            "hint": _check_hint(reason or text, code),
            # 四态结论（附加字段）：valid=接口确认有效 / expired=4001 失效 /
            # none=本机无会话 / unknown=网络或接口异常——异常不得误报成重新登录
            "result": result,
            # 本轮粗校时结论：ok/warn/unknown 都带，界面逐轮替换而不是累积
            "clock": clock,
        }
        if err_code:
            payload["runtime_error"] = err_code
        self._emit("check", payload)

    def _do_list(self) -> None:
        try:
            self._do_list_inner()
        except _ClosingError:
            # 关闭取消：恰一次失败终态收尾（骨架必能结束），但属取消语义，
            # 不归类组件码、不诱导重下/重检
            self._emit("activities", {"ok": False,
                                      "text": "程序正在关闭，活动列表加载已取消",
                                      "items": []})
        except OSError as exc:
            # 进程创建失败（缺文件/权限/其他 OS 层）才归组件码——此前任意
            # Exception 都会被归成 spawn_failed，数据异常被误报成「重新下载」
            err_code = _component_error(exc)
            self._emit("log", "[X] 组件诊断 %s：%s: %s" % (err_code, type(exc).__name__, exc))
            self._emit("activities", {"ok": False,
                                      "text": WORKER_PROBLEM_TEXT.get(err_code, "活动列表没能加载"),
                                      "items": [], "runtime_error": err_code})
        except Exception as exc:
            # 数据/解析等普通异常：仍必给恰一次失败终态（此前 FileNotFoundError
            # 从这里泄出导致骨架永远加载），但不是组件问题——不设 runtime_error、
            # 不诱导重新下载或重新登录
            self._emit("log", "[X] 列表请求异常 %s: %s" % (type(exc).__name__, exc))
            self._emit("activities", {"ok": False, "text": "活动列表没能加载", "items": []})
        finally:
            with self._lock:
                self._listing = False

    def _do_list_inner(self) -> None:
        if DEMO:
            items = demo_items()
            self._activities = {str(it["id"]): it for it in items}
            self._emit("activities", {"ok": True, "items": items})
            return
        res = self._run(worker_command("list", "--json"), timeout=120)
        if res["code"] != 0:
            self._emit("activities", {"ok": False,
                                      "text": res["out"].strip(), "items": []})
            return
        try:
            items = json.loads(res["out"].strip() or "[]")
        except json.JSONDecodeError:
            self._emit("activities", {"ok": False,
                                      "text": "活动列表解析失败", "items": []})
            return
        now = None
        try:
            now = self._clock_now()      # 列表「距开放」按业务北京时间
        except Exception:
            now = None                   # 校时不可用：显示未知，不用系统时间补
        for it in items:
            it["seconds_to_open"] = _seconds_until(it.get("sign_start"), now)
        self._activities = {str(it.get("id")): dict(it) for it in items if it.get("id")}
        self._emit("activities", {"ok": True, "items": items})

    # ---------- 动作 ----------
    def login(self) -> dict:
        if DEMO:
            # 演示模式不打开学校登录页、不写 session（R20）
            return {"ok": False, "demo": True,
                    "text": "演示模式不联网：这里不会打开学校登录页。"
                            "要看真实登录，请不带 --demo 启动。"}
        with self._lock:
            if self._closing:
                # 不带 error 码：前端不得把关闭取消当成组件问题挂横幅
                return {"ok": False, "text": "程序正在关闭，已取消本次登录"}
            if self._plan and self._plan.snapshot().get("status") in {"running", "ready"}:
                return {"ok": False, "text": "报名计划正在运行，先停止计划再登录"}
        return self._spawn(worker_command("login"), "登录", kind="login")

    def grab(self, activity_id: str, name: str, at: str = "",
             headed: bool = False) -> dict:
        args = worker_command("grab", "--activity-id", activity_id, "--name", name)
        if at:
            args += ["--at", at]
        if headed:
            args.append("--headed")
        return self._spawn(args, "等待并抢报名：%s" % name, kind="grab")

    START_KEEP = 32

    # 已完成计划的留档上限：重复终态幂等只看这段窗口，足够覆盖迟到回调
    FINISHED_PLAN_KEEP = 32

    def _prune_finished_plans_locked(self) -> None:
        """已完成计划登记只留最近有限条（调用方持锁）。"""
        if len(self._finished_plan_ids) <= self.FINISHED_PLAN_KEEP:
            return
        for plan_id in list(self._finished_plan_ids)[:-self.FINISHED_PLAN_KEEP]:
            self._finished_plan_ids.pop(plan_id, None)

    def _remember_start(self, request_id: str, state: str, **extra) -> None:
        """登记开始请求的结论（调用方持锁）。只保留最近 32 条，pending 不驱逐。"""
        record = dict(self._start_requests.get(request_id) or {})
        record.update({"state": state, "at": record.get("at") or time.time()})
        record.update(extra)
        self._start_requests[request_id] = record
        if request_id in self._start_order:
            self._start_order.remove(request_id)
        self._start_order.append(request_id)
        while len(self._start_order) > self.START_KEEP:
            victim = next((rid for rid in self._start_order
                           if self._start_requests.get(rid, {}).get("state") != "pending"), None)
            if victim is None:
                break
            self._start_order.remove(victim)
            self._start_requests.pop(victim, None)

    def _start_replay(self, record: dict, request_id: str) -> dict:
        """同一 request_id 重放：返回原结论，绝不另建计划。"""
        state = record.get("state")
        if state in {"accepted", "rejected"}:
            return dict(record.get("result") or {}, request_id=request_id, duplicate=True)
        if state == "canceled":
            return {"ok": False, "text": "这次开始已取消，可以重新发起。",
                    "request_id": request_id, "canceled": True}
        return {"ok": True, "queued": True, "pending": True, "request_id": request_id,
                "text": "正在确认是否启动，请稍候"}

    def start_request_state(self, request_id: str) -> dict:
        """只读对账：这次开始到底是 pending / accepted / rejected / canceled。"""
        rid = str(request_id or "")
        with self._lock:
            record = self._start_requests.get(rid)
            plan = self._plan
            # 代次只在该请求的计划仍是最近接纳计划时给出：对旧请求对账不得
            # 伪造成最新接纳代次（P1）
            generation = (self._plan_generation
                          if str(record.get("plan_id") or "") == self._latest_plan_id
                          else 0) if record else 0
        if not record:
            return {"ok": True, "state": "unknown", "request_id": rid,
                    "text": "没有这次开始的记录，可能程序重启过"}
        state = str(record.get("state") or "unknown")
        plan_id = str(record.get("plan_id") or "")
        payload = {"ok": True, "state": state, "plan_id": plan_id, "request_id": rid,
                   "plan_generation": generation}
        if state == "accepted":
            if plan is not None and plan.plan_id == plan_id:
                snapshot = plan.snapshot()
                payload["running"] = snapshot.get("status") in {"ready", "running"}
                payload["snapshot"] = snapshot
            else:
                # 计划已经收尾：返回这次请求自己的终态快照，不借别的较新计划
                payload["running"] = False
                payload["finished"] = True
                payload["snapshot"] = record.get("snapshot") or {
                    "status": record.get("final_status") or "unknown",
                    "summary": {}, "tasks": []}
            payload["status"] = (payload.get("snapshot") or {}).get("status")
        else:
            payload["text"] = str((record.get("result") or {}).get("text") or "")
        return payload

    def cancel_start_request(self, request_id: str) -> dict:
        """放弃一次还在 pending 的开始；已有结论的请求不能取消。"""
        rid = str(request_id or "")
        with self._lock:
            record = self._start_requests.get(rid)
            if not record:
                return {"ok": False, "text": "没有这次开始的记录"}
            if record.get("state") == "pending":
                record["state"] = "canceled"
                return {"ok": True, "state": "canceled", "request_id": rid}
            if record.get("state") == "accepted":
                # 已被接纳就不能再答复「取消成功」：给出真实 plan_id 让界面去停止
                return {"ok": False, "state": "accepted",
                        "plan_id": str(record.get("plan_id") or ""),
                        "request_id": rid,
                        "text": "这次开始已经被接纳，请用「停止计划」结束"}
            return {"ok": False, "state": record.get("state"),
                    "text": "这次开始已经有结论，不能再取消"}

    def start_plan(self, items: list[dict], headed: bool = False,
                   retry_policy: dict | None = None, request_id: str = "") -> dict:
        """校验并启动计划；带 request_id 时按进程内登记实现幂等（R11）。

        UI 的超时不能靠「清掉正在提交」来放行第二次开始——那可能真的重复报名。
        这里按 request_id 记住接纳/拒绝结论：同 ID 再来返回原结论，
        另提供只读对账与取消接纳，让界面能问清楚「到底启没启」。
        """
        rid = request_id if isinstance(request_id, str) else ""
        if rid and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", rid):
            return {"ok": False, "text": "开始请求标识无效，请重试。"}
        if rid:
            # 查重与占位必须在同一临界区：分成两段锁会让两个同 ID 请求
            # 都读到「不存在」，然后各自去建计划（C04）
            with self._lock:
                known = self._start_requests.get(rid)
                if known is not None:
                    replay = self._start_replay(known, rid)
                else:
                    self._remember_start(rid, "pending")
                    replay = None
            if replay is not None:
                return replay
        try:
            result = self._start_plan_inner(items, headed, retry_policy, rid)
        except Exception as exc:  # noqa: BLE001 - 本机异常也要留下真实结论
            with self._lock:
                record = self._start_requests.get(rid) or {}
                if rid and record.get("state") not in {"accepted", "canceled"}:
                    self._remember_start(rid, "rejected",
                                         result={"ok": False,
                                                 "text": "开始计划时本机异常：%s"
                                                         % type(exc).__name__})
            return {"ok": False, "text": "开始计划时本机异常：%s" % type(exc).__name__,
                    "request_id": rid}
        if rid:
            with self._lock:
                record = self._start_requests.get(rid) or {}
                state = record.get("state")
                if state == "canceled":
                    # 取消先赢：如实返回取消，不把结论覆盖成 accepted/rejected
                    result = {"ok": False, "text": "这次开始已取消，可以重新发起。",
                              "canceled": True}
                elif state == "accepted":
                    # inner 已在线性化点写过接纳结论，外层不得覆盖
                    result = dict(record.get("result") or result)
                else:
                    self._remember_start(
                        rid, "accepted" if result.get("ok") else "rejected",
                        plan_id=str(result.get("plan_id") or ""), result=dict(result))
        return dict(result, request_id=rid) if rid else result

    def _start_plan_inner(self, items: list[dict], headed: bool,
                          retry_policy: dict | None, request_id: str = "") -> dict:
        if not isinstance(items, list) or not items:
            return {"ok": False, "text": "先至少选择一场活动"}
        # 北京时间门禁（决策 012）：时钟不可用时不新启自动报名（demo 离线豁免）；
        # 计划接纳时刻只取一次业务 now——开始后的校时/鉴权/开页不后移 instant 窗口
        if not DEMO and not self._clock_ready():
            return {"ok": False,
                    "text": "北京时间还没有校准，暂不能开始自动报名；稍后点「重新校时」再试。"}
        accepted_at = self._clock_now()
        with self._lock:
            if self._closing:
                return {"ok": False, "text": "程序正在关闭，已取消开始计划"}
            if not DEMO and not self._clock_ready():
                return {"ok": False,
                        "text": "北京时间在开始前失去校准，这次没有启动；稍后重试。"}
            if self._is_busy_locked():
                return {"ok": False, "text": "已有登录、报名或计划在运行，先停止后再开始"}
        # 真实计划只在学校接口确认登录有效后开始（最终防线，界面按钮门禁之外）：
        # checking / unknown / none / expired 一律拒绝并给出下一步；demo 没有真实
        # 登录态，继续走演示流程不受此限。
        if not DEMO:
            problem = worker_problem()
            if problem:
                # 组件不齐的计划必然全场失败：这里就拒绝，不让用户等到点才发现；
                # demo 模式不起 worker，不做此检查
                return {"ok": False,
                        "text": "%s；计划没法开始" % WORKER_PROBLEM_TEXT[problem],
                        "error": problem}
            with self._lock:
                checking = self._checking
                last = dict(self._last_check)
            if checking:
                return {"ok": False, "text": "正在检查登录态，等这次检查完成再开始"}
            result = last.get("result") or "unknown"
            if result == "valid":
                pass                     # 学校接口确认有效：唯一放行形态
            elif result == "none":
                return {"ok": False, "text": "还没有登录：点右上角「登录」先登录一次"}
            elif result == "expired":
                return {"ok": False, "text": "登录态已失效：点右上角「重新登录」后再开始"}
            else:
                return {"ok": False,
                        "text": "暂时无法确认登录态：点「刷新」重试，确认登录有效后再开始"}

        def _policy_int(value, fallback: int, minimum: int = 0) -> int:
            try:
                number = int(value)
            except (TypeError, ValueError):
                return fallback
            return number if number >= minimum else fallback

        policy = retry_policy if isinstance(retry_policy, dict) else {}
        retry_for_s = _policy_int(policy.get("retry_for_s"), 120)
        max_attempts = _policy_int(policy.get("max_attempts"), 0)

        # 前端只提交「选了哪一场」：资格与时间一律以本进程缓存为准，
        # 陈旧前端快照绝不能覆盖更新的 signed/截止时间（R05）。
        normalized = []
        seen_ids: set = set()
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                return {"ok": False,
                        "text": "计划里有格式不对的活动项，请刷新列表后重新选择。",
                        "errors": [{"kind": "bad_item", "index": index}]}
            aid = str(item.get("id") or "").strip()
            if not aid:
                return {"ok": False,
                        "text": "计划里有缺少 id 的活动，请刷新列表后重新选择。",
                        "errors": [{"kind": "missing_id", "index": index}]}
            if aid in seen_ids:
                return {"ok": False, "text": "同一活动被选了两遍，去掉重复的那一条。",
                        "errors": [{"kind": "duplicate_id", "activity_id": aid}]}
            seen_ids.add(aid)
            cached = self._activities.get(aid)
            if not cached:
                # 列表还没加载过/被清空：没有权威快照就没有放行依据
                return {"ok": False,
                        "text": "列表里找不到这一场活动，请点「刷新」重新加载后再选。",
                        "errors": [{"kind": "unknown_activity", "activity_id": aid}]}
            changed = [key for key in ("sign_start", "sign_end", "act_start", "act_end")
                       if key in item and str(item.get(key) or "") != str(cached.get(key) or "")]
            if "signed" in item and bool(item.get("signed")) != bool(cached.get("signed")):
                changed.append("signed")
            if changed:
                # 不静默换成另一个时间开始：让用户刷新后重新确认
                return {"ok": False,
                        "text": "活动信息已变化，请刷新后重新确认。",
                        "errors": [{"kind": "activity_changed", "activity_id": aid,
                                    "name": cached.get("name"), "fields": changed}]}
            normalized.append(dict(cached))
        # 报名截止是硬门禁（前端复选/校验之外的后端最终防线）：
        # 已截止活动不能进入 worker；pre_register 与 instant_signup 是两个
        # 正式模式，混合计划允许共存，逐场按各自模式运行。
        closed = [item for item in normalized
                  if classify_signup_mode(str(item.get("sign_start") or ""),
                                          str(item.get("sign_end") or ""),
                                          accepted_at) == "closed"]
        if closed:
            names = "、".join("「%s」" % (item.get("name") or "活动") for item in closed[:3])
            more = " 等 %d 场" % len(closed) if len(closed) > 3 else ""
            return {"ok": False,
                    "text": "报名已截止：%s%s（报名截止 %s）不能加入计划，只能在列表里查看。"
                            % (names, more, closed[0].get("sign_end") or "时间未公布"),
                    "errors": [{"kind": "sign_end_passed", "activity_id": item.get("id"),
                                "name": item.get("name"), "sign_end": item.get("sign_end")}
                               for item in closed]}
        checked = validate_plan(normalized, now=accepted_at)
        if not checked["ok"]:
            return {"ok": False, "text": "计划里有不能同时报名的活动", "conflicts": checked["conflicts"],
                    "errors": checked["errors"]}

        plan_id = uuid.uuid4().hex[:12]
        specs = [TaskSpec.from_activity(item, "task-%02d-%s" % (i + 1, item["id"]), headed,
                                        retry_for_s=retry_for_s, max_attempts=max_attempts,
                                        now=accepted_at)
                 for i, item in enumerate(normalized)]

        # 接纳代次在下方最终接纳的同一临界区赋值；emit 闭包捕获自己的代次，
        # 事件与终态快照的归属都按代次判定，不以 _plan 槽为空推断（P1）
        accepted_generation = [0]

        def annotated(payload: dict) -> dict:
            """内部桥接事件统一携带 plan_id/plan_generation/request_id（P1）。"""
            data = dict(payload) if isinstance(payload, dict) else {}
            data.setdefault("plan_id", plan_id)
            data["plan_generation"] = accepted_generation[0]
            if "request_id" not in data:
                rid = next((key for key, value in self._start_requests.items()
                            if value.get("plan_id") == plan_id), "")
                if rid:
                    data["request_id"] = rid
            return data

        def emit(kind: str, payload: dict) -> None:
            if kind != "plan_done":
                # 旧计划迟到的进度事件不得广播到当前界面：事件只属于最近
                # 接纳的计划；历史计划的进度仍可按 request_id 只读对账（P1）
                with self._lock:
                    is_latest = self._latest_plan_id == plan_id
                    forward = annotated(payload) if is_latest else None
                if is_latest:
                    self._emit(kind, forward)
                return
            # 完成状态先提交、再发布：收到 plan_done 的同步回调（测试、未完成的
            # bootstrap、开始对账）必须当场就看到 _plan 已清空、终态快照与
            # request_id 记录已可读；先发事件再改状态会留下可观察的中间态（FR05）。
            with self._lock:
                owns_slot = self._plan is coordinator
                if owns_slot:
                    self._plan = None
                elif (self._plan is None
                      and self._latest_plan_id == plan_id):
                    # 关闭/停止已把当前计划清空：只有「这就是最近接纳的计划」
                    # 时才算它的收尾。槽为空≠归属——较新计划已结束后，旧计划
                    # 的迟到完成不得覆盖它的终态快照（P1）
                    owns_slot = True
                first_done = not self._finished_plan_ids.get(plan_id)
                try:
                    snapshot = coordinator.snapshot()
                except Exception:
                    snapshot = None
                if owns_slot and first_done:
                    # 终态快照只写一次：重复终态不得把已定结论改成另一状态；
                    # 旧计划迟到的 plan_done 不得覆盖后来计划或它的终态快照
                    self._finished_plan_ids[plan_id] = True
                    self._prune_finished_plans_locked()
                    self._last_plan = snapshot
                if first_done:
                    # 终态快照按 request_id 留档：ack 丢了也要能核实「已经结束」（C05）
                    rid = next((key for key, value in self._start_requests.items()
                                if value.get("plan_id") == plan_id), "")
                    if rid:
                        final_status = str(payload.get("status")
                                           or (snapshot or {}).get("status") or "")
                        self._remember_start(
                            rid, "accepted", plan_id=plan_id, finished=True,
                            final_status=final_status,
                            snapshot={"status": final_status,
                                      "summary": payload.get("summary") or {},
                                      "tasks": (snapshot or {}).get("tasks") or []})
                forward_done = self._latest_plan_id == plan_id
                forward_payload = annotated(payload) if forward_done else None
            # 先提交后发布（顺序保留，FR05）；旧计划的完成事件不广播（P1）
            if forward_done:
                self._emit("plan_done", forward_payload)

        def runner(spec: TaskSpec, cancel_event: threading.Event,
                   status: callable) -> dict:
            if DEMO:
                # demo 只演示任务状态，不创建 grab.py 子进程，也不访问学校站点。
                status("submitting", {"detail": "演示：正在提交"})
                for _ in range(4):
                    if cancel_event.is_set():
                        return {"status": "canceled", "reason": "用户取消了这一场"}
                    time.sleep(0.05)
                if spec.activity_id.endswith("career"):
                    return {"status": "failed", "code": 2, "reason": "演示：名额已满"}
                return {"status": "success", "code": 0, "reason": "演示：报名成功"}
            try:
                return self._run_plan_task(spec, cancel_event, status)
            except _SpawnFailure as exc:
                # 没拿到进程树所有权就不会有网络或浏览器动作：这一场如实记成
                # 「本机没能启动」，既不冒充学校拒绝，也不静默降级成无主进程。
                return {"status": "failed", "code": None,
                        "reason": "本场没能启动：%s" % exc,
                        "stop_reason": "spawn_failed", "attempts": 0}
            except _ClosingError:
                # 关闭拒绝落到协调器里就是取消：不冒充报名失败，也不让异常
                # 冲出 runner 污染任务状态（协调器业务不改）
                return {"status": "canceled", "code": None,
                        "reason": "程序正在关闭，本场次未启动"}

        coordinator = PlanCoordinator(plan_id, specs, runner, emit,
                                      now=self._clock_now)
        with self._lock:
            # FB1 最终接纳点：从开头的 busy 检查到这里隔着校验与构建，期间
            # 可能已有别的请求预留计划、或关闭已开始——同一临界区内重检并
            # 只允许一个请求预留唯一协调器，未接纳的请求不启动、不覆盖
            if self._closing:
                return {"ok": False, "text": "程序正在关闭，已取消开始计划"}
            if not DEMO and not self._clock_ready():
                return {"ok": False,
                        "text": "北京时间在开始前失去校准，这次没有启动；稍后重试。"}
            if self._is_busy_locked():
                return {"ok": False, "text": "已有登录、报名或计划在运行，先停止后再开始"}
            # 取消接纳与最终预留同一临界区：pending 期间点了取消，
            # 这里必须拒绝，绝不能先占位再补一个「已取消」的计划
            if request_id and self._start_requests.get(request_id, {}).get("state") == "canceled":
                return {"ok": False, "text": "这次开始已取消，可以重新发起。",
                        "canceled": True}
            self._plan = coordinator
            self._last_plan = None
            # 接纳代次与最近接纳计划在同一临界区赋值：此后所有事件/快照的
            # 归属判定都以这一线性化点为基准（P1）
            self._plan_generation += 1
            accepted_generation[0] = self._plan_generation
            self._latest_plan_id = plan_id
            if request_id:
                # 接纳与预留同一线性化点：此后取消只能回报真实 plan_id（C04）
                self._remember_start(request_id, "accepted", plan_id=plan_id,
                                     result={"ok": True, "queued": True,
                                             "plan_id": plan_id})
        try:
            # start 在 Api 锁外：协调器启动会 emit 回调（plan 事件要拿 _lock），
            # 锁内调用会双向等死
            coordinator.start()
        except Exception:
            # 启动失败只撤销本请求仍持有的预留，不碰别的计划，不残留 busy
            with self._lock:
                if self._plan is coordinator:
                    self._plan = None
                if request_id:
                    self._remember_start(request_id, "rejected", plan_id="",
                                         result={"ok": False,
                                                 "text": "计划没能开始，请再试一次"})
            return {"ok": False, "text": "计划没能开始，请再试一次"}
        return {"ok": True, "queued": True, "plan_id": plan_id,
                "plan_generation": accepted_generation[0]}

    def cancel_task(self, plan_id: str, task_id: str) -> dict:
        with self._lock:
            plan = self._plan
        if not plan or plan.plan_id != str(plan_id):
            return {"ok": False, "text": "计划已经结束"}
        ok = plan.cancel_task(str(task_id))
        return {"ok": ok, "text": "已请求取消这一场" if ok else "这一场已经结束"}

    def stop_plan(self, plan_id: str) -> dict:
        with self._lock:
            plan = self._plan
        if not plan or plan.plan_id != str(plan_id):
            return {"ok": False, "text": "当前没有这个计划"}
        # 幂等：重复停止同一个计划也返回 ok。顺序必须是「先置停协调器，
        # 再杀还活着的 worker」——反过来杀，等待中的批次可能在杀完之后
        # 又被拉起来。树终止不等 Playwright 自然超时，确认后立即生效。
        plan.stop()
        self._terminate_plan_procs()
        return {"ok": True, "text": "已停止计划"}

    def _terminate_plan_procs(self) -> None:
        """立刻按进程树终止计划里所有活着的 worker。"""
        with self._lock:
            procs = list(self._plan_procs.values())
        for proc in procs:
            _kill_tree(proc)

    def shutdown(self) -> None:
        """窗口正常关闭的进程收尾：先同锁置关闭屏障并取快照，再停协调器、树终止**仅本 Api 登记**的 worker。

        关闭屏障：_closing 与一切 worker 创建/登记共用 _lock——快照取到时，
        任何「已开始创建」的进程必然已登记完成（创建若还没进锁，随后会被
        _closing 拒绝），因此快照是完备的，收走不会漏；锁外调用 plan.stop 与
        树终止：协调器回调与任务线程的登记/清理都需要 _lock，锁内等待会互相
        等死。重复/并发 shutdown：后到调用在锁外等待首次收尾的完成 Event，
        返回即代表收尾真正完成——入口置位不算 done，主正常退出不会先于收尾
        完成继续 finalize。
        覆盖登录/单活动（_proc）、计划任务（_plan_procs）与 check/list（_aux_procs）；
        onefile 主程序退出后运行期资源根即消失，留下还握着它的孤儿 worker 只会
        拿着旧资源继续跑——必须先收干净再 finalize 生命周期。
        绝不按进程名扫杀：用户可能同时开着别的实例或别的程序，只动自己登记的进程。
        """
        with self._lock:
            if self._closing:
                # 已有收尾在进行：后到调用不重取快照、不重复 stop/kill，
                # 在锁外等同一个完成 Event——返回即代表收尾真正完成（FB2）
                done_event = self._shutdown_done_event
            else:
                done_event = None
                self._closing = True
                plan = self._plan
                plan_procs = list(self._plan_procs.values())
                aux_procs = list(self._aux_procs)
                proc = self._proc
        if done_event is not None:
            done_event.wait()
            return
        try:
            if plan:
                try:
                    plan.stop()
                except Exception:
                    pass   # 窗口已在关闭，收尾尽力而为，不让异常挡住生命周期写入
            for orphan in plan_procs + aux_procs:
                _kill_tree(orphan)
            if proc and proc.poll() is None:
                _kill_tree(proc)
        finally:
            # 完成信号在收尾 finally 发出：提前置位会让后到调用先于真正收尾返回
            self._shutdown_done_event.set()

    def stop(self) -> dict:
        with self._lock:
            plan = self._plan
        if plan:
            return self.stop_plan(plan.plan_id)
        with self._lock:
            proc = self._proc
        if proc and proc.poll() is None:
            _kill_tree(proc)
            self._emit("log", "已请求停止")
            return {"ok": True}
        return {"ok": False, "text": "当前没有在跑的任务"}

    def open_data(self) -> dict:
        path = ensure_data_dir()
        os.startfile(str(path))            # noqa: S606 - Windows 桌面工具
        return {"ok": True}

    def open_latest_release(self) -> dict:
        """用户点「GitHub 最新版本」时用系统默认浏览器打开固定的 releases/latest 页。

        只打开常量地址：不收任意 URL、不在 WebView 内替换工具页面、
        启动时也不自动访问 GitHub——版本检查是人的事，不是工具的网络行为。
        """
        try:
            os.startfile(RELEASES_LATEST_URL)   # noqa: S606 - 系统默认浏览器
            return {"ok": True, "url": RELEASES_LATEST_URL}
        except Exception as exc:  # noqa: BLE001 - 打不开也要给界面一个可读结论
            return {"ok": False, "text": "没能打开浏览器（%s）" % type(exc).__name__,
                    "url": RELEASES_LATEST_URL}

    def save_prefs(self, prefs: dict) -> dict:
        # 与生命周期心跳共用同一把锁的原子读-合并-写（update_prefs）
        if not update_prefs(prefs or {}):
            return {"ok": False, "error": "prefs_busy",
                    "text": "设置暂未保存，另一个窗口正在保存数据。"}
        return {"ok": True}

    # ---------- 内部 ----------
    def _is_busy_locked(self) -> bool:
        if self._proc and self._proc.poll() is None:
            return True
        if self._plan:
            status = self._plan.snapshot().get("status")
            if status in {"ready", "running"}:
                return True
        return any(proc.poll() is None for proc in self._plan_procs.values())

    def _is_busy(self) -> bool:
        with self._lock:
            return self._is_busy_locked()

    def _run_plan_task(self, spec: TaskSpec, cancel_event: threading.Event,
                       status: callable) -> dict:
        """运行一个单活动脚本；取消只终止对应进程，不影响同批其他任务。"""
        # 已开放的场次不能再把原开放时刻传给脚本：grab.py 的重试窗口
        # 是「at + 120 秒」，开放超过 2 分钟的 at 会让循环一次都不进、
        # 直接报「名额已满」。这里把过期的 at 换成现在，窗口从现在起算。
        at_text = spec.sign_start
        # 学校时间一律按 UTC+8 解析：本机时区不同也不能差 8 小时
        at_ts = school_epoch(spec.sign_start)
        # 是否「已开放」只由业务北京时间判断：错误 OS 日期不得把未来场次改写成现在
        try:
            business_now = self._clock_now()
        except ClockUnavailable:
            business_now = None
        if at_ts is not None and business_now is not None and at_ts <= business_now:
            # 已开放场次把 --at 换成「现在」只是让等待逻辑立即通过；
            # 窗口长度由下面冻结的 --retry-start 决定，不随这一刻后移
            at_text = school_time_text(epoch=business_now)
        elif at_ts is None and business_now is not None and not spec.sign_start:
            at_text = school_time_text(epoch=business_now)
        # 业务时刻读不到时保持原定开放：worker 会按自己的时钟门禁收成
        # clock_unavailable，绝不把系统日期当成北京时间
        args = worker_command("grab", "--activity-id", spec.activity_id, "--name", spec.name,
                              "--at", at_text,
                              "--lead", str(max(0, int(spec.lead_s or 0))),
                              "--retry-for", str(spec.retry_for_s),
                              "--max-attempts", str(spec.max_attempts))
        if spec.sign_end:
            args += ["--sign-end", spec.sign_end]
        # 计划接纳时冻结的重试起点：worker 解码/鉴权/校时/开页都消耗这段预算，
        # 慢启动不会把用户设的窗口整体后移（R04）。前端不提交这个锚点。
        if getattr(spec, "retry_started_at", 0):
            args += ["--retry-start", "%.3f" % float(spec.retry_started_at)]
        if spec.headed:
            args.append("--headed")
        with self._lock:
            # 关闭屏障：创建与登记同临界区——关闭先发生则零创建（取消语义）；
            # 创建已开始则 shutdown 必等登记完成才能取快照，收走不会漏。
            # 创建本身也在这里完成：worker 先挂起、归属 Job、再恢复，所以
            # 任何进度回调与日志都发生在「所有权已确定」之后（FR02）。
            if self._closing:
                raise _ClosingError("程序正在关闭，本场次未启动")
            tree = spawn_owned_worker(args, cwd=str(HERE), env=_env())
            proc = tree.proc
            self._plan_procs[spec.task_id] = proc
        if status and business_now is not None:
            # Popen 只是「父进程把 worker 拉起来了」，不是 worker 自己的入口时间：
            # run_started_at 必须等 [checkpoint] worker_started 到达再写（R10）。
            # 业务时刻读不到就不填，绝不拿系统时间补一个假节点。
            status("submitting", {"worker_spawned_at": business_now})
        self._emit("log", "▶ [%s] 等待并抢报名" % spec.name)

        # 父层监督：到期后收走本场进程树，不让「不 EOF 的管道」把任务永远挂着。
        # 所有权在进程恢复执行前就已取得（Job Object）：onefile 引导进程先退出时，
        # 解压出的真实 worker 仍属于这个 job，收走不依赖一个还活着的父 PID。
        supervised = {"fired": False}
        observed = {"attempts": 0}
        deadline = _task_deadline(spec)
        cancel_watch = threading.Event()
        lines: queue.Queue[str] = queue.Queue()
        reader_done = threading.Event()
        tail: list[str] = []
        canceled = False
        killed = False

        def read_output() -> None:
            try:
                for line in proc.stdout or []:
                    lines.put(line.rstrip("\r\n"))
            finally:
                reader_done.set()

        reader = threading.Thread(target=read_output, name="signup-output", daemon=True)
        reader.start()

        def _watchdog() -> None:
            """到「有效截止 + 清理余量」就收走本场树。

            等待用 Event 而不是 time.sleep：正常收尾/取消/关闭都在 finally
            置位，线程立刻退出，不把一个睡到截止的后台线程留在进程里。
            退出条件不看父进程是否还活着：父已退出、孙进程握着 stdout 时，
            仍然必须由这里结束监控。
            """
            if watch_delay is None:
                return
            if cancel_watch.wait(watch_delay):
                return
            supervised["fired"] = True
            tree.terminate()

        # 延迟只算一次，看门狗与主循环共用同一个上限：各算各的会在
        # 「截止已过」时出现主循环认为已超时、看门狗还在等 grace 的分叉
        # 监督上限按业务时刻换算成一次性延迟：重校向后纠正不得延长已接纳的时长。
        # 校时失效时退回「本场最长可能时长 + grace」的独立经过时长上限——
        # 只用于收走本场进程树，不重写树所有权，也不把系统时间当业务时刻。
        if deadline:
            if business_now is not None:
                watch_delay = max(0.0, deadline - business_now) + SUPERVISE_GRACE_S
            else:
                watch_delay = float(spec.retry_for_s or 120) + SUPERVISE_GRACE_S
        else:
            watch_delay = None
        watchdog = None
        if watch_delay is not None:
            watchdog = threading.Thread(target=_watchdog, name="signup-watchdog",
                                        daemon=True)
            watchdog.start()

        def feed(line: str) -> None:
            """输出行统一入口：落日志、存尾巴，并把源事件时间与遥测推成任务 detail。

            节点时间只认 worker 自己记录的 [checkpoint] 源时间：日志转发慢 2 秒，
            「页面已打开」也不会被记成晚 2 秒的接收时刻（R10）。没有结构化事件
            就不填，让界面老实显示「待打开/待发生」。
            """
            tail.append(line)
            del tail[:-40]
            self._emit("log", "[%s] %s" % (spec.name, line))
            checkpoint = _parse_checkpoint_line(line)
            if checkpoint and status:
                kind, at = checkpoint
                field = {"worker_started": "run_started_at",
                         "page_opened": "page_opened_at",
                         "watch_started": "watch_started_at"}[kind]
                status("submitting", {field: at})
            hit = _ATTEMPT_RE.match(line)
            if hit:
                observed["attempts"] = max(observed["attempts"], int(hit.group(1)))
                if status:
                    # 只数真正发起 signUp 的行；刷新 / is_joined 查询不会长这样
                    status("submitting", {"attempts": int(hit.group(1)),
                                          "last_reason": hit.group(2).strip()})
            parsed = _parse_retry_line(line)
            if parsed:
                # 结构化提交行也要计数：人话行可能被日志转发吞掉，次数不能
                # 因此归零；两路按「已承认的最大值」归并，不重复累加。
                raw_attempts = parsed.get("attempts")
                if raw_attempts not in (None, ""):
                    try:
                        observed["attempts"] = max(observed["attempts"], int(raw_attempts))
                    except (TypeError, ValueError):
                        pass
            if parsed and status:
                # 窗口确定/请求发起都是进度行：先把截止与次数推上去，
                # 不必等进程收尾；终态行才带 stop_reason，由收尾逻辑消费
                payload = {}
                if parsed.get("retry_deadline"):
                    payload["retry_deadline"] = str(parsed["retry_deadline"])
                if parsed.get("attempts") and not parsed.get("stop_reason"):
                    try:
                        payload["attempts"] = int(parsed["attempts"])
                    except (TypeError, ValueError):
                        pass
                if payload:
                    status("submitting", payload)

        code = None
        try:
            hard_stop = (time.monotonic() + watch_delay) if watch_delay is not None else None
            # 正常收尾要求 reader_done（输出读完）；取消与父层监督只要求
            # 进程树死透——onefile 引导进程被收走后，真实 worker 可能还握着
            # stdout 管道，再等 EOF 这条循环就永远出不去，plan_done 也发不出去。
            while True:
                if cancel_event.is_set() and not killed:
                    canceled = True
                    killed = True
                    tree.terminate()
                if proc.poll() is not None and (canceled or reader_done.is_set()):
                    break
                if supervised["fired"]:
                    # 监督已收走进程树：不再等 EOF，也不把管道当「还活着」的证据
                    break
                if hard_stop is not None and time.monotonic() >= hard_stop:
                    # 看门狗线程可能被调度延迟；主循环自己再守一次同一个上限
                    supervised["fired"] = True
                    tree.terminate()
                    break
                try:
                    while True:
                        line = lines.get_nowait()
                        if line:
                            feed(line)
                except queue.Empty:
                    pass
                time.sleep(0.05)
            # 收走之后把队列里剩下的行读完（有界）：最后一条终态不能丢
            reader_done.wait(1.5)
            while True:
                try:
                    line = lines.get_nowait()
                except queue.Empty:
                    break
                if line:
                    feed(line)
            try:
                code = proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                # 父进程不退出：树已终止还等不到就按已收走处理，绝不再等
                supervised["fired"] = True
                tree.terminate()
                try:
                    code = proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    code = -1
        finally:
            # 监控循环无论怎么退出（含异常）都要：撤销监督、收走本场树、
            # 清进程表、关管道。顺序是硬要求——先置取消、再有界 join 监督
            # 线程，最后才终止/释放 Job 句柄：否则迟到的 watchdog 可能拿着
            # 已关闭（甚至已被复用）的句柄去 TerminateJobObject（FR02）。
            cancel_watch.set()
            if watchdog is not None:
                watchdog.join(timeout=2.0)
            try:
                tree.terminate()
            except Exception:
                pass
            reader.join(timeout=2.0)     # 有界 join：不在另一线程持读锁时同步 close
            if reader_done.is_set() and proc.stdout:
                try:
                    proc.stdout.close()
                except Exception:
                    pass
            try:
                tree.close()
            except Exception:
                pass
            with self._lock:
                self._plan_procs.pop(spec.task_id, None)
        # 结构化终态行是策略信息的第一来源：只认带 stop_reason 的那一条，
        # 进度行（窗口确定/请求发起）不得冒充终态
        stop: dict = {}
        for line in reversed(tail):
            parsed = _parse_retry_line(line)
            if parsed and parsed.get("stop_reason"):
                stop = parsed
                break

        # 停止只终止本地执行：它撤不回已经发出的请求，也不能推翻已经收到的
        # 学校答复。所以「已获得的证据」优先于「本地怎么结束的」（FR03）——
        # 先认已确认结果，再认确定失败，最后才落到取消/待核对。
        attempts_seen = max(observed["attempts"], _attempts_of(stop))
        if stop.get("stop_reason") == "already_signed" \
                or any("你已经报过这个活动" in line for line in tail):
            return {"status": "already_signed", "code": code, "reason": "你已经报过这一场",
                    **_retry_payload(_with_attempts(stop, attempts_seen), spec,
                                     stop_reason="already_signed")}
        # 成功不要求退出码为 0：强杀会给出非 0，但杀之前的成功终态已经收到
        if stop.get("stop_reason") == "success" \
                or any("[OK] 报名成功" in line for line in tail):
            return {"status": "success", "code": code, "reason": "报名成功",
                    **_retry_payload(_with_attempts(stop, attempts_seen), spec,
                                     stop_reason="success")}
        if canceled:
            definite = str(stop.get("stop_reason") or "")
            if definite:
                # 取消晚到：worker 已经给出确定终态，按那个结论记，不降级成取消
                return {"status": "failed", "code": code,
                        "reason": "已按你的要求停止；停止前已收到确定结果",
                        "locally_stopped": True,
                        **_retry_payload(_with_attempts(stop, attempts_seen), spec,
                                         stop_reason=definite)}
            if attempts_seen > 0:
                # 本地确实停了，但已发出的提交没有确定回应：两层事实都要保留，
                # 次数不能归零，也不能说成「没抢到」（FR03）。
                return {"status": "failed", "code": code,
                        "reason": "已按你的要求停止；最后一次提交没有拿到确定回应",
                        "locally_stopped": True,
                        **_retry_payload(dict(stop, attempts=attempts_seen), spec,
                                         stop_reason="unconfirmed")}
            return {"status": "canceled", "code": code, "reason": "用户取消了这一场",
                    **_retry_payload(stop, spec, stop_reason="user_stopped")}
        reason = ""
        for line in reversed(tail):
            if line.startswith("[X] "):
                reason = line[4:].strip()
                break
        if supervised["fired"] and not stop.get("stop_reason"):
            # 父层收走时没有终态行：保留已观察到的提交次数；
            # 发过请求但没结论的，必须说「未确认」，不能冒充学校明确失败
            stop = {"attempts": attempts_seen,
                    "stop_reason": "unconfirmed" if attempts_seen else ""}
            if not attempts_seen:
                binding = "sign_end" if spec.sign_end and _task_deadline(spec) == school_epoch(spec.sign_end) else "retry_duration"
                stop_reason = binding
            else:
                stop_reason = "unconfirmed"
            return {"status": "failed", "code": code,
                    "reason": "本机监督到期，已收走本场进程",
                    **_retry_payload(stop, spec, stop_reason=stop_reason)}
        stop_reason = stop.get("stop_reason") or (
            "login_invalid" if code == 3 and "登录失效" in "".join(tail) else "process_error")
        return {"status": "failed", "code": code,
                "reason": reason or "退出码 %s" % code,
                **_retry_payload(stop, spec, stop_reason=stop_reason)}

    def _trace(self, msg: str) -> None:
        """调试用：只有设了 TJISE_TRACE 才落盘，平时零副作用。"""
        path = os.environ.get("TJISE_TRACE")
        if not path:
            return
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(str(msg) + "\n")
        except Exception:
            pass

    def _run(self, args: list[str], timeout: int) -> dict:
        """同步跑一个短命 worker（check/list）：创建、登记与关闭屏障同锁。

        Popen 在锁内：关闭先发生则拒绝创建（_ClosingError，取消语义）；
        创建已开始则 shutdown 必等登记完成才能取快照，不会留下未登记孤儿。
        OSError（缺文件/权限）原样抛给调用方归类成结构化组件错误。
        """
        with self._lock:
            if self._closing:
                raise _ClosingError("程序正在关闭，已取消本次 worker 调用")
            proc = subprocess.Popen(
                args, cwd=str(HERE), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                env=_env(), creationflags=CREATE_NO_WINDOW)
            self._aux_procs.add(proc)
        try:
            try:
                out, err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                # 超时同样按进程树终止：onefile 引导进程死后真实 worker 可能还活着
                _kill_tree(proc)
                try:
                    proc.communicate(timeout=5)
                except Exception:
                    pass
                return {"code": -1, "out": "[X] 执行超时（%d 秒）" % timeout}
        finally:
            with self._lock:
                self._aux_procs.discard(proc)
        return {"code": proc.returncode, "out": (out or "") + (err or "")}

    def _spawn(self, args: list[str], label: str, kind: str = "grab") -> dict:
        problem = worker_problem()
        if problem:
            # 组件不齐：同步给结构化失败——不占 busy 位、不写 _proc 占位
            self._emit("log", "[X] 组件诊断 %s：组件路径 %s" % (problem, GRAB))
            return {"ok": False, "text": WORKER_PROBLEM_TEXT[problem], "error": problem}
        try:
            with self._lock:
                if self._closing:
                    # 关闭拒绝不带 error 码：不是组件损坏，前端不得挂横幅
                    return {"ok": False, "text": "程序正在关闭，已取消本次「%s」" % label}
                if self._is_busy_locked():
                    return {"ok": False, "text": "已经有一个任务在跑，先等它结束"}
                self._proc = subprocess.Popen(
                    args, cwd=str(HERE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                    env=_env(), creationflags=CREATE_NO_WINDOW)
                proc = self._proc
        except OSError as exc:
            # Popen 竞态失败（文件在自检后被删/被锁）：同样给结构化结论，不冒充成功
            err_code = _component_error(exc)
            self._emit("log", "[X] 组件诊断 %s：%s: %s" % (err_code, type(exc).__name__, exc))
            return {"ok": False, "text": WORKER_PROBLEM_TEXT[err_code], "error": err_code}

        threading.Thread(target=self._pump, args=(proc, label, kind),
                         daemon=True).start()
        return {"ok": True}

    def _anchor_login_success(self) -> None:
        """新登录真实成功后建立 v2 锚点；没有可信时钟就不写（不拿系统时间冒充）。

        只在本次 login worker 成功路径调用：check/refresh/bootstrap 都不续期，
        也不改 session/storage_state 结构，旧有效凭证不要求重登。
        """
        try:
            lower, upper = self._clock.bounds()
            token = str(json.loads(SESSION_FILE.read_text(encoding="utf-8"))
                        .get("token") or "").strip()
        except Exception:
            return
        if not token or not (upper >= lower):
            return
        save_session_timing_anchor(token, (lower + upper) / 2.0,
                                   (upper - lower) / 2.0)

    def _pump(self, proc: subprocess.Popen, label: str, kind: str = "grab") -> None:
        self._emit("log", "▶ " + label)
        tail: list[str] = []
        for line in proc.stdout or []:
            line = line.rstrip("\r\n")
            if not line:
                continue
            tail.append(line)
            del tail[:-40]
            self._emit("log", line)
        proc.wait()
        if kind == "login":
            # 登录只看退出码：grab.py login 成功时不会打印「报名成功」
            ok = proc.returncode == 0
            if ok:
                # 只有本次登录真实成功才建立 v2 锚点：check/refresh 不续期
                self._anchor_login_success()
        else:
            ok = proc.returncode == 0 and any("报名成功" in t for t in tail)
        reason = ""
        if not ok:
            for t in reversed(tail):
                if t.startswith("[X] "):
                    reason = t[4:].strip()
                    break
            reason = reason or ("退出码 %s" % proc.returncode)
        self._emit("done", {"ok": ok, "code": proc.returncode,
                            "reason": reason, "label": label, "kind": kind})
        with self._lock:
            if self._proc is proc:
                self._proc = None


def _seconds_until(ts: str | None, now: "float | None") -> int | None:
    """学校时间文本距现在多少秒；非法/缺失/时钟未知都返回 None（不猜）。"""
    if not ts or now is None:
        return None
    epoch = school_epoch(ts)
    return None if epoch is None else int(epoch - now)


def _kill_tree(proc: subprocess.Popen) -> bool:
    """按进程树终止 worker，返回是否已确认终止。

    只 terminate 直接子进程的话，PyInstaller onefile 的引导进程死后，
    解压出的真实 worker 会带着浏览器继续按重试节奏跑——就是
    「停止不了、还在无限重试」的那个形态。收尸（wait）交给调用方
    的监控循环，这里只负责让它死透。

    注意：本函数只在父进程还活着时有效；父进程已退出时调用方必须用
    :class:`_ProcessTree`（Job Object）收走它留下的后代。返回值是
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


# ---------------------------------------------------------------- 进程树所有权
# Windows Job Object：进程一创建就挂进去，之后它派生的一切（onefile 解压出
# 的真实 worker、Playwright node、chromium）都自动属于这个 job。父进程先退出
# 也不会让后代脱管——TerminateJobObject 只认 job 句柄，不认父 PID。
#
# 关键顺序：CREATE_SUSPENDED 建进程 → 建 Job → 归属 → ResumeThread。
# 先跑起来再 Assign 存在真实窗口：worker 在这几毫秒里就能派生 Playwright
# 子进程，那些后代不会自动进入 job，父进程先退出时照样脱管。
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


class _SpawnFailure(RuntimeError):
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


def _win_kernel32():
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


def _close_handle(handle) -> None:
    """关闭自有句柄；0/None 直接跳过，失败不抛（清理路径不制造新异常）。"""
    if not handle:
        return
    try:
        _win_kernel32().CloseHandle(handle)
    except Exception:
        pass


def _open_primary_thread(pid: int):
    """打开刚创建的挂起进程的主线程。

    挂起进程此刻只有一个线程，所以「按拥有者 PID 找到的第一个线程」
    就是主线程。找不到或打不开返回 None，由调用方按启动失败处理。
    """
    kernel32 = _win_kernel32()
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
        _close_handle(snapshot)


def _create_job_for(proc: subprocess.Popen):
    """建一个 KILL_ON_JOB_CLOSE 的 Job 并把进程挂进去；失败返回 None。

    返回 None 由调用方升级成 _SpawnFailure：不再有「没拿到所有权也继续跑」
    的降级路径。失败时自己关掉已建的 job 与进程句柄，不留泄漏。
    """
    if os.name != "nt":
        return None
    kernel32 = _win_kernel32()
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
            _close_handle(job)
            return None
        handle = kernel32.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _PROCESS_QUERY_INFORMATION,
            False, proc.pid)
        if not handle:
            _close_handle(job)
            return None
        if not kernel32.AssignProcessToJobObject(job, handle):
            _close_handle(job)
            return None
        return job
    except Exception:
        _close_handle(job)
        return None
    finally:
        _close_handle(handle)


class _ProcessTree:
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
                    if not _win_kernel32().TerminateJobObject(self._job, 1):
                        ok = False
                except Exception:
                    ok = False
            if self.proc.poll() is None:
                if not _kill_tree(self.proc):
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
            _close_handle(self._job)
            self._job = None


def spawn_owned_worker(args: list[str], *, cwd: str, env: dict) -> _ProcessTree:
    """创建报名 worker：先挂起，取得进程树所有权后再让它跑。

    任何一步（挂起创建 / 建 Job / 归属 / 恢复线程）失败，都先把仍挂起的
    自有进程结束掉、关掉所有自有句柄，再抛 :class:`_SpawnFailure`；
    调用方据此把这一场记成「本机没能启动」，不产生任何网络或浏览器动作。
    """
    if os.name != "nt":
        raise _SpawnFailure("非 Windows 环境无法取得进程树所有权")
    proc = subprocess.Popen(
        args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        env=env, creationflags=CREATE_NO_WINDOW | _CREATE_SUSPENDED)
    job = None
    try:
        thread = _open_primary_thread(proc.pid)
        if not thread:
            raise _SpawnFailure("找不到刚创建进程的主线程")
        try:
            job = _create_job_for(proc)
            if not job:
                raise _SpawnFailure("worker 没能加入进程树（Job 创建或归属失败）")
            if _win_kernel32().ResumeThread(thread) == _RESUME_FAILED:
                raise _SpawnFailure("worker 主线程没能恢复执行")
        finally:
            _close_handle(thread)
    except BaseException:
        # 失败路径：结束仍挂起的自有进程（此刻它还没有任何后代），
        # 管道与句柄一并关闭，绝不留下半个 worker。
        _close_handle(job)
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
    return _ProcessTree(proc, job)


SUPERVISE_GRACE_S = 5.0


def _task_deadline(spec) -> float | None:
    """父层视角的本场绝对截止（与 worker 的预算同一公式）。

    worker 自己也会算，但管道卡死、进程不退出时只有父进程能收走它；
    这里只用于监督兜底，不改变 worker 的调度。
    """
    start = float(getattr(spec, "retry_started_at", 0) or 0)
    if start <= 0:
        return None
    end = school_epoch(spec.sign_end) if getattr(spec, "sign_end", "") else None
    retry_for = int(getattr(spec, "retry_for_s", 120) or 0)
    if retry_for > 0:
        deadline = start + retry_for
        return min(deadline, end) if end else deadline
    if end:
        return end
    return start + 120.0


def _parse_checkpoint_line(line: str) -> tuple[str, float] | None:
    """解析 worker 的 [checkpoint] JSON 行，返回 (kind, 源事件 epoch 秒)。

    kind 只认三个真实发生过的事件；坏 JSON、未知 kind 都返回 None，
    调用方保持已有 checkpoint 不变，绝不用接收时刻补一个假节点。
    """
    text = str(line or "")
    marker = "[checkpoint] "
    index = text.find(marker)
    if index < 0:
        return None
    try:
        data = json.loads(text[index + len(marker):].strip())
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    kind = str(data.get("kind") or "")
    if kind not in {"worker_started", "page_opened", "watch_started"}:
        return None
    try:
        at = float(data.get("at"))
    except (TypeError, ValueError):
        return None
    return (kind, at) if at > 0 else None


def _parse_clock_line(out: str) -> dict | None:
    """取输出里最后一条 [clock] 结构化粗校时结论；没有则 None。

    每轮 check 都要带结论（ok/warn/unknown），界面才能用新状态替换旧告警；
    只取最后一行会让「本轮没有采样」被误当成「上一轮仍然有效」。
    """
    found = None
    for line in str(out or "").splitlines():
        marker = "[clock] "
        index = line.find(marker)
        if index < 0:
            continue
        try:
            data = json.loads(line[index + len(marker):].strip())
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and data.get("source") == "http_date":
            found = data
    return found


def _parse_retry_line(line: str) -> dict | None:
    """解析 worker 的结构化终态行：[retry] attempts=N stop_reason=X retry_deadline=<epoch> sign_end=<epoch> last_reason=<原文>。

    时间字段是 epoch 秒（无空格）；last_reason 恒为最后一个字段（可含空格）。
    解析失败返回 None，调用方按「无策略信息」处理，不影响既有退出码语义。
    """
    text = str(line or "").strip()
    prefix = "[retry] "
    if not text.startswith(prefix):
        return None
    head, _, reason = text[len(prefix):].partition(" last_reason=")
    fields: dict = {}
    for pair in head.split(" "):
        key, _, value = pair.partition("=")
        if key:
            fields[key] = value
    if reason:
        fields["last_reason"] = reason
    return fields or None


def _attempts_of(stop: dict) -> int:
    """从结构化终态行里读「已承认的提交次数」；非法值按 0。"""
    try:
        return max(0, int((stop or {}).get("attempts", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _with_attempts(stop: dict, attempts: int) -> dict:
    """终态载荷统一写「两路归并后的次数」，不让某一路缺失把次数清零。"""
    merged = dict(stop or {})
    merged["attempts"] = max(attempts, _attempts_of(stop))
    return merged


def _retry_payload(stop: dict, spec: "TaskSpec", stop_reason: str) -> dict:
    """把 [retry] 行整理成任务终态附带的 telemetry 字段（缺省值兜底）。"""

    def epoch_or_blank(value) -> str:
        """epoch 秒文本；0/非法表示学校没给，落空串交给活动自身字段兜底。"""
        try:
            number = int(value or 0)
        except (TypeError, ValueError):
            return ""
        return str(number) if number > 0 else ""

    try:
        attempts = int((stop or {}).get("attempts", 0) or 0)
    except (TypeError, ValueError):
        attempts = 0
    payload = {
        "stop_reason": (stop or {}).get("stop_reason") or stop_reason,
        "attempts": attempts,
        "retry_deadline": epoch_or_blank((stop or {}).get("retry_deadline")),
        "sign_end": epoch_or_blank((stop or {}).get("sign_end")) or spec.sign_end,
    }
    if (stop or {}).get("last_reason"):
        payload["last_reason"] = stop["last_reason"]
    return payload


# worker 每次真正发起 signUp 时打印的行（带 [i] 前缀落日志）；
# 只匹配这个形状，刷新 / is_joined 查询不会撞上
_ATTEMPT_RE = re.compile(r"^(?:\[i\]\s*)?第 (\d+) 次提交[：:]\s*(.*)$")


def _check_reason_line(out: str) -> tuple[str, str]:
    """从 check 的完整输出提取 (来源行, 末行)。

    归因只信第一条 [X] 来源行——失败输出的末尾固定是「请重新登录」指引，
    拿末行分类会把网络错误误判成登录失效；末行仍返回给界面做时钟偏差展示。
    结构化 [clock]/[checkpoint] 行不是人话，既不当来源也不当末行。
    空输出返回 ("", "")，调用方按「认不出的失败」落 unknown。
    """
    lines = [ln.strip() for ln in str(out or "").strip().splitlines() if ln.strip()]
    lines = [ln for ln in lines if "[clock] " not in ln and "[checkpoint] " not in ln]
    reason = next((ln for ln in lines if ln.startswith("[X] ")), "")
    return reason, (lines[-1] if lines else "")


def _check_result(state: dict, code: int, text: str) -> str:
    """check 的四态结论：valid / expired / unknown / none（附加字段，旧 UI 可忽略）。

    只有学校接口真实成功才是 valid；本机没有会话是 none；能认出的登录失效
    （4001/登录类字样，且不含网络词）是 expired；网络类与认不出的失败一律
    unknown——网络异常不能被误报成「必须重新登录」。
    """
    if code == 0:
        return "valid"
    if not state.get("session_present"):
        return "none"
    hint = _check_hint(text, code)
    if hint == "network":
        return "unknown"
    if hint == "login":
        return "expired"
    return "unknown"


def _check_hint(text: str, code: int) -> str:
    """把 check 的来源行粗分类：network / login / error（成功返回空）。

    成功输出本来就常含「登录」字样（如「登录态还有 17 小时」），
    必须先按退出码认成功。归因顺序网络词优先：身份验证服务的超时、
    连接失败是网络问题而不是登录失效（「身份验证服务超时」不得误归
    login）；网络词都不命中时，4001/「登录」/「身份验证失败」才算登录类；
    其余含糊文本落 error（上游统一归 unknown，宁可暂不确认也不误导重登）。
    """
    raw = str(text or "")
    if code == 0:
        return ""
    if any(word in raw for word in ("网络", "连接", "超时", "URLError", "HTTPError", "timed out", "Timeout")):
        return "network"
    if "4001" in raw or "登录" in raw or "身份验证失败" in raw:
        return "login"
    return "error"


def _runtime_check_main(argv: list[str]) -> int:
    """离线自检 CLI（--runtime-check --report <绝对JSON路径>）。

    在 GUI/数据根/生命周期**之前**分发：只验证「冻结程序能按真实解析路径启动
    内置 worker --help 并正常退出」，不访问学校、不读凭证、不初始化统一数据根。
    缺 TJISE_DATA_DIR 直接拒绝——worker 自身启动会建数据根，验收必须显式指定
    临时目录，绝不摸到真实登录态。窗口版没有控制台：结论只经 JSON 文件与
    进程退出码表达（成功 0 / 失败 1 / 报告写不出去 2）。
    """
    report = ""
    for i, part in enumerate(argv):
        if part == "--report" and i + 1 < len(argv):
            report = argv[i + 1]
    result = {
        "version": APP_VERSION,
        "frozen": bool(getattr(sys, "frozen", False)),
        "worker": str(GRAB),
        "ok": False,
        "exit_code": None,
    }
    if not report:
        print("[X] runtime-check 缺少 --report <绝对JSON路径>", file=sys.stderr)
        return 2
    try:
        report_path = Path(report)
        if not report_path.is_absolute():
            raise ValueError("--report 需要绝对路径（窗口版没有可依赖的 cwd）")
        if not os.environ.get("TJISE_DATA_DIR", "").strip():
            raise ValueError("缺少 TJISE_DATA_DIR：离线自检必须显式指定临时数据根，"
                             "拒绝触碰真实登录态")
        problem = worker_problem()
        if problem:
            result["component"] = problem
            raise RuntimeError(WORKER_PROBLEM_TEXT[problem])
        proc = subprocess.Popen(
            worker_command("--help"), cwd=str(HERE),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env=_env(), creationflags=CREATE_NO_WINDOW)
        try:
            proc.communicate(timeout=RUNTIME_CHECK_TIMEOUT_S)
            result["exit_code"] = proc.returncode
            result["ok"] = proc.returncode == 0
            if not result["ok"]:
                result["error"] = "worker --help 退出码 %s" % proc.returncode
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            result["error"] = "worker --help 超时（%d 秒）" % RUNTIME_CHECK_TIMEOUT_S
    except Exception as exc:  # noqa: BLE001 - 结论一律落 JSON，不向上抛
        result.setdefault("error", str(exc))
    try:
        rp = Path(report)
        rp.parent.mkdir(parents=True, exist_ok=True)
        tmp = rp.with_name(rp.name + ".tmp")
        tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, rp)
    except Exception as exc:
        print("[X] runtime-check 报告写不出去：%s" % exc, file=sys.stderr)
        return 2
    return 0 if result["ok"] else 1


def main() -> int:
    # --runtime-check 先于一切副作用分发：不建数据根、不起生命周期、不开窗口
    if "--runtime-check" in sys.argv:
        return _runtime_check_main(sys.argv)
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    # 启动即确保统一数据根可用；冻结进程首次启动会顺带迁移旧 EXE 旁数据
    ensure_data_dir()
    global DEMO
    if "--demo" in sys.argv:
        DEMO = True
    if not UI.exists():
        print("找不到界面文件：%s" % UI, file=sys.stderr)
        return 1
    # 进程级生命周期：冻结上一运行周期并开始心跳（F5 重载不重置；
    # 懒创建兜底——bootstrap 先被调用时也能拿到同一实例）
    # 独立北京时间（决策 012）：主程序唯一 BusinessClock 生命周期；
    # 后台同步不阻塞 UI，Api import 时不联网；demo 完全离线（标「演示时间」）
    clock = BusinessClock(background=not DEMO)
    configure_time_source(clock.now)
    try:
        snap = clock.snapshot()
        if snap.get("owner_id"):
            _CLOCK_ENV["path"] = clock._snapshot_path()
            _CLOCK_ENV["owner"] = str(snap.get("owner_id"))
    except Exception:
        pass
    lifecycle = get_lifecycle(now_fn=clock.now)
    api = Api(clock=clock)
    api._start_clock_monitor()
    window = webview.create_window(
        "天软活动预报名工具", str(UI),
        js_api=api, width=1120, height=800, min_size=(980, 700),
        background_color="#E9EEF6",
    )
    api._window = window
    webview.start(debug=False)
    # 窗口已关（正常路径）：先收本实例登记的 worker（计划/登录/单活动/check/list）——
    # onefile 退出后运行期资源根即消失，绝不能留下还握着它的孤儿进程；
    # 再停心跳并落关闭时刻，clean/abnormal 的分界不变
    api.shutdown()
    lifecycle.finalize()
    clock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
