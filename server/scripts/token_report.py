#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""token 存活报告：把 token_probe.log 汇总成一份人看的结论。

用途：探针每 15 分钟记一行原始数据，人不可能去读 96 行/天。
本脚本按 token 指纹分组，算出每枚 token 的「签发时刻 → 最后成功 → 首次失败」，
把结论写进 reports/token-lifetime.md，随时打开看，不依赖任何会话或本机。

边界：只读 data/token_probe.log，只写 reports/token-lifetime.md；
不发起任何网络请求，不改 session.json / state.json。

@author LiXinwen
"""
import os
import re
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE_LOG = os.path.join(ROOT, "data", "token_probe.log")
REPORT_DIR = os.path.join(ROOT, "reports")
REPORT_FILE = os.path.join(REPORT_DIR, "token-lifetime.md")

# 探针行的固定格式，detail 可能带空格所以放最后。
# saved_at 本身含一个空格（"2026-10-21 10:11:43"），所以这里必须用
# 非贪婪的 "(.+?) age=" 收边，不能用 \S+，否则整行都匹配不上（曾因此报告空白）。
LINE_RE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) token=(\S+) saved_at=(.+?) "
    r"age=(\S+) result=(\S+)(?: detail=(.*))?$"
)


def parse_lines(path):
    """读出所有可解析的探针记录，坏行直接跳过（日志被截断时不至于整体失败）。"""
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            matched = LINE_RE.match(raw.strip())
            if not matched:
                continue
            ts, fingerprint, saved_at, age, result, detail = matched.groups()
            rows.append({
                "ts": ts,
                "fp": fingerprint,
                "saved_at": saved_at,
                "age": age,
                "result": result,
                "detail": (detail or "").strip(),
            })
    return rows


def group_by_token(rows):
    """按指纹分组，保持出现顺序；同一指纹可能跨多天复用。"""
    groups = []
    index = {}
    for row in rows:
        fp = row["fp"]
        if fp not in index:
            index[fp] = {"fp": fp, "rows": []}
            groups.append(index[fp])
        index[fp]["rows"].append(row)
    return groups


def first(rows, predicate):
    for row in rows:
        if predicate(row):
            return row
    return None


def last(rows, predicate):
    for row in reversed(rows):
        if predicate(row):
            return row
    return None


def hours_between(start_text, end_text):
    """两个 'YYYY-MM-DD HH:MM:SS' 之间的小时数，算不出来返回 None。"""
    try:
        start = datetime.strptime(start_text, "%Y-%m-%d %H:%M:%S")
        end = datetime.strptime(end_text, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return (end - start).total_seconds() / 3600.0


def describe(group):
    """把一枚 token 的观测压成几个时刻和一句结论。

    saved_at 取「最早一次落盘时刻」而不是最新一次：守护进程每次复验成功都会
    把同一枚 token 重新写盘，saved_at 因此被刷新，但 token 本身没换。
    用最新值会把一枚活了十几个小时的 token 算成刚签发，寿命被严重低估。
    """
    rows = group["rows"]
    ok_row = first(rows, lambda r: r["result"] == "ok")
    fail_row = first(rows, lambda r: r["result"] == "fail")
    last_ok = last(rows, lambda r: r["result"] == "ok")
    last_row = rows[-1]

    saved_candidates = [r["saved_at"] for r in rows if r["saved_at"] not in ("", "-")]
    if saved_candidates:
        saved_at = min(saved_candidates, key=lambda s: s)
    else:
        saved_at = ""

    alive_hours = None
    if last_ok and saved_at:
        alive_hours = hours_between(saved_at, last_ok["ts"])

    verdict = ""
    if fail_row and saved_at:
        life = hours_between(saved_at, fail_row["ts"])
        if life is not None:
            verdict = "已确认失效：签发后约 %.1f 小时（失败于 %s）" % (life, fail_row["ts"])
    elif last_row["result"] == "ok" and alive_hours is not None:
        verdict = "仍有效：已连续存活 %.1f 小时，未观察到失效" % alive_hours
    elif last_row["result"] != "ok":
        verdict = "当前状态异常：%s" % last_row["result"]

    return {
        "fp": group["fp"],
        "saved_at": saved_at,
        "first_seen": rows[0]["ts"],
        "last_seen": last_row["ts"],
        "last_ok": last_ok["ts"] if last_ok else "-",
        "first_fail": fail_row["ts"] if fail_row else "-",
        "alive_hours": alive_hours,
        "last_result": last_row["result"],
        "last_detail": last_row["detail"],
        "ok_count": sum(1 for r in rows if r["result"] == "ok"),
        "fail_count": sum(1 for r in rows if r["result"] == "fail"),
        "verdict": verdict,
    }


def render(rows, groups):
    """生成 markdown：先给结论，再给每枚 token 的时间线。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    out.append("# token 存活观测")
    out.append("")
    out.append("生成时间：%s ｜ 探针记录 %d 行 ｜ 观测到 %d 枚 token"
               % (now, len(rows), len(groups)))
    out.append("")
    if not rows:
        out.append("还没有探针数据。cron 每 15 分钟跑一次，等第一行写入后本报告才有内容。")
        return "\n".join(out) + "\n"

    out.append("## 结论")
    out.append("")
    for item in [describe(g) for g in groups]:
        out.append("- `%s`（%s 签发）：%s" % (item["fp"], item["saved_at"] or "未知", item["verdict"]))
    out.append("")

    out.append("## 每枚 token 的时间线")
    out.append("")
    out.append("| token 指纹 | 签发时刻 | 首次观测 | 最后成功 | 首次失败 | 成功/失败次数 |")
    out.append("|---|---|---|---|---|---|")
    for item in [describe(g) for g in groups]:
        out.append("| `%s` | %s | %s | %s | %s | %d / %d |" % (
            item["fp"], item["saved_at"] or "-", item["first_seen"],
            item["last_ok"], item["first_fail"], item["ok_count"], item["fail_count"]))
    out.append("")

    out.append("## 最近 12 条原始记录")
    out.append("")
    out.append("```")
    for row in rows[-12:]:
        detail = (" detail=" + row["detail"]) if row["detail"] else ""
        out.append("%s token=%s saved_at=%s age=%s result=%s%s" % (
            row["ts"], row["fp"], row["saved_at"], row["age"], row["result"], detail))
    out.append("```")
    out.append("")
    out.append("## 怎么读这份报告")
    out.append("")
    out.append("- 「首次失败 - 签发时刻」就是这枚 token 的实测寿命；出现两枚不同寿命时，说明不是固定 TTL。")
    out.append("- 指纹变化意味着 token 被换过（守护进程或站点轮换），此时按指纹分行看，不要跨行连算。")
    out.append("- `saved_at` 是落盘时刻，守护进程复验成功会刷新它；同一指纹取最早值当签发时刻，否则寿命会被低估。")
    out.append("- 探针每 15 分钟一次，寿命精度为 ±15 分钟。")
    return "\n".join(out) + "\n"


def main():
    rows = parse_lines(PROBE_LOG)
    groups = group_by_token(rows)
    text = render(rows, groups)
    if not os.path.isdir(REPORT_DIR):
        os.makedirs(REPORT_DIR)
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(text)
    print("已写入 %s（%d 行观测 / %d 枚 token）" % (REPORT_FILE, len(rows), len(groups)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
