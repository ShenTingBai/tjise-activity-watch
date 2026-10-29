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
import updater

# 登录态/偏好统一数据根：开发用 local/data，冻结 EXE 用 %LOCALAPPDATA%
# （TJISE_DATA_DIR 可覆盖），与 grab-worker 解析出同一份登录文件
from runtime_paths import DATA_DIR, SESSION_FILE, UI_STATE_FILE, ensure_data_dir

# 分层依赖（只准向下）：app（interface 外观）→ application → domain 纯规则；
# 外部副作用统一走 infrastructure 适配器。旧模块级名字保留为兼容包装/再导出，
# 测试与调用方按原名注入、patch 的语义不变。
from domain import demo_data as _demo_data
from domain import lifecycle_rules as _lifecycle_rules
from domain import login_rules as _login_rules
from domain import plan_rules as _plan_rules
from domain import retry_rules as _retry_rules
from domain import time_text as _time_text
from domain import worker_lines as _worker_lines
from domain.errors import ClosingError as _ClosingError
from infrastructure import lifecycle_store as _lifecycle_store
from infrastructure import prefs_store as _prefs_store
from infrastructure import process_tree as _process_tree
from application import login_service as _login_service
from application import plan_service as _plan_service
from application import update_service as _update_service

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

# 使用条款与隐私告知的当前版本：与 docs/TERMS_AND_PRIVACY.md 顶部的版本字段
# 是同一份（契约测试会核对两处一致）。条款内容更新时同步改这两处，
# 界面据此判断是否需要重新弹出条款确认。
#
# 版本形如「日期」或「日期-rN」：本轮修订修正了许可边界、通信披露和
# 「版本变化必须再次点击同意」的接受语义，旧版本（2026-10-27）的同意
# 记录不能静默覆盖新内容，所以提升到 -r1 而不是沿用同一天。
TERMS_VERSION = "2026-10-27-r2"


def terms_unknown_payload() -> dict:
    """条款状态不可知时的统一拒绝载荷（界面据此提示重试，不猜）。"""
    return {"ok": False, "error": "terms_required",
            "text": "还没确认当前版本的使用条款；点「查看条款」阅读后再继续。"}


