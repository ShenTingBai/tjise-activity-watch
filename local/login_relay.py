# -*- coding: utf-8 -*-
"""手机重登中继的 worker 侧编排：验证码挑战、人工提交与新 token 结算。

Playwright 端口与 grab 本地函数全部由调用方注入，本模块不反向依赖 grab、
不自己拼登录 HTTP、不逆向学校接口：由页面自己发 captcha/login，这里只
观察响应、读 body、填表与结算。stdout 只打印 [login] 安全 JSON 行——
uuid/token/user/password 一律不外传。

安全边界：凭据只从 stdin 收一次、用完即弃；挑战必须 flow_id/challenge_id
匹配、4 位字母数字、60 秒内、每挑战只消费一次；最多 3 次人工提交、每次
换图、换图间隔 ≥3 秒、总时长 10 分钟。成功必须「本场 login 应答成功」且
「本场新 token 校验通过」同时成立，旧 token 轮询成功不能冒充新登录。

@author LiXinwen
"""
from __future__ import annotations

import base64
import binascii
import json
import queue
import re
import threading

# 挑战与预算（决策 5）
CHALLENGE_TTL_S = 60.0
MAX_MANUAL_TRIES = 3
REFRESH_MIN_GAP_S = 3.0
TOTAL_BUDGET_S = 600.0
MAX_IMAGE_BYTES = 128 * 1024
CODE_RE = re.compile(r"^[0-9A-Za-z]{4}$")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
LOGIN_PREFIX = "[login] "

# 页面字段（已读 server 参考）：结构不符必须安全失败，不能假成功
INPUT_SELECTOR = "input"
CHECKBOX_SELECTOR = "uni-checkbox"
SUBMIT_SELECTOR = "uni-button.view-form-box-btn"
TOKEN_KEY = "xgUniToken"
VERIFY_PAYLOAD = {"pageSize": 1, "pageNum": 1, "hdmc": "", "hdzt": None,
                  "kssj": "", "jssj": ""}


# ---- 纯规则 ---------------------------------------------------------------
def valid_code(code) -> bool:
    """人工验证码：恰好 4 位 ASCII 字母数字。"""
    return bool(CODE_RE.fullmatch(str(code or "")))


def parse_client_line(line):
    """stdin 行 → dict；畸形/超长一律 None（不猜、不抛）。"""
    text = str(line or "").strip()
    if not text or len(text) > 4096:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def extract_captcha_image(payload):
    """响应体里的 data.image → 合法 base64 PNG（≤128KiB），否则 None。"""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    image = data.get("image")
    if not isinstance(image, str) or not image.startswith("data:image/png;base64,"):
        return None
    raw = image.split(",", 1)[1]
    if len(raw) > MAX_IMAGE_BYTES:
        return None
    try:
        blob = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not blob.startswith(PNG_MAGIC):
        return None
    return image


class ChallengeBook:
    """挑战期状态机（纯逻辑，时刻由调用方传入）。

    只认本场 flow_id；每挑战只消费一次；过期/换图/重复/旧 flow 都不得
    复用旧码。失败只回安全码，不回任何输入内容。
    """

    def __init__(self, flow_id, ttl_s=CHALLENGE_TTL_S, max_tries=MAX_MANUAL_TRIES,
                 refresh_gap_s=REFRESH_MIN_GAP_S):
        self.flow_id = str(flow_id)
        self.ttl_s = float(ttl_s)
        self.max_tries = int(max_tries)
        self.refresh_gap_s = float(refresh_gap_s)
        self.challenge_id = ""
        self.issued_at = 0.0
        self.consumed = False
        self.tries = 0
        self.last_refresh = 0.0
        self.login_ok = False
        self.token_before = ""

    def may_refresh(self, now) -> bool:
        return (float(now) - self.last_refresh) >= self.refresh_gap_s

    def open_challenge(self, challenge_id, now) -> bool:
        """换图/新挑战：旧码立即作废；刷新间隔不足时拒绝换图。

        人工提交次数是本场计数，不随换图清零——「最多 3 次人工提交、
        每次换图」说的就是这一场的总次数。
        """
        if not self.may_refresh(now):
            return False
        self.challenge_id = str(challenge_id or "")
        self.issued_at = float(now)
        self.consumed = False
        self.last_refresh = float(now)
        return bool(self.challenge_id)

    def accept(self, flow_id, challenge_id, code, now):
        """提交判定：返回 (接受?, 错误码)。接受后该挑战立即作废。"""
        if str(flow_id or "") != self.flow_id:
            return False, "stale_flow"
        if self.tries >= self.max_tries:
            return False, "max_tries"
        if not self.challenge_id or self.consumed:
            return False, "consumed"
        if str(challenge_id or "") != self.challenge_id:
            return False, "stale_challenge"
        if float(now) - self.issued_at > self.ttl_s:
            return False, "expired"
        if not valid_code(code):
            return False, "bad_code"
        self.consumed = True
        self.tries += 1
        return True, ""


