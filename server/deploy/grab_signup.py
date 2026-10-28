#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""到点自动报名（单账号）。

背景：活动名额在开放瞬间被抢光，人工点太慢。本脚本在报名开放前把详情页加载好，
到点自动点「立即报名 → 确定」，成功后私聊发卡片通知。

边界：
- 只操作 session.json 里那一个账号，不代他人报名；
- 只在传入的单个 activity-id 上动作，不批量扫活动；
- 失败不无限重试：默认 120 秒窗口，之后放弃并私聊告知失败原因；
  连续 404 视为接口未就绪，退避重试并把窗口自适应延长（起点起最长 25 分钟），
  传入 --sign-end 时报名截止是硬上限，扩窗绝不越过它；
- 不记录 token、姓名、学号；日志只留结果分类与时间。

提交节奏与本机 local/grab.py 共用同一套阶段/字段契约（独立实现，互不导入）：
开放后 0~1.6 秒黄金风暴（最多 10 轮、间隔 90ms）→ 常规全速（180ms）→
连续 3 次 404 自适应退避（1.6 倍、上限 3 秒）。每笔真实请求唯一记账，
响应按 request 归属结算，乱序合法；未决不伪造成失败，停止时如实报未确认。

用法：
  python3 grab_signup.py --activity-id <id> --at "2026-10-22 00:59:59" \
      --name "活动名" [--lead 100] [--window 120] [--sign-end "2026-10-23 23:59:59"] \
      [--dry-run]

@author LiXinwen
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta

from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

LOGIN_DIR = "/root/tjise-login"
APP_DIR = "/root/tjise-activity-watch"
STATE_FILE = os.path.join(LOGIN_DIR, "storage_state.json")
SESSION_FILE = os.path.join(APP_DIR, "data", "session.json")
# 测试用 GRAB_LOG_FILE 重定向日志落点，绝不写真实 /root/tjise-login
LOG_FILE = os.path.join(LOGIN_DIR, "grab_signup.log")
API = "https://xg.tjise.edu.cn/dr/api"
REFERER = "https://xg.tjise.edu.cn/stu/"
# 开放前多少秒开始逐次看按钮：名额放开往往只有几秒窗口，
# 等到开放前一秒再刷新会丢掉首轮点击时机。
POLL_LEAD = 5.0
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

sys.path.insert(0, os.path.join(APP_DIR, "scripts"))


def log(msg):
    # 落点每次调用时解析：测试重定向到临时文件，不会碰真实登录目录
    path = os.environ.get("GRAB_LOG_FILE") or LOG_FILE
    line = "%s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass    # 日志写不进去不能反过来把报名流程炸掉
    print(line, flush=True)


def _token():
    try:
        with open(SESSION_FILE, "r", encoding="utf-8") as f:
            return str(json.load(f).get("token") or "").strip()
    except Exception:
        return ""