def terms_state(prefs: dict) -> dict:
    """条款同意快照（纯函数，供契约测试隔离求值）。

    只读 ui.json 偏好里的 terms_version / terms_accepted_at 两个键：不碰
    任何登录文件，也不把同意状态写进运行日志。bootstrap 把这份快照
    带给界面，界面按「已同意版本 != 当前版本」判定门禁。
    """
    return {
        "version": TERMS_VERSION,
        "accepted_version": str((prefs or {}).get("terms_version") or ""),
        "accepted_at": str((prefs or {}).get("terms_accepted_at") or ""),
    }


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

    实现已下沉 domain.login_rules（纯分类）；此处保留旧名字兼容包装。
    """
    return _login_rules.component_error(exc)


# 关闭屏障语义类型在 domain.errors；旧名字保留为再导出（同 class 对象）。
# 调用方必须把它与组件异常分开处理——关闭拒绝不得归类为 worker_* 组件码，
# 也不得冒充报名失败向上抛。

# --demo：用假数据起界面，方便调样式、截图，不需要登录
DEMO = False


def demo_items() -> list[dict]:
    """演示样例集：纯数据模型在 domain.demo_data，时间与格式化在此注入。"""
    return _demo_data.demo_items(time.time(), school_time_text)


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


# 偏好存储原语已下沉 infrastructure.prefs_store；旧名字保留为再导出，
# 测试与调用方继续用 app.load_prefs_from / app._atomic_write_prefs 等注入。
load_prefs_from = _prefs_store.load_from
_atomic_write_prefs = _prefs_store.atomic_write
_PrefsLockTimeout = _prefs_store.PrefsLockTimeout
_acquire_prefs_file_lock = _prefs_store.acquire_file_lock
_release_prefs_file_lock = _prefs_store.release_file_lock
_prefs_transaction = _prefs_store.prefs_transaction


def load_prefs() -> dict:
    return load_prefs_from(PREFS)


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
    """本机内部凭证摘要（纯规则在 domain.login_rules）：只用于把 v2 锚点和具体凭证绑定。"""
    return _login_rules.credential_id(token)


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
    now = None
    if clock is not None:
        try:
            now = clock.now()
        except Exception:
            now = None
    return _login_rules.hours_left_from_anchor(timing, token, now, TOKEN_LIFETIME_H)


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
# 否则心跳的整份快照会覆盖用户刚存的筛选/引导状态（反之亦然）。
# 锁与事务原语在 infrastructure.prefs_store；这里保留旧常量名供注入/patch。
_PREFS_LOCK = _prefs_store.PREFS_LOCK
_PREFS_LOCK_TIMEOUT_S = _prefs_store.LOCK_TIMEOUT_S   # 生命周期元数据：拿不到就放弃本次写入
_PREFS_SAVE_TIMEOUT_S = _prefs_store.SAVE_TIMEOUT_S   # 用户偏好：给正在收尾的另一个进程留出时间


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

    「没有登录」= session.json 不存在、无法解析或缺少 token；只有 token 还在但
    已过期才叫登录态失效。缺 token 时 hours_left 必须是 None——按 saved_at 报
    一个假的小时数会把空壳会话显示成有效。saved_at / estimated_expires_at 只是
    估算提醒，登录有效性永远以学校真实校验为准，二者绝不参与放行或拦截。
    分类规则在 domain.login_rules（纯函数）；这里只负责读 session.json 并
    保持旧名兼容（测试按 app.SESSION_FILE 注入）。
    """
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        token = str(data.get("token") or "").strip()
        saved_text = str(data.get("saved_at") or "").strip()
    except Exception:
        return _login_rules.build_session_state("", "", None, None)
    if not token:
        return _login_rules.build_session_state("", "", None, None)
    # 剩余时间只信 v2 锚点：旧 saved_at 是本机墙钟文本，宁可显示「暂无法估算」
    hours_left = token_hours_left(clock)
    now = None
    if hours_left is not None and clock is not None:
        try:
            now = clock.now()
        except Exception:
            now = None
    return _login_rules.build_session_state(token, saved_text, hours_left, now)


# 时间文本换算已下沉 domain.time_text；旧名字保留为兼容包装（学校时间换算
# 在此注入 time_contract.school_epoch，domain/application 不直连混合模块；
# 墙钟读取也留在此界面层，domain 只做纯格式化）。
_parse_local_time = _time_text.parse_local_time
_latest_moment = _time_text.latest_moment


def _now_text() -> str:
    """本地墙钟文本（兼容名；实现在 domain.time_text.format_local_text，取时在此）。"""
    return _time_text.format_local_text(datetime.now())


def _seconds_until(ts: str | None, now: "float | None") -> "int | None":
    """学校时间文本距现在多少秒（实现在 domain.time_text，换算函数在此注入）。"""
    return _time_text.seconds_until(ts, now, school_epoch)


def _task_deadline(spec) -> "float | None":
    """父层视角的本场绝对截止（实现在 domain.time_text，换算函数在此注入）。"""
    return _time_text.task_deadline(spec, school_epoch)

# 生命周期纯记账已下沉 domain.lifecycle_rules；旧名字保留为再导出。
compute_lifecycle = _lifecycle_rules.compute_lifecycle
compute_lifecycle_v2 = _lifecycle_rules.compute_lifecycle_v2


# 生命周期持久化已下沉 infrastructure.lifecycle_store；旧名字保留为再导出。
AppLifecycle = _lifecycle_store.AppLifecycle





_LIFECYCLE: "AppLifecycle | None" = None
_LIFECYCLE_LOCK = threading.Lock()