# ---- 页面填表 -------------------------------------------------------------
def fill_login_form(page, username, password, code) -> None:
    """按 server 参考填表并提交；页面结构不符时抛异常，上层安全失败。"""
    inputs = page.locator(INPUT_SELECTOR)
    inputs.nth(0).fill(str(username))
    inputs.nth(1).fill(str(password))
    inputs.nth(2).fill(str(code))
    page.locator(CHECKBOX_SELECTOR).click()
    page.locator(SUBMIT_SELECTOR).click()


def read_token(page) -> str:
    try:
        return str(page.evaluate(
            "() => (localStorage.getItem('%s') || '').trim()" % TOKEN_KEY) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def page_local_storage(page):
    return page.evaluate(
        "() => { const o = {}; for (let i = 0; i < localStorage.length; i++) {"
        " const k = localStorage.key(i); o[k] = localStorage.getItem(k); } return o; }")


# ---- 主编排 ---------------------------------------------------------------
def _emit_line(stdout, flow_id, **fields):
    payload = {"flow_id": str(flow_id or "")}
    payload.update(fields)
    try:
        stdout.write(LOGIN_PREFIX + json.dumps(payload, ensure_ascii=False) + "\n")
        stdout.flush()
    except Exception:  # noqa: BLE001 - 管道断了由收口处理
        pass


def _start_stdin_reader(stdin, inbox):
    """stdin 由 daemon reader 逐行有界解析入队：主线程不阻塞在读上。"""

    def read_loop():
        try:
            for line in stdin or []:
                data = parse_client_line(line)
                if data is not None:
                    inbox.put(data)
        finally:
            inbox.put({"kind": "_eof"})

    thread = threading.Thread(target=read_loop, name="relay-stdin", daemon=True)
    thread.start()
    return thread


def _wait_credentials(inbox, deps):
    """等首条 credentials；超时/EOF/别的一律 None（安全失败）。"""
    deadline = deps.now() + 30.0
    while True:
        try:
            item = inbox.get(timeout=0.2)
        except queue.Empty:
            deps.sleep(0.2)
            if deps.now() >= deadline:
                return None
            continue
        if item.get("kind") == "_eof":
            return None
        if item.get("kind") != "credentials":
            continue
        user = item.get("username")
        secret = item.get("password")
        if not isinstance(user, str) or not isinstance(secret, str) or not user or not secret:
            return None
        return {"flow_id": str(item.get("flow_id") or ""), "username": user,
                "password": secret}


def run_relay(*, deps, stdin, stdout) -> int:
    """worker 登录编排主循环。返回 0=成功；其余都是安全失败。

    deps 需提供：open_page() -> (page, ctx, browser)、post(path, payload, tok)、
    save_session(dict)、save_state(dict)、now()、sleep(s)、new_id()、login_url。
    stdin/stdout 是文本管道；stdout 只允许 [login] JSON 行。
    """
    inbox: "queue.Queue" = queue.Queue()
    reader = _start_stdin_reader(stdin, inbox)
    try:
        creds = _wait_credentials(inbox, deps)
        if creds is None:
            _emit_line(stdout, "", kind="failed", error="eof")
            return 1
        return _relay_session(deps, stdout, inbox, creds)
    finally:
        reader.join(timeout=0.1)   # daemon reader：管道未关也不把收尾挂住


def _relay_session(deps, stdout, inbox, creds):
    flow_id = creds["flow_id"]
    username = creds.pop("username")
    password = creds.pop("password")
    deadline = float(deps.now()) + TOTAL_BUDGET_S
    try:
        try:
            page, ctx, browser = deps.open_page()
        except Exception:  # noqa: BLE001 - 页面打不开不回堆栈
            _emit_line(stdout, flow_id, kind="failed", error="bad_page")
            return 2
        book = ChallengeBook(
            flow_id,
            ttl_s=float(getattr(deps, "challenge_ttl_s", CHALLENGE_TTL_S)),
            max_tries=int(getattr(deps, "max_tries", MAX_MANUAL_TRIES)),
            refresh_gap_s=float(getattr(deps, "refresh_gap_s", REFRESH_MIN_GAP_S)))
        responses: "queue.Queue" = queue.Queue()
        try:
            page.on("response", lambda resp: _enqueue_response(responses, resp))
            page.goto(deps.login_url)
            book.token_before = read_token(page)
        except Exception:  # noqa: BLE001
            _close_all(ctx, browser)
            _emit_line(stdout, flow_id, kind="failed", error="bad_page")
            return 2
        return _drive_loop(deps, stdout, inbox, responses, book, page, ctx,
                           browser, flow_id, username, password, deadline)
    finally:
        # 函数作用域即销毁：不留任何凭据副本
        username = ""
        password = ""


def _enqueue_response(responses, resp):
    """响应回调只入队 captcha/login 响应；body 由主线程读，不做事件线程 IO。"""
    try:
        url = str(resp.url)
    except Exception:  # noqa: BLE001
        return
    if "captcha" in url or "login" in url:
        responses.put(resp)


def _drive_loop(deps, stdout, inbox, responses, book, page, ctx, browser,
                flow_id, username, password, deadline):
    while True:
        now = float(deps.now())
        if now >= deadline:
            _close_all(ctx, browser)
            _emit_line(stdout, flow_id, kind="failed", error="timeout")
            return 1
        if _page_dead(page):
            _close_all(ctx, browser)
            _emit_line(stdout, flow_id, kind="failed", error="bad_page")
            return 2
        # 先结算响应（挑战/登录应答）再收人工提交：挑战没开时不得误拒真码
        _drain_responses(deps, stdout, responses, book, flow_id)
        outcome = _drain_inbox(deps, inbox, stdout, book, page, flow_id,
                               username, password, now)
        if outcome is not None:
            _close_all(ctx, browser)
            error = {1: "eof", 2: "bad_page", 5: "canceled"}.get(outcome, "internal")
            _emit_line(stdout, flow_id, kind="failed", error=error)
            return outcome
        if book.login_ok:
            code = _settle_success(deps, stdout, page, ctx, flow_id, book)
            _close_all(ctx, browser)
            return code
        _pause(deps, page)


def _page_dead(page) -> bool:
    try:
        return bool(page.is_closed())
    except Exception:  # noqa: BLE001
        return True


def _pause(deps, page) -> None:
    try:
        page.wait_for_timeout(50)
    except Exception:  # noqa: BLE001
        deps.sleep(0.05)


def _drain_inbox(deps, inbox, stdout, book, page, flow_id,
                 username, password, now):
    """处理 stdin 消息；返回 None 继续，返回退出码表示要收口。"""
    while True:
        try:
            item = inbox.get_nowait()
        except queue.Empty:
            return None
        kind = str(item.get("kind") or "")
        if kind == "_eof":
            return 1
        if kind == "cancel":
            return 5
        if kind != "submit":
            continue
        accepted, error = book.accept(item.get("flow_id"),
                                      item.get("challenge_id"),
                                      item.get("code"), now)
        if not accepted:
            _emit_line(stdout, flow_id, kind="rejected", reason=error,
                       challenge_id=book.challenge_id, attempts=book.tries)
            continue
        _emit_line(stdout, flow_id, kind="verifying",
                   challenge_id=book.challenge_id)
        try:
            fill_login_form(page, username, password, item.get("code"))
        except Exception:  # noqa: BLE001 - 页面结构不符：安全失败
            return 2


def _drain_responses(deps, stdout, responses, book, flow_id):
    while True:
        try:
            resp = responses.get_nowait()
        except queue.Empty:
            return
        try:
            url = str(resp.url)
            body = resp.body()
        except Exception:  # noqa: BLE001
            continue
        if len(body) > MAX_IMAGE_BYTES * 2:
            continue
        try:
            payload = json.loads(bytes(body).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if "captcha" in url:
            image = extract_captcha_image(payload)
            if image is None:
                continue
            now = float(deps.now())
            if not book.may_refresh(now):
                continue          # 换图间隔不足：沿用当前挑战，不复用旧码
            challenge_id = str(deps.new_id())
            if not book.open_challenge(challenge_id, now):
                continue
            _emit_line(stdout, flow_id, kind="challenge",
                       challenge_id=challenge_id, image=image,
                       expires_in=int(CHALLENGE_TTL_S), attempts=book.tries,
                       max_attempts=book.max_tries)
        elif "login" in url:
            # 只有本场 login 应答成功才算登录成功；旧 token 轮询成功不算
            code = payload.get("code") if isinstance(payload, dict) else None
            if str(code) == "1":
                book.login_ok = True


def _settle_success(deps, stdout, page, ctx, flow_id, book):
    """结算：本场**新** token 校验通过才 success，失败不碰旧会话。"""
    token = read_token(page)
    if not token or token == book.token_before:
        # 没换到新 token：不能把旧会话的成功冒充成本次登录
        _emit_line(stdout, flow_id, kind="failed", error="token_unverified")
        return 3
    try:
        result = deps.post("/rjyHd/listActivity", dict(VERIFY_PAYLOAD), token)
    except Exception:  # noqa: BLE001
        _emit_line(stdout, flow_id, kind="failed", error="token_unverified")
        return 3
    if not (isinstance(result, dict) and str(result.get("code")) == "1"):
        _emit_line(stdout, flow_id, kind="failed", error="token_unverified")
        return 3
    try:
        items = page_local_storage(page)
        state = ctx.storage_state()
        deps.save_state(state)
        deps.save_session(_session_from_items(items, token))
    except Exception:  # noqa: BLE001
        _emit_line(stdout, flow_id, kind="failed", error="internal")
        return 4
    _emit_line(stdout, flow_id, kind="success", verified=True)
    return 0


def _session_from_items(items, token):
    session = {"token": str(token), "saved_at": "", "name": "", "student_no": "",
               "gender": ""}
    try:
        user = json.loads(items.get("user") or "{}")
    except Exception:  # noqa: BLE001
        user = {}
    if isinstance(user, dict):
        session["name"] = str(user.get("xm") or "")
        session["student_no"] = str(user.get("xh") or "")
        session["gender"] = str(user.get("xb") or "")
    session["name"] = session["name"] or str(items.get("xm") or "")
    return session


def _close_all(ctx, browser):
    for closer in (getattr(ctx, "close", None), getattr(browser, "close", None)):
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass
