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
CONFIG_FILE = HERE / "config.json"

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
    line = "%s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
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


def _post(path, payload=None, tok=None):
    body = json.dumps(payload if payload is not None else {}).encode("utf-8")
    req = urllib.request.Request(API + path, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/plain, */*")
    req.add_header("token", tok if tok is not None else token())
    req.add_header("Referer", REFERER)
    req.add_header("User-Agent", UA)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        try:
            raw = resp.read().decode("utf-8", "replace")
        finally:
            resp.close()
        return json.loads(raw)
    except Exception as exc:
        return {"code": -1, "message": type(exc).__name__}


def check_token():
    """用活动列表接口判定登录态：code=1 有效，4001 表示 token 失效。"""
    tok = token()
    if not tok:
        return False, "本机还没有登录过（data/session.json 不存在）"
    data = _post("/rjyHd/listActivity",
                 {"pageSize": 1, "pageNum": 1, "hdmc": "", "hdzt": None,
                  "kssj": "", "jssj": ""}, tok)
    code = data.get("code")
    if code == 1:
        return True, "ok"
    if str(code) == "4001":
        return False, "登录态已过期（%s）" % (data.get("message") or "身份验证失败")
    return False, "接口异常：%s" % (data.get("message") or code)


def clock_skew():
    """拿服务器 Date 头比对本机时钟，差太多会让"到点"抢跑或迟到。"""
    try:
        req = urllib.request.Request(BASE + "/stu/", method="HEAD")
        req.add_header("User-Agent", UA)
        resp = urllib.request.urlopen(req, timeout=10)
        try:
            srv = resp.headers.get("Date")
        finally:
            resp.close()
        if not srv:
            return None
        from email.utils import parsedate_to_datetime
        remote = parsedate_to_datetime(srv).timestamp()
        return remote - time.time()
    except Exception:
        return None


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


def fetch_activities():
    """分页拉活动列表；返回本地字段结构（字段名已换成好读的名字）。"""
    out, page, total = [], 1, None
    while page <= 30:
        data = _post("/rjyHd/listActivity",
                     {"pageSize": PAGE_SIZE, "pageNum": page, "hdmc": "",
                      "hdzt": None, "kssj": "", "jssj": ""})
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


def is_joined(act_id):
    """getJoinInfo：未报名时 data 为 null，报名后 data 是对象。"""
    data = _post("/rjyHdCyr/getJoinInfo/" + act_id, {})
    if not isinstance(data, dict) or data.get("code") != 1:
        return False
    return data.get("data") not in (None, {}, [])


def parse_ts(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").timestamp()


def still_relevant(act, now=None):
    """报名还没截止（或没给截止时间）的活动才算「还值得看」。"""
    now = now or time.time()
    if not act.get("sign_end"):
        return True
    try:
        return parse_ts(act["sign_end"]) >= now - 3600
    except Exception:
        return True


def sign_state(act, now=None):
    now = now or time.time()
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
        skew = clock_skew()
        if skew is not None and abs(skew) > 3:
            print("[!] 本机时钟与服务器相差 %+.0f 秒，建议校准系统时间，否则会抢跑或迟到。" % skew)
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

    now = time.time()
    # 默认只看报名还没截止的（历史活动几百条，混在一起没法挑）
    rows = list(acts) if args.all else [a for a in acts if still_relevant(a, now)]

    def rank(a):
        # 未开放的排最前，其次开放中，已报名的垫底
        if a["signed"]:
            return 2
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
                "state": "已报名" if act["signed"] else sign_state(act, now),
            })
        print(json.dumps(out, ensure_ascii=False))
        return 0

    print("%3s  %s  %s  %s" % ("#", "报名开始", "状态", "活动名"))
    for i, act in enumerate(rows, 1):
        flag = "已报名" if act["signed"] else sign_state(act, now)
        print("%3d  %s  %s  %s" % (i, act["sign_start"] or "-", flag, act["name"]))
    print("\n提示：双击 2-抢活动.bat 可直接选择并到点抢；也可以双击 4-图形界面.bat 用鼠标点。")
    return 0


def candidates(acts, now=None):
    """可抢的活动：未报名、有报名开始时间、还没截止；未开放的排最前。

    交互选择与 --pick/--name 非交互选择共用这一份顺序，保证序号一致。
    """
    now = now or time.time()
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


def wait_until(ts):
    while True:
        left = ts - time.time()
        if left <= 0:
            return
        time.sleep(min(0.2, left))


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
    if ts is not None and ts <= time.time():
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return at_text


def resolve_target(args):
    """确定 activity-id / 报名开始时间 / 活动名。"""
    acts = None
    act = None
    if args.activity_id:
        aid = args.activity_id
        if not args.at:
            act = next((a for a in (fetch_activities() if acts is None else acts)
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
        acts = fetch_activities()
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



def clamp_wait_ms(deadline: float, want_ms: int) -> int:
    """把重试循环里的阻塞等待压进窗口剩余时间（毫秒）。

    重试窗口是进程级硬截止：循环条件只约束「迭代开始」，
    reload / expect_response 这些固定超时最坏会把一次迭代拖出
    窗口两分多钟。窗口耗尽时给 1ms，让调用尽快抛超时回到循环条件。
    """
    remain_ms = int((deadline - time.time()) * 1000)
    return max(1, min(want_ms, remain_ms))


def retry_anchor(at_ts, now=None):
    """重试窗口起算点；必须在 page.goto 之前取值，之后不再移动。

    未来场次归一到原定 sign_start（等待开放后起算）；已开放场次固定为
    worker 启动时刻——页面加载慢不得把用户配置的重试窗口整体后移。
    配套用法：compute_retry_window(anchor, ..., now=anchor) 把 retry_start
    钉在 anchor 上，等待和首次刷新都不改变 deadline。
    """
    now = time.time() if now is None else now
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
    now = time.time() if now is None else now
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
    """秒级时间戳 → 完整本地时间文本；None 显示 '-'（结构化日志用）。"""
    if not ts:
        return "-"
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")


def cmd_grab(args):
    from playwright.sync_api import sync_playwright
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    ok, msg = check_token()
    if not ok:
        print("[X] %s" % msg)
        print("    请先运行：python grab.py login")
        return 3

    skew = clock_skew()
    if skew is not None and abs(skew) > 3:
        print("[!] 本机时钟与服务器相差 %+.0f 秒，建议校准系统时间后再抢。" % skew)

    target = resolve_target(args)
    if not target:
        return 0

    at_ts = parse_ts(target["at"])
    now = time.time()
    if at_ts - now > 86400:
        print("[i] 报名开始时间在 24 小时之后，确认无误再跑。")
    start = now if args.dry_run else at_ts - args.lead
    # 预报名提前准备契约：协调器会在 sign_start-lead 秒启动本进程——
    # 详情页此刻就打开（「详情页已加载」是真实事件），wait_until(start)
    # 几乎立即通过；不会用提前启动时间代替原定开放时间（at 未过期原样保留）。
    # 已开放场次 at 已被换成现在，两条路径在开放前 5 秒汇合进入盯按钮。
    # 重试起算点在打开页面前固定（retry_anchor）：未来场次归一到原定开放，
    # 已开放场次固定为 worker 启动时刻——下面的 page.goto 无论多慢，
    # 用户配置的重试窗口都从这一刻起算，不被页面加载整体后移。
    retry_anchor_ts = retry_anchor(at_ts, now)

    print("[i] 目标：%s" % target["name"])
    original_at = target.get("original_at") or ""
    if original_at and original_at != target["at"]:
        # 双模式口径：报名已开放的场次不是「等待原定时刻」，而是立即报名/抢名额；
        # 原定开放只作参考时间保留，重试窗口从本次执行起点起算。
        print("[i] 原定开放：%s（已开放，本次按立即报名/抢名额执行）" % original_at)
        print("[i] 本次执行：%s；提前 %.0f 秒打开页面；失败重试窗口 %.0f 秒"
              % (target["at"], args.lead, args.window))
    else:
        print("[i] 报名开始：%s；提前 %.0f 秒打开页面；失败重试窗口 %.0f 秒"
              % (target["at"], args.lead, args.window))

    header = None
    with sync_playwright() as pw:
        browser = launch_browser(pw, headless=not args.headed)
        try:
            ctx = new_context(browser)
            page = ctx.new_page()
            url = BASE + "/stu/" + DETAIL_HASH.format(aid=target["id"])
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1500)
            # checkpoint 事件：app.py 据此记 page_opened_at（时间线「活动页已打开」）。
            # 必须在详情页真正加载完成后才输出，不得提前打印或移到 goto 之前。
            log("[i] 详情页已加载")

            if args.dry_run:
                btns = page.eval_on_selector_all(
                    "uni-button", "els => els.map(e => e.innerText.trim()).filter(Boolean)")
                log("[i] dry-run：joined=%s buttons=%s"
                    % (is_joined(target["id"]), btns))
                return 0

            if is_joined(target["id"]):
                log("[i] 该活动已是报名状态，跳过提交")
                log("[retry] attempts=0 stop_reason=already_signed retry_deadline=0 sign_end=%d last_reason="
                    % int(parse_ts(args.sign_end) if args.sign_end else 0))
                print("[i] 你已经报过这个活动了。")
                return 0

            if not args.headed:
                print("[i] 后台等待中，到点自动提交；想看着它点就加 --headed。")
            wait_until(start)

            log("[i] 开始等待开放")
            # 重试策略：持续时长（0=直到报名截止）+ 最大提交次数（0=不限），
            # sign_end 永远是绝对硬上限；缺 sign_end 且选「直到截止」回退 120 秒。
            # deadline 从固定好的 retry_anchor 起算（now=anchor 钉住 retry_start），
            # 页面加载、等待与首次刷新都不改变它。
            retry_for = args.retry_for if args.retry_for is not None else args.window
            sign_end_ts = parse_ts(args.sign_end) if args.sign_end else None
            _, deadline, binding = compute_retry_window(retry_anchor_ts, sign_end_ts, retry_for,
                                                        now=retry_anchor_ts)
            if sign_end_ts:
                log("[i] 报名截止：%s；重试到 %s 截止"
                    % (_ts_text(sign_end_ts), _ts_text(deadline)))
            resp_ok, attempts, last_reason = False, 0, ""
            joined_by_check = False
            stop_reason = ""
            last_reload = 0.0

            # 开放前 5 秒起每 300ms 看一次按钮：名额放开往往只有几秒，
            # 等到开放前一秒再刷新会丢掉首轮点击时机。
            if time.time() < at_ts - POLL_LEAD:
                wait_until(at_ts - POLL_LEAD)

            # 按钮不出现通常是「名额已满」：间隔从 2 秒逐次放大到 8 秒，
            # 满员时不会每 2 秒刷一次页面（每次刷新两个接口），
            # 有人取消、按钮重新出现时最多 8 秒内就能抢上。
            reload_gap = 2.0
            # checkpoint 事件：app.py 据此记 watch_started_at（时间线「盯按钮」）。
            # 只在等待结束、真正进入按钮监视循环前输出；已开放场次会立刻到达这里。
            log("[i] 开始盯按钮")
            while time.time() < deadline:
                btn = page.locator("uni-button").filter(has_text="立即报名")
                if not btn.count():
                    if time.time() - last_reload >= reload_gap:
                        page.reload(wait_until="domcontentloaded",
                                    timeout=clamp_wait_ms(deadline, 60000))
                        page.wait_for_timeout(clamp_wait_ms(deadline, 250))
                        last_reload = time.time()
                        reload_gap = min(8.0, reload_gap * 1.5)
                    else:
                        page.wait_for_timeout(clamp_wait_ms(deadline, 300))
                    continue

                # 次数上限先于点击检查：这一步不算一次提交，刷新/等待更不算
                if not attempts_allowed(attempts, args.max_attempts):
                    stop_reason = "max_attempts"
                    break
                attempts += 1
                try:
                    # 包住「点按钮 + 点确定」，直接读服务端 signUp 的返回码
                    with page.expect_response(
                            lambda r: "/rjyHdCyr/signUp/" in r.url,
                            timeout=clamp_wait_ms(deadline, 12000)) as info:
                        btn.first.click(timeout=clamp_wait_ms(deadline, 3000))
                        page.get_by_text("确定", exact=True).first.click(
                            timeout=clamp_wait_ms(deadline, 3000))
                    payload = info.value.json()
                    code = payload.get("code") if isinstance(payload, dict) else None
                    last_reason = "code:%s %s" % (
                        code, str((payload or {}).get("message") or "")[:30])
                    resp_ok = (code == 1)
                except PlaywrightTimeoutError:
                    last_reason = "未收到 signUp 响应"
                except Exception as exc:
                    last_reason = type(exc).__name__
                log("[i] 第 %d 次提交：%s" % (attempts, last_reason))

                if resp_ok:
                    break
                # 登录失效是终态：继续刷新只会白跑，交给用户重登
                if "4001" in last_reason or "登录" in last_reason or "身份验证" in last_reason:
                    stop_reason = "login_invalid"
                    break
                # 服务端已回成功但页面状态可能滞后：只补 2~3 次轻量确认；
                # 窗口耗尽后不再占用等待时间，立刻回到循环条件收尾
                for _ in range(3):
                    time.sleep(min(0.6, max(0.0, deadline - time.time())))
                    if is_joined(target["id"]):
                        resp_ok = joined_by_check = True
                        break
                if resp_ok:
                    break
                page.reload(wait_until="domcontentloaded",
                            timeout=clamp_wait_ms(deadline, 60000))
                page.wait_for_timeout(clamp_wait_ms(deadline, 300))

            # 窗口已到就不再补最后一次核对：is_joined 在循环内每次
            # 失败后都查过，这里再查一次最多多占 15 秒，破坏硬截止。
            ok = resp_ok or (time.time() < deadline + 1 and is_joined(target["id"]))
            if ok and not resp_ok:
                joined_by_check = True
            # 终态归因：成功/已报名 > 显式停止原因（次数/登录）> 时间边界（截止/时长）
            if ok:
                stop_reason = "already_signed" if joined_by_check and not resp_ok else "success"
            elif not stop_reason:
                stop_reason = "sign_end" if binding == "sign_end" else "retry_duration"
            # 结构化终态行：app.py 解析后把 attempts/stop_reason/retry_deadline 推给界面。
            # 时间用 epoch 秒（无空格，key=value 解析才不会被拆断）；0 表示没有；
            # last_reason 恒为最后一个字段，原文里的空格不破坏解析。
            log("[retry] attempts=%d stop_reason=%s retry_deadline=%d sign_end=%d last_reason=%s"
                % (attempts, stop_reason, int(deadline), int(sign_end_ts or 0),
                   (last_reason or "")[:40]))
            if ok:
                log("[OK] 报名成功（尝试 %d 次）" % attempts)
                print("\n[OK] 报名成功：%s" % target["name"])
                print("     不想去请尽快在活动页取消，别占名额。")
                notify("报名成功", ["**%s**" % target["name"],
                                    "报名时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    "如不想参加，请尽快在活动页取消。"])
                return 0

            if stop_reason == "login_invalid":
                log("[X] 登录失效（尝试 %d 次）" % attempts)
                print("\n[X] 登录失效：%s" % (last_reason or "学校要求重新登录"))
                print("    重试没有意义，请先重新登录再跑。")
                return 3
            if stop_reason == "max_attempts":
                log("[X] 达到最大提交次数 %d（最近原因 %s）" % (attempts, last_reason))
                print("\n[X] 已提交 %d 次仍没成功：%s" % (attempts, last_reason or "名额已满"))
                print("    到达你设置的最大提交次数，停止重试。")
                return 2

            log("[X] 报名失败（尝试 %d 次，最近原因 %s）"
                % (attempts, last_reason or "名额已满/未开放"))
            tail_note = "报名已截止" if stop_reason == "sign_end" else "重试时长到了"
            print("\n[X] 没抢到：%s（%s）" % (last_reason or "名额已满／页面未放开", tail_note))
            print("    建议手动打开活动页确认，必要时再试一次。")
            notify("自动报名失败", ["**%s**" % target["name"],
                                    "原因：%s（%s）" % (last_reason or "名额已满/未开放", tail_note),
                                    "建议手动确认。"], "orange")
            return 2
        finally:
            browser.close()


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
    p.add_argument("--headed", action="store_true", help="显示浏览器窗口（默认后台运行）")
    p.add_argument("--dry-run", action="store_true", help="只检查页面/按钮，不提交")
    p.set_defaults(func=cmd_grab)

    p = sub.add_parser("selftest", help="自检浏览器与网络")
    p.set_defaults(func=cmd_selftest)

    args = ap.parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n[i] 已手动中断。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
