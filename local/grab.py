#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校园活动「到点自动报名」—— 本机 Windows 版（单账号，不依赖服务器）。

用途：
    在报名开放瞬间自动点「立即报名 → 确定」，避免手速不够被抢光。

安全边界：
    - 只操作本机 data/session.json 里那一个账号，不代他人报名；
    - 密码不落盘：登录是在弹出的真实浏览器窗口里由你手动输入（含验证码），
      脚本只在登录成功后保存 cookie + localStorage（等价于浏览器"保持登录"）；
    - 日志不记录 token、姓名、学号、密码、验证码；
    - 只对传入的单个 activity-id 动作，不批量扫活动、不做任何超出报名的操作。

用法：
    python grab.py login                     # 首次/登录态失效后：弹出浏览器手动登录一次
    python grab.py check                     # 检查登录态是否还有效
    python grab.py list                      # 列出可报名的活动（含活动 id、报名开始时间）
    python grab.py grab                      # 交互式：从列表里挑一个，到点自动报名
    python grab.py grab --activity-id <id> --at "2026-10-22 00:59:59" --name "活动名"
    python grab.py selftest                  # 自检：能否拉起浏览器并打开站点

@author LiXinwen
"""

import argparse
import json
import os
import queue
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

BASE = "https://xg.tjise.edu.cn"
API = BASE + "/dr/api"
REFERER = BASE + "/stu/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
# 直发白名单：只有页面入口脚本恰好是这个已知打包（签名协议与之绑定）才启用直发；
# 其它/未知脚本一律点击兜底，不下载脚本、不抽新 key（L1 保守匹配，C01）
KNOWN_DIRECT_SCRIPT_BASENAME = "index.b489a1d6.js"

HERE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
# 登录态/偏好统一走 runtime_paths：开发与绿色版仍是脚本旁 data，
# 冻结 worker 默认 %LOCALAPPDATA%（TJISE_DATA_DIR 可覆盖），与主程序同一根
from runtime_paths import DATA_DIR as DATA
from runtime_paths import LOG_FILE, SESSION_FILE, STATE_FILE, ensure_data_dir
# 学校时间与重试预算只有一份实现（time_contract）：这里不再自己 strptime，
# 否则同一串学校时间在 CLI、GUI、JS 三处会解析成不同 epoch
from time_contract import (BudgetExhausted, RetryBudget, parse_school_time,
                           school_epoch, school_time_text, default_now,
                           configure_time_source)
# 独立北京时间（决策 012）：worker 读父进程快照，CLI 有界自校时；
# 任何失败都不回退系统墙钟，也不把校时失败说成登录失效
from business_clock import BusinessClock, ClockClient, ClockUnavailable
CONFIG_FILE = HERE / "config.json"

# ---- 独立北京时间（决策 012）-----------------------------------------------
# worker 模式：父进程把 TJISE_CLOCK_SNAPSHOT/TJISE_CLOCK_OWNER 传进来，只读快照；
# CLI 模式：自带 BusinessClock 做一次有界同步。失败显式处理，绝不回退本机墙钟。
_CLOCK = {"now": None, "bounds": None, "meta": None}


def configure_business_clock(clock_like) -> None:
    _CLOCK["now"] = clock_like.now
    _CLOCK["bounds"] = clock_like.bounds
    _CLOCK["meta"] = clock_like
    configure_time_source(clock_like.now)   # signup_plan 的缺省 now 同源


def business_now() -> float:
    fn = _CLOCK.get("now")
    if fn is None:
        raise ClockUnavailable("业务时钟未配置")
    return float(fn())


def business_bounds():
    fn = _CLOCK.get("bounds")
    if fn is None:
        raise ClockUnavailable("业务时钟未配置")
    return fn()


def clock_meta() -> dict:
    """checkpoint 附带的时钟元数据（无凭证）。"""
    meta = _CLOCK.get("meta")
    if meta is None:
        return {}
    try:
        return {"owner_id": getattr(meta, "owner_id", ""),
                "generation": getattr(meta, "generation", 0),
                "uncertainty_s": round(float(meta.bounds()[1] - meta.bounds()[0]) / 2, 3)}
    except Exception:
        return {}


def setup_business_clock(allow_sync=True, timeout_s=3.0):
    """worker 读父进程快照；CLI 有界自校时。失败返回 None，由调用方显式处理。"""
    if os.environ.get("TJISE_CLOCK_SNAPSHOT"):
        try:
            client = ClockClient()
            client.now()                      # 立即验证快照可用
            configure_business_clock(client)
            return client
        except ClockUnavailable:
            return None
    if not allow_sync:
        return None
    clock = BusinessClock(background=False, sync_timeout_s=float(timeout_s))
    try:
        if clock.sync() not in ("ready", "holdover"):
            return None
    except Exception:
        return None
    configure_business_clock(clock)
    return clock

DETAIL_HASH = ("#/secondClassroom/student/activity/studentActivity/"
               "activityDetails?activityId={aid}&from=acList")
LOGIN_HASH = "#/mainPages/login?userType=2"

PAGE_SIZE = 50

# 开放前多少秒开始逐次看按钮：名额放开往往只有几秒窗口，
# 等到开放前一秒再刷新会丢掉首轮点击时机。
POLL_LEAD = 5.0


# ---------------------------------------------------------------- 基础工具
def setup_console():
    """Windows 控制台默认 GBK，中文/特殊符号会抛 UnicodeEncodeError，这里强制 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def log(msg):
    ensure_data_dir()
    try:
        # 日志时间按北京时间（业务时钟）；时钟未配置时保留旧本机戳以免崩溃
        stamp = school_time_text(now_fn=business_now)
    except Exception:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "%s %s" % (stamp, msg)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json_atomic(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass


def token():
    sess = read_json(SESSION_FILE, {}) or {}
    return str(sess.get("token") or "").strip()


def _post(path, payload=None, tok=None, timeout=None, budget=None):
    """POST/GET 共用入口；传 budget 时超时被压进剩余预算。

    预算耗尽抛 BudgetExhausted，且不被本函数的通用 except 吞掉——
    否则「窗口到点」会被伪装成一次接口异常。
    """
    if budget is not None:
        timeout = budget.timeout_s(15 if timeout is None else timeout)
    body = json.dumps(payload if payload is not None else {}).encode("utf-8")
    req = urllib.request.Request(API + path, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/plain, */*")
    req.add_header("token", tok if tok is not None else token())
    req.add_header("Referer", REFERER)
    req.add_header("User-Agent", UA)
    try:
        # timeout 可由调用方按剩余预算收紧：固定 15 秒会在 2 秒窗口里
        # 一次查询就把流程拖出截止（R03）
        resp = urllib.request.urlopen(req, timeout=15 if timeout is None else float(timeout))
        try:
            raw = resp.read().decode("utf-8", "replace")
        finally:
            resp.close()
        return json.loads(raw)
    except BudgetExhausted:
        raise
    except Exception as exc:
        return {"code": -1, "message": type(exc).__name__}


def _get(path, tok=None, timeout=None):
    """只读 GET：详情接口没有任何写语义，失败返回 code=-1 由调用方降级。

    不打印 URL 之外的任何请求头；token 只在内存里使用，绝不进日志。
    """
    req = urllib.request.Request(API + path, method="GET")
    req.add_header("Accept", "application/json, text/plain, */*")
    req.add_header("token", tok if tok is not None else token())
    req.add_header("Referer", REFERER)
    req.add_header("User-Agent", UA)
    try:
        resp = urllib.request.urlopen(req, timeout=15 if timeout is None else float(timeout))
        try:
            raw = resp.read().decode("utf-8", "replace")
        finally:
            resp.close()
        return json.loads(raw)
    except Exception as exc:
        return {"code": -1, "message": type(exc).__name__}


def clock_sample(timeout_s=6.0):
    """用 HTTP Date 做一次粗校时，返回结构化结论（不调整任何本地时间）。

    HTTP Date 只有整秒分辨率，且响应里的时刻对应「服务器生成响应」，
    本机只能观测到发出请求和收到响应两个时刻。因此：

    * 偏移估计取区间中点，误差至少包含往返耗时的一半与整秒截断的 1 秒；
    * 差值的可信区间跨过告警阈值时，只能说「不确定」，不能断言本机慢 N 秒；
    * 没有 Date、解析失败、请求失败或往返太慢，一律 unknown。

    这不是精确对时，也绝不自动把差值加到报名时刻上。
    """
    started_wall = time.time()
    started_mono = time.monotonic()
    result = {
        "source": "http_date",
        "checked_at": started_wall,
        "rtt_s": None,
        "offset_s": None,
        "uncertainty_s": None,
        "state": "unknown",
        "detail": "",
    }
    try:
        req = urllib.request.Request(BASE + "/stu/", method="HEAD")
        req.add_header("User-Agent", UA)
        resp = urllib.request.urlopen(req, timeout=max(0.5, float(timeout_s)))
        try:
            raw = resp.headers.get("Date")
        finally:
            resp.close()
        received_wall = time.time()
        rtt = time.monotonic() - started_mono
        result["rtt_s"] = round(rtt, 3)
        if not raw:
            result["detail"] = "服务器没有返回 Date"
            return result
        from email.utils import parsedate_to_datetime
        remote = parsedate_to_datetime(raw).timestamp()
        offset = remote - (started_wall + received_wall) / 2.0
        # 整秒分辨率最多丢 1 秒；往返延迟按一半计入不确定区间
        uncertainty = rtt / 2.0 + 1.0
        result["offset_s"] = round(offset, 3)
        result["uncertainty_s"] = round(uncertainty, 3)
        # 往返太慢时，中点估计已经不能支撑「慢几秒」这种结论
        if rtt > 2.0:
            result["detail"] = "往返耗时过长，只能算粗校验"
            return result
        low = abs(offset) - uncertainty
        high = abs(offset) + uncertainty
        if low > 3.0:
            result["state"] = "warn"
            result["detail"] = "差值区间整体超过 3 秒"
        elif high <= 3.0:
            result["state"] = "ok"
        else:
            result["detail"] = "差值区间跨过 3 秒阈值，无法断定"
        return result
    except Exception as exc:  # noqa: BLE001 - 校时失败只影响提示，不影响报名
        result["detail"] = type(exc).__name__
        return result


def clock_skew():
    """兼容旧调用：只取偏移估计；没有可信样本返回 None。"""
    sample = clock_sample()
    return sample.get("offset_s") if sample.get("state") != "unknown" else None


# ---------------------------------------------------------------- 活动查询
def _tri_state(value):
    """把接口里的签到/签退要求归一化；无法确认时保留 None，禁止猜成无需。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y", "是", "需要", "有"}:
        return True
    if text in {"0", "false", "no", "n", "否", "无需", "无"}:
        return False
    return None


def _activity_flag(raw, names):
    """只读取明确的布尔字段别名；未命中时返回 None，等待真实接口字段确认。"""
    for name in names:
        if name in raw and raw.get(name) not in (None, ""):
            return _tri_state(raw.get(name))
    return None


CHECKIN_FLAG_NAMES = (
    "checkin_required", "checkInRequired", "need_checkin", "needCheckIn",
    "isCheckInRequired", "sfdq", "sfqd", "qiandao", "签到",
)
CHECKOUT_FLAG_NAMES = (
    "checkout_required", "checkOutRequired", "need_checkout", "needCheckOut",
    "isCheckOutRequired", "sfqt", "sfqtd", "qiantui", "签退",
)

# 时间字段别名：列表接口目前全为 null，详情接口才给出真实时间段。
# 名字按「内部字段 → 学校字段」排列，列表若已有同等信息则优先使用。
CHECKIN_START_NAMES = ("checkin_start", "qdkssj", "checkinStart")
CHECKIN_END_NAMES = ("checkin_end", "qdjssj", "checkinEnd")
CHECKOUT_START_NAMES = ("checkout_start", "qtkssj", "checkoutStart")
CHECKOUT_END_NAMES = ("checkout_end", "qtjssj", "checkoutEnd")

# 详情按活动 id 去重缓存：一次列表刷新只查一次，重复刷新直接复用；
# 并发上限 4，避免把学校接口和本机连接数打满。
DETAIL_CACHE: dict[str, dict] = {}
DETAIL_CACHE_LOCK = threading.Lock()
DETAIL_CONCURRENCY = 4
DETAIL_TIMEOUT_S = 12.0


def _activity_time(raw, names):
    """读取学校时间文本；非法或缺失返回空串（不猜、不补当前时间）。"""
    for name in names:
        if name in raw and raw.get(name) not in (None, ""):
            text = str(raw.get(name)).strip()
            return text if school_epoch(text) is not None else ""
    return ""


def _detail_fields(raw):
    """详情响应可能直接是 data，也可能包一层 {code,data}；两种都接受。"""
    if not isinstance(raw, dict):
        return {}
    data = raw.get("data")
    return data if isinstance(data, dict) else raw


def parse_attendance_detail(raw):
    """把 getInfo 的签到/签退字段归一化；未知保持 None，绝不猜成无需。

    真实字段：sfxyqd/qdkssj/qdjssj（签到）、sfxyqt/qtkssj/qtjssj（签退）。
    标记明确时优先；标记缺失但时间段存在才推导 true；两者都缺失 → None。
    """
    data = _detail_fields(raw)
    flag_in = _tri_state(data.get("sfxyqd")) if "sfxyqd" in data else None
    flag_out = _tri_state(data.get("sfxyqt")) if "sfxyqt" in data else None
    in_start = _activity_time(data, CHECKIN_START_NAMES)
    in_end = _activity_time(data, CHECKIN_END_NAMES)
    out_start = _activity_time(data, CHECKOUT_START_NAMES)
    out_end = _activity_time(data, CHECKOUT_END_NAMES)
    if flag_in is None and (in_start or in_end):
        flag_in = True
    if flag_out is None and (out_start or out_end):
        flag_out = True
    if flag_in is False:
        # 明确无需签到：时间段一并清空，避免界面出现「无需签到 19:00」
        in_start = in_end = ""
    if flag_out is False:
        out_start = out_end = ""
    return {
        "checkin_required": flag_in,
        "checkin_start": in_start,
        "checkin_end": in_end,
        "checkout_required": flag_out,
        "checkout_start": out_start,
        "checkout_end": out_end,
    }


def fetch_activity_detail(activity_id, timeout=DETAIL_TIMEOUT_S):
    """只读详情；失败返回 None（调用方显示「同步中」，不把失败当成无需）。"""
    aid = str(activity_id or "").strip()
    if not aid:
        return None
    with DETAIL_CACHE_LOCK:
        cached = DETAIL_CACHE.get(aid)
    if cached is not None:
        return dict(cached)
    data = _get("/rjyHd/getInfo/" + urllib.parse.quote(aid, safe=""), timeout=timeout)
    if not isinstance(data, dict) or str(data.get("code")) not in {"1", "1.0"}:
        return None
    parsed = parse_attendance_detail(data)
    with DETAIL_CACHE_LOCK:
        DETAIL_CACHE[aid] = parsed
    return dict(parsed)


def _apply_attendance(item, detail):
    """列表字段优先、详情补充；详情失败保留 pending 状态供下次刷新重试。"""
    list_has = any(item.get(key) not in (None, "")
                   for key in ("checkin_required", "checkout_required",
                               "checkin_start", "checkout_start"))
    if detail is None:
        item.setdefault("checkin_start", "")
        item.setdefault("checkin_end", "")
        item.setdefault("checkout_start", "")
        item.setdefault("checkout_end", "")
        item["attendance_source"] = "list" if list_has else "none"
        item["attendance_state"] = "pending"
        item["attendance_error"] = "detail_unavailable"
        return item
    for key, value in detail.items():
        if key in ("checkin_required", "checkout_required"):
            if item.get(key) is None:
                item[key] = value
        elif not item.get(key):
            item[key] = value
    item["attendance_source"] = "list" if list_has else "detail"
    item["attendance_state"] = "ready"
    item.pop("attendance_error", None)
    return item


def enrich_attendance_items(items, max_workers=DETAIL_CONCURRENCY):
    """按活动 id 去重并发补详情；单场失败不影响整张列表。"""
    rows = list(items or [])
    ids = []
    seen = set()
    for item in rows:
        aid = str(item.get("id") or "")
        if aid and aid not in seen:
            seen.add(aid)
            ids.append(aid)
    if not ids:
        return rows
    results: dict[str, dict | None] = {}
    try:
        with ThreadPoolExecutor(max_workers=max(1, int(max_workers))) as pool:
            for aid, detail in zip(ids, pool.map(fetch_activity_detail, ids)):
                results[aid] = detail
    except Exception:
        # 线程池不可用：全部按「同步中」处理，列表本身仍然可用
        results = {}
    for item in rows:
        _apply_attendance(item, results.get(str(item.get("id") or "")))
    return rows


def fetch_activities(budget=None, with_details=False):
    """分页拉活动列表；返回本地字段结构（字段名已换成好读的名字）。

    with_details=True 时按活动 id 去重并发补 getInfo 的签到/签退时间段：
    列表接口这三个字段当前全为 null，无法据此判断「无需」。
    """
    out, page, total = [], 1, None
    while page <= 30:
        data = _post("/rjyHd/listActivity",
                     {"pageSize": PAGE_SIZE, "pageNum": page, "hdmc": "",
                      "hdzt": None, "kssj": "", "jssj": ""}, budget=budget)
        if data.get("code") == 4001:
            raise PermissionError(data.get("message") or "身份验证失败")
        if data.get("code") != 1:
            raise RuntimeError("接口异常：%s" % (data.get("message") or data.get("code")))
        block = data.get("data") or {}
        total = int(block.get("count") or 0)
        chunk = block.get("list") or []
        for raw in chunk:
            if not raw.get("hdmc"):
                continue
            out.append({
                "id": raw.get("id", ""),
                "name": (raw.get("hdmc") or "").strip(),
                "type": raw.get("hdlx") or "其他",
                "place": raw.get("hddd") or "",
                "sign_start": raw.get("bmkssj") or "",
                "sign_end": raw.get("bmjssj") or "",
                "act_start": raw.get("hdkssj") or "",
                "act_end": raw.get("hdjssj") or "",
                "signed": raw.get("bmzt") == 1,
                "checkin_required": _activity_flag(raw, CHECKIN_FLAG_NAMES),
                "checkout_required": _activity_flag(raw, CHECKOUT_FLAG_NAMES),
                # 列表若已带回时间段就直接用；没有则留空，等详情补充
                "checkin_start": _activity_time(raw, CHECKIN_START_NAMES),
                "checkin_end": _activity_time(raw, CHECKIN_END_NAMES),
                "checkout_start": _activity_time(raw, CHECKOUT_START_NAMES),
                "checkout_end": _activity_time(raw, CHECKOUT_END_NAMES),
                "attendance_source": "list",
                "attendance_state": "unknown",
            })
        if not chunk or len(out) >= total:
            break
        page += 1
    if with_details and out:
        return enrich_attendance_items(out)
    return out


def is_joined(act_id, budget=None):
    """getJoinInfo：未报名时 data 为 null，报名后 data 是对象。

    传入 budget 时返回三态：True 已报名 / False 确认未报名 / None 查询没成功。
    最后一种绝不能被当成「未报名」——把它当未报名会再发一次提交，
    把「结果未知」变成真正的重复报名。
    """
    if budget is not None and budget.expired():
        return None
    data = _post("/rjyHdCyr/getJoinInfo/" + act_id, {},
                 timeout=None if budget is None else budget.timeout_s(15))
    if not isinstance(data, dict) or data.get("code") != 1:
        return False if budget is None else None
    return data.get("data") not in (None, {}, [])


def parse_ts(text):
    """学校时间文本 → epoch 秒；非法时抛 ValueError（调用方按「时间未知」处理）。

    必须显式按东八区解释：学校给的是不带时区的墙上时间，交给
    `datetime.strptime(...).timestamp()` 会按本机时区折算，UTC 机器上会差 8 小时。
    """
    epoch = school_epoch(text)
    if epoch is None:
        raise ValueError("学校时间格式不正确：%r" % (text,))
    return epoch


def still_relevant(act, now=None):
    """报名还没截止（或没给截止时间）的活动才算「还值得看」。"""
    now = now or default_now()
    if not act.get("sign_end"):
        return True
    try:
        return parse_ts(act["sign_end"]) >= now - 3600
    except Exception:
        return True


def sign_state(act, now=None):
    now = now or default_now()
    try:
        start = parse_ts(act["sign_start"])
    except Exception:
        return "时间未知"
    try:
        end = parse_ts(act["sign_end"])
    except Exception:
        end = None
    if now < start:
        left = int(start - now)
        return "未开放（%d 小时 %d 分后）" % (left // 3600, left % 3600 // 60)
    if end and now > end:
        return "报名已截止"
    return "开放中"


# ---------------------------------------------------------------- 浏览器
def launch_browser(pw, headless):
    """按「Chrome → Edge → 自带内核」的顺序找浏览器。

    前两个用本机已装的，免下载几百 MB；Windows 10/11 一定带 Edge，
    所以绿色版（不打包 chromium）在没装 Chrome 的机器上也能跑。
    """
    for channel in ("chrome", "msedge"):
        try:
            return pw.chromium.launch(channel=channel, headless=headless)
        except Exception:
            continue
    try:
        return pw.chromium.launch(headless=headless)
    except Exception as exc:
        raise RuntimeError(
            "没找到可用的浏览器：请先装 Chrome 或 Edge，"
            "或执行 python -m playwright install chromium 下载自带内核。") from exc


def new_context(browser, with_state=True):
    kwargs = {"locale": "zh-CN", "viewport": {"width": 1366, "height": 900}}
    if with_state and STATE_FILE.exists():
        kwargs["storage_state"] = str(STATE_FILE)
    return browser.new_context(**kwargs)


def page_local_storage(page):
    return page.evaluate(
        "() => { const o = {}; for (let i = 0; i < localStorage.length; i++) {"
        " const k = localStorage.key(i); o[k] = localStorage.getItem(k); } return o; }")


def session_from_items(items):
    """localStorage -> session.json（与服务器侧字段保持一致）。"""
    session = {"token": str(items.get("xgUniToken") or "").strip(),
               "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "name": "", "student_no": "", "gender": ""}
    try:
        user = json.loads(items.get("user") or "{}")
    except Exception:
        user = {}
    if isinstance(user, dict):
        session["name"] = str(user.get("xm") or "")
        session["student_no"] = str(user.get("xh") or "")
        session["gender"] = str(user.get("xb") or "")
    session["name"] = session["name"] or str(items.get("xm") or "")
    return session


# ---------------------------------------------------------------- 子命令
def cmd_login(args):
    from playwright.sync_api import sync_playwright

    # login/check/list/grab 共用统一数据根；冻结进程首次会顺带迁移旧 EXE 旁数据
    ensure_data_dir()
    print("=" * 64)
    print("即将打开浏览器，请在窗口里完成学生登录（账号 + 密码 + 图形验证码）。")
    print("登录成功会自动保存登录态并关闭窗口；最多等待 %d 秒。" % args.timeout)
    print("=" * 64)

    with sync_playwright() as pw:
        browser = launch_browser(pw, headless=False)
        ctx = new_context(browser, with_state=False)
        page = ctx.new_page()
        try:
            page.goto(BASE + "/stu/" + LOGIN_HASH, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            print("[X] 打不开登录页：%s" % type(exc).__name__)
            browser.close()
            return 2

        deadline = time.time() + args.timeout
        tok = ""
        while time.time() < deadline:
            try:
                if page.is_closed():
                    print("[X] 窗口被关掉了，登录未完成。")
                    browser.close()
                    return 1
                tok = page.evaluate(
                    "() => (localStorage.getItem('xgUniToken') || '').trim()")
            except Exception:
                tok = ""
            if tok:
                break
            time.sleep(1.0)

        if not tok:
            print("[X] 等了 %d 秒仍未检测到登录成功，请重试。" % args.timeout)
            browser.close()
            return 1

        page.wait_for_timeout(1500)  # 等页面把 user / xm 写进 localStorage
        items = page_local_storage(page)
        state = ctx.storage_state()
        ctx.close()
        browser.close()

    write_json_atomic(STATE_FILE, state)
    session = session_from_items(items)
    session["token"] = tok
    write_json_atomic(SESSION_FILE, session)

    ok, msg = check_token()
    if not ok:
        print("[X] 已保存登录态，但接口校验失败：%s" % msg)
        return 2
    print("[OK] 登录成功，登录态已保存（有效期约 17 小时，失效后重新运行本步即可）。")
    return 0


def cmd_check(args):
    ok, msg = check_token()
    if ok:
        print("[OK] 登录态有效。")
        # 每轮都给结构化粗校时结论：正常/未知也要有，界面才能替换旧告警
        sample = clock_sample()
        log("[clock] " + json.dumps(sample, ensure_ascii=False, separators=(",", ":")))
        if sample.get("state") == "warn":
            print("[!] 学校 HTTP Date 与本机时间有偏差（粗测含网络误差约 ±%.1f 秒）。"
                  "该粗测只作诊断，报名调度按工具自己的北京时间执行。" %
                  (sample.get("uncertainty_s") or 1.0))
        return 0
    print("[X] %s" % msg)
    print("    请运行：python grab.py login   （或双击 1-登录.bat）")
    return 2


def cmd_list(args):
    # 先判登录态：直接拉列表失败只会得到一句「接口异常：HTTPError」，
    # 图形界面里的用户看不出是没登录还是网络问题。
    ok, msg = check_token()
    if not ok:
        print("[X] %s" % msg)
        print("    请先运行：python grab.py login（或点界面上的「登录」）")
        return 2
    try:
        # 图形界面（--json）需要签到/签退时间段：详情按 id 去重补全，失败单场降级；
        # 纯文本列表只给人看活动名与状态，不为每场多发一次详情请求。
        acts = fetch_activities(with_details=bool(getattr(args, "json", False)))
    except PermissionError as exc:
        print("[X] 登录态已失效（%s），请先运行 python grab.py login" % exc)
        return 2
    except Exception as exc:
        print("[X] 拉取活动失败：%s" % exc)
        return 2

    try:
        now = business_now()          # 列表筛选按业务北京时间
    except ClockUnavailable:
        # 校时不可用仍可完成原始数据读取：不按系统时钟筛选/排行，
        # 状态与距开放一律显示未知，绝不把本机日期当北京时间。
        now = None
        log("[!] 北京时间暂不可用：本次保留全部活动，开放状态显示未知。")
    # 默认只看报名还没截止的（历史活动几百条，混在一起没法挑）；
    # 没有可信北京时间时不能按本机时钟过滤，保留全部由用户自行判断
    rows = list(acts) if (args.all or now is None) \
        else [a for a in acts if still_relevant(a, now)]

    def rank(a):
        # 未开放的排最前，其次开放中，已报名的垫底；时间未知时全部同档
        if a["signed"]:
            return 2
        if now is None:
            return 1
        try:
            return 0 if now < parse_ts(a["sign_start"]) else 1
        except Exception:
            return 1

    rows.sort(key=lambda a: (rank(a), a["sign_start"] or "9999", a["name"]))
    if not rows:
        if getattr(args, "json", False):
            print("[]")
            return 0
        print("[i] 近期没有可报名的活动（还没公布报名时间，或都已截止/已报名）。")
        print("    想连历史活动一起看：python grab.py list --all")
        return 0

    if getattr(args, "json", False):
        # 给图形界面用：字段固定，界面拿 id 直接把目标钉死，不靠序号对齐
        out = []
        for i, act in enumerate(rows, 1):
            out.append({
                "index": i,
                "id": act["id"],
                "name": act["name"],
                "sign_start": act["sign_start"],
                "sign_end": act["sign_end"],
                "act_start": act["act_start"],
                "act_end": act["act_end"],
                "place": act["place"],
                "signed": bool(act["signed"]),
                "checkin_required": act.get("checkin_required"),
                "checkout_required": act.get("checkout_required"),
                # 真实时间段与来源：界面据此显示「签到 19:00–19:40」或「同步中」
                "checkin_start": act.get("checkin_start") or "",
                "checkin_end": act.get("checkin_end") or "",
                "checkout_start": act.get("checkout_start") or "",
                "checkout_end": act.get("checkout_end") or "",
                "attendance_source": act.get("attendance_source") or "none",
                "attendance_state": act.get("attendance_state") or "unknown",
                "state": "已报名" if act["signed"]
                         else ("待确认" if now is None else sign_state(act, now)),
            })
        print(json.dumps(out, ensure_ascii=False))
        return 0

    print("%3s  %s  %s  %s" % ("#", "报名开始", "状态", "活动名"))
    for i, act in enumerate(rows, 1):
        flag = "已报名" if act["signed"] else (
            "待确认" if now is None else sign_state(act, now))
        print("%3d  %s  %s  %s" % (i, act["sign_start"] or "-", flag, act["name"]))
    print("\n提示：双击 2-抢活动.bat 可直接选择并到点抢；也可以双击 4-图形界面.bat 用鼠标点。")
    return 0


def candidates(acts, now=None):
    """可抢的活动：未报名、有报名开始时间、还没截止；未开放的排最前。

    交互选择与 --pick/--name 非交互选择共用这一份顺序，保证序号一致。
    """
    now = now or default_now()
    cand = [a for a in acts
            if not a["signed"] and a["sign_start"] and still_relevant(a, now)]
    def rank(a):
        # 未开放的排最前（这才是要抢的），开放中的其次
        try:
            return 0 if now < parse_ts(a["sign_start"]) else 1
        except Exception:
            return 1

    cand.sort(key=lambda a: (rank(a), a["sign_start"]))
    return cand


def pick_activity(acts, now=None):
    """交互式挑活动：只列未报名的，按报名开始时间排序。"""
    cand = candidates(acts, now)
    if not cand:
        print("[i] 近期没有可报名的活动（还没公布报名时间，或都已截止/已报名）。")
        return None
    print("%-4s %-19s %-19s %s" % ("序号", "报名开始", "活动开始", "活动名"))
    for i, act in enumerate(cand, 1):
        print("%-4d %-19s %-19s %s" % (i, act["sign_start"], act["act_start"][:16], act["name"]))
    while True:
        raw = input("\n请输入序号（回车选 1，q 退出）：").strip().lower()
        if raw == "q":
            return None
        idx = 1 if raw == "" else (int(raw) if raw.isdigit() else -1)
        if 1 <= idx <= len(cand):
            return cand[idx - 1]
        print("输入的序号不对，请重试。")


def notify(title, lines, template="green"):
    """可选飞书群机器人通知：config.json 里配了 webhook 才会发，没配就只在控制台打印。"""
    cfg = read_json(CONFIG_FILE, {}) or {}
    hook = str(cfg.get("feishu_webhook") or "").strip()
    if not hook:
        return False
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"template": template,
                   "title": {"tag": "plain_text", "content": title}},
        "elements": [{"tag": "div", "text": {"tag": "lark_md",
                                             "content": "\n".join(lines)}}],
    }
    body = json.dumps({"msg_type": "interactive", "card": card},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(hook, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        ok = payload.get("code") == 0 or payload.get("StatusCode") == 0
        log("[i] 飞书通知%s" % ("已发送" if ok else "失败：%s" % str(payload)[:80]))
        return ok
    except Exception as exc:
        log("[i] 飞书通知失败：%s" % type(exc).__name__)
        return False


def wait_until(ts, budget=None):
    """等到指定墙钟时刻；给了预算时，预算一到就放弃等待。

    等待开放用的是学校绝对时刻（墙钟语义），但用户设的持续时长是单调预算：
    系统时钟被回拨时，墙钟的「还差 10 秒」会凭空变长，这时必须由预算把
    这段时间截住，不能靠墙钟自己走回来（R06）。
    """
    while True:
        left = ts - business_now()      # 业务北京时间：不随系统墙钟跳变
        if left <= 0:
            return
        step = min(0.2, left)
        if budget is not None:
            remain = budget.remaining_s()
            if remain <= 0:
                raise BudgetExhausted("等待开放时重试窗口已到点")
            step = min(step, remain)
        time.sleep(step)


def _by_index_or_name(cand, args):
    """非交互挑活动：--pick 序号（与 list / 交互列表一致），或 --name 关键词。

    只在这一步命中一个活动才继续；命中多个就列出来让用户改用序号，避免猜错抢错。
    """
    pick = int(getattr(args, "pick", 0) or 0)
    kw = str(getattr(args, "name", "") or "").strip()
    if pick:
        if not 1 <= pick <= len(cand):
            print("[X] --pick %d 超出范围（当前有 %d 个可抢的活动）" % (pick, len(cand)))
            print("    运行 python grab.py list 查看序号。")
            return None
        return cand[pick - 1]
    if not kw:
        return None
    hits = [a for a in cand if kw.lower() in a["name"].lower()]
    if not hits:
        print("[X] 没有名称含「%s」的活动。" % kw)
        print("    运行 python grab.py list 查看当前可抢的活动。")
        return None
    if len(hits) > 1:
        print("[X] 「%s」匹配到 %d 个活动，请改用 --pick 序号：" % (kw, len(hits)))
        for i, a in enumerate(cand, 1):
            if a in hits:
                print("    %-3d %s  %s" % (i, a["sign_start"], a["name"]))
        return None
    return hits[0]


def _at_or_now(at_text):
    """已开放的场次把 at 换成现在：重试窗口是「at + 120 秒」，
    开放超过 2 分钟的原时刻会让提交循环一次都不进、直接误报名额已满。"""
    if not at_text:
        return at_text
    try:
        ts = parse_ts(at_text)
    except Exception:
        return at_text
    if ts is not None and ts <= business_now():
        # 回写的「现在」必须按学校时区格式化：随后 parse_ts 会按 UTC+8 读回，
        # 用本机时区写会凭空产生 8 小时偏移；「现在」取业务北京时间
        return school_time_text(now_fn=business_now)
    return at_text


def resolve_target(args, budget=None):
    """确定 activity-id / 报名开始时间 / 活动名。"""
    acts = None
    act = None
    if args.activity_id:
        aid = args.activity_id
        if not args.at:
            act = next((a for a in (fetch_activities(budget) if acts is None else acts)
                        if a["id"] == aid), None)
            if not act or not act["sign_start"]:
                print("[X] 活动 %s 找不到报名开始时间，请显式传 --at" % aid)
                return None
            at = act["sign_start"]
        else:
            at = args.at
        name = args.name or (act["name"] if act else "活动")
        # original_at 保留学校原定开放时刻：已开放场次 at 会被换成现在，
        # 命令行输出要同时给出「原定开放」和「本次执行」两个时间
        return {"id": aid, "at": _at_or_now(at), "original_at": at, "name": name}

    try:
        acts = fetch_activities(budget)
    except PermissionError as exc:
        print("[X] 登录态已失效（%s），请先运行 python grab.py login" % exc)
        return None

    cand = candidates(acts)
    if not cand:
        print("[i] 近期没有可报名的活动（还没公布报名时间，或都已截止/已报名）。")
        return None

    act = _by_index_or_name(cand, args)
    if act is None:
        if getattr(args, "pick", 0) or str(getattr(args, "name", "") or "").strip():
            return None            # 非交互模式选不出目标就直接结束，不再弹交互
        act = pick_activity(acts)
    if not act:
        return None
    print("\n已选择：%s" % act["name"])
    print("报名开始：%s   活动时间：%s   地点：%s" %
          (act["sign_start"], act["act_start"], act["place"]))
    # 用 --pick/--name 指定时视为已确认（脚本/快捷方式调用不该再卡在输入上）
    if not (getattr(args, "pick", 0) or str(getattr(args, "name", "") or "").strip()):
        if input("确认到点自动报名？[y/N] ").strip().lower() not in ("y", "yes"):
            print("[i] 已取消。")
            return None
    return {"id": act["id"], "at": _at_or_now(act["sign_start"]),
            "original_at": act["sign_start"], "name": act["name"]}



def retry_anchor(at_ts, now=None):
    """重试窗口起算点；必须在 page.goto 之前取值，之后不再移动。

    未来场次归一到原定 sign_start（等待开放后起算）；已开放场次固定为
    worker 启动时刻——页面加载慢不得把用户配置的重试窗口整体后移。
    配套用法：compute_retry_window(anchor, ..., now=anchor) 把 retry_start
    钉在 anchor 上，等待和首次刷新都不改变 deadline。
    """
    now = default_now() if now is None else now
    return at_ts if at_ts and at_ts > now else now


# 「直到报名截止」缺 sign_end 时的兜底窗口：绝不无限运行
DEFAULT_RETRY_WINDOW_S = 120.0


def compute_retry_window(at_ts, sign_end_ts, retry_for_s, now=None):
    """计算重试起算点与有效截止（纯函数，跨层单测共用同一口径）。

    未来活动从 sign_start 起算；已开放活动从本进程启动（≈用户启动计划）起算。
    - retry_for_s > 0：用户选的持续时长，sign_end 更早时被截断；
    - retry_for_s == 0：持续到 sign_end；sign_end 缺失回退默认 120 秒。
    返回 (retry_start, deadline, binding)，binding 是最终起约束的边界
    （duration / sign_end / fallback），终态归因用它。
    """
    now = default_now() if now is None else now
    retry_start = at_ts if at_ts and at_ts > now else now
    if retry_for_s and retry_for_s > 0:
        duration_end = retry_start + retry_for_s
        if sign_end_ts and sign_end_ts < duration_end:
            return retry_start, sign_end_ts, "sign_end"
        return retry_start, duration_end, "duration"
    if sign_end_ts:
        return retry_start, sign_end_ts, "sign_end"
    return retry_start, retry_start + DEFAULT_RETRY_WINDOW_S, "fallback"


def attempts_allowed(attempts: int, max_attempts: int) -> bool:
    """max_attempts=0 表示不限；否则已发起的 signUp 次数必须小于上限才能再点。"""
    return max_attempts <= 0 or attempts < max_attempts


# 提交失败后的下一轮节奏（RC-05）：学校回「人数已满」时，固定 2 秒连打
# 只会在几秒内把次数烧光。2 秒起、1.5 倍退避、8 秒封顶，既给名额释放留时间，
# 又不至于整段窗口只试一两次。等待仍受 RetryBudget（含 sign_end）钳制。
SUBMIT_BACKOFF_START_S = 2.0
SUBMIT_BACKOFF_MAX_S = 8.0
SUBMIT_BACKOFF_FACTOR = 1.5


def next_submit_gap(current: float) -> float:
    """返回下一次提交前的等待秒数；输入非法时从起点重新起算。"""
    try:
        value = float(current)
    except (TypeError, ValueError):
        value = SUBMIT_BACKOFF_START_S
    if value < SUBMIT_BACKOFF_START_S:
        value = SUBMIT_BACKOFF_START_S
    return min(SUBMIT_BACKOFF_MAX_S, value * SUBMIT_BACKOFF_FACTOR)


def wait_before_next_submit(budget, gap: float) -> float:
    """按剩余预算截断等待，返回下一轮间隔（不越过 sign_end / 时长上限）。"""
    try:
        remain = budget.remaining_s()
    except ClockUnavailable:
        raise
    budget.sleep(min(max(0.0, float(gap)), max(0.0, remain)))
    return next_submit_gap(gap)


# ---------------------------------------------------------------------------
# 黄金风暴 / 常规全速 / 404 自适应（2026-10-28 用户确认方案）
#
# 开放瞬间 0~1.6 秒内最多 10 轮、最小间隔 90ms；随后 180ms 常规全速；
# 连续 3 次 404 视为接口未就绪/限流，1.6 倍指数退避、上限 3 秒，并把有效
# 窗口自动扩到起点起 25 分钟；收到非 404 真实响应立即切回全速。
# sign_end 永远是硬截止，扩窗不得越过它。
#
# 旧 RC-05（2 秒起、1.5 倍、8 秒封顶）纯函数保留在上方不动：test_retry_policy
# 直接锁定它们的数值契约，本文件外的测试不可在本轮改动范围内。
# ---------------------------------------------------------------------------

GOLDEN_WINDOW_S = 1.6            # 开放后黄金窗口
GOLDEN_INTERVAL_S = 0.09         # 黄金阶段最小间隔（90ms）
GOLDEN_ROUNDS = 10               # 黄金阶段最多轮次
FULL_SPEED_INTERVAL_S = 0.18     # 常规全速最小间隔（180ms）
NOT_READY_404_LIMIT = 3          # 连续 404 视为接口未就绪
BACKOFF_FACTOR = 1.6             # 自适应退避倍率
BACKOFF_MAX_S = 3.0              # 自适应退避上限
ADAPTIVE_WINDOW_S = 1500.0       # 自适应扩窗上限（25 分钟）
# --- v4 直发核心：调度账本与降级地板（§3/§5/§12）---
SERVER_ERROR_MIN_GAP_S = 0.5     # 5xx 之后的最小间隔地板
ADAPTIVE_IN_GOLDEN = False       # golden 内默认不进 adaptive（真实 200 才退黄金）
MAX_INFLIGHT = 2                 # 物理在途请求上限（直发与 click 共用）
INFLIGHT_TTL_S = 8.0             # 非 lost pending 超过此时长不再占发射名额
INFLIGHT_WAIT_S = 0.05           # 名额满时原地泵的步长
CONFIRM_INFLIGHT_WAIT_S = 1.0    # 确认点击前名额满时最多原地等待的秒数
NOT_READY_SLOWDOWN_AFTER_S = 20.0  # 纯 404 段持续超过此时长开始降速
NOT_READY_SLOW_GAP_S = 0.8         # 纯 404 慢未就绪的最小间隔地板


class BurstPolicy:
    """黄金风暴/常规全速/404 自适应的显式状态机与真实请求记账。

    纯调度与记账，不做任何 I/O：时间读数全部走注入的 clock（单测里是虚拟
    时钟，绝不真实 sleep 1.6 秒）。四个阶段 golden / full_speed /
    adaptive_backoff / terminal 的转移规则见类常量上方注释。

    不变量：
    * attempts 只数真正发出过的 signUp 请求，单调递增；
    * pending 里每笔都有唯一 request_id，响应按归属结算，乱序合法；
    * 同一 request 重复结算只算一次；别家响应不改本场结论；
    * effective_deadline 只会因自适应扩窗变大，且永不越过 sign_end；
    * hard_deadline 一旦冻结就不再改写，sign_end 截止永远优先于扩窗；
    * 进入 terminal 后拒绝再发起任何动作（用户停止同此）；
    * 登记序 == 发送序（attempt_no 严格递增），倒扫 _order 即倒序扫发送序
      ——这是乱序结算安全（F06 5xx 地板、连击重算）的前提。
    """

    PHASE_GOLDEN = "golden"
    PHASE_FULL_SPEED = "full_speed"
    PHASE_ADAPTIVE = "adaptive_backoff"
    PHASE_TERMINAL = "terminal"

    def __init__(self, open_ts, base_deadline, sign_end_ts=None,
                 max_attempts=0, clock=None, retry_start=None,
                 allow_extend=True):
        self._clock = clock
        self.open_ts = float(open_ts)
        self.retry_start = float(retry_start if retry_start is not None else open_ts)
        self.sign_end_ts = float(sign_end_ts) if sign_end_ts else None
        self.max_attempts = int(max_attempts or 0)
        self.hard_deadline = float(base_deadline)
        if self.sign_end_ts:
            self.hard_deadline = min(self.hard_deadline, self.sign_end_ts)
        self.effective_deadline = self.hard_deadline
        self.attempts = 0
        self.streak_404 = 0
        self.golden_rounds = 0
        self._phase = self.PHASE_GOLDEN
        self._backoff_s = FULL_SPEED_INTERVAL_S
        self._requests = {}
        self._seq = 0
        self._stopped = False
        # 显式 --retry-for 时禁扩窗（用户给的秒数是硬承诺，不被自适应拉长）
        self._allow_extend = bool(allow_extend)
        # §5 5xx 降速地板（倒扫派生，见 _derive_min_gap）
        self._min_gap = 0.0
        # 发送序（只追加，绝不重排/删除）与相关缓存
        self._order = []
        self._last_attempt_no = 0
        self._level_prev = False            # 上一次 _apply_level 的 ungated 电平
        self._seg_has_throttled = False     # 缓存：最新连击段内是否出现 429
        self._seg_oldest_issued = None      # 缓存：最新连击段最旧 issued_at

    # -- 时间读数 ---------------------------------------------------------
    def _now(self):
        if self._clock is None:
            import time as _time
            return _time.monotonic()
        return float(self._clock.monotonic() if hasattr(self._clock, "monotonic")
                     else self._clock())

    def _wall_now(self):
        """业务墙上时钟读数：只认 clock.now()（业务 epoch），不回退墙钟。

        clock 不可用时抛 ClockUnavailable（由上层统一转 unconfirmed），
        绝不静默回退 time.time()——那会让扩窗/截止在时钟失效时偷偷按真实
        墙钟走，破坏「时钟不可用即停止发射」的契约。
        """
        if self._clock is None:
            import time as _time
            return _time.time()
        if hasattr(self._clock, "now"):
            now = self._clock.now()
            return float(now) if now is not None else time.time()
        return float(self._clock())

    def _elapsed(self):
        """单调时钟读数（issued_at/TTL/慢地板用），永不抛 ClockUnavailable。"""
        return float(self._clock.elapsed_s()) if self._clock is not None else self._now()

    # -- 发送序倒扫（乱序安全的核心） ---------------------------------------
    def _recompute_streak(self):
        """按发送序（新→旧）倒扫 _order，返回最新连击段的 (长度, 含429, 最旧issued_at)。

        与登记序同口径：首个已结算的非 404/throttled（更新的 real/server_error）
        断连击——止步于此，更旧的段不看。pending/lost 跳过（不参与判定）。
        """
        n = 0
        has_throttled = False
        oldest_issued = None
        for rid in reversed(self._order):
            rec = self._requests[rid]
            if rec["status"] != "settled":
                continue                     # pending / lost：不看
            if rec["kind"] not in ("404", "throttled"):
                break                        # 更新的 real/5xx：段已断
            n += 1
            if rec["kind"] == "throttled":
                has_throttled = True
            issued = rec.get("issued_at")
            if issued is not None and (oldest_issued is None or issued < oldest_issued):
                oldest_issued = issued
        return n, has_throttled, oldest_issued

    def _derive_min_gap(self):
        """5xx 降速地板：按发送序倒扫，最新「有定论」的记录说了算（§5/F06）。

        倒扫取最新有定论者，天然乱序安全：
        - server_error（5xx）→ 0.5；
        - 404 且状态码==404（未就绪）或 real 且状态码==200（干净成功）→ 0；
        - throttled(429) / 非200 real / 无状态码 bare404 → 跳过（看更旧）；
        - 全部跳过（从未有定论）→ 0。
        替代 feature 的事件式 _note_min_gap/_clear_min_gap（旧 5xx 晚到会在新
        200 之后错误置 0.5 的 F06 缺陷）。
        """
        for rid in reversed(self._order):
            rec = self._requests[rid]
            if rec["status"] != "settled":
                continue                     # pending/lost：跳过
            kind = rec["kind"]
            status = rec.get("http_status")
            if kind == "server_error":
                return SERVER_ERROR_MIN_GAP_S
            if kind == "404" and status == 404:
                return 0.0
            if kind == "real" and status == 200:
                return 0.0
            # throttled / non-200 real / bare 404 → 跳过，看更旧
            continue
        return 0.0

    # -- 阶段与调度 -------------------------------------------------------
    @property
    def phase(self):
        self._apply_level()
        return self._phase

    def current_phase(self):
        """只读返回当前 phase，不触发 _apply_level（回调路径不读时钟）。"""
        return self._phase

    def _golden_time_up(self):
        """黄金墙钟是否已到点；clock 不可用时返回 False（保守留在 golden）。"""
        try:
            return self._wall_now() >= self.open_ts + GOLDEN_WINDOW_S
        except Exception:
            return False

    def _apply_level(self):
        """电平/边沿两层重算：streak、5xx 地板、种子边沿、golden 退出、phase。

        电平：当前事实直接决定 level（ungated = streak>=3 且段内含 429）。
        边沿：level 从 false→true 才给退避种子，true→false 且非 ADAPTIVE 才清——
        否则单个 429 每次重算都重置退避/清种子，把种子语义打碎。
        """
        if self._stopped or self._phase == self.PHASE_TERMINAL:
            return
        n, has_throttled, oldest_issued = self._recompute_streak()
        self.streak_404 = n
        self._seg_has_throttled = has_throttled
        self._seg_oldest_issued = oldest_issued
        self._min_gap = self._derive_min_gap()
        ungated = n >= NOT_READY_404_LIMIT and has_throttled
        # 边沿：false→true 给种子；true→false 且非 ADAPTIVE 清种子
        if ungated and not self._level_prev:
            self._backoff_s = self._seed_backoff()
        elif (not ungated and self._level_prev
              and self._phase != self.PHASE_ADAPTIVE):
            self._backoff_s = FULL_SPEED_INTERVAL_S
        self._level_prev = ungated
        # golden：默认不进 adaptive（ADAPTIVE_IN_GOLDEN=False）；轮数/墙钟到点退位
        if self._phase == self.PHASE_GOLDEN:
            if self.golden_rounds >= GOLDEN_ROUNDS or self._golden_time_up():
                self._exit_golden()
            return
        self._refresh_phase(ungated)

    def _exit_golden(self):
        """golden 的唯一出口：退到 FULL_SPEED，按当前事实决定是否带种子进 adaptive。

        统一从这里退出，避免路径分叉把 golden 内累积的种子清掉。
        """
        if self._phase != self.PHASE_GOLDEN:
            return
        self._phase = self.PHASE_FULL_SPEED
        n, has_throttled, _ = self._recompute_streak()
        ungated = n >= NOT_READY_404_LIMIT and has_throttled
        self._refresh_phase(ungated)
        if ungated and self._backoff_s == FULL_SPEED_INTERVAL_S:
            self._backoff_s = self._seed_backoff()

    def _refresh_phase(self, ungated):
        """按已重算的 ungated 电平刷 phase。gated 必须在退出 golden 之后判定。

        ADAPTIVE_IN_GOLDEN=False（§3 常量开关）⇒ gated 的第二项恒假，即 golden
        内永不进 ADAPTIVE；改成 True 才会让 golden 内的段也切 phase（死分支，
        留着是为了让开关真的能切，不是遗留代码）。
        """
        in_golden_ok = ADAPTIVE_IN_GOLDEN if self._phase == self.PHASE_GOLDEN else True
        if ungated and in_golden_ok:
            if self._phase != self.PHASE_ADAPTIVE:
                self._phase = self.PHASE_ADAPTIVE
        else:
            if self._phase == self.PHASE_ADAPTIVE:
                self._phase = self.PHASE_FULL_SPEED
                self._backoff_s = FULL_SPEED_INTERVAL_S

    def _seed_backoff(self):
        """退避种子值：一段含 throttled 的连击降速后从这里起步。"""
        return min(BACKOFF_MAX_S, FULL_SPEED_INTERVAL_S * BACKOFF_FACTOR)

    def interval_s(self):
        """下一次尝试前必须等待的最小间隔（base / 5xx 地板 / 纯404慢地板 取 max）。"""
        self._apply_level()
        if self._phase == self.PHASE_GOLDEN:
            base = GOLDEN_INTERVAL_S
        elif self._phase == self.PHASE_ADAPTIVE:
            base = min(BACKOFF_MAX_S, max(FULL_SPEED_INTERVAL_S, self._backoff_s))
        else:
            base = FULL_SPEED_INTERVAL_S
        # §12：纯 404 段一直未就绪（服务器晚开几分钟）时降速到 0.8s 地板——
        # 段内一旦混进 throttled 就交给退避，出现 real 立即随连击清零而解除。
        slow_floor = 0.0
        if (self.streak_404 > 0 and not self._seg_has_throttled
                and self._seg_oldest_issued is not None):
            age_s = self._elapsed() - self._seg_oldest_issued
            if age_s > NOT_READY_SLOWDOWN_AFTER_S:
                slow_floor = NOT_READY_SLOW_GAP_S
        # §5：5xx 后的最小间隔地板，与退避/黄金/慢未就绪取 max
        return max(base, self._min_gap, slow_floor)

    def next_interval_after_wait(self):
        """等待完本次间隔后推进退避计数，返回下一轮间隔。"""
        if self._phase == self.PHASE_ADAPTIVE:
            if self._level_prev and self._backoff_s == FULL_SPEED_INTERVAL_S:
                # 段内种子边沿已消耗（_backoff_s 已是 seed），此处只兜底
                # 进 ADAPTIVE 时按 level 重放种子，否则从全速起步被错算成「无退避」
                self._backoff_s = self._seed_backoff()
            self._backoff_s = min(BACKOFF_MAX_S,
                                  max(FULL_SPEED_INTERVAL_S, self._backoff_s) * BACKOFF_FACTOR)
        return self.interval_s()

    def can_attempt(self):
        """是否还允许发起新的一次提交（次数/截止/停止都算）。"""
        if self._stopped or self._phase == self.PHASE_TERMINAL:
            return False
        if self.attempts >= self.max_attempts > 0:
            return False
        if self._wall_now() >= self.effective_deadline:
            return False
        return True

    def extend_deadline_for_adaptive(self):
        """404 连击触发自适应扩窗：有效截止拉到起点起 25 分钟，sign_end 仍硬截。

        显式 --retry-for 时（allow_extend=False）整条扩窗路径不生效：
        用户给的秒数是硬承诺，不能被自适应偷偷拉长。
        """
        if not self._allow_extend:
            return self.effective_deadline
        target = self.retry_start + ADAPTIVE_WINDOW_S
        if self.sign_end_ts:
            target = min(target, self.sign_end_ts)
        if target > self.effective_deadline:
            self.effective_deadline = target
        return self.effective_deadline

    # -- 请求记账 ---------------------------------------------------------
    def register_request(self):
        """为一笔真实发出的 signUp 请求分配唯一 ID 并入账。"""
        self._seq += 1
        request_id = "req-%d" % self._seq
        attempt_no = self.attempts + 1
        # 不变量：attempt_no 严格递增 ⇒ 登记序 == 发送序，倒扫 _order 才等价
        # 按发送顺序从新到旧（乱序结算的安全前提）。本方法跑在回调里，
        # assert 被 -O 剥掉或异常被吞都会漏账，所以只记日志不抛。
        if attempt_no <= self._last_attempt_no:
            log("attempt_no 未递增（%s → %s）：倒序重算不再等于发送序"
                % (self._last_attempt_no, attempt_no))
        self._last_attempt_no = attempt_no
        # issued_at 只在登记时读一次单调时钟（永不抛 ClockUnavailable），
        # 供纯 404 慢地板（段起点 = 段最旧 issued_at）与 inflight TTL 复用。
        self._requests[request_id] = {"status": "pending", "kind": None,
                                      "attempt_no": attempt_no,
                                      "issued_at": self._elapsed()}
        self._order.append(request_id)   # 只追加，绝不重排/删除
        self.attempts += 1
        if self._phase == self.PHASE_GOLDEN:
            self.golden_rounds += 1
        return request_id

    def settle(self, request_id, kind, http_status=None):
        """结算一笔请求的响应归属；kind ∈ {404, throttled, server_error, real, lost}。

        乱序合法：响应可按任意顺序回来；重复结算同一笔只认第一次。
        - 404：计入连击段；连击达阈值只扩窗，不切 phase（§4）；
        - throttled：计入连击段、连击达阈值给退避种子（含 throttled 才给），
          但不结束 golden（§4）；单个 429 只结算，不单独触发退避；
        - server_error：断开连击、设 5xx 地板（§5），永不切 phase；
        - real：断开连击；http_status==200 时结束 golden 并回 FULL_SPEED（§11），
          非 200 real 只断连击、不结束 golden；
        - lost：不结算，请求保持未决，终态如实报「未确认」。
        http_status 记原始 HTTP 状态码，供 §5/§11 判定。
        """
        record = self._requests.get(request_id)
        if record is None or record["status"] == "settled":
            return None
        if http_status is not None:
            record["http_status"] = http_status
        if kind == "lost":
            record["kind"] = "lost"
            return record          # 不改 status，仍是 pending
        record["status"] = "settled"
        record["kind"] = kind
        # prev 必须在 _apply_level 之前取：_apply_level 会把重算结果写回 streak_404
        prev_len = self.streak_404
        # 连击/地板/种子/phase 全部交给 _apply_level：按发送顺序倒序重算（乱序安全），
        # 不再在 settle 里做 +=1 / =0——旧 404 后到不得重建连击，非 200 real 断连击但不退黄金（§11）。
        self._apply_level()
        if kind == "real" and http_status == 200 and self._phase == self.PHASE_GOLDEN:
            # §11：200-real 事件驱动结束 golden（接口已就绪，与它是否清连击无关）。
            self._exit_golden()
        # 扩窗边沿只看长度（§4「纯 404 段不切 phase，只扩窗」）；种子边沿在 _apply_level
        self._maybe_extend_window(prev_len, self.streak_404)
        return record

    def _maybe_extend_window(self, prev_len, new_len):
        """扩窗边沿：只看连击长度（404 与 throttled 同等计入），new>prev 每增长一次就扩一次。

        判据刻意不含 has_throttled——纯 404 也照常扩窗（§4），纯 404 的 20s→0.8s
        慢地板依赖这个能力。extend_deadline_for_adaptive 幂等且被 sign_end 硬截；
        显式 --retry-for 时整条路径不生效。
        """
        if new_len >= NOT_READY_404_LIMIT and new_len > prev_len:
            self.extend_deadline_for_adaptive()

    def pending_ids(self):
        return [rid for rid, rec in self._requests.items()
                if rec["status"] == "pending"]

    def pending_count(self):
        return len(self.pending_ids())

    def inflight_count(self):
        """占发射名额的在途请求数（§2）：pending、非 lost、且未超 INFLIGHT_TTL_S。

        lost 不占名额——页面重载/导航中止只发 requestfailed，若继续占名额，
        名额会一点点占满直到永久不发。pending 超过 INFLIGHT_TTL_S 也不占名额
        （等不回来的请求不该堵死发射），但它仍是 pending：终态如实报「未确认」，
        迟到响应照常结算。
        """
        now = self._elapsed()
        count = 0
        for rec in self._requests.values():
            if rec["status"] != "pending" or rec["kind"] == "lost":
                continue
            issued = rec.get("issued_at")
            if issued is not None and now - issued >= INFLIGHT_TTL_S:
                continue
            count += 1
        return count

    def settled_kind(self, request_id):
        rec = self._requests.get(request_id)
        return rec["kind"] if rec and rec["status"] == "settled" else None

    # -- 终态 -------------------------------------------------------------
    def stop(self, reason):
        """进入终态；之后 can_attempt() 恒 False，不再发起本场任何动作。"""
        self._stopped = True
        self._phase = self.PHASE_TERMINAL
        self.stop_reason = reason
        return reason

    @property
    def stopped(self):
        return self._stopped


def _classify_response(response):
    """把一次 signUp 响应归类为五档：404 / throttled / server_error / real / lost。

    - 404：接口未就绪（还没开放报名）；
    - 429→throttled：限流信号，据此进退避种子；
    - >=500→server_error：服务端异常（网关/超载），当 real 断 404 连击但需 5xx 地板降速；
    - 其余（200+JSON、400 等业务响应）→real：必须结束黄金阶段；
    - 无响应→lost：请求保持未决，终态如实报「未确认」。
    5xx 常是 HTML 错误页，不能按「读不出就 lost」处理（会永久挂 pending）。
    """
    if response is None:
        return "lost"
    status = getattr(response, "status", 200)
    try:
        status_code = int(status)
    except (TypeError, ValueError):
        status_code = 200
    if status_code == 404:
        return "404"
    if status_code == 429:
        return "throttled"
    if status_code >= 500:
        return "server_error"
    return "real"


def _decode_body(raw):
    """字节响应体 → dict。先严格 utf-8，失败再 gb18030（学校接口多为 GBK 系）。

    解不出时返回 {}（等价 code=None），绝不抛异常——否则 HTML 错误页会把
    结算路径炸掉、请求永久 pending。
    """
    if not raw:
        return {}
    for encoding in ("utf-8", "gb18030"):
        try:
            text = raw.decode(encoding) if isinstance(raw, (bytes, bytearray)) else str(raw)
            data = json.loads(text)
            return data if isinstance(data, dict) else {}
        except (UnicodeDecodeError, ValueError):
            continue
    return {}


def _ts_text(ts) -> str:
    """秒级时间戳 → 学校时区的完整时间文本；None 显示 '-'（结构化日志用）。"""
    if not ts:
        return "-"
    return school_time_text(ts)


def _checkpoint_event(kind, at=None):
    """结构化 checkpoint 行：kind 只取 worker_started/page_opened/watch_started。

    父层必须用这里记录的源事件时间，不能用读到这一行的接收时刻——
    日志转发慢 2 秒时，父层接收时刻会把「页面已打开」整体推后 2 秒（R10）。
    """
    if at is None:
        try:
            at = business_now()       # 源事件时间按业务北京时间记录
        except ClockUnavailable:
            return                    # 没有可信时钟就不写时间节点，不用本机时间补
    at = float(at)
    payload = {"kind": kind, "at": round(at, 3)}
    meta = clock_meta()
    if meta:
        payload["clock"] = meta
    log("[checkpoint] " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _retry_event(**fields):
    """进度型 [retry] 行：只带进度字段，绝不带 stop_reason（终态才有一条）。"""
    body = " ".join("%s=%s" % (key, value) for key, value in fields.items())
    log("[retry] " + body)


PREFLIGHT_BUDGET_S = 30.0


class _SubmitAborted(RuntimeError):
    """确认前的本地拦截：条件已不满足，绝不点「确定」。"""


def probe_token(budget=None):
    """登录探测四态：valid / expired / none / unknown。

    只有学校明确回 4001 才是 expired；网络不通、超时、5xx、认不出的返回码
    都归 unknown——把接口异常说成“登录失效”会诱导用户重登，而重登解决不了网络。
    """
    tok = token()
    if not tok:
        return "none", "本机还没有登录过（data/session.json 不存在）"
    data = _post("/rjyHd/listActivity",
                 {"pageSize": 1, "pageNum": 1, "hdmc": "", "hdzt": None,
                  "kssj": "", "jssj": ""}, tok, budget=budget)
    code = data.get("code")
    if code == 1:
        return "valid", "ok"
    if str(code) == "4001":
        return "expired", "登录态已过期（%s）" % (data.get("message") or "身份验证失败")
    return "unknown", "接口异常：%s" % (data.get("message") or code)


def check_token(budget=None):
    """用活动列表接口判定登录态：code=1 有效，4001 表示 token 失效。"""
    state, detail = probe_token(budget)
    return state == "valid", detail


def _known_policy_start(args):
    """GUI 传入的冻结起点；没有则 None（CLI 需要先查目标）。"""
    raw = getattr(args, "retry_start", 0) or 0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _policy_window(start, sign_end_ts, retry_for):
    _, deadline, binding = compute_retry_window(start, sign_end_ts, retry_for, now=start)
    return deadline, binding


def cmd_grab(args):
    from playwright.sync_api import sync_playwright
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    # 入口即建立独立北京时间：worker 读父进程快照，CLI 有界自校时（决策 012）
    clock = setup_business_clock(allow_sync=True)
    if clock is None:
        # 没有可信北京时间：自动报名拒绝启动；不动凭证、不判登录失效
        log("[X] 北京时间未校准，本次自动报名不启动")
        print("\n[X] 暂时无法获取可信北京时间，自动报名没有开始。")
        print("    登录态与活动列表不受影响；稍后重试。")
        return 3
    # 入口即记起点：之后任何鉴权、拉列表、开页都消耗这段预算。
    # 起点与 checkpoint 都用业务北京时间；绝不再写系统墙钟 epoch。
    try:
        entry_epoch = business_now()   # 业务起点只取一次：开始后的校时/开页不后移 instant 窗口
    except ClockUnavailable:
        log("[X] 北京时间在入口处不可用，本次自动报名不启动")
        print("\n[X] 暂时无法获取可信北京时间，自动报名没有开始。")
        return 3
    _checkpoint_event("worker_started", entry_epoch)
    retry_for = args.retry_for if args.retry_for is not None else args.window
    try:
        sign_end_ts = parse_ts(args.sign_end) if args.sign_end else None
    except ValueError:
        print("[X] 报名截止时间格式不对：%r" % (args.sign_end,))
        return 2
    try:
        at_ts = parse_ts(args.at) if args.at else None
    except ValueError:
        print("[X] 报名开始时间格式不对：%r" % (args.at,))
        return 2

    frozen = _known_policy_start(args)
    if frozen is None and at_ts is not None:
        # CLI 直接给了开放时间：入口即冻结起点，后面查不查目标都不影响窗口
        frozen = retry_anchor(at_ts, entry_epoch)

    display = getattr(args, "name", "") or "这一场活动"
    # 已观察事实：attempts 只数真实请求；pending 是「已发出但还没有确定结果」的请求数
    state = {"attempts": 0, "pending": 0, "last_reason": "", "resp_ok": False,
             "login_invalid": False, "deadline": 0.0, "binding": ""}
    done = {"value": False}

    def terminal(stop_reason):
        """唯一结构化终态：恰一条 [retry] kind=done，随后给人读结论与退出码。"""
        if done["value"]:
            return 2
        done["value"] = True
        log("[retry] kind=done attempts=%d stop_reason=%s retry_deadline=%d sign_end=%d "
            "last_reason=%s pending=%d streak_404=%d phase=%s"
            % (state["attempts"], stop_reason, int(state["deadline"]),
               int(sign_end_ts or 0), (state["last_reason"] or "")[:40],
               state.get("pending", 0), state.get("streak_404", 0),
               state.get("phase", "")))
        attempts, last = state["attempts"], state["last_reason"]
        if stop_reason == "success":
            log("[OK] 报名成功（尝试 %d 次）" % attempts)
            print("\n[OK] 报名成功：%s" % display)
            print("     不想去请尽快在活动页取消，别占名额。")
            notify("报名成功", ["**%s**" % display,
                                "报名时间：%s" % school_time_text(now_fn=business_now),
                                "如不想参加，请尽快在活动页取消。"])
            return 0
        if stop_reason == "already_signed":
            print("[i] 你已经报过这个活动了。")
            return 0
        if stop_reason == "login_invalid":
            log("[X] 登录失效（尝试 %d 次）" % attempts)
            print("\n[X] 登录失效：%s" % (last or "学校要求重新登录"))
            print("    重试没有意义，请先重新登录再跑。")
            return 3
        if stop_reason == "auth_unavailable":
            # 网络/接口无法确认 ≠ 登录失效：不指导用户重登
            log("[X] 登录状态暂时无法确认（%s）" % (last or "接口没有应答"))
            print("\n[X] 登录状态暂时无法确认：%s" % (last or "接口没有应答"))
            print("    先检查网络或校园网，再点「刷新」重试；不要急着重新登录。")
            return 3
        if stop_reason == "max_attempts":
            log("[X] 达到最大提交次数 %d（最近原因 %s）" % (attempts, last))
            print("\n[X] 已提交 %d 次仍没成功：%s" % (attempts, last or "名额已满"))
            print("    到达你设置的最大提交次数，停止重试。")
            return 2
        if stop_reason == "unconfirmed":
            # 已发出的请求撤不回来：不能声称没抢到，也不能声称成功
            log("[X] 结果未确认（已发起 %d 次提交，仍有 %d 次没有确定结果）"
                % (attempts, state["pending"]))
            print("\n[?] 结果未确认：提交已经发出，但没有拿到学校的确定结果。")
            print("    请打开活动页核对是否已报名；不要直接重复提交。")
            notify("报名结果未确认", ["**%s**" % display,
                                      "提交已发出但结果未确认，请到活动页核对。"], "orange")
            return 2
        if stop_reason == "clock_unavailable":
            log("[X] 北京时间暂不可用，已停止自动动作（尝试 %d 次）" % attempts)
            print("\n[X] 暂时无法获取可信北京时间，已停止自动报名动作。")
            print("    已发出的提交不受影响，请到活动页核对；不要直接重复提交。")
            return 3
        if stop_reason == "process_error":
            log("[X] 本机流程异常结束（%s）" % (last or "未知异常"))
            print("\n[X] 本机流程异常结束：%s" % (last or "未知异常"))
            print("    这不代表学校拒绝了报名；请到活动页核对后再决定。")
            return 2
        log("[X] 报名失败（尝试 %d 次，最近原因 %s）"
            % (attempts, last or "名额已满/未开放"))
        tail_note = "报名已截止" if stop_reason == "sign_end" else "重试时长到了"
        print("\n[X] 没抢到：%s（%s）" % (last or "名额已满／页面未放开", tail_note))
        print("    建议手动打开活动页确认，必要时再试一次。")
        notify("自动报名失败", ["**%s**" % display,
                                "原因：%s（%s）" % (last or "名额已满/未开放", tail_note),
                                "建议手动确认。"], "orange")
        return 2

    # 预算必须在任何阻塞之前建立：已知起点时窗口已过就零网络、零浏览器
    preflight_only = frozen is None

    def _clock_stop():
        """校时在流程中途失效：已发出的未决请求保留为「未确认」。

        零提交才是 clock_unavailable；有 pending 时不能声称没抢到，
        也不能把校时失败说成学校拒绝。
        """
        state["last_reason"] = "北京时间暂不可用"
        if state["pending"] > 0:
            return terminal("unconfirmed")
        return terminal("clock_unavailable")

    try:
        if preflight_only:
            # CLI 没给开放时间：只给“查目标”一段有界前置预算，绝不无限查询
            budget = RetryBudget(entry_epoch + PREFLIGHT_BUDGET_S,
                                 business_clock=clock)
        else:
            deadline, binding = _policy_window(frozen, sign_end_ts, retry_for)
            state["deadline"], state["binding"] = deadline, binding
            budget = RetryBudget(deadline, business_clock=clock)
            if budget.expired():
                state["last_reason"] = "重试窗口在启动前已结束"
                return terminal("sign_end" if binding == "sign_end" else "retry_duration")
    except ClockUnavailable:
        return _clock_stop()

    def _budget_stop(phase):
        state["last_reason"] = "%s时重试窗口已到点" % phase
        return terminal("sign_end" if state["binding"] == "sign_end" else "retry_duration")

    # 鉴权也要受限：固定 15 秒会把 2 秒窗口直接撑破
    try:
        token_state, token_detail = probe_token(budget)
    except BudgetExhausted:
        return _budget_stop("鉴权")
    except ClockUnavailable:
        return _clock_stop()
    if token_state == "none":
        print("[X] %s" % token_detail)
        print("    请先运行：python grab.py login")
        return 3
    if token_state == "expired":
        state["last_reason"] = token_detail
        return terminal("login_invalid")
    if token_state == "unknown":
        state["last_reason"] = token_detail
        return terminal("auth_unavailable")

    # 粗校时只作提示；剩余太少就跳过，不能为一句话花掉窗口
    try:
        remaining_before_sample = budget.remaining_s()
    except ClockUnavailable:
        return _clock_stop()
    if remaining_before_sample > 1.5:
        try:
            sample = clock_sample(timeout_s=budget.timeout_s(6))
        except BudgetExhausted:
            return _budget_stop("粗校时")
        except ClockUnavailable:
            return _clock_stop()
        log("[clock] " + json.dumps(sample, ensure_ascii=False, separators=(",", ":")))
        if sample.get("state") == "warn":
            print("[!] 学校 HTTP Date 粗测与本机有偏差（含网络误差约 ±%.1f 秒）；"
                  "该粗测只作诊断参考，报名调度按工具的独立北京时间执行。"
                  % (sample.get("uncertainty_s") or 1.0))

    try:
        target = resolve_target(args, budget=budget)
    except BudgetExhausted:
        return _budget_stop("查目标")
    except ClockUnavailable:
        return _clock_stop()
    if not target:
        return 0
    display = target["name"]
    try:
        at_ts = parse_ts(target["at"])
    except ValueError:
        print("[X] 报名开始时间格式不对：%r" % (target["at"],))
        return 2

    if preflight_only:
        # 目标查到了：用入口时刻重新建正式预算，前置耗时算进窗口
        frozen = retry_anchor(at_ts, entry_epoch)
        deadline, binding = _policy_window(frozen, sign_end_ts, retry_for)
        state["deadline"], state["binding"] = deadline, binding
        try:
            budget = RetryBudget(deadline, business_clock=clock)
            if budget.expired():
                state["last_reason"] = "重试窗口在查目标时已结束"
                return terminal("sign_end" if binding == "sign_end" else "retry_duration")
        except ClockUnavailable:
            return _clock_stop()

    start = entry_epoch if args.dry_run else at_ts - max(0.0, float(args.lead or 0.0))
    print("[i] 目标：%s" % display)
    original_at = target.get("original_at") or ""
    instant_mode = bool(original_at and original_at != target["at"])
    if instant_mode:
        # 已开放场次没有「提前打开」这一步：执行起点就是 worker 入口时刻，
        # 不能说成「提前 100 秒打开页面」（RC-04）。
        print("[i] 活动已开放，本次立即报名/抢名额（原定开放 %s）" % original_at)
        print("[i] 本次执行：%s；失败重试窗口 %.0f 秒"
              % (target["at"], args.window))
    else:
        print("[i] 报名开始：%s；提前 %.0f 秒打开页面；失败重试窗口 %.0f 秒"
              % (target["at"], args.lead, args.window))
    if sign_end_ts:
        log("[i] 报名截止：%s；重试到 %s 截止"
            % (_ts_text(sign_end_ts), _ts_text(state["deadline"])))
    _retry_event(kind="window", retry_deadline=int(state["deadline"]),
                 sign_end=int(sign_end_ts or 0))

    # 前置阶段耗尽：不创建浏览器、不发任何请求（C02）
    try:
        if not args.dry_run and budget.expired():
            return _budget_stop("前置检查")
    except ClockUnavailable:
        return _clock_stop()

    # ---- 浏览器阶段：任何异常都收成唯一终态 ------------------------------
    try:
        with sync_playwright() as pw:
            browser = launch_browser(pw, headless=not args.headed)
            # 监听容器先于 try 建立：finally 里停用监听不会因未定义而失败
            armed = {"on": False, "dead": False}
            listeners = []
            burst = None    # goto 前异常时 _settle_pending/_finalize 安全空转
            try:
                ctx = new_context(browser)
                page = ctx.new_page()
                url = BASE + "/stu/" + DETAIL_HASH.format(aid=target["id"])
                page.goto(url, wait_until="domcontentloaded",
                          timeout=budget.timeout_ms(60000))
                # 页面渲染稳定最多等 1.5 秒，但绝不把短窗口整个吃掉
                budget.sleep(min(1.5, max(0.2, budget.remaining_s() * 0.25)))
                _checkpoint_event("page_opened")
                log("[i] 详情页已加载")

                if args.dry_run:
                    btns = page.eval_on_selector_all(
                        "uni-button", "els => els.map(e => e.innerText.trim()).filter(Boolean)")
                    log("[i] dry-run：joined=%s buttons=%s"
                        % (is_joined(target["id"], budget=budget), btns))
                    return 0

                # 前置核对与最终核对分开（F07）：这里查询失败不等于没报名，吞掉
                # 非时钟异常后照常进入盯按钮；最终核对的失败如实上抛。
                try:
                    joined = is_joined(target["id"], budget=budget)
                except ClockUnavailable:
                    raise
                except Exception:
                    joined = None
                if joined is True:
                    return terminal("already_signed")
                if joined is None:
                    state["last_reason"] = "已有报名查询未确认"

                if not args.headed:
                    print("[i] 后台等待中，到点自动提交；想看着它点就加 --headed。")
                wait_until(start, budget)

                # 只有真正还没到开放时刻才说「等待开放」；已开放场次（含原定
                # 时刻已过的立即模式）直接进入盯按钮，文案与真实行为一致（RC-04）。
                try:
                    now_entry = business_now()
                except ClockUnavailable:
                    return _clock_stop()
                if now_entry >= at_ts:
                    log("[i] 活动已开放，立即报名/抢名额")
                else:
                    log("[i] 开始等待开放")
                    if now_entry < at_ts - POLL_LEAD:
                        wait_until(at_ts - POLL_LEAD, budget)

                reload_gap = 2.0
                last_reload = 0.0
                stop_reason = ""
                # 黄金风暴状态机：开放时刻进入 10 轮 90ms，随后 180ms 全速；
                # 404 连击进退避并扩窗，非 404 立即回全速（2026-10-28 方案）
                burst = BurstPolicy(
                    open_ts=at_ts,
                    base_deadline=state["deadline"] or _policy_window(
                        frozen, sign_end_ts, retry_for)[0],
                    sign_end_ts=sign_end_ts,
                    max_attempts=args.max_attempts,
                    clock=clock,
                    retry_start=frozen or at_ts,
                    allow_extend=args.retry_for is None,
                )
                # 受控提交窗口：只承认本场盯按钮期间发出的 signUp 请求。
                # 学校真实接口是 GET signUp/<opaque-hash>，hash 不含活动 ID，
                # 因此不能再用「URL 里有 id」来认领（C01）。请求事件的派发可能
                # 晚于网络发起，窗口覆盖整个提交阶段才不会漏计真实请求；
                # 页面加载阶段的别家请求在监听注册之前，天然不入账。
                admitted_ids = set()
                seen_ids = set()
                admitted_all = []
                request_to_bid = {}
                response_queue = []

                # 直发（主）+ 点击（单向兜底）同一调度账本：直发仅当入口脚本为
                # 已知打包且 signer 可用时启用；否则全程点击。direct 字典始终存在，
                # 未启用时 downgrade=False 且 transport=None，对点击路径零影响。
                direct = {
                    "enabled": False,
                    "downgrade": False,
                    "protocol_stop": False,   # 通道/协议异常：停止新发，统一收尾
                    "signer": None,
                    "transport": None,
                    "run_id": os.urandom(8).hex(),
                    "seq": 0,
                    "ticket_to_bid": {},
                    "registry": {},
                    "reserved": 0,   # 已提交未 started：预留名额与次数配额
                    "queue": None,
                }

                def _detect_direct_signer():
                    """入口脚本恰为已知直发包且 signer 可用才返回 build_signup_hash。

                    只读已加载页面 script src，不下载脚本、不在热路径额外请求学校；
                    任何异常一律返回 None（点击兜底），不扰动既有流程。
                    """
                    try:
                        from signup_signing import build_signup_hash
                        srcs = page.eval_on_selector_all(
                            "script", "els => els.map(e => e.getAttribute('src') || '')")
                        for s in (srcs or []):
                            base = str(s).split("?")[0].rstrip("/").rsplit("/", 1)[-1]
                            if base == KNOWN_DIRECT_SCRIPT_BASENAME:
                                return build_signup_hash
                    except Exception:
                        return None
                    return None

                _signer = _detect_direct_signer()
                if _signer is not None:
                    try:
                        from signup_transport import DirectTransport
                    except Exception:
                        DirectTransport = None
                    if DirectTransport is not None:
                        direct["signer"] = _signer
                        direct["queue"] = queue.Queue()
                        direct["transport"] = DirectTransport(direct["queue"])
                        direct["enabled"] = True
                        log("[i] 入口脚本为已知直发包：启用直发（点击兜底保留）")

                def _is_signup_url(url_text):
                    return url_text.startswith(BASE) and "/rjyHdCyr/signUp/" in url_text

                def _sync_pending():
                    state["pending"] = burst.pending_count()
                    state["attempts"] = burst.attempts
                    state["streak_404"] = burst.streak_404
                    state["phase"] = burst.phase

                def on_request(request):
                    if armed["dead"] or not armed["on"]:
                        return
                    # 直发在途（未降级）时，页面自发的同源请求不入账本、不改结论
                    if direct["enabled"] and not direct["downgrade"]:
                        return
                    try:
                        url_text = str(request.url)
                    except Exception:
                        return
                    if not _is_signup_url(url_text):
                        return
                    ident = id(request)
                    if ident in admitted_ids or ident in seen_ids:
                        return          # 同一个 Request 重复通知只计 1
                    admitted_ids.add(ident)
                    seen_ids.add(ident)
                    admitted_all.append(request)
                    bid = burst.register_request()
                    request_to_bid[ident] = bid
                    _sync_pending()
                    state["last_reason"] = "已发起，等响应"
                    # 请求发起即上报，不等响应回来才让界面知道已提交
                    log("[i] 第 %d 次提交：已发起" % state["attempts"])
                    _retry_event(kind="submit", attempts=state["attempts"],
                                 request_id=bid, phase=burst.phase)

                def on_response(response):
                    # 回调只入队：结算统一走主循环的事件泵，不在回调里做 I/O
                    if armed["dead"]:
                        return
                    if direct["enabled"] and not direct["downgrade"]:
                        return
                    try:
                        url_text = str(response.url)
                    except Exception:
                        return
                    if not _is_signup_url(url_text):
                        return
                    response_queue.append(response)

                def on_requestfailed(request):
                    # 回调只入队：导航中止只发 requestfailed，没有 response，
                    # 交 _settle_failed 按既有 request_id 结算为 lost（立即释放名额）
                    if armed["dead"]:
                        return
                    if direct["enabled"] and not direct["downgrade"]:
                        return
                    try:
                        url_text = str(request.url)
                    except Exception:
                        return
                    if not _is_signup_url(url_text):
                        return
                    response_queue.append(("failed", request))

                def _sync_budget_window():
                    """扩窗同步（F05）：policy 截止变大时重建 RetryBudget 并广播事件。

                    纯 404 与 429 连击都会经 burst.settle→_maybe_extend_window 拉长
                    effective_deadline；这里统一把预算与事件对齐，sign_end 仍硬截。
                    """
                    nonlocal budget
                    if (burst is not None
                            and burst.effective_deadline > getattr(budget, "deadline_epoch", 0)):
                        budget = RetryBudget(burst.effective_deadline, business_clock=clock)
                        state["deadline"] = burst.effective_deadline
                        _retry_event(kind="window_extended",
                                     retry_deadline=int(burst.effective_deadline),
                                     sign_end=int(sign_end_ts or 0))

                def _apply_conclusion(payload):
                    """对一笔 real 应答的 body 结论：成功粘性 / 只认 str(code)==4001 / 版本不兼容单向降级。

                    click 与直发共用同一套结论语义，避免两条通道判据漂移。
                    """
                    code = payload.get("code") if isinstance(payload, dict) else None
                    message = ""
                    if isinstance(payload, dict):
                        message = str(payload.get("message") or "")
                    state["last_reason"] = "code:%s %s" % (code, message[:30])
                    # 成功结论粘性：只要有一笔 code=1 就是成功，后续失败响应不改写
                    if code == 1:
                        state["resp_ok"] = True
                    # 只认 str(code)=='4001'：message 子串/关键词不作数（避免误判）
                    if code is not None and str(code) == "4001":
                        state["login_invalid"] = True
                    # 版本不兼容：code 规范化'0'且 message 去末尾句号后精确匹配才降级（本场单向 click）
                    if code is not None and str(code).strip() == "0":
                        norm = message.strip()
                        if norm.endswith("。"):
                            norm = norm[:-1]
                        if norm == "版本不兼容，请刷新页面或清除缓存后重试" and not direct["downgrade"]:
                            direct["downgrade"] = True
                            log("[!] 版本不兼容：本场切换为点击兜底")

                def _settle_one(response):
                    """按 request_id 结算一笔在途响应；乱序合法，别家响应不改本场结论。"""
                    if burst is None:
                        return
                    resp_request = getattr(response, "request", None)
                    bid = None
                    if resp_request is not None:
                        bid = request_to_bid.get(id(resp_request))
                    elif burst.pending_count() == 1:
                        # 替身/旧驱动可能不给 response.request：只在唯一未决时认领
                        bid = burst.pending_ids()[0]
                    if bid is None:
                        state["last_reason"] = "收到其他请求的响应，本场结论未变"
                        return
                    raw_kind = _classify_response(response)
                    http_status = getattr(response, "status", None)
                    try:
                        http_status = int(http_status) if http_status is not None else None
                    except (TypeError, ValueError):
                        http_status = None
                    payload = None
                    if raw_kind == "real":
                        # 已得 HTTP 应答按 real 结算：200+HTML（学校错误页）不伪装
                        # lost；仅响应体完全读不出才降为 lost（保持未决，不假造结论）。
                        try:
                            raw = response.body()
                        except Exception:
                            raw_kind = "lost"
                        else:
                            payload = _decode_body(raw)
                    burst.settle(bid, raw_kind, http_status=http_status)
                    _sync_budget_window()
                    if raw_kind == "lost":
                        state["last_reason"] = "响应读取失败，请求仍未决"
                    elif raw_kind == "404":
                        state["last_reason"] = "404 接口未就绪/限流"
                        _retry_event(kind="not_ready", request_id=bid,
                                     streak_404=burst.streak_404,
                                     phase=burst.phase,
                                     retry_deadline=int(burst.effective_deadline))
                    elif raw_kind == "throttled":
                        state["last_reason"] = "429 限流"
                        _retry_event(kind="throttled", request_id=bid,
                                     streak_404=burst.streak_404,
                                     phase=burst.phase,
                                     retry_deadline=int(burst.effective_deadline))
                    elif raw_kind == "server_error":
                        state["last_reason"] = "5xx 服务端异常"
                        _retry_event(kind="server_error", request_id=bid,
                                     http_status=http_status, phase=burst.phase)
                    else:
                        _apply_conclusion(payload)
                    _sync_pending()
                    log("[i] 第 %d 次提交：%s" % (state["attempts"], state["last_reason"]))

                def _settle_failed(request):
                    """requestfailed：按既有 request_id 结算为 lost，立即释放名额。"""
                    if burst is None:
                        return
                    ident = id(request)
                    bid = request_to_bid.get(ident)
                    if bid is None and burst.pending_count() == 1:
                        bid = burst.pending_ids()[0]
                    if bid is None:
                        return
                    burst.settle(bid, "lost")
                    state["last_reason"] = "请求中止（requestfailed），仍未决"
                    _sync_pending()

                def _settle_pending():
                    _drain_direct()     # 直发通道与页面通道同一事件泵
                    while response_queue:
                        item = response_queue.pop(0)
                        if isinstance(item, tuple) and item and item[0] == "failed":
                            _settle_failed(item[1])
                        else:
                            _settle_one(item)
                    _sync_pending()

                def _settle_direct_event(ev):
                    """把一笔直发传输事件并入同一调度账本（started 登记 / completed 五档 / failed 保持未决）。

                    F03：先核对本场 run_id、已登记 request_id 与票据 attempt_no；
                    任何未知/跨场/不匹配事件无副作用——不释放他人预留、不改
                    policy/last_reason、不套业务结论。
                    """
                    kind = ev.get("kind")
                    req_id = ev.get("request_id")
                    reg = direct["registry"].get(req_id)
                    if reg is None or ev.get("run_id") != direct["run_id"] \
                            or ev.get("attempt_no") != reg["attempt_no"]:
                        return      # 跨场 / 未知 / 身份不匹配：无副作用
                    if kind == "started":
                        # started 仅一次登记；transport 受锁保护的入队顺序
                        # 即发送尝试提交序，预留号只做校验不猜发送序
                        if reg["state"] != "reserved":
                            return
                        reg["state"] = "started"
                        if direct["reserved"] > 0:
                            direct["reserved"] -= 1
                        bid = burst.register_request()
                        reg["bid"] = bid
                        direct["ticket_to_bid"][req_id] = bid
                        _sync_pending()
                        state["last_reason"] = "直发已发起"
                        log("[i] 第 %d 次提交：直发已发起" % state["attempts"])
                        _retry_event(kind="submit", attempts=state["attempts"],
                                     request_id=bid, phase=burst.phase)
                        return
                    if kind == "not_started":
                        # 只释放仍预留的本条；重复、started 后的 not_started 无副作用
                        if reg["state"] == "reserved":
                            reg["state"] = "cancelled"
                            if direct["reserved"] > 0:
                                direct["reserved"] -= 1
                        return
                    bid = reg["bid"]
                    if bid is None:
                        return      # 未跨接纳点的票据没有可结算身份
                    if kind == "failed":
                        if reg["state"] != "started":
                            return  # 已决 / lost 后重复 failed：无副作用
                        # lost 保持 pending（不假造结论），同一合法请求仍可在
                        # 尾窗内被迟到的确定应答结算
                        if burst.settle(bid, "lost") is None:
                            return
                        reg["state"] = "lost"
                        _sync_budget_window()
                        state["last_reason"] = "直发网络失败，仍未决"
                        _sync_pending()
                        return
                    if kind == "completed":
                        if reg["state"] == "settled":
                            return  # 已决不改写
                        http_status = ev.get("http_status")
                        body = ev.get("body_bytes")
                        raw_kind = "real"
                        if http_status == 404:
                            raw_kind = "404"
                        elif http_status == 429:
                            raw_kind = "throttled"
                        elif isinstance(http_status, int) and http_status >= 500:
                            raw_kind = "server_error"
                        payload = None
                        if raw_kind == "real":
                            payload = _decode_body(body)
                        applied = burst.settle(bid, raw_kind, http_status=http_status)
                        if applied is None:
                            return  # 重复结算被拒：结论/扩窗/降级副作用一并拒绝
                        _sync_budget_window()
                        # 通道/协议异常（F01 返修）：非 200/404/429/5xx 的 HTTP，
                        # 或 200 解码后无可识别业务 code。已收应答按 real 结算
                        # （不伪装 lost），不套业务结论（不冒充 4001、不切 click），
                        # 停止新发后由统一收尾按无成功 pending>0→unconfirmed 解析。
                        anomaly = None
                        if raw_kind == "real":
                            # A2 裁决：可识别业务 code 仅限非 bool 有限整数
                            # 数值或严格十进制整数字符串；其余按协议异常停发
                            has_code = (isinstance(payload, dict)
                                        and payload.get("code") is not None)
                            if http_status != 200:
                                anomaly = "通道/协议异常 HTTP %s，停止新发" % (http_status,)
                            elif not has_code:
                                anomaly = "通道/协议异常：200 无业务 code，停止新发"
                        if anomaly is not None:
                            reg["state"] = "settled"
                            direct["protocol_stop"] = True
                            state["last_reason"] = anomaly
                            log("[!] " + anomaly)
                        elif raw_kind == "404":
                            reg["state"] = "settled"
                            state["last_reason"] = "404 接口未就绪/限流"
                            _retry_event(kind="not_ready", request_id=bid,
                                         streak_404=burst.streak_404, phase=burst.phase,
                                         retry_deadline=int(burst.effective_deadline))
                        elif raw_kind == "throttled":
                            reg["state"] = "settled"
                            state["last_reason"] = "429 限流"
                            _retry_event(kind="throttled", request_id=bid,
                                         streak_404=burst.streak_404, phase=burst.phase,
                                         retry_deadline=int(burst.effective_deadline))
                        elif raw_kind == "server_error":
                            reg["state"] = "settled"
                            state["last_reason"] = "5xx 服务端异常"
                            _retry_event(kind="server_error", request_id=bid,
                                         http_status=http_status, phase=burst.phase)
                        else:
                            reg["state"] = "settled"
                            _apply_conclusion(payload)
                        _sync_pending()
                        log("[i] 第 %d 次提交：%s" % (state["attempts"], state["last_reason"]))

                def _drain_direct():
                    if burst is None or direct["queue"] is None:
                        return
                    q = direct["queue"]
                    while True:
                        try:
                            ev = q.get_nowait()
                        except queue.Empty:
                            break
                        _settle_direct_event(ev)

                def _try_direct_fire():
                    """现算签名并直发一笔：时间取自 business clock（ClockUnavailable 停发射，不用 time.time）。

                    签名失败/try_submit 拒绝不计次数；返回 "fired"/"busy"/"downgrade"/"stop"。
                    token 只取本场内存：缺失不建 ticket、不转 click，安全停发（F01）。
                    """
                    if direct["transport"] is None or direct["signer"] is None:
                        direct["downgrade"] = True
                        return "downgrade"
                    try:
                        captured_token = token()
                    except Exception:
                        captured_token = ""
                    if not captured_token:
                        direct["protocol_stop"] = True
                        state["last_reason"] = "token 缺失，停止发送"
                        log("[!] token 缺失：停止发送")
                        return "stop"
                    try:
                        epoch_ms = int(business_now() * 1000)
                    except ClockUnavailable:
                        raise
                    try:
                        sig = direct["signer"](str(target["id"]), epoch_ms)
                    except Exception:
                        direct["downgrade"] = True
                        return "downgrade"
                    # 签名耗时后重新过预算闸门（F02）：remaining 已含 sign_end
                    # 硬截；不足不建 ticket、不发送，也不后台读业务时钟迁就。
                    remaining = budget.remaining_s()
                    if remaining <= 0:
                        return "busy"
                    mono_now = time.monotonic()
                    direct["seq"] += 1
                    req_id = "%s-%d" % (direct["run_id"], direct["seq"])
                    url = API + "/rjyHdCyr/signUp/" + sig
                    ticket = {
                        "run_id": direct["run_id"],
                        "request_id": req_id,
                        "attempt_no": burst.attempts + direct["reserved"] + 1,
                        "url": url,
                        "headers": {"Accept": "application/json, text/plain, */*",
                                    "token": captured_token,
                                    "Referer": REFERER, "User-Agent": UA},
                        # dispatch 只管最晚启动；request_deadline 是网络预算截止，
                        # timeout_s 为本笔捕获的预算，线程不得延长它们。
                        "dispatch_deadline": mono_now + min(INFLIGHT_WAIT_S, remaining),
                        "request_deadline": mono_now + remaining,
                        "timeout_s": min(8.0, remaining),
                    }
                    # try_submit 之前登记 reservation；拒绝时释放该条（F03/F04）
                    direct["registry"][req_id] = {
                        "run_id": direct["run_id"],
                        "attempt_no": ticket["attempt_no"],
                        "state": "reserved",
                        "bid": None,
                    }
                    direct["reserved"] += 1
                    ok = direct["transport"].try_submit(ticket)
                    if not ok:
                        direct["reserved"] -= 1
                        direct["registry"].pop(req_id, None)
                        return "busy"
                    return "fired"

                def _pump_wait(seconds):
                    """短事件泵：按预算截断等待，同时派发并结算在途响应。

                    Playwright 同步 API 在等待里派发事件回调；成功/登录失效
                    立即返回，让调度循环收成唯一终态。等待按整毫秒推进，
                    不让浮点截断把 90ms 间隔悄悄吃短。
                    """
                    remain = budget.remaining_s()
                    wait_s = max(0.0, min(float(seconds), remain))
                    # 请求了等待就必须有推进：亚毫秒余量取 1ms，否则边界泵会空转
                    wait_ms = int(round(wait_s * 1000)) if wait_s > 0 else 0
                    if wait_s > 0:
                        wait_ms = max(1, wait_ms)
                    deadline_ms = int(round(time.monotonic() * 1000)) + wait_ms
                    while True:
                        left_ms = deadline_ms - int(round(time.monotonic() * 1000))
                        if left_ms <= 0:
                            break
                        try:
                            page.wait_for_timeout(min(left_ms, 50))  # 50ms 一片
                        except Exception:
                            break
                        _settle_pending()
                        if state["resp_ok"] or state["login_invalid"]:
                            return

                def _may_fire():
                    """统一发射闸门：drain → 成功 → 4001 → 次数/截止 → 业务时间 → 名额。

                    返回 "ok" 才允许发下一笔；其余为不发原因（调用方据此收口或原地等）。
                    回调路径不读时钟的部分用 current_phase，业务时间用 business_bounds。
                    """
                    _settle_pending()
                    if state["resp_ok"]:
                        return "success"
                    if state["login_invalid"]:
                        return "login_invalid"
                    if direct.get("protocol_stop"):
                        return "protocol_stop"
                    if not burst.can_attempt():
                        return "attempts"
                    # 直发已提交未 started 的预留也占次数/名额配额（started 滞后也不允许第二笔）；
                    # 预留只是暂时占用，占满时泵等核清，不当作真实 attempts 耗尽漏账终止
                    if (args.max_attempts > 0
                            and burst.attempts + direct["reserved"] >= args.max_attempts):
                        if direct["reserved"] > 0:
                            return "inflight"
                        return "attempts"
                    try:
                        now_lower, _now_upper = business_bounds()
                    except ClockUnavailable:
                        return "clock"
                    if now_lower < at_ts:
                        return "not_open"
                    if burst.inflight_count() + direct["reserved"] >= MAX_INFLIGHT:
                        return "inflight"
                    return "ok"

                def _finalize(stop_reason, fallback=None):
                    """统一有界收尾（F04）：先结算可读队列，1 秒尾窗（受预算/sign_end
                    截断），再按「成功粘性最优先」解析唯一终态。page/时钟不可用不忙转。

                    成功不被 False/异常覆盖（F01）；pending 且无成功＝未确认（F02）；
                    流程/鉴权/校时异常有未决保留事实。_settle_pending 不依赖 budget，
                    时钟失效也先把可读队列结算掉，再跳过收尾泵。
                    """
                    if direct.get("transport") is not None:
                        direct["transport"].close()
                    _settle_pending()
                    for reg in direct["registry"].values():
                        if reg["state"] == "reserved":
                            reg["state"] = "cancelled"
                    direct["reserved"] = 0
                    # 1 秒尾窗（main 语义，取消 not login_invalid：4001 后同窗内成功仍可结算）
                    try:
                        tail_gate = state["pending"] > 0 and not budget.expired()
                    except ClockUnavailable:
                        tail_gate = False
                    if tail_gate:
                        try:
                            tail_deadline = (time.monotonic()
                                             + min(1.0, max(0.0, budget.remaining_s())))
                            while (time.monotonic() < tail_deadline
                                   and not budget.expired()
                                   and state["pending"] > 0
                                   and not state["resp_ok"]):
                                tail_left = tail_deadline - time.monotonic()
                                if tail_left <= 0:
                                    break
                                _pump_wait(min(0.05, tail_left))
                        except ClockUnavailable:
                            pass    # 时钟失效：收尾泵停；可读队列已由 _settle_pending 结算
                    # 成功粘性最优先：已到 code=1 就是成功，不被 False/异常覆盖（F01）
                    if state["resp_ok"]:
                        return terminal("success")
                    if state["login_invalid"]:
                        stop_reason = "login_invalid"
                    # 单次最终核对（F07）：仅原预算允许开始才查；不引入 not_joined
                    try:
                        can_check = bool(state["attempts"]) and not budget.expired()
                    except ClockUnavailable:
                        can_check = False
                    if can_check:
                        # 最终核对失败如实上抛为流程异常：无 pending 时报 process_error，
                        # 有 pending 保留未决（F02）；成功已粘住时上面已 return（F01）。
                        try:
                            joined_now = is_joined(target["id"], budget=budget)
                        except ClockUnavailable:
                            raise
                        except Exception:
                            if state["pending"] > 0:
                                return terminal("unconfirmed")
                            return terminal("process_error")
                        if joined_now is True:
                            return terminal("success")
                    # pending 且无成功＝未确认（F02）
                    if state["pending"] > 0:
                        return terminal("unconfirmed")
                    if stop_reason:
                        return terminal(stop_reason)
                    if fallback:
                        return terminal(fallback)
                    return terminal("sign_end" if state["binding"] == "sign_end" else "retry_duration")

                listeners.append(("request", on_request))
                listeners.append(("response", on_response))
                listeners.append(("requestfailed", on_requestfailed))
                page.on("request", on_request)
                page.on("response", on_response)
                page.on("requestfailed", on_requestfailed)
                armed["on"] = True    # 整个提交阶段受控：在途响应按 request_id 结算
                _checkpoint_event("watch_started")
                log("[i] 开始盯按钮")

                while not budget.expired():
                    # 统一发射闸门：先结算在途，再判成功/4001/次数/业务时间/名额
                    verdict = _may_fire()
                    if verdict == "success":
                        break
                    if verdict == "login_invalid":
                        stop_reason = "login_invalid"
                        break
                    if verdict == "protocol_stop":
                        # 通道/协议异常或 token 缺失：停止新发，统一收尾解析
                        # process_error / unconfirmed（不新增公开终态）
                        stop_reason = "process_error"
                        break
                    if verdict == "attempts":
                        if state["attempts"] >= args.max_attempts > 0:
                            stop_reason = "max_attempts"
                        else:
                            stop_reason = ("sign_end" if (sign_end_ts and
                                           burst.effective_deadline >= sign_end_ts)
                                           else "retry_duration")
                        break
                    if verdict == "clock":
                        # 校时失效：停止自动动作；已提交未决保留，零提交不算失败
                        stop_reason = ("unconfirmed" if state["pending"] > 0
                                       else "clock_unavailable")
                        if stop_reason == "clock_unavailable":
                            state["last_reason"] = "北京时间暂不可用"
                        break
                    if verdict == "not_open":
                        # 业务下界还没到开放时刻：原地等，不耗次数（决策 012）
                        try:
                            _now_lower, _now_upper = business_bounds()
                        except ClockUnavailable:
                            stop_reason = ("unconfirmed" if state["pending"] > 0
                                           else "clock_unavailable")
                            break
                        _pump_wait(min(0.25, max(0.0, at_ts - _now_lower)))
                        continue
                    if verdict == "inflight":
                        # 名额满：原地泵等在途释放，不耗次数；受预算截断
                        _pump_wait(INFLIGHT_WAIT_S)
                        continue

                    # 直发为主：已启用且未降级时用直发，不再等按钮可见；降级后落到点击兜底
                    if direct["enabled"] and not direct["downgrade"]:
                        res = _try_direct_fire()
                        if res == "fired":
                            gap = burst.interval_s()
                            _pump_wait(min(max(0.0, gap), max(0.0, budget.remaining_s())))
                            burst.next_interval_after_wait()
                            continue
                        if res == "busy":
                            _pump_wait(INFLIGHT_WAIT_S)
                            continue
                        if res == "stop":
                            # token 缺失等安全停发：不转 click，统一收尾
                            stop_reason = "process_error"
                            break
                        # res == "downgrade"：本场单向切到下方点击兜底

                    btn = page.locator("uni-button").filter(has_text="立即报名")
                    if not btn.count():
                        if time.monotonic() - last_reload >= reload_gap:
                            page.reload(wait_until="domcontentloaded",
                                        timeout=budget.timeout_ms(60000))
                            _pump_wait(0.25)
                            last_reload = time.monotonic()
                            reload_gap = min(8.0, reload_gap * 1.5)
                        else:
                            _pump_wait(0.3)
                        continue

                    # 非阻塞提交：点完「确定」立刻回调度循环（黄金风暴时序）
                    round_start = len(admitted_all)
                    aborted = False
                    try:
                        btn.first.click(timeout=budget.timeout_ms(3000))
                        # 打开确认框后、点「确定」前再过一次闸门（含名额满原地等≤1s）
                        gate2 = _may_fire()
                        if gate2 == "inflight":
                            waited = 0.0
                            while gate2 == "inflight" and waited < CONFIRM_INFLIGHT_WAIT_S:
                                _pump_wait(INFLIGHT_WAIT_S)
                                waited += INFLIGHT_WAIT_S
                                gate2 = _may_fire()
                        if gate2 in ("success", "login_invalid"):
                            # 在途已出结论：不点确定，交给收尾解析唯一终态
                            aborted = True
                            state["last_reason"] = "确认前已出结论，取消本次确认"
                        elif gate2 != "ok":
                            aborted = True
                            state["last_reason"] = "确认前闸门未过（%s）" % gate2
                            raise _SubmitAborted(state["last_reason"])
                        else:
                            if budget.expired():
                                raise _SubmitAborted("确认前重试窗口已到点")
                            if business_now() < at_ts:
                                raise _SubmitAborted("确认前还没到开放时间")
                            page.get_by_text("确定", exact=True).first.click(
                                timeout=budget.timeout_ms(3000))
                    except ClockUnavailable:
                        raise           # 校时失效不是点击失败：交统一收尾
                    except _SubmitAborted as exc:
                        aborted = True
                        state["last_reason"] = str(exc)
                    except PlaywrightTimeoutError:
                        state["last_reason"] = ("点击确认超时" if admitted_all[round_start:]
                                                else "点击后没发出提交请求")
                    except Exception as exc:
                        state["last_reason"] = ("点击确认失败：%s" % type(exc).__name__
                                                if admitted_all[round_start:]
                                                else type(exc).__name__)

                    round_requests = admitted_all[round_start:]
                    # 已到响应立即结算；迟到的由下一片事件泵接管
                    _settle_pending()
                    if not round_requests and not aborted:
                        log("[i] 本次点击没有发出提交：%s"
                            % (state["last_reason"] or "未点开确认框"))

                    if state["resp_ok"]:
                        break
                    if len(round_requests) > 1:
                        # 页面一次动作真的发了多次：按真实次数记账后继续，不藏成 1 次；
                        # 未拿到确定结果的那些留在 pending，终态会如实说「未确认」
                        state["last_reason"] = "一次动作发出 %d 次提交" % len(round_requests)
                    # 登录失效是终态：继续刷新只会白跑
                    if state["login_invalid"]:
                        stop_reason = "login_invalid"
                        break
                    if not burst.can_attempt():
                        if state["attempts"] >= args.max_attempts > 0:
                            stop_reason = "max_attempts"
                        else:
                            stop_reason = ("sign_end" if (sign_end_ts and
                                           burst.effective_deadline >= sign_end_ts)
                                           else "retry_duration")
                        break
                    # 下一轮节奏由状态机统一提供：黄金 90ms / 全速 180ms /
                    # 404 自适应退避 1.6 倍上限 3 秒。等待走短事件泵，
                    # 响应未到也能继续发下一笔。
                    gap = burst.interval_s()
                    _pump_wait(min(max(0.0, gap), max(0.0, budget.remaining_s())))
                    burst.next_interval_after_wait()

                # 统一有界收尾：结算可读队列 + 1 秒尾窗 + 唯一终态（在关浏览器前，
                # F04）。取消尾窗 not login_invalid 限制：4001 后同窗内成功仍可结算。
                return _finalize(stop_reason)
            except ClockUnavailable:
                # 校时在浏览器阶段失效：统一收尾（有未决保留事实），不向外抛
                return _finalize("clock_unavailable")
            except BudgetExhausted:
                _fallback = ("sign_end" if state["binding"] == "sign_end"
                             else "retry_duration")
                return _finalize(_fallback, fallback=_fallback)
            except Exception as exc:  # noqa: BLE001 - 流程异常也要在关浏览器前收口
                state["last_reason"] = "运行异常：%s" % type(exc).__name__
                return _finalize("process_error", fallback="process_error")
            finally:
                # 停止/异常路径统一解除监听并失活回调：不留跨计划的残留监听
                armed["on"] = False
                armed["dead"] = True
                for event_name, handler in listeners:
                    try:
                        page.remove_listener(event_name, handler)
                    except Exception:
                        pass
                # 直发通道：关新发射（不等待在途线程，不假装取消学校处理）
                if direct.get("transport") is not None:
                    try:
                        direct["transport"].close()
                    except Exception:
                        pass
                try:
                    browser.close()
                except Exception:
                    pass
    except ClockUnavailable:
        # 校时在浏览器阶段失效：已发出的提交保持未决事实，零提交不算失败
        return _clock_stop()
    except BudgetExhausted:
        # 任何阻塞撞上预算：走同一个结构化终态，不向外抛装置异常
        return terminal("sign_end" if state["binding"] == "sign_end" else "retry_duration")
    except Exception as exc:  # noqa: BLE001 - 浏览器/管道异常也要有唯一终态
        state["last_reason"] = "运行异常：%s" % type(exc).__name__
        return terminal("process_error")

def cmd_selftest(args):
    from playwright.sync_api import sync_playwright

    print("[i] 自检：拉起浏览器并打开站点首页 ...")
    with sync_playwright() as pw:
        browser = launch_browser(pw, headless=True)
        try:
            ctx = new_context(browser, with_state=False)
            page = ctx.new_page()
            page.goto(BASE + "/stu/", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1500)
            title = page.title()
            body = page.inner_text("body")[:120].replace("\n", " ")
            print("[OK] 打开成功：title=%s" % title)
            print("[i] 页面片段：%s" % body)
            if "学生" not in body and "智慧元小元" not in title:
                print("[!] 页面内容看起来不对，可能被网络/代理拦截。")
                return 2
            return 0
        except Exception as exc:
            print("[X] 自检失败：%s" % type(exc).__name__)
            print("    请确认已安装：pip install playwright && playwright install chromium")
            return 2
        finally:
            browser.close()


def main():
    setup_console()
    # 子命令统一入口先初始化数据根：直接运行冻结 worker 执行 check/list/grab
    # 时，也要先把旧 EXE 旁 data 迁到统一目录——与 GUI 起的 worker 同一套初始化
    ensure_data_dir()
    ap = argparse.ArgumentParser(
        description="校园活动到点自动报名（本机版，单账号）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python grab.py login\n"
               "  python grab.py list\n"
               "  python grab.py grab\n"
               "  python grab.py grab --activity-id 2ae75dd103654543b1a8b97053adf4a6 "
               "--at \"2026-10-22 00:59:59\" --name \"网络安全·数字时代大长城\"")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("login", help="打开浏览器手动登录一次，保存登录态")
    p.add_argument("--timeout", type=int, default=600, help="等待登录的秒数，默认 600")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("check", help="检查登录态是否有效")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("list", help="列出活动与报名开始时间")
    p.add_argument("--all", action="store_true", help="连已报名的也列出来")
    p.add_argument("--json", action="store_true", help="输出 JSON（给图形界面调用）")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("grab", help="到点自动报名（不传 --pick/--name/--activity-id 则交互选择）")
    p.add_argument("--pick", type=int, default=0, metavar="N",
                   help="按 list 里的序号直接选（与交互列表顺序一致）")
    p.add_argument("--name", default="",
                   help="活动名关键词，或配合 --activity-id 作为通知里的名字")
    p.add_argument("--activity-id", default="", help="活动 id（一般不用手抄，用 --pick 即可）")
    p.add_argument("--at", default="", help="报名开始时间；不给则自动从活动列表读取")
    p.add_argument("--lead", type=float, default=100, help="提前多少秒打开页面，默认 100")
    p.add_argument("--window", type=float, default=120, help="开放后重试窗口秒数，默认 120（--retry-for 缺省时沿用）")
    p.add_argument("--sign-end", default="", help="报名截止时间；是自动重试的绝对硬上限")
    p.add_argument("--retry-for", type=float, default=None,
                   help="重试持续秒数，默认沿用 --window；0 表示持续到 --sign-end（缺截止则回退 120 秒）")
    p.add_argument("--max-attempts", type=int, default=0,
                   help="最大提交次数，只数真正点 signUp 的次数；0 表示不限")
    p.add_argument("--retry-start", type=float, default=0,
                   help="内部：计划接纳时固定的重试起点（epoch 秒），由图形界面传入；"
                        "CLI 独跑留空则用本进程入口时刻")
    p.add_argument("--headed", action="store_true", help="显示浏览器窗口（默认后台运行）")
    p.add_argument("--dry-run", action="store_true", help="只检查页面/按钮，不提交")
    p.set_defaults(func=cmd_grab)

    p = sub.add_parser("selftest", help="自检浏览器与网络")
    p.set_defaults(func=cmd_selftest)

    args = ap.parse_args()
    # CLI check/list 也按需建立同一业务时钟：授时失败不阻断鉴权与原始数据读取，
    # 由各子命令显式降级（列表状态未知、不做本机时钟筛选）
    if args.cmd in ("check", "list"):
        setup_business_clock(allow_sync=True)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n[i] 已手动中断。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
