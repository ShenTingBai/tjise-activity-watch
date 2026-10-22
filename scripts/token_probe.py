#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""token 存活探针：只读判断当前 session.json 里的 token 是否仍被接口接受。

用途：每 15 分钟由 cron 跑一次，把「时刻 + token 指纹 + 已存活时长 + 接口返回码」
追加成一行日志，用来测出真实失效时刻，而不是靠猜。

失效告警（2026-10-21 起，复用下面那一次接口调用，不新增学校接口请求）：
- 连续 2 次非 ok 才告警；同一失效周期只提醒一次，恢复 ok 后自动清零；
- 优先私聊 owner，私聊不可用才退回群通道；
- 告警文案不含 token、姓名、学号；状态写 data/token_probe_state.json，
  与业务去重用的 state.json 完全隔离。

边界（故意做窄）：
- 只调一次 listActivity，参数与 fetch.py / server_refresh.js tokenWorks 保持一致；
- 不写 session.json、state.json、latest.json；
- 日志里只出现 token 的 sha1 前 8 位，绝不打印 token 原文、姓名、学号；
- 任何异常都只记一行，退出码恒为 0，避免 cron 反复告警。

@author LiXinwen
"""
import hashlib
import json
import os
import sys
import time
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import ACTIVITY_API, REFERER  # noqa: E402

SESSION_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "session.json",
)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# 字段要和 fetch.py 一致，少字段接口会直接拒绝
PAYLOAD = {"pageSize": 1, "pageNum": 1, "hdmc": "", "hdzt": None, "kssj": "", "jssj": ""}

# 告警状态单独存一个文件：state.json 是业务去重用的，探针只读不碰（见模块说明）
ALERT_STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "token_probe_state.json",
)
FAIL_ALERT_THRESHOLD = 2   # 连续 2 次非 ok（约 30 分钟）才告警，滤掉单次网络抖动
MAX_ALERT_ATTEMPTS = 3     # 私聊/群都发不出去时最多试 3 次，之后放弃，避免每 15 分钟刷日志


def _age(saved_at):
    """把 saved_at 换算成「已存活 12h03m」，算不出来就留空。"""
    try:
        started = datetime.strptime(saved_at, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return ""
    secs = int((datetime.now() - started).total_seconds())
    if secs < 0:
        return ""
    return "%dh%02dm" % (secs // 3600, (secs % 3600) // 60)


def _line(ts, fp, saved_at, age, result, extra=""):
    """统一一行的日志格式，方便 grep 和后续统计。"""
    parts = [
        ts,
        "token=" + (fp or "-"),
        "saved_at=" + (saved_at or "-"),
        "age=" + (age or "-"),
        "result=" + result,
    ]
    if extra:
        parts.append("detail=" + extra)
    return " ".join(parts)


def _load_alert_state():
    """读取告警状态；文件缺失或损坏都当作「未告警、无连续失败」。"""
    try:
        with open(ALERT_STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
        return {
            "fail_streak": max(0, int(st.get("fail_streak") or 0)),
            "alerted": bool(st.get("alerted")),
            "attempts": max(0, int(st.get("attempts") or 0)),
        }
    except Exception:
        return {"fail_streak": 0, "alerted": False, "attempts": 0}


def _save_alert_state(st):
    """原子写；写失败只记一行，不影响探针退出码。"""
    try:
        os.makedirs(os.path.dirname(ALERT_STATE_FILE), exist_ok=True)
        tmp = ALERT_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
        os.replace(tmp, ALERT_STATE_FILE)
    except Exception as exc:
        print("[x] 告警状态写入失败：%s" % type(exc).__name__)


def _send_alert(text):
    """优先私聊 owner；私聊不可用（None）或失败时才退回群通道。"""
    ok = None
    try:
        from notify import send_private
        ok = send_private(text)
    except Exception as exc:
        print("[i] 私聊通道异常：%s" % type(exc).__name__)

    if ok is True:
        print("[!] 登录态告警已私聊 owner")
        return True

    try:
        from notify import send
        ok = send(text)
    except Exception as exc:
        print("[x] 群通道异常：%s" % type(exc).__name__)
        return False
    print("[!] 登录态告警已发到群通道（私聊不可用）" if ok is True
          else "[x] 登录态告警发送失败")
    return ok is True


def _handle_alert(result, detail, ts):
    """按探测结果维护告警状态，必要时发一次告警。

    规则：连续 FAIL_ALERT_THRESHOLD 次非 ok 才告警；同一失效周期只发一次；
    恢复 ok 立即清零。整个函数吞掉所有异常——探针每次都必须留下那一行日志。
    """
    try:
        st = _load_alert_state()

        if result == "ok":
            if st["fail_streak"] or st["alerted"] or st["attempts"]:
                _save_alert_state({"fail_streak": 0, "alerted": False, "attempts": 0})
                print("[i] 登录态恢复正常，告警状态已清零")
            return

        st["fail_streak"] += 1
        if st["fail_streak"] >= FAIL_ALERT_THRESHOLD and not st["alerted"]:
            if result in ("http-error", "bad-json"):
                # 接口连不上 ≠ 登录态过期：文案要能让人一眼分清该查网络还是该重新登录
                text = (
                    "【校园活动机器人】学校接口连续 %d 次探测不通（最近一次 %s，%s），"
                    "推送已停摆。这多半是服务器出网/带宽问题，不是登录态过期；"
                    "先看机器能不能访问 xg.tjise.edu.cn，恢复后探针会自动清零。"
                ) % (st["fail_streak"], ts, detail or result)
            else:
                text = (
                    "【校园活动机器人】登录态探测连续 %d 次失败（最近一次 %s，%s），"
                    "推送已停摆，需要重新登录。恢复：本机 python scripts/login.py 登录，"
                    "再按 README「状态失效了怎么办」同步到服务器。"
                ) % (st["fail_streak"], ts, detail or result)
            if _send_alert(text):
                st["alerted"] = True
            else:
                st["attempts"] += 1
                if st["attempts"] >= MAX_ALERT_ATTEMPTS:
                    st["alerted"] = True  # 放弃重试：日志里已留下 3 次失败痕迹
                    print("[x] 告警连续发送失败，本周期不再重试")
        _save_alert_state(st)
    except Exception as exc:
        print("[x] 告警处理异常：%s" % type(exc).__name__)


def main():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        with open(SESSION_FILE, "r", encoding="utf-8") as f:
            session = json.load(f)
    except Exception as exc:  # 文件缺失、权限、坏 JSON 都归到这里
        print(_line(ts, "", "", "", "no-session", type(exc).__name__))
        _handle_alert("no-session", type(exc).__name__, ts)
        return 0

    token = str(session.get("token") or "").strip()
    if not token:
        print(_line(ts, "", session.get("saved_at"), "", "no-token"))
        _handle_alert("no-token", "", ts)
        return 0

    fp = hashlib.sha1(token.encode("utf-8")).hexdigest()[:8]
    saved_at = str(session.get("saved_at") or "")
    age = _age(saved_at)

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
    except Exception as exc:  # 网络、超时、5xx 都只记一行
        # 卡顿/抖动很常见（尤其按量带宽被打满时），先重试一次再判死，
        # 免得一次 20 秒超时把「连续 2 次失败」凑满，误发告警。
        try:
            time.sleep(5)
            resp = urllib.request.urlopen(req, timeout=25)
            try:
                raw = resp.read().decode("utf-8", "replace")
            finally:
                resp.close()
        except Exception as exc2:
            print(_line(ts, fp, saved_at, age, "http-error", str(exc2)[:80]))
            _handle_alert("http-error", str(exc2)[:80], ts)
            return 0

    try:
        data = json.loads(raw)
    except ValueError:
        print(_line(ts, fp, saved_at, age, "bad-json"))
        _handle_alert("bad-json", "", ts)
        return 0

    code = data.get("code") if isinstance(data, dict) else None
    ok = code == 1
    detail = ""
    if not ok and isinstance(data, dict):
        # 只留 message 前 60 字，够判断是过期还是别的业务错误
        detail = str(data.get("message") or "")[:60].replace("\n", " ")
    print(_line(ts, fp, saved_at, age, "ok" if ok else "fail", detail))
    _handle_alert("ok" if ok else "fail", detail, ts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
