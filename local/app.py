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

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import webview

from signup_plan import PlanCoordinator, TaskSpec, classify_signup_mode, validate_plan

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
        start = datetime.fromtimestamp(now + s_off).strftime("%Y-%m-%d %H:%M:%S")
        end = datetime.fromtimestamp(now + e_off).strftime("%Y-%m-%d %H:%M:%S")
        # 举办时间未公布时保持空串，和 grab.py list --json 的缺失形态一致
        act_start = "" if act_s_off is None else datetime.fromtimestamp(now + act_s_off).strftime("%Y-%m-%d %H:%M:%S")
        act_end = "" if act_e_off is None else datetime.fromtimestamp(now + act_e_off).strftime("%Y-%m-%d %H:%M:%S")
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


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # 把主程序解析好的数据根传给 worker：即使 worker 所在目录不同，
    # 两侧也会读写同一份 session/storage_state
    env["TJISE_DATA_DIR"] = str(DATA_DIR)
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


# 界面偏好与生命周期心跳共用的持久化锁：读-合并-写必须串行，
# 否则心跳的整份快照会覆盖用户刚存的筛选/引导状态（反之亦然）
_PREFS_LOCK = threading.Lock()


def update_prefs(patch: dict) -> None:
    """统一的原子读-合并-写（Api.save_prefs 与生命周期心跳共用同一把锁）。"""
    with _PREFS_LOCK:
        prefs = load_prefs()
        prefs.update(patch or {})
        _atomic_write_prefs(PREFS, prefs)


