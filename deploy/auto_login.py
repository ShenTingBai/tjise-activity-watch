#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无人值守重新登录：xgUniToken 失效时的兜底登录。

调用方式：cron / systemd timer 定时跑；脚本自己先校验 token，只有失效才真正登录，
正常时段不写任何状态文件。

登录流程：
1. Playwright Chromium（headless）打开真实登录页，页面自己带请求签名，不逆向接口；
2. 拦截页面自身发出的 /dr/api/auth/captcha 响应，取 base64 验证码图；
3. 识别 4 位验证码：ddddocr（beta 模型）为主，长度不是 4 位时依次回退默认模型、
   Tesseract 多变体投票（20 张标注样本实测 95% / 80% / 70%，三源串联 20/20）；
4. 填账号、密码、验证码，勾选隐私协议，点「立即登录」；
5. 拦截 /dr/api/auth/login 响应判断结果，成功后从 localStorage 读 xgUniToken；
6. 调 listActivity 校验 token 真被接口接受，再原子写入：
   - /root/tjise-login/storage_state.json（Playwright 完整 state，含 cookies 与 localStorage）
   - /root/tjise-activity-watch/data/session.json（token/saved_at/name/student_no/gender）

边界与安全：
- 最多尝试 MAX_ATTEMPTS 次，每次重新加载页面换一张验证码，两次之间隔 RETRY_DELAY_SEC
  秒（背靠背连打容易撞同一段降质窗口）；全失败才私聊 owner 告警；
- 告警 30 分钟内只发一次，避免 cron 反复刷屏；5 分钟后下一轮会继续试，
  避免一次验证码运气不好就断一整晚；
- 日志与告警只写结果分类和时间，绝不写账号、密码、验证码、token、姓名、学号；
- 不写 state.json、不动 token_probe 日志、不触发推送，因此不影响业务去重状态；
- 账号密码只从 credentials.json（权限必须 600）读取。

用法：
  python3 auto_login.py --check        # 只读：当前 token 是否有效
  python3 auto_login.py               # 默认：读 token_probe.log，失败才登录（零额外请求）
  python3 auto_login.py --self-test   # 假账号跑通整条链路，不落盘
  python3 auto_login.py --force       # 不论 token 状态都登录（联调用）
  python3 auto_login.py --dry-run 10  # 取 10 张验证码验识别率，不提交登录

