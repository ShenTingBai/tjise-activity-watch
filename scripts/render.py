"""推送文案的排版，清单 / 机器人 / 冲刺提醒共用一套样式。

排版原则：
- 每条活动固定 3-4 行，第一行是「什么时候该动手」，方便快速扫读
- 活动名称单独占一行，不和类型挤在一起
- 类型、地点、截止时间降级为辅助信息，用全角空格缩进
- 空值不占位，避免出现「—」
"""
from common import MAX_LIST_ITEMS, fmt_when

BULLET = "▸"          # 每条的开头
INDENT = "　"          # 全角空格，飞书里缩进看起来最整齐


def _short(ts, base_ts, now):
    """统一走 fmt_when，日期必须带，避免只看到「17:59」不知道是哪天。"""
    return fmt_when(ts, now)


def _pending_block(it, now):
    """待开放报名：第一行是「什么时候开放」。"""
    out = []
    mark = "  🆕" if it.get("is_new") else ""
    out.append("%s %s 开放%s" % (BULLET, fmt_when(it["sign_start"], now), mark))
    out.append(INDENT + it["name"])
    meta = [it.get("type") or "其他"]
    if it.get("place"):
        meta.append(str(it["place"]))
    out.append(INDENT + "｜".join(meta))
    tail = []
    if it.get("sign_end"):
        tail.append("截止 %s" % _short(it["sign_end"], it["sign_start"], now))
    if it.get("act_start"):
        tail.append("活动 %s" % _short(it["act_start"], it["sign_start"], now))
    if tail:
        out.append(INDENT + "｜".join(tail))
    out.append("")
    return out


def _open_block(it, now):
    """已开放报名：第一行是「什么时候截止」，因为这才是最要紧的。"""
    out = []
    out.append("%s %s 截止" % (BULLET, fmt_when(it["sign_end"], now)))
    out.append(INDENT + it["name"])
    meta = [it.get("type") or "其他"]
    if it.get("place"):
        meta.append(str(it["place"]))
    out.append(INDENT + "｜".join(meta))
    out.append("")
    return out


def pending_lines(items, now, limit=MAX_LIST_ITEMS):
    out = []
    for it in items[:limit]:
        out += _pending_block(it, now)
    if len(items) > limit:
        out.append("……另有 %d 项" % (len(items) - limit))
    return out


def open_lines(items, now, limit=MAX_LIST_ITEMS):
    out = []
    for it in items[:limit]:
        out += _open_block(it, now)
    if len(items) > limit:
        out.append("……另有 %d 项" % (len(items) - limit))
    return out


def digest_items(summary):
    """推送清单：正在报名的排前面，还没开放的排后面。

    以前只推「待开放」，已经开放报名的活动整片从群里消失；
    抓取账号自己报过名之后，那条活动看起来就像被藏起来了。
    名额是随时会被抢完的，先看到才来得及点，所以报名中的排最前面。
    """
    items = []
    for it in summary.get("open", []) or []:
        items.append(dict(it, kind="open"))
    for it in summary.get("pending", []) or []:
        items.append(dict(it, kind="pending"))
    return items


def digest_lines(items, now, limit=MAX_LIST_ITEMS):
    """推送正文：两种行按 kind 分别渲染，合计不超过 MAX_LIST_ITEMS 项。"""
    out = []
    for it in items[:limit]:
        if it.get("kind") == "open":
            out += _open_block(it, now)
        else:
            out += _pending_block(it, now)
    if len(items) > limit:
        out.append("……另有 %d 项" % (len(items) - limit))
    return out


def trim(lines):
    """去掉尾部空行，并拼成一段。"""
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)