def get_lifecycle(now_fn=None) -> AppLifecycle:
    """进程级单例（懒创建）：main() 启动时先建，bootstrap 重复取同一实例。"""
    global _LIFECYCLE
    with _LIFECYCLE_LOCK:
        if _LIFECYCLE is None:
            # 偏好路径从 app 外观注入：测试 patch app.PREFS 后单例仍写隔离文件
            _LIFECYCLE = AppLifecycle(prefs_file=PREFS, now_fn=now_fn)
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
        # _updating 表示「任一更新动作在途」（检查或下载），单飞共用一个槽位；
        # _update_installer 是检查阶段选好的安装器资产（URL/大小/哈希），
        # 下载只信它，不收界面传来的地址；_update_installer_path 是校验通过后的本地产物。
        self._updating = False
        self._update_request_id = ""
        self._update_result: dict | None = None
        self._update_installer: dict | None = None
        self._update_installer_path = ""
        self._update_cancelled = False
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

    # ---------- 条款授权（学校动作的最终防线） ----------
    def _terms_gate(self, request_id: str = "") -> "dict | None":
        """学校联网动作的统一条款门禁；返回 None 才放行。

        界面按钮只是第一道：脚本、快捷键、事件重放都能绕过按钮，所以真实
        判定放在后端。只读 ui.json 的 terms_version / terms_accepted_at，
        不读登录文件、不写运行日志；演示模式（DEMO）完全离线，不受此限。

        未同意、版本不匹配、偏好读不出（load_prefs 兜底返回空）都按
        **未授权**处理：不启动线程、worker、学校请求或计划协调器。
        """
        if DEMO:
            return None
        state = terms_state(load_prefs())
        if state["accepted_version"] == TERMS_VERSION:
            return None
        payload = dict(terms_unknown_payload(), terms=state)
        if request_id:
            # 带上原 request_id：界面按 ID 对账，不把拒绝当成新一轮在途
            payload["request_id"] = request_id
        return payload

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
            # 条款同意快照：界面据此决定是否弹条款、是否放行学校网络动作；
            # 同意状态本身只存 ui.json（terms_version / terms_accepted_at）
            "terms": terms_state(prefs),
            "business_clock": self._clock_payload(),
            "help": self.help(),
            "login": login_info,
            # 生命周期元数据（上一周期关闭时长/质量 + 登录估算）：只作提示，
            # 不参与任何放行/拦截；F5 重载重复调用拿到的是同一份进程级快照
            "lifecycle": lifecycle_info,
            # demo 标记：界面据此放行演示计划（真实模式仍要求登录校验 valid）
            "demo": DEMO,
            # 三种重试模式的权威选项与默认值：桌面/手机共用这一份口径，
            # 不各维护第二套清单；缺截止的 0 模式回退秒数也在这里声明
            "retry_options": {
                "default": int(_retry_rules.DEFAULT_RETRY_SECONDS),
                "fallback_seconds": int(_retry_rules.DEFAULT_RETRY_SECONDS),
                "adaptive_cap_seconds": int(_retry_rules.ADAPTIVE_SECONDS),
                "choices": [
                    {"value": 120, "label": "2 分钟"},
                    {"value": 600, "label": "10 分钟"},
                    {"value": 1800, "label": "30 分钟"},
                    {"value": 3600, "label": "1 小时"},
                    {"value": int(_retry_rules.ADAPTIVE_RETRY),
                     "label": "自动延长（最多25分钟）"},
                    {"value": 0, "label": "直到报名截止"},
                ],
            },
        }
        # busy 与计划快照必须在同一「判定点」取：先取协调器快照（锁外），
        # 再回 Api 锁内确认这份快照仍属于当前计划并重新计算 busy。这样既不会
        # 出现「busy=true 但 plan 是上一份终态快照」，也不在持 Api 锁时取
        # 协调器锁（P2-1 锁序）。
        snapshot, busy = self._bootstrap_plan_snapshot()
        with self._lock:
            payload["busy"] = busy
            payload["update_check"] = {"checking": self._updating,
                                       "request_id": self._update_request_id,
                                       "result": self._update_result,
                                       "installer_path": self._update_installer_path}
            generation = self._plan_generation
        if snapshot is not None:
            # 页面重载会丢掉 plan/task 事件：把计划快照一并交给 bootstrap，
            # 界面才能恢复任务列表和「停止」按钮；计划本身不会重复提交。
            # 复制一份再补归属字段，不改写共享的 _last_plan。
            boot_plan = dict(snapshot)
            boot_plan["plan_generation"] = generation
            boot_plan.setdefault("request_id",
                                 self._find_request_id(boot_plan.get("plan_id")))
            payload["plan"] = boot_plan
        return payload

    def _find_request_id(self, plan_id) -> str:
        """按 plan_id 反查开始请求 ID（只读，不加锁时调用者需保证一致性）。"""
        target = str(plan_id or "")
        if not target:
            return ""
        with self._lock:
            return next((key for key, value in self._start_requests.items()
                         if value.get("plan_id") == target), "")

    def _bootstrap_plan_snapshot(self) -> tuple["dict | None", bool]:
        """锁外取计划快照，再锁内确认归属；返回 (快照, busy)。

        取快照期间计划可能被替换或结束：重读最多三次，最终以 Api 锁内的
        当前计划/终态快照为准，不返回已过期计划。
        """
        for _ in range(3):
            with self._lock:
                plan = self._plan
            if plan is None:
                with self._lock:
                    return self._last_plan, self._is_busy_locked()
            try:
                snapshot = plan.snapshot()
            except Exception:
                snapshot = None
            with self._lock:
                # 只有「这份快照仍属于当前计划」时才连 busy 一起返回：
                # 否则重新读，避免把旧计划的运行态配上新计划的忙态
                if self._plan is plan:
                    return snapshot, self._is_busy_locked()
        with self._lock:
            return self._last_plan, self._is_busy_locked()

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
        # 用例在 application.update_service；fetch 端口在此注入（测试按
        # app.check_latest_release 替换）。状态字段留在 Api 供界面重载对账。
        _update_service.run_check(self, request_id, fetch=check_latest_release,
                                  current_version=APP_VERSION,
                                  latest_url=RELEASES_LATEST_URL)

    def _publish_update(self, payload: dict, finished: bool) -> None:
        """把更新状态推给界面并留档；finished 才释放单飞槽位。"""
        with self._lock:
            self._update_result = payload
            if finished:
                self._updating = False
            closing = self._closing
        if not closing:
            self._emit("update", payload)

    def update_download(self, request_id: str) -> dict:
        """用户确认后启动安装器下载：单飞后台流式下载并校验，事件回推进度与结论。"""
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_id):
            return {"ok": False, "queued": False, "text": "更新请求无效，请重试。"}
        with self._lock:
            if self._closing:
                return {"ok": False, "queued": False, "text": "程序正在关闭。"}
            if self._updating:
                return {"ok": True, "queued": True, "skipped": True,
                        "request_id": self._update_request_id}
            installer = self._update_installer
            if not isinstance(installer, dict) or not installer.get("url"):
                return {"ok": False, "queued": False,
                        "text": "当前没有可下载的安装包，请前往下载页。"}
            self._updating = True
            self._update_request_id = request_id
            self._update_cancelled = False
        try:
            threading.Thread(target=self._do_update_download,
                             args=(request_id, installer), daemon=True).start()
        except Exception:
            with self._lock:
                self._updating = False
            return {"ok": False, "queued": False, "text": "下载没能启动，请重试。"}
        return {"ok": True, "queued": True, "request_id": request_id}

    def _do_update_download(self, request_id: str, installer: dict) -> None:
        # 用例在 application.update_service；download/publish 端口在此注入
        # （updater 模块属性即替身缝）。进度/结论都经 _publish_update 推给界面。
        _update_service.run_download(
            self, request_id, installer,
            download=updater.download_installer, publish=self._publish_update,
            current_version=APP_VERSION, latest_url=RELEASES_LATEST_URL)

    def update_cancel(self) -> dict:
        """用户取消下载：只置取消标记，下载线程在下一块之间停下并清理半成品。"""
        with self._lock:
            self._update_cancelled = True
        return {"ok": True}

    def update_launch(self, request_id: str) -> dict:
        """用户确认后启动已校验的安装器；启动前先回推 launching 状态。

        关闭主程序由调用方控制：这里不退出、不自动重启，失败不改会话和偏好。
        """
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_id):
            return {"ok": False, "text": "更新请求无效，请重试。"}
        with self._lock:
            if self._closing:
                return {"ok": False, "text": "程序正在关闭。"}
            path = self._update_installer_path
            base = dict(self._update_result or {})
        if not path:
            return {"ok": False, "text": "还没有已校验的安装包，请先下载。"}
        # 安装器启动前先回推状态：界面能看到「正在启动安装程序…」
        self._publish_update(dict(base, request_id=request_id, state="launching",
                                  text="正在启动安装程序…", installer_path=path,
                                  error_code=""), False)
        got = updater.launch_installer(path)
        if got.get("ok"):
            text = "安装程序已启动。请先关闭本工具，再按向导完成安装。"
            self._publish_update(dict(base, request_id=request_id, state="launching",
                                      text=text, installer_path=path, error_code=""), False)
            return {"ok": True, "path": path, "text": text}
        text = str(got.get("text") or "安装程序没能启动，请重试。")
        self._publish_update(dict(base, request_id=request_id, state="failed",
                                  text=text, installer_path=path,
                                  error_code=str(got.get("error_code") or "launch_failed")), False)
        return {"ok": False, "text": text}


    def check(self, request_id: str = "") -> dict:
        # js_api 的处理跑在 UI 线程上：一旦在这里跑 subprocess，
        # 窗口会直接"未响应"。所以只负责起线程，结果用事件推回去。
        # 条款门禁：登录态校验要访问学校接口，未获授权就不起线程（后端最终防线）
        gate = self._terms_gate(request_id)
        if gate is not None:
            return dict(gate, queued=False)
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
        # 条款门禁同上：活动列表来自学校接口，未获授权不拉列表
        gate = self._terms_gate(request_id)
        if gate is not None:
            return dict(gate, queued=False)
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
        # 事件载荷整理在 application.login_service（分类纯规则在 domain）：
        # 归因只信第一条 [X] 来源行；三态字段跟结果一起发，粗校时逐轮替换（R08）。
        state = login_state(self._clock)
        result, clock, payload = _login_service.build_check_event(
            state, code, out, err_code)
        self._last_check = {"result": result, "checked_at": time.time(),
                            "runtime_error": err_code}
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

    # ---------- 报名结果回写 ----------
    # 学校列表短暂滞后是常态：报名成功后立即刷新仍可能拿到 signed=false。
    # 已确认成功在这段窗口内不被撤销；超过窗口仍为 false 时以学校列表为最终权威
    # （本地不能长期伪造已报名，否则会挡住用户真正需要重报的场次）。
    SIGNED_SYNC_WINDOW_S = 120.0

    def _confirm_signed(self, activity_id: str, status: str) -> bool:
        """worker 终态为 success/already_signed 时原子回写活动缓存。

        unconfirmed/failed/canceled/auth_unavailable 一律不写：未知结果不能被
        猜成成功，也不能因为本地停止就撤销学校已确认的事实。
        """
        aid = str(activity_id or "")
        if not aid or status not in {"success", "already_signed"}:
            return False
        try:
            confirmed_at = float(self._clock_now())
        except Exception:
            # 校时不可用时也保留事实，只是同步窗口改由「无截止」处理（见合并）
            confirmed_at = 0.0
        with self._lock:
            item = self._activities.get(aid)
            if not item:
                return False
            item = dict(item)
            item["signed"] = True
            item["signed_source"] = ("plan_success" if status == "success"
                                     else "plan_already_signed")
            item["signed_confirmed_at"] = confirmed_at
            item["sync_pending"] = False
            self._activities[aid] = item
            return True

    def _merge_list_activities(self, items: list[dict]) -> list[dict]:
        """列表刷新结果与本地已确认成功合并（纯策略在 domain.plan_rules）。

        学校说已报名 → 权威；本地已确认成功但学校还没同步 → 窗口内保留
        signed=true 并标 sync_pending，让界面继续把这场留在「已报名」而不可再选。
        """
        try:
            now = float(self._clock_now())
        except Exception:
            now = None            # 没有可信现在：保留本地确认，不按系统时间算窗口
        return _plan_rules.merge_list_activities(
            items, self._activities, now, self.SIGNED_SYNC_WINDOW_S)

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
        # 报名成功后学校列表可能短暂滞后：合并本地已确认成功，再下发界面和缓存。
        # 合并只读本地缓存、不联网，也不改写学校返回的其它字段。
        items = self._merge_list_activities(items)
        for it in items:
            it["seconds_to_open"] = _seconds_until(it.get("sign_start"), now)
        with self._lock:
            self._activities = {str(it.get("id")): dict(it) for it in items if it.get("id")}
        # 详情补充是逐活动异步完成的：把「还在同步/已失败」如实交给界面，
        # 不让整张列表因为某一场详情查不到而失败。
        pending = sum(1 for it in items if it.get("attendance_state") == "pending")
        failed = sum(1 for it in items if it.get("attendance_state") == "unknown")
        self._emit("activities", {"ok": True, "items": items,
                                  "attendance": {"pending": pending, "unknown": failed}})

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
            if self._plan and self._plan.published_status() in {"running", "ready"}:
                return {"ok": False, "text": "报名计划正在运行，先停止计划再登录"}
        # 条款门禁：登录要打开学校登录页并写回会话，未获授权不起 worker
        gate = self._terms_gate()
        if gate is not None:
            return gate
        return self._spawn(worker_command("login"), "登录", kind="login")

    def grab(self, activity_id: str, name: str, at: str = "",
             headed: bool = False) -> dict:
        if DEMO:
            # 演示模式绝不起真实 worker：grab 是学校动作，demo 只给离线结论
            # （与计划协调器里的 demo runner 同一口径，不绕过条款门禁）
            return {"ok": False, "demo": True,
                    "text": "演示模式不联网：这里不会真正提交报名。"}
        # 条款门禁：单场报名直接打学校接口，未获授权不起 worker
        gate = self._terms_gate()
        if gate is not None:
            return gate
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
        # 条款门禁放在「幂等重放之后、真正建计划之前」：已接纳/已拒绝的
        # 结论照旧重放，新的请求未获授权则立即把本次登记收成 rejected，
        # 既不留下悬空 pending，也不启动计划协调器与任何 worker。
        gate = self._terms_gate()
        if gate is not None:
            rejected = dict(gate)
            if rid:
                rejected["request_id"] = rid
                with self._lock:
                    record = self._start_requests.get(rid) or {}
                    if record.get("state") == "pending":
                        self._remember_start(rid, "rejected", result=rejected)
            return rejected
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

        policy = retry_policy if isinstance(retry_policy, dict) else {}
        # 三种模式：正数=硬时长，0=直到截止，-1=自动延长（最多25分钟）；
        # 其他负值/非法值回 120，不猜用户意图
        retry_for_s = _retry_rules.policy_int(policy.get("retry_for_s"), 120,
                                              minimum=_retry_rules.ADAPTIVE_RETRY)
        max_attempts = _retry_rules.policy_int(policy.get("max_attempts"), 0)

        # 计划项归一化/门禁的纯策略在 domain.plan_rules：前端只提交「选了哪
        # 一场」，资格与时间一律以本进程缓存为准（R05）；不合格整单拒绝。
        normalized, error = _plan_rules.normalize_plan_items(
            items, self._activities, accepted_at)
        if error is not None:
            return error
        # 模式分类/冲突校验的纯函数在此注入 signup_plan（domain 不直连）
        error = _plan_rules.closed_items_error(
            normalized, accepted_at, classify_signup_mode)
        if error is not None:
            return error
        # 已报名活动占用的举办时段也是冲突基线：学校列表 signed=true 的全量活动
        # 与本次选择按 act_start/act_end 比较。缓存读取加锁：worker 线程可能正在
        # 回写报名结果（dict 迭代中变更会抛 RuntimeError）。
        with self._lock:
            signed_baseline = [dict(item) for item in self._activities.values()
                               if item.get("signed")]
        checked = _plan_rules.check_conflicts(
            normalized, accepted_at, signed_baseline, validate_plan)
        if not checked["ok"]:
            return checked

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
            # 报名结果先回写活动缓存，再转发事件：界面收到 task/plan_done 时，
            # 后端立场（下一次开始会不会重新接纳这场）已经同步更新。
            if kind == "task" and isinstance(payload, dict):
                status = str(payload.get("status") or "")
                if status in {"success", "already_signed"}:
                    self._confirm_signed(str(payload.get("activity_id") or ""), status)
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
            # 快照在 Api 锁外取：协调器锁只在它自己内部持有，反向边不再成环（P2-1）。
            # plan_done 发出时所有任务已终态，这里的快照就是终态快照。
            try:
                snapshot = coordinator.snapshot()
            except Exception:
                snapshot = None
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
        """打开数据目录，并把**真实路径**回给界面。

        只回 {"ok": true} 是不够的：用户最常问的就是「数据到底在哪」，
        窗口一关就又不知道了。路径必须能被看见、能被复制，所以原样回传
        实际解析出来的目录（TJISE_DATA_DIR 覆盖过的话也如实反映）。
        """
        path = ensure_data_dir()
        os.startfile(str(path))            # noqa: S606 - Windows 桌面工具
        return {"ok": True, "path": str(path)}

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

    def accept_terms(self, document_version: str = "") -> dict:
        """同意使用条款：先核验界面实际读到的文档版本，再经 ui.json 原子偏好事务落盘。

        同意必须绑定「用户读到的那份全文」：document_version 为空、旧版、
        未知新版或类型非法，都说明界面副本与程序要求不同源（混合资源或旧
        HTML），此时零写入直接拒绝——否则界面提示不匹配却已留下当前版同意，
        刷新后会放行。只写 terms_version / terms_accepted_at 两个键——不读写
        任何登录文件，不把同意状态写进运行日志；拿不到跨进程锁时如实返回
        失败，让界面留在条款弹窗里重试。
        """
        # 版本判定必须在 time.strftime 与 update_prefs 之前：拒绝路径零副作用
        if not (isinstance(document_version, str)
                and document_version == TERMS_VERSION):
            return {
                "ok": False,
                "error": "terms_version_mismatch",
                "text": "界面显示的条款版本与程序要求的版本不一致，请重新下载完整版本后再确认。",
                "terms": terms_state(load_prefs()),
            }
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        if not update_prefs({"terms_version": TERMS_VERSION,
                             "terms_accepted_at": stamp}):
            return {"ok": False, "error": "prefs_busy",
                    "text": "条款同意状态暂未保存，另一个窗口正在保存数据；请再点一次。"}
        return {"ok": True, "terms": terms_state(load_prefs())}

    # ---------- 内部 ----------
    def _is_busy_locked(self) -> bool:
        # 只读协调器已发布的不可变状态：持 Api 锁时绝不取协调器锁（P2-1）。
        # 反向路径（协调器 → Api）同样已改为锁外发布，两条边不再成环。
        if self._proc and self._proc.poll() is None:
            return True
        if self._plan:
            if self._plan.published_status() in {"ready", "running"}:
                return True
        return any(proc.poll() is None for proc in self._plan_procs.values())

    def _is_busy(self) -> bool:
        with self._lock:
            return self._is_busy_locked()

    def _run_plan_task(self, spec: TaskSpec, cancel_event: threading.Event,
                       status: callable) -> dict:
        """运行一个单活动脚本；取消只终止对应进程，不影响同批其他任务。

        用例实现在 application.plan_service；worker 命令、环境、进程树创建、
        监督余量、学校时间换算与时钟异常类型都从这里以端口注入（裸名解析
        保持测试按 app.X 替换的语义）。
        """
        return _plan_service.run_plan_task(
            spec, cancel_event, status,
            emit=self._emit, lock=self._lock, plan_procs=self._plan_procs,
            clock_now=self._clock_now, is_closing=lambda: self._closing,
            worker_command=worker_command, env=_env, spawn=spawn_owned_worker,
            cwd=str(HERE), grace_s=SUPERVISE_GRACE_S,
            epoch_of=school_epoch, time_text_of=school_time_text,
            clock_unavailable=ClockUnavailable)

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
        # done 结论整理在 application.login_service：login 只看退出码，
        # grab 还要求见过「报名成功」行；失败原因取第一条 [X] 来源行。
        ok, reason = _login_service.done_result(kind, proc.returncode, tail)
        if kind == "login" and ok:
            # 只有本次登录真实成功才建立 v2 锚点：check/refresh 不续期，
            # 失败/取消路径根本不走这里，旧锚点与旧会话原样保留。
            # 锚点写入失败（偏好锁超时/磁盘只读）不能吞掉登录结论：
            # 否则 done 永远不来，界面就永远停在「登录中」。
            try:
                self._anchor_login_success()
            except Exception as exc:  # noqa: BLE001 - 兜底上报，不中断收口
                self._emit("log", "[!] 登录成功，但会话时间锚点没有写成：%s" % exc)
        self._emit("done", {"ok": ok, "code": proc.returncode,
                            "reason": reason, "label": label, "kind": kind})
        with self._lock:
            if self._proc is proc:
                self._proc = None