@author LiXinwen
"""
import argparse
import base64
import fcntl
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime

import numpy as np
from PIL import Image
from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

LOGIN_URL = "https://xg.tjise.edu.cn/stu/#/mainPages/login?userType=2"
API_BASE = "https://xg.tjise.edu.cn/dr/api"
ACTIVITY_API = API_BASE + "/rjyHd/listActivity"
CAPTCHA_API = API_BASE + "/auth/captcha"
REFERER = "https://xg.tjise.edu.cn/stu/"

# 登录页 bundle 里的固定服务编码（VITE_APP_CLIENT）；接口用它区分终端类型
CLIENT = (
    "NzVmNDdlMjJiMGNjMTFlZGExNGQ4MmUxNDExNWM2OWY6"
    "N2FkOTI4YTliMGNjMTFlZGExNGQ4MmUxNDExNWM2OWY="
)

LOGIN_DIR = "/root/tjise-login"
APP_DIR = "/root/tjise-activity-watch"
CREDENTIALS_FILE = os.path.join(LOGIN_DIR, "credentials.json")
STATE_FILE = os.path.join(LOGIN_DIR, "storage_state.json")
SESSION_FILE = os.path.join(APP_DIR, "data", "session.json")
LOG_FILE = os.path.join(LOGIN_DIR, "auto_login.log")
LOCK_FILE = os.path.join(LOGIN_DIR, ".auto_login.lock")
ALERT_STATE_FILE = os.path.join(LOGIN_DIR, "auto_login_state.json")
DRY_RUN_DIR = os.path.join(LOGIN_DIR, "probe", "dryrun")
PROBE_LOG = os.path.join(APP_DIR, "data", "token_probe.log")

MAX_ATTEMPTS = 5
ALERT_COOLDOWN_SEC = 30 * 60
PROBE_STALE_SEC = 3600
# 两次尝试之间等一会儿：连着打容易撞同一段降质窗口，间隔后换到的是新图
RETRY_DELAY_SEC = 30
# 验证码字符集（学校侧就是大写字母 + 数字，4 位）
CAPTCHA_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
# ddddocr 模型按优先级排列：beta（common.onnx）明显强于默认（common_old.onnx）
DDDDOCR_MODELS = ("beta", "old")
# token 寿命不稳定：早期样本 17.5 小时，2026-10-22 这次只活了 10 小时 40 分。
# 等到 probe 报失效才登录，中间有 15–20 分钟推送必挂的盲窗（21:30 那条推送就是
# 这么丢的）。--preempt 在用到 8 小时时主动换新（最短寿命的约七成），每天多登录
# 一次，换掉这个盲窗。
PREEMPT_HOURS = 8.0

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
PAYLOAD = {"pageSize": 1, "pageNum": 1, "hdmc": "", "hdzt": None, "kssj": "", "jssj": ""}

# 7 个预处理变体：饱和度/亮度 × 竖线过滤阈值 × 放大倍数 × psm。
# 单选一个变体约 40~50%，逐位投票后约 70%（20 张标注样本实测，2026-10-22）。
# 它现在只做兜底：主引擎是 ddddocr，见 recognize()。
OCR_VARIANTS = (
    ("sat", 4, 6, 7),
    ("sat", 4, 6, 6),
    ("lum", 4, 6, 7),
    ("lum", 4, 6, 6),
    ("sat", 4, 8, 8),
    ("sat", 3, 6, 7),
    ("lum", 4, 8, 6),
)


# ---------------------------------------------------------------- 通用小工具
def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    """统一日志：只允许写结果分类，不允许带入敏感字段。"""
    line = "%s %s" % (_now(), msg)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def atomic_write_json(path, obj, mode=0o600):
    """同目录临时文件 + os.replace：避免写一半被 server_refresh.js 读到。"""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _token_fp(token):
    import hashlib
    return hashlib.sha1(token.encode("utf-8")).hexdigest()[:8]


def token_valid(token):
    """用一次 listActivity 校验 token 是否被接口接受；返回 (bool, 说明)。"""
    if not token:
        return False, "no-token"
    body = json.dumps(PAYLOAD).encode("utf-8")
    req = urllib.request.Request(ACTIVITY_API, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/plain, */*")
    req.add_header("token", token)
    req.add_header("Referer", REFERER)
    req.add_header("User-Agent", UA)
    try:
        resp = urllib.request.urlopen(req, timeout=20)
        try:
            raw = resp.read().decode("utf-8", "replace")
        finally:
            resp.close()
    except Exception as exc:
        return False, "http-error:" + type(exc).__name__
    try:
        data = json.loads(raw)
    except ValueError:
        return False, "bad-json"
    code = data.get("code") if isinstance(data, dict) else None
    if code == 1:
        return True, "ok"
    return False, "code:%s" % str(code)[:20]


# ------------------------------------------------------------ 验证码 OCR
def _run_mask(mask, minrun):
    """按列保留竖长 >= minrun 的像素段：验证码上的干扰线只有 1~2px 粗。"""
    out = np.zeros_like(mask)
    h, w = mask.shape
    for x in range(w):
        col = mask[:, x]
        y = 0
        while y < h:
            if col[y]:
                y2 = y
                while y2 < h and col[y2]:
                    y2 += 1
                if y2 - y >= minrun:
                    out[y:y2, x] = True
                y = y2
            else:
                y += 1
    return out


def _render(gray, scale, pad=4):
    img = Image.fromarray(np.clip(gray, 0, 255).astype(np.uint8))
    img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    canvas = Image.new("L", (img.width + 2 * pad * scale, img.height + 2 * pad * scale), 255)
    canvas.paste(img, (pad * scale, pad * scale))
    return canvas


def _ocr(img, psm):
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        img.save(path)
        r = subprocess.run(
            ["tesseract", path, "stdout", "--psm", str(psm), "--oem", "1",
             "-c", "tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"],
            capture_output=True, text=True, timeout=20,
        )
    finally:
        if os.path.exists(path):
            os.unlink(path)
    return "".join(c for c in r.stdout.strip() if c.isalnum())


# ddddocr 引擎按需加载后缓存在这里；值为 None 表示依赖缺失，已回退 Tesseract
_DDDD_ENGINES = {}


def _clean_code(text):
    """只做大写字母数字清洗，不做语义纠错：干扰圆环常被认成 O/0，纠了更容易错。"""
    return "".join(c for c in str(text or "").upper() if c in CAPTCHA_CHARSET)


def _ddddocr_engine(which):
    """按需加载 ddddocr 模型，加载不到就记住 None（回退 Tesseract，不反复抛异常）。"""
    if which in _DDDD_ENGINES:
        return _DDDD_ENGINES[which]
    engine = None
    try:
        import ddddocr  # 可选依赖；没装时整条链路仍靠 Tesseract 走通

        # show_ad=False 关掉首次运行的横幅文字，免得混进日志
        engine = ddddocr.DdddOcr(
            ocr=True, det=False, beta=(which == "beta"), show_ad=False
        )
    except Exception as exc:
        log("[w] ddddocr(%s) 不可用，回退 Tesseract：%s" % (which, type(exc).__name__))
    _DDDD_ENGINES[which] = engine
    return engine


def _ddddocr_read(image_b64, which):
    """单个 ddddocr 模型读一次，返回清洗后的原始输出（可能为空或长度不是 4）。"""
    engine = _ddddocr_engine(which)
    if engine is None:
        return None
    try:
        raw = base64.b64decode(image_b64.split(",", 1)[-1])
        return _clean_code(engine.classification(raw)) or None
    except Exception as exc:
        log("[w] ddddocr(%s) 识别异常：%s" % (which, type(exc).__name__))
        return None


def recognize(image_b64):
    """把验证码 base64 图转成 4 位码，凑不齐就返回 None。

    主引擎 ddddocr beta（20 张标注样本 19/20）。它有两个坑必须靠长度校验兜住：
    长度多于 4 位说明把干扰圆环吃成了 O（M483 读成 M4083），少于 4 位说明漏字
    （DPJS 读成 DJS）；这两种情况依次交给默认模型、Tesseract 多变体投票重读。
    三源串联在同一批样本上是 20/20。
    """
    for which in DDDDOCR_MODELS:
        code = _ddddocr_read(image_b64, which)
        if code and len(code) == 4:
            return code
    return _tesseract_recognize(image_b64)


def _tesseract_recognize(image_b64):
    """Tesseract 兜底：不同预处理变体逐位投票，仍凑不齐 4 位就返回 None。"""
    raw = base64.b64decode(image_b64.split(",", 1)[-1])
    rgb = np.asarray(Image.open(io.BytesIO(raw)).convert("RGB")).astype(np.float32)
    sat = rgb.max(2) - rgb.min(2)
    lum = 0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]
    votes = [dict() for _ in range(4)]
    for src, thin, scale, psm in OCR_VARIANTS:
        gray = (255.0 - sat) if src == "sat" else lum
        if thin:
            gray = np.where(_run_mask(sat > 55, thin), gray, 255.0)
        got = _ocr(_render(gray, scale), psm)
        if len(got) != 4:
            continue
        for i, ch in enumerate(got):
            votes[i][ch] = votes[i].get(ch, 0) + 1
    if any(not v for v in votes):
        return None
    return "".join(max(v.items(), key=lambda kv: kv[1])[0] for v in votes)


# ------------------------------------------------------------ 登录动作
def _local_storage(page):
    return page.evaluate(
        """() => {const o = {}; for (let i = 0; i < localStorage.length; i++) {
             const k = localStorage.key(i); o[k] = localStorage.getItem(k);} return o;}"""
    )


def _login_accepted(payload):
    if not isinstance(payload, dict):
        return False
    code = payload.get("code")
    return code == 1 or code == "1"


def _submit_once(ctx, creds, persist=True, dummy_code=None):
    """一次完整尝试：新开页面 → 识别验证码 → 提交 → 校验 token → 落盘。

    persist=False 时只验证链路，不写任何会话文件（--self-test 用）。
    dummy_code 用于识别失败时也把提交动作走通（只给自测）。
    返回 (ok, reason)。reason 只用于日志分类，不含敏感内容。
    """
    page = ctx.new_page()
    captcha_responses = []
    page.on("response",
            lambda r: captcha_responses.append(r) if "/auth/captcha" in r.url else None)
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
        deadline = time.time() + 25
        while not captcha_responses and time.time() < deadline:
            page.wait_for_timeout(250)
        if not captcha_responses:
            return False, "captcha_missing"

        try:
            data = captcha_responses[-1].json()
        except Exception:
            return False, "captcha_bad_json"
        image = ((data or {}).get("data") or {}).get("image") or ""
        if not image:
            return False, "captcha_no_image"
        code = recognize(image)
        if not code:
            if dummy_code:
                code = dummy_code
            else:
                return False, "ocr_incomplete"

        inputs = page.locator("input")
        inputs.nth(0).fill(creds["username"])
        inputs.nth(1).fill(creds["password"])
        inputs.nth(2).fill(code)
        # 隐私协议复选框：不勾选时按钮点了不提交
        try:
            cb = page.locator("uni-checkbox").first
            if cb.count():
                cb.click(timeout=3000)
        except Exception:
            pass

        try:
            with page.expect_response(
                lambda r: "/auth/login" in r.url, timeout=25000
            ) as info:
                page.locator("uni-button.view-form-box-btn").first.click(timeout=10000)
            payload = info.value.json()
        except PlaywrightTimeoutError:
            return False, "login_no_response"
        except Exception:
            return False, "login_bad_json"

        if not _login_accepted(payload):
            code_value = payload.get("code") if isinstance(payload, dict) else None
            return False, "login_rejected:%s" % str(code_value)[:12]

        items = _local_storage(page)
        token = str(items.get("xgUniToken") or "").strip()
        if not token:
            return False, "token_missing"

        ok, detail = token_valid(token)
        if not ok:
            return False, "token_rejected:%s" % detail

        if not persist:  # 自测模式永远不落盘
            log("[i] 链路自测：登录成功但按 --self-test 不写入会话文件")
            return True, "self_test_ok"

        state = ctx.storage_state()
        atomic_write_json(STATE_FILE, state, 0o600)

        session = {"token": token, "saved_at": _now(), "name": "", "student_no": "", "gender": ""}
        try:
            user = json.loads(items.get("user") or "{}")
        except Exception:
            user = {}
        if isinstance(user, dict):
            session["name"] = str(user.get("xm") or "")
            session["student_no"] = str(user.get("xh") or "")
            session["gender"] = str(user.get("xb") or "")
        session["name"] = session["name"] or str(items.get("xm") or "")
        atomic_write_json(SESSION_FILE, session, 0o600)

        log("[OK] 登录成功，token fp=%s 已通过接口校验" % _token_fp(token))
        return True, "ok"
    except Exception as exc:
        # 带异常类型和简短原因：区分「断网/超时」和「页面结构变了」全靠这行
        brief = " ".join(str(exc).split())[:60]
        return False, ("%s: %s" % (type(exc).__name__, brief)) if brief else type(exc).__name__
    finally:
        try:
            page.close()
        except Exception:
            pass


# ------------------------------------------------------------ 告警
def _alert(reason):
    """全部尝试失败后的告警：30 分钟内只发一次，私聊优先、群通道兜底。"""
    st = _read_json(ALERT_STATE_FILE) or {}
    last = st.get("last_alert_ts") or 0
    if time.time() - float(last) < ALERT_COOLDOWN_SEC:
        log("[i] 告警冷却中，本次不再重复提醒")
        return
    text = (
        "【校园活动机器人】自动登录连续 %d 次失败（最近原因：%s），"
        "推送会随 token 过期停摆。需要人工登录：本机 python scripts/login.py，"
        "再按 README「状态失效了怎么办」同步 storage_state.json 到服务器。"
    ) % (MAX_ATTEMPTS, reason or "unknown")

    ok = None
    try:
        sys.path.insert(0, os.path.join(APP_DIR, "scripts"))
        from notify import send_private, send
    except Exception as exc:
        log("[x] 告警模块不可用：%s" % type(exc).__name__)
        return
    try:
        ok = send_private(text)
    except Exception as exc:
        log("[i] 私聊通道异常：%s" % type(exc).__name__)
    if ok is not True:
        try:
            ok = send(text)
            if ok is True:
                log("[!] 告警已发到群通道（私聊不可用）")
        except Exception as exc:
            log("[x] 群通道异常：%s" % type(exc).__name__)
            ok = False
    else:
        log("[!] 告警已私聊 owner")
    if ok is True:
        atomic_write_json(ALERT_STATE_FILE, {"last_alert_ts": int(time.time())}, 0o600)


# ------------------------------------------------------------ 自测
def _probe_says_failing():
    """读 token_probe.log 最后一行判断最近一次探测结果。

    返回 True=失败（该尝试登录）、False=正常（跳过）、None=日志缺失/过旧（调用方退回接口校验）。
    正常时段靠这个判据跳过登录，不给学校接口增加任何请求。
    """
    try:
        with open(PROBE_LOG, "r", encoding="utf-8", errors="replace") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
    except Exception:
        return None
    if not lines:
        return None
    tokens = lines[-1].split()
    if len(tokens) < 2:
        return None
    try:
        when = datetime.strptime(tokens[0] + " " + tokens[1], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    if (datetime.now() - when).total_seconds() > PROBE_STALE_SEC:
        return None
    for token in tokens:
        if token.startswith("result="):
            result = token[len("result="):]
            if result == "ok":
                return False
            if result in ("fail", "no-session", "no-token"):
                return True
            # http-error / bad-json 这类是「判定不了」，交给调用方现查接口；
            # 否则一次断网就会被当成登录态失效，白跑一轮重新登录。
            return None
    return None


def _session_fresh(seconds=600):
    """会话文件是不是刚刚更新过（刚登录成功就别再试一遍密码）。"""
    session = _read_json(SESSION_FILE) or {}
    stamp = str(session.get("saved_at") or "").strip()
    if not stamp:
        return False
    try:
        when = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return (datetime.now() - when).total_seconds() < seconds


def _clear_alert_state():
    """登录态被确认有效时清掉告警冷却。

    否则一次误报（比如断网被当成失效）会把冷却期内的真失效告警一起压掉。
    """
    try:
        st = _read_json(ALERT_STATE_FILE) or {}
        st["last_alert_ts"] = 0
        atomic_write_json(ALERT_STATE_FILE, st, 0o600)
    except Exception:
        pass


def _fp_age_hours(token):
    """这枚 token 已经用了多少小时；首次见到时记为 0 并落盘。

    不能用 session.json 的 saved_at 算寿命：server_refresh.js 每 2 小时校验
    成功后会重写 saved_at，它表示「上次校验时间」而不是「签发时间」，
    拿它算永远到不了 PREEMPT_HOURS，--preempt 就永远不触发。
    指纹只是 token 的 sha1 前 8 位，不含 token 原文。
    """
    fp = _token_fp(token)
    if not fp:
        return None
    st = _read_json(ALERT_STATE_FILE) or {}
    seen = st.get("token_first_seen")
    if not isinstance(seen, dict):
        seen = {}
    ts = seen.get(fp)
    if not ts:
        seen[fp] = int(time.time())
        # 只留最近 5 个指纹：这是排障用的辅助记录，不能无限增长
        for old in sorted(seen, key=lambda k: seen[k])[:-5]:
            seen.pop(old, None)
        st["token_first_seen"] = seen
        try:
            atomic_write_json(ALERT_STATE_FILE, st, 0o600)
        except Exception:
            pass
        return 0.0
    return (time.time() - float(ts)) / 3600.0


def _self_test():
    """用假账号走通整条链路：页面加载 → 验证码拦截 → OCR → 填表提交 → 响应解析。

    不读 credentials.json、不写任何会话文件；预期被服务端拒绝（账号不存在），
    只要拿到 /auth/login 的响应就说明链路正常。
    """
    creds = {"username": "selftest-not-real-user", "password": "selftest"}
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        try:
            ctx = browser.new_context(locale="zh-CN", viewport={"width": 1366, "height": 900})
            try:
                ok, reason = _submit_once(ctx, creds, persist=False, dummy_code="0000")
            finally:
                ctx.close()
        finally:
            browser.close()
    print("[i] 自测结果：ok=%s reason=%s（假账号，预期 login_rejected:*；报异常/超时才是链路问题）"
          % (ok, reason))
    return 0


def dry_run(count, verbose=False):
    """不开浏览器：直接请求验证码接口测识别率（不提交登录、不落任何会话文件）。"""
    out_dir = os.path.join(DRY_RUN_DIR, datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    ok = 0
    per_engine = dict((which, 0) for which in DDDDOCR_MODELS)
    for i in range(count):
        body = json.dumps({"client": CLIENT, "userType": "2"}).encode("utf-8")
        req = urllib.request.Request(CAPTCHA_API, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Referer", REFERER)
        req.add_header("Origin", "https://xg.tjise.edu.cn")
        req.add_header("User-Agent", UA)
        try:
            resp = urllib.request.urlopen(req, timeout=20)
            try:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            finally:
                resp.close()
        except Exception as exc:
            print("sample %02d http-error %s" % (i, type(exc).__name__))
            continue
        image = ((data or {}).get("data") or {}).get("image") or ""
        if not image:
            print("sample %02d no-image" % i)
            continue
        with open(os.path.join(out_dir, "cap%02d.png" % i), "wb") as f:
            f.write(base64.b64decode(image.split(",", 1)[-1]))
        code = recognize(image)
        if code:
            ok += 1
        # 顺带统计单引擎表现，方便 A/B 判断回退链是否还必要
        for which in DDDDOCR_MODELS:
            got = _ddddocr_read(image, which)
            if got and len(got) == 4:
                per_engine[which] += 1
        if verbose:
            print("sample %02d code=%s" % (i, code or "<未识别>"))
    rate = (ok / float(count)) if count else 0.0
    # 注意：这里统计的是「凑齐 4 位」的比例，不等于识别正确率；
    # 正确率要用有人工标注的样本另算（20 张标注样本实测：融合 100%，单 ddddocr beta 95%）。
    print("[i] 融合凑齐 4 位 %d/%d = %.0f%%；ddddocr 单独：%s；样本图片：%s"
          % (ok, count, rate * 100,
             "，".join("%s %d/%d" % (which, per_engine[which], count) for which in DDDDOCR_MODELS),
             out_dir))
    return 0


# ------------------------------------------------------------ 主流程
class _Lock:
    """同一时刻只允许一个实例，避免两个 Chromium 抢同一份状态文件。"""

    def __init__(self, path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.handle = open(self.path, "w")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            return False
        return True

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
        except Exception:
            pass


def _load_credentials():
    if not os.path.exists(CREDENTIALS_FILE):
        log("[x] 缺少 credentials.json，无法自动登录")
        return None
    mode = os.stat(CREDENTIALS_FILE).st_mode & 0o777
    if mode & 0o077:
        log("[x] credentials.json 权限为 %o，必须是 600，拒绝使用" % mode)
        return None
    data = _read_json(CREDENTIALS_FILE)
    if not isinstance(data, dict) or not data.get("username") or not data.get("password"):
        log("[x] credentials.json 内容不完整")
        return None
    return {"username": str(data["username"]), "password": str(data["password"])}


def main():
    parser = argparse.ArgumentParser(description="xgUniToken 无人值守重新登录")
    parser.add_argument("--check", action="store_true", help="只读校验当前 token")
    parser.add_argument("--force", action="store_true", help="不论 token 状态都登录")
    parser.add_argument("--self-test", action="store_true", help="用假账号验链路（不落盘）")
    parser.add_argument("--trigger", choices=("log", "api"), default="log",
                        help="失效判据：log=读 token_probe.log（默认，零额外请求）；api=现查接口")
    parser.add_argument("--preempt", action="store_true",
                        help="token 用到 %.0f 小时就主动换新，消掉失效前的盲窗" % PREEMPT_HOURS)
    parser.add_argument("--dry-run", type=int, metavar="N", help="取 N 张验证码验识别率")
    parser.add_argument("--verbose", action="store_true", help="dry-run 时打印识别结果")
    args = parser.parse_args()

    if args.self_test:
        return _self_test()
    if args.dry_run:
        return dry_run(args.dry_run, args.verbose)

    session = _read_json(SESSION_FILE) or {}
    token = str(session.get("token") or "").strip()

    if args.check:
        ok, detail = token_valid(token)
        print("TOKEN_VALID=%d detail=%s" % (1 if ok else 0, detail))
        return 0 if ok else 1

    if not args.force:
        # --preempt：不等失效，用到接近寿命上限就换新。这是唯一会「token 还有效
        # 也去登录」的正常路径，所以要先把网络问题挡在外面，否则断网会被当成
        # 该换新，白试一轮验证码。
        preempt_due = False
        if args.preempt:
            age = _fp_age_hours(token)
            if age is not None and age >= PREEMPT_HOURS:
                ok, detail = token_valid(token)
                if detail.startswith(("http-error", "bad-json")):
                    log("[i] 探测不到学校接口（%s），本轮跳过，等下一轮" % detail[:40])
                    return 0
                log("[i] token 已用 %.1f 小时（阈值 %.0f），主动换新"
                    % (age, PREEMPT_HOURS))
                preempt_due = True

        if not preempt_due and args.trigger == "log":
            verdict = _probe_says_failing()
            if verdict is False:
                log("[i] token_probe 最近一次结果为 ok，跳过自动登录")
                _clear_alert_state()
                return 0
            if verdict is None:
                ok, detail = token_valid(token)
                if ok:
                    log("[i] token 有效（fp=%s），跳过自动登录" % _token_fp(token))
                    _clear_alert_state()
                    return 0
                if detail.startswith(("http-error", "bad-json")):
                    log("[i] 探测不到学校接口（%s），本轮跳过，等下一轮" % detail[:40])
                    return 0
                log("[i] token 失效（%s），开始自动登录" % detail)
            else:
                log("[i] token_probe 最近一次结果为失败，开始自动登录")
        elif not preempt_due:
            ok, detail = token_valid(token)
            if ok:
                log("[i] token 有效（fp=%s），跳过自动登录" % _token_fp(token))
                _clear_alert_state()
                return 0
            if detail.startswith(("http-error", "bad-json")):
                log("[i] 探测不到学校接口（%s），本轮跳过，等下一轮" % detail[:40])
                return 0
            log("[i] token 失效（%s），开始自动登录" % detail)

        # 刚登录成功没多久，probe 日志里可能还留着上一轮的失败行，
        # 这里按会话时间挡一次，避免 5 分钟内白试两轮密码。
        if _session_fresh():
            log("[i] 会话文件 10 分钟内刚更新过，跳过本轮")
            return 0

    # 已告警过的失效周期在冷却期内不再反复试密码，避免触发学校账号风控（--force 除外）
    if not args.force:
        alert_state = _read_json(ALERT_STATE_FILE) or {}
        if time.time() - float(alert_state.get("last_alert_ts") or 0) < ALERT_COOLDOWN_SEC:
            log("[i] 上一轮自动登录失败并已告警，冷却期内不再尝试")
            return 0

    creds = _load_credentials()
    if not creds:
        return 3

    with _Lock(LOCK_FILE) as acquired:
        if not acquired:
            log("[i] 已有另一个自动登录进程在跑，本次跳过")
            return 0

        reason = "unknown"
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            try:
                for attempt in range(1, MAX_ATTEMPTS + 1):
                    ctx = browser.new_context(
                        locale="zh-CN", viewport={"width": 1366, "height": 900}
                    )
                    try:
                        ok, reason = _submit_once(ctx, creds)
                    finally:
                        try:
                            ctx.close()
                        except Exception:
                            pass
                    if ok:
                        atomic_write_json(ALERT_STATE_FILE, {"last_alert_ts": 0}, 0o600)
                        return 0
                    log("[i] 第 %d/%d 次自动登录失败：%s" % (attempt, MAX_ATTEMPTS, reason))
                    if attempt < MAX_ATTEMPTS:
                        log("[i] %d 秒后换一张验证码重试" % RETRY_DELAY_SEC)
                        time.sleep(RETRY_DELAY_SEC)
            finally:
                browser.close()

        _alert(reason)
        return 2


if __name__ == "__main__":
    sys.exit(main())