def _post(path, payload=None, token=""):
    body = json.dumps(payload if payload is not None else {}).encode("utf-8")
    req = urllib.request.Request(API + path, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/plain, */*")
    req.add_header("token", token)
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


def is_joined(act_id):
    """getJoinInfo：未报名时 data 为 null，报名后 data 是对象。"""
    data = _post("/rjyHdCyr/getJoinInfo/" + act_id, {}, _token())
    if not isinstance(data, dict) or data.get("code") != 1:
        return False
    return data.get("data") not in (None, {}, [])


def ensure_token():
    """报名前确认 token 可用；失效就先跑一次自动登录。"""
    chk = subprocess.run(["/usr/bin/python3", os.path.join(LOGIN_DIR, "auto_login.py"), "--check"],
                         capture_output=True, text=True, timeout=90)
    if chk.returncode == 0:
        return True
    log("[i] token 已失效，先执行自动登录")
    run = subprocess.run(["/usr/bin/python3", os.path.join(LOGIN_DIR, "auto_login.py"), "--force"],
                         capture_output=True, text=True, timeout=300)
    return run.returncode == 0


def notify(title, lines, template="green"):
    """私聊发交互卡片：优先用 users.json 里登记的 p2p chat_id；
    没登记（owner 还没私聊过机器人）就直接按 open_id 发，避免静默退化到群发。"""
    try:
        import notify as nt
        cfg = nt.load_config()["notify"]
        app = cfg.get("feishu_app") or {}
        token = nt._tenant_token(app) if app else None
        if not token:
            log("[x] 取 tenant_access_token 失败，通知未发送")
            return False

        chat_id = nt._p2p_chat_id(cfg)
        owner = os.environ.get("PRIVATE_OWNER_OPEN_ID", "").strip()
        if chat_id:
            rid, rid_type = chat_id, "chat_id"
        elif owner:
            rid, rid_type = owner, "open_id"
        else:
            log("[x] 既无 p2p chat_id 也无 owner open_id，通知未发送")
            return False

        card = {
            "config": {"wide_screen_mode": True},
            "header": {"template": template,
                       "title": {"tag": "plain_text", "content": title}},
            "elements": [{"tag": "div", "text": {"tag": "lark_md",
                                                 "content": "\n".join(lines)}}],
        }
        ok, body = nt._post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=" + rid_type,
            {"receive_id": rid, "msg_type": "interactive",
             "content": json.dumps(card, ensure_ascii=False)}, token=token)
        if ok and nt._business_ok(body, "feishu"):
            log("[i] 私聊卡片已发送（%s）" % rid_type)
            return True
        log("[i] 私聊卡片失败（%s）：%s" % (rid_type, str(body)[:120]))
        return bool(nt.send_private(title + "\n" + "\n".join(lines)))
    except Exception as exc:
        log("[x] 通知失败：%s" % type(exc).__name__)
        return False


def detail_url(act_id):
    return ("https://xg.tjise.edu.cn/stu/#/secondClassroom/student/activity/"
            "studentActivity/activityDetails?activityId=%s&from=acList" % act_id)


def wait_until(ts):
    while True:
        left = ts - time.time()
        if left <= 0:
            return
        time.sleep(min(0.2, left))


# ---------------------------------------------------------------------------
# 黄金风暴 / 常规全速 / 404 自适应（2026-10-28 用户确认方案，与本机契约一致）
#
# 开放瞬间 0~1.6 秒内最多 10 轮、最小间隔 90ms；随后 180ms 常规全速；
# 连续 3 次 404 视为接口未就绪/限流，1.6 倍指数退避、上限 3 秒，并把有效
# 窗口自动扩到起点起 25 分钟；收到非 404 真实响应立即切回全速。
# sign_end 永远是硬截止，扩窗不得越过它。
# ---------------------------------------------------------------------------

GOLDEN_WINDOW_S = 1.6            # 开放后黄金窗口
GOLDEN_INTERVAL_S = 0.09         # 黄金阶段最小间隔（90ms）
GOLDEN_ROUNDS = 10               # 黄金阶段最多轮次
FULL_SPEED_INTERVAL_S = 0.18     # 常规全速最小间隔（180ms）
NOT_READY_404_LIMIT = 3          # 连续 404 视为接口未就绪
BACKOFF_FACTOR = 1.6             # 自适应退避倍率
BACKOFF_MAX_S = 3.0              # 自适应退避上限
ADAPTIVE_WINDOW_S = 1500.0       # 自适应扩窗上限（25 分钟）


class BurstPolicy:
    """黄金风暴/常规全速/404 自适应的显式状态机与真实请求记账。

    纯调度与记账，不做任何 I/O：时间读数全部走注入的 clock（单测里是虚拟
    时钟，绝不真实 sleep 1.6 秒）。四个阶段 golden / full_speed /
    adaptive_backoff / terminal 的转移规则见类常量上方注释。

    不变量：
    * attempts 只数真正发出过的 signUp 请求，单调递增；
    * pending 里每笔都有唯一 request_id，响应按归属结算，乱序合法；
    * 同一 request 重复通知只算一次；别家响应不改本场结论；
    * effective_deadline 只会因自适应扩窗变大，且永不越过 sign_end；
    * hard_deadline 一旦冻结就不再改写，sign_end 截止永远优先于扩窗；
    * 进入 terminal 后拒绝再发起任何动作（用户停止同此）。
    """

    PHASE_GOLDEN = "golden"
    PHASE_FULL_SPEED = "full_speed"
    PHASE_ADAPTIVE = "adaptive_backoff"
    PHASE_TERMINAL = "terminal"

    def __init__(self, open_ts, base_deadline, sign_end_ts=None,
                 max_attempts=0, clock=None, retry_start=None):
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

    # -- 时间读数 ---------------------------------------------------------
    def _now(self):
        if self._clock is None:
            import time as _time
            return _time.monotonic()
        return float(self._clock.monotonic() if hasattr(self._clock, "monotonic")
                     else self._clock())

    def _wall_now(self):
        if self._clock is None:
            import time as _time
            return _time.time()
        return float(self._clock.time() if hasattr(self._clock, "time")
                     else self._clock())

    # -- 阶段与调度 -------------------------------------------------------
    @property
    def phase(self):
        self._refresh_phase()
        return self._phase

    @property
    def round(self):
        """当前轮次计数：黄金阶段是黄金轮次，之后随真实请求递增。"""
        return self.golden_rounds if self._phase == self.PHASE_GOLDEN else self.attempts

    @property
    def backoff_s(self):
        """当前退避间隔（日志字段 backoff_s 的唯一来源）。"""
        if self._phase == self.PHASE_ADAPTIVE:
            return min(BACKOFF_MAX_S, max(FULL_SPEED_INTERVAL_S, self._backoff_s))
        return self._backoff_s

    def _refresh_phase(self):
        """按已观察事实刷新阶段；非 404 响应会让黄金/退避立即退位。"""
        if self._phase == self.PHASE_TERMINAL:
            return
        if self._phase == self.PHASE_GOLDEN:
            now = self._wall_now()
            if self.golden_rounds >= GOLDEN_ROUNDS or now >= self.open_ts + GOLDEN_WINDOW_S:
                self._phase = self.PHASE_FULL_SPEED
                self._backoff_s = FULL_SPEED_INTERVAL_S
            return
        if self._phase == self.PHASE_ADAPTIVE and self.streak_404 < NOT_READY_404_LIMIT:
            self._phase = self.PHASE_FULL_SPEED
            self._backoff_s = FULL_SPEED_INTERVAL_S

    def interval_s(self):
        """下一次尝试前必须等待的最小间隔。"""
        self._refresh_phase()
        if self._phase == self.PHASE_GOLDEN:
            return GOLDEN_INTERVAL_S
        if self._phase == self.PHASE_ADAPTIVE:
            return min(BACKOFF_MAX_S, max(FULL_SPEED_INTERVAL_S, self._backoff_s))
        return FULL_SPEED_INTERVAL_S

    def next_interval_after_wait(self):
        """等待完本次间隔后推进退避计数，返回下一轮间隔。"""
        if self._phase == self.PHASE_ADAPTIVE:
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
        """404 连击触发自适应扩窗：有效截止拉到起点起 25 分钟，sign_end 仍硬截。"""
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
        self._requests[request_id] = {"status": "pending", "kind": None,
                                      "attempt_no": self.attempts + 1}
        self.attempts += 1
        if self._phase == self.PHASE_GOLDEN:
            self.golden_rounds += 1
        return request_id

    def settle(self, request_id, kind):
        """结算一笔请求的响应归属；kind ∈ {404, real, lost}。

        乱序合法：响应可以按任意顺序回来；重复结算同一笔只认第一次。
        404 会加连击并可能进退避；real 清连击并退出退避/黄金。
        lost（响应体丢失/读不出）不结算：请求保持未决，终态如实报「未确认」。
        """
        record = self._requests.get(request_id)
        if record is None or record["status"] == "settled":
            return None
        if kind == "lost":
            record["kind"] = "lost"
            return record          # 不改 status，仍是 pending
        record["status"] = "settled"
        record["kind"] = kind
        if kind == "404":
            self.streak_404 += 1
            if self.streak_404 >= NOT_READY_404_LIMIT:
                if self._phase != self.PHASE_TERMINAL:
                    self._phase = self.PHASE_ADAPTIVE
                    self._backoff_s = min(BACKOFF_MAX_S,
                                          FULL_SPEED_INTERVAL_S * BACKOFF_FACTOR)
                self.extend_deadline_for_adaptive()
        elif kind == "real":
            self.streak_404 = 0
            if self._phase == self.PHASE_GOLDEN:
                self._phase = self.PHASE_FULL_SPEED
                self._backoff_s = FULL_SPEED_INTERVAL_S
            elif self._phase == self.PHASE_ADAPTIVE:
                self._phase = self.PHASE_FULL_SPEED
                self._backoff_s = FULL_SPEED_INTERVAL_S
        return record

    def pending_ids(self):
        return [rid for rid, rec in self._requests.items()
                if rec["status"] == "pending"]

    def pending_count(self):
        return len(self.pending_ids())

    def settled_kind(self, request_id):
        rec = self._requests.get(request_id)
        return rec["kind"] if rec and rec["status"] == "settled" else None

    def request_ids(self):
        """按发出顺序列出所有 request_id（日志/测试核对唯一性用）。"""
        return ["req-%d" % i for i in range(1, self._seq + 1)]

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
    """把一次 signUp 响应归类为 404 / real / lost。

    HTTP 404 明确是「接口未就绪/限流」；其余 HTTP 状态（含 200+JSON、
    5xx）都算真实响应，必须结束黄金阶段。响应体丢失时不能挂成 404。
    """
    if response is None:
        return "lost"
    status = getattr(response, "status", 200)
    try:
        status_code = int(status)
    except (TypeError, ValueError):
        status_code = 200
    return "404" if status_code == 404 else "real"


def run_submit_loop(page, *, activity_id, at_ts, window_s, sign_end_ts=None,
                    max_attempts=0, is_joined_fn=None, notify_fn=None,
                    clock=None, log_fn=None, name="活动"):
    """事件驱动非阻塞提交循环（黄金风暴/全速/404 自适应）。

    只做「盯按钮 → 点击 → 记账 → 收口」，不做登录和浏览器生命周期：
    页面、查询、通知、时钟全部注入，单测用替身即可覆盖全部阶段转移。
    返回 (stop_reason, state)；state 含 attempts/pending/404_streak/backoff_s
    等日志字段。未决不伪造成失败：停止时仍有 pending 就报 unconfirmed。
    """
    log = log_fn or globals()["log"]
    notify_owner = notify_fn or notify

    base_deadline = float(at_ts) + float(window_s)
    burst = BurstPolicy(open_ts=at_ts, base_deadline=base_deadline,
                        sign_end_ts=sign_end_ts, max_attempts=max_attempts,
                        clock=clock, retry_start=at_ts)
    state = {"attempts": 0, "pending": 0, "last_reason": "", "resp_ok": False,
             "login_invalid": False, "retry_deadline": burst.effective_deadline,
             "streak_404": 0, "backoff_s": FULL_SPEED_INTERVAL_S,
             "phase": burst.phase, "round": 0}

    def _wall():
        return burst._wall_now()

    def _sync():
        state["attempts"] = burst.attempts
        state["pending"] = burst.pending_count()
        state["streak_404"] = burst.streak_404
        state["backoff_s"] = burst.backoff_s
        state["phase"] = burst.phase
        state["round"] = burst.round
        state["retry_deadline"] = burst.effective_deadline

    def _log_fields():
        _sync()
        return ("phase=%s round=%d 404_streak=%d backoff_s=%.2f attempts=%d "
                "pending=%d retry_deadline=%d"
                % (state["phase"], state["round"], state["streak_404"],
                   state["backoff_s"], state["attempts"], state["pending"],
                   int(state["retry_deadline"])))

    # 受控提交窗口：只承认本场盯按钮期间发出的 signUp 请求。请求事件的派发
    # 可能晚于网络发起，窗口覆盖整个提交阶段才不会漏计真实请求；页面加载
    # 阶段的别家请求在监听注册之前，天然不入账。
    # admitted_all 必须持有 Request 引用：只记 id() 的话对象一被回收，
    # 下一笔请求可能拿到复用的地址而被误判成重复（真实请求会被吞掉）。
    admitted_ids = set()
    admitted_all = []
    request_to_bid = {}
    response_queue = []
    armed = {"on": False, "dead": False}
    listeners = []

    def _is_signup_url(url_text):
        return "/rjyHdCyr/signUp/" in url_text

    def on_request(request):
        if armed["dead"] or not armed["on"]:
            return
        try:
            url_text = str(request.url)
        except Exception:
            return
        if not _is_signup_url(url_text):
            return
        ident = id(request)
        if ident in admitted_ids:
            return          # 同一个 Request 重复通知只计 1
        admitted_ids.add(ident)
        admitted_all.append(request)   # 钉住引用，防 id() 复用误判重复
        bid = burst.register_request()
        request_to_bid[ident] = bid
        _sync()
        state["last_reason"] = "已发起，等响应"
        # 请求发起即记账：发出就计次，不等响应回来
        log("[retry] kind=submit request_id=%s status=pending %s"
            % (bid, _log_fields()))

    def on_response(response):
        # 回调只入队：结算统一走主循环的事件泵，不在回调里做 I/O
        if armed["dead"]:
            return
        try:
            url_text = str(response.url)
        except Exception:
            return
        if not _is_signup_url(url_text):
            return
        response_queue.append(response)

    def _settle_one(response):
        """按 request_id 结算一笔在途响应；乱序合法，别家响应不改本场结论。"""
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
        payload = None
        if raw_kind == "real":
            try:
                payload = response.json()
            except Exception:
                raw_kind = "lost"   # 响应体读不出：保持未决，不假造结论
        burst.settle(bid, raw_kind)
        if raw_kind == "lost":
            state["last_reason"] = "响应读取失败，请求仍未决"
        elif raw_kind == "404":
            state["last_reason"] = "404 接口未就绪/限流"
        else:
            code = payload.get("code") if isinstance(payload, dict) else None
            message = ""
            if isinstance(payload, dict):
                message = str(payload.get("message") or "")
            state["last_reason"] = "code:%s %s" % (code, message[:30])
            # 成功结论粘性：只要有一笔 code=1 就是成功，后续失败响应不改写
            if code == 1:
                state["resp_ok"] = True
            code_text = "" if code is None else str(code)
            if (code_text == "4001" or "4001" in message
                    or "登录" in message or "身份验证" in message):
                state["login_invalid"] = True
        _sync()
        log("[retry] kind=settle request_id=%s status=%s %s last_reason=%s"
            % (bid, raw_kind, _log_fields(), (state["last_reason"] or "")[:40]))

    def _settle_pending():
        while response_queue:
            _settle_one(response_queue.pop(0))
        _sync()

    def _pump_wait(seconds):
        """短事件泵：按预算截断等待，同时派发并结算在途响应。

        Playwright 同步 API 在等待里派发事件回调；成功/登录失效立即返回，
        让调度循环收成唯一终态。等待按整毫秒推进，不让浮点截断把 90ms
        间隔悄悄吃短。
        """
        remain = max(0.0, burst.effective_deadline - _wall())
        wait_s = max(0.0, min(float(seconds), remain))
        wait_ms = int(round(wait_s * 1000)) if wait_s > 0 else 0
        if wait_s > 0:
            wait_ms = max(1, wait_ms)
        deadline_ms = int(round(_now_ms())) + wait_ms
        while True:
            left_ms = deadline_ms - int(round(_now_ms()))
            if left_ms <= 0:
                break
            try:
                page.wait_for_timeout(min(left_ms, 50))  # 50ms 一片
            except Exception:
                break
            _settle_pending()
            if state["resp_ok"] or state["login_invalid"]:
                return

    def _now_ms():
        if clock is None:
            return time.monotonic() * 1000
        return burst._now() * 1000

    def _finish(stop_reason):
        """唯一结构化终态：一条 [retry] kind=done + 一条 owner 私聊结论。"""
        _sync()
        log("[retry] kind=done stop_reason=%s %s last_reason=%s"
            % (stop_reason, _log_fields(), (state["last_reason"] or "")[:40]))
        attempts, pending = state["attempts"], state["pending"]
        if stop_reason == "success":
            log("[OK] 报名成功（尝试 %d 次）" % attempts)
            notify_owner("🎟 报名成功", [
                "**%s**" % name,
                "报名时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "如不想参加，请尽快在活动页取消，别占着名额。",
            ])
        elif stop_reason == "already_signed":
            notify_owner("已报名", ["**%s**" % name,
                                    "此前已报名，本次未重复提交"], "blue")
        elif stop_reason == "login_invalid":
            log("[x] 登录失效（尝试 %d 次）" % attempts)
            notify_owner("⚠️ 自动报名失败", [
                "**%s**" % name,
                "登录已失效，重试没有意义，请重新登录后再试。",
            ], "red")
        elif stop_reason == "unconfirmed":
            # 未决不伪造成失败：如实报「待核对」，退出码沿用服务器旧语义
            log("[x] 结果未确认（尝试 %d 次，未决 %d 笔）" % (attempts, pending))
            notify_owner("⚠️ 自动报名结果未确认", [
                "**%s**" % name,
                "已提交 %d 次（未决 %d 笔），没有拿到确定回应。" % (attempts, pending),
                "结果未确认，请到活动页核对。",
            ], "orange")
        else:
            log("[x] 报名失败（尝试 %d 次，stop_reason=%s，最近异常 %s）"
                % (attempts, stop_reason, (state["last_reason"] or "名额已满/未开放")))
            notify_owner("⚠️ 自动报名失败", [
                "**%s**" % name,
                "窗口内未成功（尝试 %d 次，stop_reason=%s），可能名额已满或页面未放开。"
                % (attempts, stop_reason),
                "建议手动打开活动页确认。",
            ], "orange")
        return stop_reason, state

    listeners.append(("request", on_request))
    listeners.append(("response", on_response))
    page.on("request", on_request)
    page.on("response", on_response)
    armed["on"] = True
    log("[i] 开始盯按钮 %s" % _log_fields())

    reload_gap = 2.0
    last_reload = 0.0
    stop_reason = ""
    try:
        while _wall() < burst.effective_deadline:
            # 先结算在途响应：成功/登录失效立即收口，绝不为下一笔再等
            _settle_pending()
            if state["resp_ok"]:
                break
            if state["login_invalid"]:
                stop_reason = "login_invalid"
                break

            btn = page.locator("uni-button").filter(has_text="立即报名")
            if not btn.count():
                # 按钮迟迟不出现通常是「名额已满」：间隔逐次放大（2 秒起、
                # 1.5 倍、8 秒封顶），满员场景不刷爆接口
                if _now_ms() / 1000.0 - last_reload >= reload_gap:
                    page.reload(wait_until="domcontentloaded", timeout=45000)
                    _pump_wait(0.25)
                    last_reload = _now_ms() / 1000.0
                    reload_gap = min(8.0, reload_gap * 1.5)
                else:
                    _pump_wait(0.3)
                continue

            # 多笔未决合法：黄金风暴允许并发在途请求，不因 pending 停手
            if not burst.can_attempt():
                if burst.attempts >= burst.max_attempts > 0:
                    stop_reason = "max_attempts"
                else:
                    stop_reason = ("sign_end" if (burst.sign_end_ts and
                                   burst.effective_deadline >= burst.sign_end_ts)
                                   else "retry_duration")
                break

            # 按钮提前可见也不能提前发请求：到开放时刻才动手
            if _wall() < at_ts:
                _pump_wait(min(0.25, at_ts - _wall()))
                continue

            # 非阻塞提交：点完「确定」立刻回调度循环，不逐笔等响应
            try:
                btn.first.click(timeout=3000)
                # 打开确认框之后、点「确定」之前再核一次：
                # 到期/次数到/还没开放都绝不确认
                if _wall() >= burst.effective_deadline:
                    state["last_reason"] = "确认前重试窗口已到点"
                    continue
                if not burst.can_attempt():
                    state["last_reason"] = "确认前已达最大提交次数或窗口关闭"
                    continue
                if _wall() < at_ts:
                    state["last_reason"] = "确认前还没到开放时间"
                    continue
                page.get_by_text("确定", exact=True).first.click(timeout=3000)
            except PlaywrightTimeoutError:
                state["last_reason"] = "点击确认超时"
            except Exception as exc:
                state["last_reason"] = "点击确认失败：%s" % type(exc).__name__

            # 已到响应立即结算；迟到的由下一片事件泵接管
            _settle_pending()
            if state["resp_ok"]:
                break
            if state["login_invalid"]:
                stop_reason = "login_invalid"
                break
            if not burst.can_attempt():
                if burst.attempts >= burst.max_attempts > 0:
                    stop_reason = "max_attempts"
                else:
                    stop_reason = ("sign_end" if (burst.sign_end_ts and
                                   burst.effective_deadline >= burst.sign_end_ts)
                                   else "retry_duration")
                break
            # 下一轮节奏由状态机统一提供：黄金 90ms / 全速 180ms /
            # 404 自适应退避 1.6 倍上限 3 秒
            gap = burst.interval_s()
            _pump_wait(min(max(0.0, gap), max(0.0, burst.effective_deadline - _wall())))
            burst.next_interval_after_wait()

        # 有界收尾：给在途响应一个短暂结算窗口（最长 1 秒且不越截止）
        if burst.pending_count() > 0 and _wall() < burst.effective_deadline:
            tail_deadline = _now_ms() / 1000.0 + min(
                1.0, max(0.0, burst.effective_deadline - _wall()))
            while (_now_ms() / 1000.0 < tail_deadline
                   and burst.pending_count() > 0
                   and not state["resp_ok"] and not state["login_invalid"]):
                _pump_wait(0.05)

        if state["login_invalid"]:
            return _finish("login_invalid")
        if state["resp_ok"]:
            return _finish("success")

        # 收尾查询：查询不到不等于没报名
        if burst.attempts and is_joined_fn is not None:
            try:
                if is_joined_fn(activity_id) is True:
                    return _finish("success")
            except Exception:
                pass    # 查询失败不改写已记账事实
        if burst.pending_count() > 0:
            return _finish("unconfirmed")
        if stop_reason:
            return _finish(stop_reason)
        return _finish("sign_end" if (burst.sign_end_ts and
                       burst.effective_deadline >= burst.sign_end_ts)
                       else "retry_duration")
    except KeyboardInterrupt:
        # 用户停止（Ctrl-C / 终止信号）立即收口；已发出的请求如实保留未决
        burst.stop("user_stopped")
        state["last_reason"] = "收到中断"
        return _finish("user_stopped")
    except Exception as exc:  # noqa: BLE001 - 浏览器/管道异常也要有唯一终态
        burst.stop("process_error")
        state["last_reason"] = "运行异常：%s" % type(exc).__name__
        return _finish("process_error")
    finally:
        # 停止/异常路径统一解除监听并失活回调：不留跨计划的残留监听
        armed["on"] = False
        armed["dead"] = True
        for event_name, handler in listeners:
            try:
                page.remove_listener(event_name, handler)
            except Exception:
                pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--activity-id", required=True)
    ap.add_argument("--at", required=True, help="报名开始时间，如 2026-10-22 00:59:59")
    ap.add_argument("--name", default="活动")
    ap.add_argument("--lead", type=float, default=100, help="提前多少秒打开页面")
    ap.add_argument("--window", type=float, default=120, help="开放后最多尝试多少秒")
    ap.add_argument("--sign-end", default="", help="报名截止时间（硬上限），如 2026-10-23 23:59:59")
    ap.add_argument("--dry-run", action="store_true", help="只检查页面与按钮，不点击")
    args = ap.parse_args()

    at = datetime.strptime(args.at, "%Y-%m-%d %H:%M:%S").timestamp()
    start = at - args.lead
    sign_end_ts = None
    if str(args.sign_end or "").strip():
        sign_end_ts = datetime.strptime(args.sign_end.strip(), "%Y-%m-%d %H:%M:%S").timestamp()
    if args.dry_run:  # 自测不等开放时间，立刻检查页面状态
        start = time.time()

    if not ensure_token():
        log("[x] 登录态不可用，放弃本次报名")
        notify("自动报名未执行", ["**%s**" % args.name, "登录态无法恢复，需要人工检查"], "red")
        return 3

    wait_until(start)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True,
                                    args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            ctx = browser.new_context(locale="zh-CN",
                                      viewport={"width": 1366, "height": 900},
                                      storage_state=STATE_FILE)
            page = ctx.new_page()
            page.goto(detail_url(args.activity_id), wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1500)
            log("[i] 详情页已加载，等待 %s 开放报名" % args.at)

            if args.dry_run:
                btns = page.eval_on_selector_all(
                    "uni-button", "els=>els.map(e=>e.innerText.trim()).filter(Boolean)")
                log("[i] dry-run：joined=%s buttons=%s" % (is_joined(args.activity_id), btns))
                return 0

            if is_joined(args.activity_id):
                log("[i] 该活动已是报名状态，跳过提交")
                notify("已报名", ["**%s**" % args.name, "此前已报名，本次未重复提交"], "blue")
                return 0

            wait_until(at - POLL_LEAD)

            # 终态收口（含 owner 私聊结论）统一在循环内完成，这里只映射退出码
            stop_reason, state = run_submit_loop(
                page, activity_id=args.activity_id, at_ts=at,
                window_s=args.window, sign_end_ts=sign_end_ts,
                is_joined_fn=is_joined, notify_fn=notify, name=args.name)

            if stop_reason in ("success", "already_signed"):
                return 0
            if stop_reason == "login_invalid":
                return 3
            return 2
        finally:
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
