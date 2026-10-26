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
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = "https://xg.tjise.edu.cn"
API = BASE + "/dr/api"
REFERER = BASE + "/stu/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

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


def fetch_activities(budget=None):
    """分页拉活动列表；返回本地字段结构（字段名已换成好读的名字）。"""
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
            })
        if not chunk or len(out) >= total:
            break
        page += 1
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
        acts = fetch_activities()
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
             "joined_by_check": False, "deadline": 0.0, "binding": ""}
    done = {"value": False}

    def terminal(stop_reason):
        """唯一结构化终态：恰一条 [retry] kind=done，随后给人读结论与退出码。"""
        if done["value"]:
            return 2
        done["value"] = True
        log("[retry] kind=done attempts=%d stop_reason=%s retry_deadline=%d sign_end=%d "
            "last_reason=%s" % (state["attempts"], stop_reason, int(state["deadline"]),
                                int(sign_end_ts or 0), (state["last_reason"] or "")[:40]))
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
    if original_at and original_at != target["at"]:
        print("[i] 原定开放：%s（已开放，本次按立即报名/抢名额执行）" % original_at)
        print("[i] 本次执行：%s；提前 %.0f 秒打开页面；失败重试窗口 %.0f 秒"
              % (target["at"], args.lead, args.window))
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

                joined = is_joined(target["id"], budget=budget)
                if joined is True:
                    return terminal("already_signed")
                if joined is None:
                    state["last_reason"] = "已有报名查询未确认"

                if not args.headed:
                    print("[i] 后台等待中，到点自动提交；想看着它点就加 --headed。")
                wait_until(start, budget)

                log("[i] 开始等待开放")
                if business_now() < at_ts - POLL_LEAD:
                    wait_until(at_ts - POLL_LEAD, budget)

                reload_gap = 2.0
                last_reload = 0.0
                stop_reason = ""
                # 受控提交窗口：只承认「我方点击/确认期间」发出的 signUp 请求。
                # 学校真实接口是 GET signUp/<opaque-hash>，hash 不含活动 ID，
                # 因此不能再用「URL 里有 id」来认领（C01）。
                armed = {"on": False}
                admitted_ids = set()
                seen_ids = set()
                admitted_all = []

                def _is_signup_url(url_text):
                    return url_text.startswith(BASE) and "/rjyHdCyr/signUp/" in url_text

                def on_request(request):
                    if not armed["on"]:
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
                    state["attempts"] += 1
                    state["pending"] += 1
                    state["last_reason"] = "已发起，等响应"
                    # 请求发起即上报，不等响应回来才让界面知道已提交
                    log("[i] 第 %d 次提交：已发起" % state["attempts"])
                    _retry_event(kind="submit", attempts=state["attempts"])

                page.on("request", on_request)
                _checkpoint_event("watch_started")
                log("[i] 开始盯按钮")

                while not budget.expired():
                    btn = page.locator("uni-button").filter(has_text="立即报名")
                    if not btn.count():
                        if time.monotonic() - last_reload >= reload_gap:
                            page.reload(wait_until="domcontentloaded",
                                        timeout=budget.timeout_ms(60000))
                            budget.sleep(0.25)
                            last_reload = time.monotonic()
                            reload_gap = min(8.0, reload_gap * 1.5)
                        else:
                            budget.sleep(0.3)
                        continue

                    # 次数上限先于点击检查；已发出的未决请求也不再重发（C03）
                    if state["pending"] > 0:
                        stop_reason = "pending_result"
                        break
                    if not attempts_allowed(state["attempts"], args.max_attempts):
                        stop_reason = "max_attempts"
                        break

                    # 按钮提前可见也不能提前发请求：业务下界 >= 开放时刻才动手，
                    # 上界判断避免测量误差导致提前或贴线发起（决策 012）
                    try:
                        now_lower, now_upper = business_bounds()
                    except ClockUnavailable:
                        # 校时失效：停止自动动作；已提交未决保留，零提交不算失败
                        stop_reason = ("unconfirmed" if state["pending"] > 0
                                       else "clock_unavailable")
                        if stop_reason == "clock_unavailable":
                            state["last_reason"] = "北京时间暂不可用"
                        break
                    if now_upper < at_ts:
                        budget.sleep(min(0.25, at_ts - now_upper))
                        continue
                    if now_lower < at_ts:
                        budget.sleep(min(0.25, at_ts - now_lower))
                        continue
                    if budget.expired():
                        break

                    round_start = len(admitted_all)
                    payload, response = None, None
                    aborted = False
                    armed["on"] = True
                    try:
                        with page.expect_response(
                                lambda r: _is_signup_url(str(r.url)),
                                timeout=budget.timeout_ms(12000)) as info:
                            btn.first.click(timeout=budget.timeout_ms(3000))
                            # 打开确认框之后、点「确定」之前再核一次：
                            # 到期/次数到/还没开放都绝不确认（C02）
                            if budget.expired():
                                raise _SubmitAborted("确认前重试窗口已到点")
                            if not attempts_allowed(state["attempts"], args.max_attempts):
                                raise _SubmitAborted("确认前已达最大提交次数")
                            if business_now() < at_ts:
                                raise _SubmitAborted("确认前还没到开放时间")
                            page.get_by_text("确定", exact=True).first.click(
                                timeout=budget.timeout_ms(3000))
                        response = info.value
                        payload = response.json()
                    except _SubmitAborted as exc:
                        aborted = True
                        state["last_reason"] = str(exc)
                    except PlaywrightTimeoutError as exc:
                        state["last_reason"] = ("响应读取超时" if admitted_all[round_start:]
                                                else "点击后没发出提交请求")
                    except Exception as exc:
                        state["last_reason"] = ("响应读取失败：%s" % type(exc).__name__
                                                if admitted_all[round_start:]
                                                else type(exc).__name__)
                    finally:
                        armed["on"] = False

                    round_requests = admitted_all[round_start:]

                    # 响应必须对得上本轮承认过的请求，别家活动的响应不得改本场结论
                    matched = None
                    if response is not None:
                        resp_request = getattr(response, "request", None)
                        if resp_request is not None and id(resp_request) in admitted_ids:
                            matched = resp_request
                        elif resp_request is None and round_requests:
                            # 替身/旧驱动可能不给 response.request：只在唯一请求时认领
                            matched = round_requests[0] if len(round_requests) == 1 else None

                    if isinstance(payload, dict) and matched is not None:
                        code = payload.get("code")
                        state["last_reason"] = "code:%s %s" % (
                            code, str(payload.get("message") or "")[:30])
                        state["pending"] = max(0, state["pending"] - 1)
                        state["resp_ok"] = (code == 1)
                    elif response is not None and matched is None:
                        state["last_reason"] = "收到其他请求的响应，本场结论未变"

                    if round_requests:
                        log("[i] 第 %d 次提交：%s" % (state["attempts"], state["last_reason"]))
                    elif not aborted:
                        log("[i] 本次点击没有发出提交：%s"
                            % (state["last_reason"] or "未点开确认框"))

                    if state["resp_ok"]:
                        break
                    if len(round_requests) > 1:
                        # 页面一次动作真的发了多次：按真实次数记账后停手，不藏成 1 次；
                        # 未拿到确定结果的那一笔留在 pending，终态会如实说「未确认」
                        state["last_reason"] = "一次动作发出 %d 次提交" % len(round_requests)
                        break
                    if state["pending"] > 0:
                        # 上一次请求还没有确定结果：不再自动重发
                        stop_reason = "pending_result"
                        break
                    # 登录失效是终态：继续刷新只会白跑
                    if "4001" in state["last_reason"] or "登录" in state["last_reason"] \
                            or "身份验证" in state["last_reason"]:
                        stop_reason = "login_invalid"
                        break
                    # 服务端已回成功但页面状态可能滞后：只补 2~3 次轻量确认
                    for _ in range(3):
                        if budget.expired():
                            break
                        budget.sleep(0.6)
                        joined_now = is_joined(target["id"], budget=budget)
                        if joined_now is True:
                            state["resp_ok"] = state["joined_by_check"] = True
                            break
                    if state["resp_ok"]:
                        break
                    if budget.expired():
                        break
                    try:
                        page.reload(wait_until="domcontentloaded",
                                    timeout=budget.timeout_ms(60000))
                        budget.sleep(0.3)
                    except Exception:
                        state["last_reason"] = state["last_reason"] or "刷新失败"
                        stop_reason = "process_error"
                        break

                if state["resp_ok"] or state["joined_by_check"]:
                    return terminal("success")

                # 收尾查询：有剩余预算才查；查询不到不等于没报名
                joined_now = None
                if state["attempts"] and not budget.expired():
                    joined_now = is_joined(target["id"], budget=budget)
                if joined_now is True:
                    return terminal("success")
                if state["pending"] > 0:
                    return terminal("unconfirmed")
                if stop_reason == "pending_result":
                    return terminal("unconfirmed")
                if stop_reason:
                    return terminal(stop_reason)
                return terminal("sign_end" if state["binding"] == "sign_end" else "retry_duration")
            finally:
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
