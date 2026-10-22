#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""到点自动报名（单账号）。

背景：活动名额在开放瞬间被抢光，人工点太慢。本脚本在报名开放前把详情页加载好，
到点自动点「立即报名 → 确定」，成功后私聊发卡片通知。

边界：
- 只操作 session.json 里那一个账号，不代他人报名；
- 只在传入的单个 activity-id 上动作，不批量扫活动；
- 失败不无限重试：默认 120 秒窗口，之后放弃并私聊告知失败原因；
- 不记录 token、姓名、学号；日志只留结果分类与时间。

用法：
  python3 grab_signup.py --activity-id <id> --at "2026-10-22 00:59:59" \
      --name "活动名" [--lead 100] [--window 120] [--dry-run]

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
    line = "%s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--activity-id", required=True)
    ap.add_argument("--at", required=True, help="报名开始时间，如 2026-10-22 00:59:59")
    ap.add_argument("--name", default="活动")
    ap.add_argument("--lead", type=float, default=100, help="提前多少秒打开页面")
    ap.add_argument("--window", type=float, default=120, help="开放后最多尝试多少秒")
    ap.add_argument("--dry-run", action="store_true", help="只检查页面与按钮，不点击")
    args = ap.parse_args()

    at = datetime.strptime(args.at, "%Y-%m-%d %H:%M:%S").timestamp()
    start = at - args.lead
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

            resp_ok = False
            deadline = at + args.window
            attempts = 0
            last_reason = ""
            last_reload = 0.0
            # 按钮迟迟不出现通常是「名额已满」：固定 2 秒重载会在 2 分钟里刷
            # 60 次重载（每次两个接口），形态上很像爬虫。间隔逐次放大到 8 秒，
            # 满员场景的请求量降到 1/4，抢到名额那一刻仍能在 8 秒内重新提交。
            reload_gap = 2.0
            while time.time() < deadline:
                btn = page.locator("uni-button").filter(has_text="立即报名")
                if not btn.count():
                    if time.time() - last_reload >= reload_gap:
                        page.reload(wait_until="domcontentloaded", timeout=45000)
                        page.wait_for_timeout(250)
                        last_reload = time.time()
                        reload_gap = min(8.0, reload_gap * 1.5)
                    else:
                        page.wait_for_timeout(300)
                    continue
                reload_gap = 2.0

                attempts += 1
                try:
                    # 包住「点按钮 + 点确定」，直接拿服务端 signUp 的返回码判断
                    with page.expect_response(
                            lambda r: "/rjyHdCyr/signUp/" in r.url, timeout=12000) as info:
                        btn.first.click(timeout=3000)
                        page.get_by_text("确定", exact=True).first.click(timeout=3000)
                    payload = info.value.json()
                    code = payload.get("code") if isinstance(payload, dict) else None
                    last_reason = "code:%s %s" % (
                        code, str((payload or {}).get("message") or "")[:30])
                    resp_ok = (code == 1)
                except PlaywrightTimeoutError:
                    last_reason = "no_response"
                except Exception as exc:
                    last_reason = type(exc).__name__
                log("[i] 第 %d 次提交：%s" % (attempts, last_reason))

                if resp_ok:
                    break
                # 服务端已回 code=1 但页面状态可能滞后；这里只补 2~3 次轻量确认，
                # 避免对学校接口高频轮询
                for _ in range(3):
                    time.sleep(0.6)
                    if is_joined(args.activity_id):
                        resp_ok = True
                        break
                if resp_ok:
                    break
                # 提交失败后等一会儿再重载：名额刚放出时几个客户端同抢，
                # 连着重载只会让页面卡在同一个旧状态上。
                page.wait_for_timeout(600)
                page.reload(wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(300)

            ok = resp_ok or is_joined(args.activity_id)
            if ok:
                log("[OK] 报名成功（尝试 %d 次）" % attempts)
                notify("🎟 报名成功", [
                    "**%s**" % args.name,
                    "报名时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "如不想参加，请尽快在活动页取消，别占着名额。",
                ])
                return 0

            log("[x] 报名失败（尝试 %d 次，窗口 %.0fs，最近异常 %s）"
                % (attempts, args.window, last_reason or "名额已满/未开放"))
            notify("⚠️ 自动报名失败", [
                "**%s**" % args.name,
                "窗口 %.0f 秒内未成功（尝试 %d 次），可能名额已满或页面未放开。" % (args.window, attempts),
                "建议手动打开活动页确认。",
            ], "orange")
            return 2
        finally:
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