def token_hours_left() -> float | None:
    """按 session.json 的 saved_at 估算剩余时间（足够给界面显示，不参与判断）。"""
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        saved = datetime.strptime(data["saved_at"], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None
    used = (datetime.now() - saved).total_seconds() / 3600.0
    return max(0.0, TOKEN_LIFETIME_H - used)


def login_state() -> dict:
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
        state["estimated_expires_at"] = saved.timestamp() + TOKEN_LIFETIME_H * 3600.0
    state["hours_left"] = token_hours_left()
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


class AppLifecycle:
    """进程级生命周期：启动冻结上一周期 → 每分钟心跳 → 正常关闭写 last_closed_at。

    生命周期字段只写 ui.json，绝不写 session.json / storage_state.json；
    F5 重载只是页面重取 bootstrap，不会重建这里的任何状态。心跳与界面
    save_prefs 共用 _PREFS_LOCK，做原子读-合并-写，互不覆盖。
    """

    INTERVAL_S = 60

    def __init__(self, prefs_file: "Path | None" = None) -> None:
        # 创建时定格偏好路径：测试传入临时目录后，心跳线程绝不会写回真实数据根
        self._prefs = prefs_file or PREFS
        with _PREFS_LOCK:
            prev = load_prefs_from(self._prefs)
            # 先冻结上一运行周期，再在同一原子写入里记录本次周期开始：
            # started + 首个心跳 + run_open=true 三者一起落盘——短启动就崩溃的
            # 周期也有「最后存活时刻」，不会回退拿更早周期的关闭时间当精确值
            self._prev = compute_lifecycle(prev, datetime.now())
            self._started_text = _now_text()
            merged = dict(prev)
            merged["last_started_at"] = self._started_text
            merged["last_heartbeat_at"] = self._started_text
            merged["run_open"] = True
            self._atomic_write(merged)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._heartbeat, daemon=True,
                                        name="lifecycle-heartbeat")
        self._thread.start()

    def _atomic_write(self, prefs: dict) -> None:
        _atomic_write_prefs(self._prefs, prefs)

    def _write_merged(self, patch: dict, force: bool = False) -> None:
        """与 update_prefs 同一把锁的读-合并-写，但钉住创建时的偏好文件。

        force=False（心跳路径）：锁内先核停止标记——finalize 已开始收尾时，
        迟到的心跳直接放弃，绝不把 last_heartbeat_at 写到关闭时刻之后。
        """
        with _PREFS_LOCK:
            if not force and self._stop.is_set():
                return
            prefs = load_prefs_from(self._prefs)
            prefs.update(patch or {})
            self._atomic_write(prefs)

    def _heartbeat(self) -> None:
        while not self._stop.wait(self.INTERVAL_S):
            try:
                self._write_merged({"last_heartbeat_at": _now_text()})
            except Exception:
                pass   # 单次心跳失败不影响界面，下一分钟再试

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
        不持锁 join 心跳线程：持锁 join 会和心跳的锁内写入互相等死。"""
        self._stop.set()
        try:
            self._write_merged({"last_closed_at": _now_text(), "run_open": False},
                               force=True)
        except Exception:
            pass


_LIFECYCLE: "AppLifecycle | None" = None
_LIFECYCLE_LOCK = threading.Lock()


def get_lifecycle() -> AppLifecycle:
    """进程级单例（懒创建）：main() 启动时先建，bootstrap 重复取同一实例。"""
    global _LIFECYCLE
    with _LIFECYCLE_LOCK:
        if _LIFECYCLE is None:
            _LIFECYCLE = AppLifecycle()
        return _LIFECYCLE


class Api:
    def __init__(self) -> None:
        self._window: webview.Window | None = None
        self._proc: subprocess.Popen | None = None
        self._plan: PlanCoordinator | None = None
        self._plan_procs: dict[str, subprocess.Popen] = {}
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
        # 最近一次 check 的四态结论：start_plan 的放行/拒绝只信它
        self._last_check: dict = {}

    # ---------- 事件推送 ----------
    def _emit(self, kind: str, payload) -> None:
        if not self._window:
            return
        js = "window.onBackend && window.onBackend.%s(%s)" % (
            kind, json.dumps(payload, ensure_ascii=False))
        try:
            self._window.evaluate_js(js)
        except Exception:
            pass

    # ---------- 读数据 ----------
    def bootstrap(self) -> dict:
        prefs = load_prefs()
        payload = {
            "version": APP_VERSION,
            "prefs": prefs,
            "help": self.help(),
            "login": login_state(),
            # 生命周期元数据（上一周期关闭时长/质量 + 登录估算）：只作提示，
            # 不参与任何放行/拦截；F5 重载重复调用拿到的是同一份进程级快照
            "lifecycle": get_lifecycle().snapshot(),
            "busy": self._is_busy(),
            # demo 标记：界面据此放行演示计划（真实模式仍要求登录校验 valid）
            "demo": DEMO,
        }
        with self._lock:
            plan = self._plan
        if plan is not None:
            # 页面重载会丢掉 plan/task 事件：把计划快照一并交给 bootstrap，
            # 界面才能恢复任务列表和「停止」按钮；计划本身不会重复提交。
            payload["plan"] = plan.snapshot()
        return payload

    def help(self) -> dict:
        return {
            "title": "天软活动预报名工具怎么用",
            "subtitle": "确认登录，选活动并开始，再看每场结果。",
            # 版本与最新版本入口：界面据此显示「版本 1.1.0」和 GitHub 外链按钮
            # （APP_VERSION 恒来自 read_version()，这里只是示例注释，不写死实现）
            "version": APP_VERSION,
            "latest_url": RELEASES_LATEST_URL,
            # 三步只讲「怎么操作」：选中不等于开始，开始按钮必须点，结果逐场看
            "steps": [
                {"no": 1, "title": "确认登录",
                 "text": "首次点「登录」，在浏览器里完成登录。重开会自动检查；"
                         "仅在提示失效时重新登录，暂时无法确认就刷新。"},
                {"no": 2, "title": "选活动并开始",
                 "text": "勾选不冲突的活动，设置重试时长和最多次数，再点「开始报名计划」。"
                         "未来场次等开放，已开放场次立即尝试。"},
                {"no": 3, "title": "看进度与结果",
                 "text": "电脑保持唤醒和联网，窗口可最小化。逐场看结果；"
                         "停止会结束本机任务，不会撤销学校报名。"},
            ],
            "note": "仅操作本人账号；选中活动还没有开始，报名结果以学校活动页为准。",
        }

    def front_ready(self) -> dict:
        # 仅排障：前端 boot 完成时打一声招呼，确认桥接真的通了
        self._trace("front_ready")
        return {"ok": True}

    def check(self) -> dict:
        # js_api 的处理跑在 UI 线程上：一旦在这里跑 subprocess，
        # 窗口会直接"未响应"。所以只负责起线程，结果用事件推回去。
        with self._lock:
            if self._closing:
                # 关闭拒绝是取消语义：不给组件码，不再起线程
                return {"ok": False, "queued": False,
                        "text": "程序正在关闭，已取消本次检查"}
            if self._checking:
                return {"ok": True, "queued": True, "skipped": "已在检查登录态"}
            self._checking = True
        threading.Thread(target=self._do_check, daemon=True).start()
        return {"ok": True, "queued": True}

    def list_activities(self) -> dict:
        with self._lock:
            if self._closing:
                return {"ok": False, "queued": False,
                        "text": "程序正在关闭，已取消拉取活动列表"}
            if self._listing:
                return {"ok": True, "queued": True, "skipped": "已经在拉活动列表"}
            self._listing = True
        threading.Thread(target=self._do_list, daemon=True).start()
        return {"ok": True, "queued": True}

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
        state = login_state()
        result = _check_result(state, code, reason or text)
        if err_code:
            result = "unknown"
        self._last_check = {"result": result, "checked_at": time.time(),
                            "runtime_error": err_code}
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
        now = time.time()
        for it in items:
            it["seconds_to_open"] = _seconds_until(it.get("sign_start"), now)
        self._activities = {str(it.get("id")): dict(it) for it in items if it.get("id")}
        self._emit("activities", {"ok": True, "items": items})

    # ---------- 动作 ----------
    def login(self) -> dict:
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

    def start_plan(self, items: list[dict], headed: bool = False,
                   retry_policy: dict | None = None) -> dict:
        """校验并启动计划；同一开放批次由协调器并发拉起单活动脚本。

        retry_policy 是计划级重试策略：{retry_for_s, max_attempts}，
        0 分别表示「直到报名截止」和「不限次数」；非法值回落默认
        （120 秒 / 不限），不让脏输入把 worker 挂成无限重试。
        """
        if not isinstance(items, list) or not items:
            return {"ok": False, "text": "先至少选择一场活动"}
        with self._lock:
            if self._closing:
                return {"ok": False, "text": "程序正在关闭，已取消开始计划"}
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

        normalized = []
        for item in items:
            current = dict(self._activities.get(str(item.get("id")), item))
            # 前端快照可能没有最新 seconds_to_open，但计划校验只信关键时间字段。
            current.update({key: item.get(key) for key in
                            ("id", "name", "sign_start", "sign_end", "act_start", "act_end", "signed")
                            if key in item})
            normalized.append(current)
        # 报名截止是硬门禁（前端复选/校验之外的后端最终防线）：
        # 已截止活动不能进入 worker；pre_register 与 instant_signup 是两个
        # 正式模式，混合计划允许共存，逐场按各自模式运行。
        closed = [item for item in normalized
                  if classify_signup_mode(str(item.get("sign_start") or ""),
                                          str(item.get("sign_end") or "")) == "closed"]
        if closed:
            names = "、".join("「%s」" % (item.get("name") or "活动") for item in closed[:3])
            more = " 等 %d 场" % len(closed) if len(closed) > 3 else ""
            return {"ok": False,
                    "text": "报名已截止：%s%s（报名截止 %s）不能加入计划，只能在列表里查看。"
                            % (names, more, closed[0].get("sign_end") or "时间未公布"),
                    "errors": [{"kind": "sign_end_passed", "activity_id": item.get("id"),
                                "name": item.get("name"), "sign_end": item.get("sign_end")}
                               for item in closed]}
        checked = validate_plan(normalized)
        if not checked["ok"]:
            return {"ok": False, "text": "计划里有不能同时报名的活动", "conflicts": checked["conflicts"],
                    "errors": checked["errors"]}

        plan_id = uuid.uuid4().hex[:12]
        specs = [TaskSpec.from_activity(item, "task-%02d-%s" % (i + 1, item["id"]), headed,
                                        retry_for_s=retry_for_s, max_attempts=max_attempts)
                 for i, item in enumerate(normalized)]

        def emit(kind: str, payload: dict) -> None:
            self._emit(kind, payload)
            if kind == "plan_done":
                with self._lock:
                    if self._plan is coordinator:
                        self._plan = None

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
            except _ClosingError:
                # 关闭拒绝落到协调器里就是取消：不冒充报名失败，也不让异常
                # 冲出 runner 污染任务状态（协调器业务不改）
                return {"status": "canceled", "code": None,
                        "reason": "程序正在关闭，本场次未启动"}

        coordinator = PlanCoordinator(plan_id, specs, runner, emit)
        with self._lock:
            # FB1 最终接纳点：从开头的 busy 检查到这里隔着校验与构建，期间
            # 可能已有别的请求预留计划、或关闭已开始——同一临界区内重检并
            # 只允许一个请求预留唯一协调器，未接纳的请求不启动、不覆盖
            if self._closing:
                return {"ok": False, "text": "程序正在关闭，已取消开始计划"}
            if self._is_busy_locked():
                return {"ok": False, "text": "已有登录、报名或计划在运行，先停止后再开始"}
            self._plan = coordinator
        try:
            # start 在 Api 锁外：协调器启动会 emit 回调（plan 事件要拿 _lock），
            # 锁内调用会双向等死
            coordinator.start()
        except Exception:
            # 启动失败只撤销本请求仍持有的预留，不碰别的计划，不残留 busy
            with self._lock:
                if self._plan is coordinator:
                    self._plan = None
            return {"ok": False, "text": "计划没能开始，请再试一次"}
        return {"ok": True, "queued": True, "plan_id": plan_id}

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
        update_prefs(prefs or {})
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
        try:
            at_ts = datetime.strptime(spec.sign_start, "%Y-%m-%d %H:%M:%S").timestamp()
        except (ValueError, TypeError):
            at_ts = None
        if at_ts is None or at_ts <= time.time():
            at_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        args = worker_command("grab", "--activity-id", spec.activity_id, "--name", spec.name,
                              "--at", at_text,
                              "--lead", str(max(0, int(spec.lead_s or 0))),
                              "--retry-for", str(spec.retry_for_s),
                              "--max-attempts", str(spec.max_attempts))
        if spec.sign_end:
            args += ["--sign-end", spec.sign_end]
        if spec.headed:
            args.append("--headed")
        with self._lock:
            # 关闭屏障：Popen 与登记同临界区——关闭先发生则零创建（取消语义）；
            # 创建已开始则 shutdown 必等登记完成才能取快照，收走不会漏
            if self._closing:
                raise _ClosingError("程序正在关闭，本场次未启动")
            proc = subprocess.Popen(
                args, cwd=str(HERE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=_env(), creationflags=CREATE_NO_WINDOW)
            self._plan_procs[spec.task_id] = proc
        if status:
            # run_started_at 必须在子进程创建之后记录：这是 worker 真正启动
            # 的时刻，不能提前写成计划启动时刻（预报名要靠它区分准备窗口）
            status("submitting", {"run_started_at": time.time()})
        self._emit("log", "▶ [%s] 等待并抢报名" % spec.name)

        lines: queue.Queue[str] = queue.Queue()
        reader_done = threading.Event()

        def read_output() -> None:
            try:
                for line in proc.stdout or []:
                    lines.put(line.rstrip("\r\n"))
            finally:
                reader_done.set()

        threading.Thread(target=read_output, name="signup-output", daemon=True).start()
        tail: list[str] = []
        canceled = False
        killed = False

        def feed(line: str) -> None:
            """输出行统一入口：落日志、存尾巴，并把 checkpoint 与 telemetry 推成任务 detail。

            时间戳取事件实际到达时刻（time.time()），前端据此显示真实节点；
            缺事件时才由前端按模式安全回退，这里不伪造。"""
            tail.append(line)
            del tail[:-40]
            self._emit("log", "[%s] %s" % (spec.name, line))
            if status and "详情页已加载" in line:
                status("submitting", {"page_opened_at": time.time()})
            if status and "开始盯按钮" in line:
                status("submitting", {"watch_started_at": time.time()})
            hit = _ATTEMPT_RE.match(line)
            if hit and status:
                # 只数真正发起 signUp 的行；刷新 / is_joined 查询不会长这样
                status("submitting", {"attempts": int(hit.group(1)),
                                      "last_reason": hit.group(2).strip()})
            parsed = _parse_retry_line(line)
            if parsed and status:
                # 结构化 retry 行到达即写统一 detail：重试截止不必等进程收尾
                # 才上屏，路线/任务卡/LED 与终态共用同一份 retry_deadline。
                # attempts/last_reason 不在这里推——提交行已逐次推过、终态
                # 事件会带最终值，再推一次只会让提交序列出现重复计数。
                stop = _retry_payload(parsed, spec,
                                      stop_reason=str(parsed.get("stop_reason") or ""))
                status("submitting", {"retry_deadline": stop.get("retry_deadline")})

        try:
            # 正常收尾要求 reader_done（输出读完）；取消路径只要求进程死透——
            # onefile 引导进程被树终止后，真实 worker 可能还握着 stdout 管道，
            # 再等 EOF 这条循环就永远出不去，canceled / plan_done 都发不出去。
            while True:
                if cancel_event.is_set() and not killed and proc.poll() is None:
                    canceled = True
                    killed = True
                    _kill_tree(proc)
                if proc.poll() is not None and (canceled or reader_done.is_set()):
                    break
                try:
                    while True:
                        line = lines.get_nowait()
                        if line:
                            feed(line)
                except queue.Empty:
                    pass
                time.sleep(0.05)
            while True:
                try:
                    line = lines.get_nowait()
                except queue.Empty:
                    break
                if line:
                    feed(line)
            code = proc.wait()
        finally:
            # 监控循环无论怎么退出（含异常），进程表都必须清干净——
            # 否则 _is_busy_locked 会把界面永远锁在「计划运行中」。
            # 顺带关掉输出管道：进程树已终止，读线程要么已 EOF 要么因关闭而退出。
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass
            with self._lock:
                self._plan_procs.pop(spec.task_id, None)
        # 结构化终态行 [retry] 是策略信息的第一来源，没有也不影响退出码语义
        stop: dict = {}
        for line in reversed(tail):
            parsed = _parse_retry_line(line)
            if parsed:
                stop = parsed
                break

        if canceled:
            return {"status": "canceled", "code": code, "reason": "用户取消了这一场",
                    **_retry_payload(stop, spec, stop_reason="user_stopped")}
        if any("你已经报过这个活动" in line for line in tail) or stop.get("stop_reason") == "already_signed":
            return {"status": "already_signed", "code": code, "reason": "你已经报过这一场",
                    **_retry_payload(stop, spec, stop_reason="already_signed")}
        if code == 0 and any("报名成功" in line for line in tail):
            return {"status": "success", "code": code, "reason": "报名成功",
                    **_retry_payload(stop, spec, stop_reason=stop.get("stop_reason") or "success")}
        reason = ""
        for line in reversed(tail):
            if line.startswith("[X] "):
                reason = line[4:].strip()
                break
        stop_reason = stop.get("stop_reason") or ("login_invalid" if code == 3 else "process_error")
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


def _seconds_until(ts: str | None, now: float) -> int | None:
    if not ts:
        return None
    try:
        return int(datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").timestamp() - now)
    except Exception:
        return None


def _kill_tree(proc: subprocess.Popen) -> None:
    """按进程树终止 worker：taskkill /F /T 连 Playwright node / chromium 一起收。

    只 terminate 直接子进程的话，PyInstaller onefile 的引导进程死后，
    解压出的真实 worker 会带着浏览器继续按重试节奏跑——就是
    「停止不了、还在无限重试」的那个形态。收尸（wait）交给调用方
    的监控循环，这里只负责让它死透。
    """
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=3,
                creationflags=CREATE_NO_WINDOW)
        except Exception:
            pass
    if proc.poll() is None:
        # taskkill 不可用/失败时的兜底：至少终止直接子进程
        try:
            proc.kill()
        except Exception:
            pass


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
    空输出返回 ("", "")，调用方按「认不出的失败」落 unknown。
    """
    lines = [ln.strip() for ln in str(out or "").strip().splitlines() if ln.strip()]
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
    lifecycle = get_lifecycle()
    api = Api()
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