# ---- 兼容包装：已下沉 domain / infrastructure 的纯规则与进程树适配器 ----
# 实现见 domain.time_text / domain.retry_rules / domain.login_rules /
# domain.worker_lines / infrastructure.process_tree；旧名字保留供测试与
# 内部调用按原名注入、patch。
_seconds_until = _time_text.seconds_until
_task_deadline = _time_text.task_deadline
_kill_tree = _process_tree.kill_tree
_SpawnFailure = _process_tree.SpawnFailure
_ProcessTree = _process_tree.ProcessTree
spawn_owned_worker = _process_tree.spawn_owned_worker
_win_kernel32 = _process_tree.win_kernel32
_parse_checkpoint_line = _worker_lines.parse_checkpoint_line
_parse_clock_line = _worker_lines.parse_clock_line
_attempts_of = _retry_rules.attempts_of
_with_attempts = _retry_rules.with_attempts
_retry_payload = _retry_rules.retry_payload
_ATTEMPT_RE = _retry_rules.ATTEMPT_RE


def _parse_retry_line(line: str) -> "dict | None":
    """解析 worker 的 [retry] 结构化终态行（实现在 domain.retry_rules）。"""
    return _retry_rules.parse_retry_line(line)


def _check_reason_line(out: str) -> tuple[str, str]:
    """从 check 的完整输出提取 (来源行, 末行)（实现在 domain.login_rules）。"""
    return _login_rules.check_reason_line(out)


def _check_result(state: dict, code: int, text: str) -> str:
    """check 的四态结论（实现在 domain.login_rules）。"""
    return _login_rules.check_result(state, code, text)


def _check_hint(text: str, code: int) -> str:
    """把 check 的来源行粗分类：network / login / error（实现在 domain.login_rules）。"""
    return _login_rules.check_hint(text, code)








SUPERVISE_GRACE_S = 5.0








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
