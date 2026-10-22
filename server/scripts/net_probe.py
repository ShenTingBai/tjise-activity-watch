#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务器出网自检：把「网络断了」和「登录态过期了」分开。

为什么单独一个探针：
    token_probe 每 15 分钟跑一次，它的 http-error 既可能是 token 失效，
    也可能只是本机出网断了（2026-10-22 的教训：按量带宽被打穿后，
    07:45-09:30 连续 http-error，被误判成登录态失效，白跑了好几轮登录）。
    这里改用最轻的 GET /stu/（静态页、不带 token、不碰接口），每 5 分钟一次，
    连续 2 次失败才告警，用于尽早发现「出网故障」而不是等 30 分钟。

规则：
    - 正常只写一行日志，不发任何消息；
    - 连续 2 次失败（约 10 分钟）才告警；同一故障周期只提醒一次；恢复 ok 立即清零；
    - 学校站点不通时再补探一次通用站点，用于区分「学校侧故障」和「整机断网」；
    - 告警文案不含 token、姓名、学号；状态写 data/net_probe_state.json；
    - 任何异常都只记一行，退出码恒为 0，避免 cron 反复告警。

用法：
    python3 scripts/net_probe.py           # 跑一次，写一行日志
    cron: */5 * * * * python3 scripts/net_probe.py >> data/net_probe.log 2>&1

@author LiXinwen
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.path.dirname(HERE), "data")
STATE_FILE = os.path.join(DATA_DIR, "net_probe_state.json")

SCHOOL_URL = "https://xg.tjise.edu.cn/stu/"
CONTROL_URL = "https://www.baidu.com/"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
FAIL_ALERT_THRESHOLD = 2   # 连续 2 次失败（10 分钟）才告警，滤掉单次抖动
MAX_ALERT_ATTEMPTS = 3     # 私聊/群都发不出去时最多试 3 次，之后放弃
TIMEOUT = 12               # 单次探测超时；比 token_probe 短，坏网时尽快落日志


def _line(ts, result, net="", detail=""):
    """统一一行日志：时间、结果、连不上的是哪一侧、异常摘要。"""
    parts = [ts, "result=" + result]
    if net:
        parts.append("net=" + net)
    if detail:
        parts.append("detail=" + " ".join(str(detail).split())[:80])
    return " ".join(parts)


def _load_state():
    """文件缺失或损坏都当作「未告警、无连续失败」。"""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
        return {
            "fail_streak": max(0, int(st.get("fail_streak") or 0)),
            "alerted": bool(st.get("alerted")),
            "attempts": max(0, int(st.get("attempts") or 0)),
        }
    except Exception:
        return {"fail_streak": 0, "alerted": False, "attempts": 0}


def _save_state(st):
    """原子写；写失败只记一行，不影响退出码。"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as exc:
        print("[x] 状态写入失败：%s" % type(exc).__name__)


def _probe(url, timeout=TIMEOUT):
    """最轻的一次 GET：只读 512 字节就断开，够证明 TLS + 首包能通。

    返回 (ok, detail)；HTTP 4xx/5xx 也算 ok——能拿到状态码说明网络是通的，
    这里的目的是「出网是否可用」，不是「业务是否正常」。
    """
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "text/html,*/*")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        try:
            resp.read(512)
        finally:
            resp.close()
        return True, ""
    except urllib.error.HTTPError as exc:
        exc.close()
        return True, "http-%s" % exc.code
    except Exception as exc:
        return False, type(exc).__name__ + ": " + str(exc)


def _send_alert(text):
    """优先私聊 owner；私聊不可用（None）或失败时才退回群通道。"""
    ok = None
    try:
        from notify import send_private
        ok = send_private(text)
    except Exception as exc:
        print("[i] 私聊通道异常：%s" % type(exc).__name__)

    if ok is True:
        print("[!] 出网告警已私聊 owner")
        return True

    try:
        from notify import send
        ok = send(text)
    except Exception as exc:
        print("[x] 群通道异常：%s" % type(exc).__name__)
        return False
    print("[!] 出网告警已发到群通道（私聊不可用）" if ok is True
          else "[x] 出网告警发送失败")
    return ok is True


def _handle_alert(ok, net, detail, ts):
    """维持失败计数与告警状态；整个函数吞掉异常，探针必须留下那一行日志。"""
    try:
        st = _load_state()
        if ok:
            if st["fail_streak"] or st["alerted"] or st["attempts"]:
                _save_state({"fail_streak": 0, "alerted": False, "attempts": 0})
                print("[i] 出网恢复正常，告警状态已清零")
            return

        st["fail_streak"] += 1
        if st["fail_streak"] >= FAIL_ALERT_THRESHOLD and not st["alerted"]:
            if net == "down":
                text = (
                    "【校园活动机器人】服务器出网异常：学校站点和普通网站都连不上"
                    "（连续 %d 次，最近 %s，%s）。推送、定时任务、自动登录都会失效，"
                    "先去查这台 ECS 的带宽/网络。"
                ) % (st["fail_streak"], ts, detail or "超时")
            else:
                text = (
                    "【校园活动机器人】服务器能上网，但学校站点连不上"
                    "（连续 %d 次，最近 %s，%s）。可能是学校侧故障或本机 IP 被限流，"
                    "登录态未必失效；先别急着重登。"
                ) % (st["fail_streak"], ts, detail or "超时")
            if _send_alert(text):
                st["alerted"] = True
            else:
                st["attempts"] += 1
                if st["attempts"] >= MAX_ALERT_ATTEMPTS:
                    st["alerted"] = True  # 放弃重试：日志里已留下失败痕迹
                    print("[x] 告警连续发送失败，本周期不再重试")
        _save_state(st)
    except Exception as exc:
        print("[x] 告警处理异常：%s" % type(exc).__name__)


def main():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ok, detail = _probe(SCHOOL_URL)
    net = ""
    if not ok:
        # 学校站点不通时补探一次通用站点：能分清楚是「学校挂了」还是「整机断网」
        ctrl_ok, ctrl_detail = _probe(CONTROL_URL)
        net = "school-only" if ctrl_ok else "down"
        if ctrl_ok:
            detail = detail or ctrl_detail
        else:
            detail = "school:%s | control:%s" % (detail, ctrl_detail)
    print(_line(ts, "ok" if ok else "fail", net, detail))
    _handle_alert(ok, net, detail, ts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
