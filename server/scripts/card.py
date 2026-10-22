"""飞书消息卡片。

用 interactive 卡片代替纯文本，换来标题色、加粗、分隔线和页脚注。
卡片构造失败时上层会退回纯文本，不会因为格式问题丢消息。
"""
import base64
import json

from common import MAX_LIST_ITEMS, fmt_when

# 卡片里最多列几条，太长了手机上一屏看不完。
# 纯文本兜底共用同一个上限，保证两条通道条数一致。
MAX_ITEMS = MAX_LIST_ITEMS


def _pending_md(it, now):
    mark = "🆕 " if it.get("is_new") else ""
    lines = ["**▸ %s 开放**" % fmt_when(it["sign_start"], now), mark + it["name"]]
    meta = [it.get("type") or "其他"]
    if it.get("place"):
        meta.append(str(it["place"]))
    lines.append("｜".join(meta))
    tail = []
    if it.get("sign_end"):
        tail.append("截止 %s" % fmt_when(it["sign_end"], now))
    if it.get("act_start"):
        tail.append("活动 %s" % fmt_when(it["act_start"], now))
    if tail:
        lines.append("｜".join(tail))
    return "\n".join(lines)


def _open_md(it, now):
    lines = ["**▸ %s 截止**" % fmt_when(it["sign_end"], now), it["name"]]
    meta = [it.get("type") or "其他"]
    if it.get("place"):
        meta.append(str(it["place"]))
    lines.append("｜".join(meta))
    return "\n".join(lines)


def _mine_md(it, now):
    lines = ["**▸ %s**" % fmt_when(it.get("act_start") or "", now), it["name"]]
    meta = [it.get("type") or "其他"]
    if it.get("place"):
        meta.append(str(it["place"]))
    lines.append("｜".join(meta))
    return "\n".join(lines)


def _items_to_elements(items, renderer, now, limit=MAX_ITEMS, empty_text=""):
    if not items:
        return [{"tag": "div", "text": {"tag": "lark_md", "content": empty_text or "暂无"}}]
    elements = []
    for idx, it in enumerate(items[:limit]):
        if idx:
            elements.append({"tag": "hr"})
        elements.append({"tag": "div",
                         "text": {"tag": "lark_md", "content": renderer(it, now)}})
    if len(items) > limit:
        elements.append({"tag": "note",
                         "elements": [{"tag": "plain_text",
                                       "content": "……另有 %d 项" % (len(items) - limit)}]})
    return elements


def _card(title, subtitle, template, elements, footer=None):
    """footer 支持字符串或多行列表，多行在同一个 note 里换行，保持版式一致。"""
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": elements,
    }
    if subtitle:
        card["header"]["subtitle"] = {"tag": "plain_text", "content": subtitle}
    if footer:
        text = "\n".join(footer) if isinstance(footer, (list, tuple)) else footer
        if text:
            card["elements"].append({"tag": "note",
                                     "elements": [{"tag": "plain_text",
                                                   "content": text}]})
    return card


# 两条通道共用的文案常量，改一处两边同时生效
BASE_FOOTER = "开放前 10 分钟会单独提醒"
EMPTY_PENDING = "暂时没有待开放报名的活动。"


def pending_card(items, now, type_filter="", buttons="pending",
                 extra_footer=(), subtitle=None):
    """待开放报名卡片。**推送和 @ 回复都走这里**，保证版式与措辞完全一致。

    buttons 传 None 表示不加按钮（webhook 自定义机器人的卡片，按钮回调
    不会回到应用，放上去是死按钮）。
    """
    title = "待开放报名 %d 项" % len(items)
    if type_filter:
        title += "（%s）" % type_filter

    elements = _items_to_elements(items, _pending_md, now, empty_text=EMPTY_PENDING)
    if buttons:
        elements.append(_buttons(buttons))

    footer = [BASE_FOOTER] if items else []
    footer += list(extra_footer)

    end = subtitle if subtitle is not None else "更新于 %s" % now.strftime("%H:%M")
    return _card(title, end, "blue", elements, footer)


def _digest_md(it, now):
    """推送清单按 kind 选版式：报名中的用「截止」，待开放的用「开放」。"""
    if it.get("kind") == "open":
        return _open_md(it, now)
    return _pending_md(it, now)


def digest_card(summary, late_checkin=False, with_open=False):
    """定时推送用：与 @ 回复同一套版式，只在页脚追加推送专属提示。

    清单把「正在报名」排在前面：名额随时会被抢完，先看到才来得及点；
    以前只推待开放的活动，已经开放报名的会整片从群里消失。
    """
    from datetime import datetime
    from render import digest_items

    now = datetime.now()
    items = digest_items(summary)
    pending_count = len(summary.get("pending", []))
    open_count = int(summary.get("open_count") or 0)

    if open_count:
        title = "可报名 %d 项 · 待开放 %d 项" % (open_count, pending_count)
    else:
        title = "待开放报名 %d 项" % pending_count

    elements = _items_to_elements(items, _digest_md, now, empty_text=EMPTY_PENDING)

    extra = []
    # 列表最多 8 项，没排进去的报名中活动只在页脚提示条数，不做成第二个清单。
    shown_open = sum(1 for it in items[:MAX_ITEMS] if it.get("kind") == "open")
    rest_open = max(0, open_count - shown_open)
    if with_open and rest_open:
        extra.append("另有 %d 项还能报名，@元小元 说「报名中」可看" % rest_open)
    if late_checkin:
        extra.append("🌙 晚归签到 22:45–23:59，别忘签")

    gen = summary.get("generated_at", "")
    subtitle = "更新于 %s" % gen[11:16] if len(gen) >= 16 else None

    footer = ["开放前 10 分钟会单独提醒"] if pending_count else []
    footer += extra

    return _card(title, subtitle, "blue", elements, footer)


def open_card(items, now, type_filter=""):
    title = "还能报名 %d 项" % len(items)
    if type_filter:
        title += "（%s）" % type_filter
    elements = _items_to_elements(
        items, _open_md, now, empty_text="现在没有还能报名的活动。")
    elements.append(_buttons("open"))
    return _card(title, "更新于 %s" % now.strftime("%H:%M"), "orange", elements,
                 ["名额可能已满，值得碰碰运气", "回复「待报名」看还没开放的活动"])


def remind_card(items, now):
    """今天的活动提醒（只走 owner 私聊）。

    以前这条是纯文本，跟群里的卡片放在一起显得像两个人发的；
    现在统一成卡片，标题颜色用绿色表示「已报名、按计划进行」。
    """
    elements = _items_to_elements(items, _mine_md, now,
                                  empty_text="今天没有要参加的活动。")
    return _card("今天要参加的活动 %d 项" % len(items),
                 "更新于 %s" % now.strftime("%m-%d %H:%M"), "green", elements,
                 ["只列今天、且你已报名的场次"])


def rush_card(items, now, lead_minutes=10):
    """冲刺提醒：报名还有几分钟开放。抢时间用，列表要短、要能一眼扫完。"""
    elements = _items_to_elements(items, _pending_md, now)
    return _card("⏰ %d 分钟内开放报名" % int(lead_minutes),
                 "更新于 %s" % now.strftime("%H:%M"), "orange", elements,
                 ["想报的同学提前打开活动详情页"])


def mine_card(items, now):
    elements = _items_to_elements(
        items, _mine_md, now, empty_text="你还没有报名任何活动。")
    return _card("我的报名 %d 项" % len(items),
                 "更新于 %s" % now.strftime("%H:%M"), "green", elements,
                 ["只列活动还没开始的"])


def _buttons(current=""):
    """底部快捷按钮。value 用字符串，飞书部分版本对 dict 值会报 200671。"""
    specs = [("待报名", "v:pending", "primary"),
             ("报名中", "v:open", "default"),
             ("全部", "v:all", "default")]
    actions = []
    for label, value, btype in specs:
        if value.split(":", 1)[1] == current:
            btype = "primary"
        actions.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": btype,
            "value": value,
        })
    return {"tag": "action", "actions": actions}


def menu_card(now):
    """群里用的「菜单」：官方机器人菜单只在单聊显示，群里只能靠按钮。"""
    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content":
            "我能查这些，点下面的按钮，或者直接 @我 说关键词。"}},
        _buttons(""),
    ]
    return _card("学生活动助手", "群聊菜单", "blue", elements,
                 "点按钮切换，或直接 @我 说关键词")


def all_card(pending, open_now, now, type_filter=""):
    title = "待开放 %d 项 · 可报名 %d 项" % (len(pending), len(open_now))
    if type_filter:
        title += "（%s）" % type_filter
    elements = [{"tag": "div",
                 "text": {"tag": "lark_md", "content": "**待开放报名**"}}]
    elements += _items_to_elements(pending, _pending_md, now,
                                   empty_text="暂时没有待开放的活动。")
    elements.append({"tag": "hr"})
    elements.append({"tag": "div",
                     "text": {"tag": "lark_md", "content": "**还能报名**（名额可能已满）"}})
    elements += _items_to_elements(open_now, _open_md, now,
                                   empty_text="现在没有还能报名的活动。")
    elements.append(_buttons("all"))
    return _card(title, "更新于 %s" % now.strftime("%H:%M"), "blue", elements,
                 ["名额可能已满，值得碰碰运气"])


def _btn_value(obj: dict) -> str:
    """按钮 value 必须是字符串：飞书部分版本对 dict 值报 200671。

    里面放 base64url 的 JSON，活动名里有引号、竖线、空格都不会把
    解析搞错位；机器人那边补回 padding 再解码。
    """
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def grab_pending_card(items, now, queued_ids=(), subtitle=None):
    """私聊专用：待开放清单 + 每项一个「预报名」按钮。

    这张卡片只发 owner 私聊。群里那张保持纯公共信息，既不放按钮也不带
    「报没报」的痕迹 —— 群成员看到的内容必须完全一致。
    """
    queued = set(queued_ids or ())
    elements = []
    for idx, it in enumerate(items[:MAX_ITEMS]):
        if idx:
            elements.append({"tag": "hr"})
        vid = str(it.get("id") or "")
        text = _pending_md(it, now)
        # 按钮翻转的同时正文也标一句：只看得到按钮变色、看不到正文变化的话，
        # 用户还得自己回忆「我刚点的到底是哪一场」。
        if vid and vid in queued:
            text += "\n**已预报名** ✅ 到点自动报名，结果私聊回报"
        elements.append({"tag": "div",
                         "text": {"tag": "lark_md", "content": text}})
        if not vid:
            continue
        if vid in queued:
            label, value, btype = "取消预报名", "u:" + vid, "danger"
        else:
            label, btype = "预报名", "primary"
            value = "g:" + _btn_value({"i": vid,
                                       "n": it.get("name") or "",
                                       "t": it.get("sign_start") or "",
                                       "l": 100})
        elements.append({"tag": "action", "actions": [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": btype,
            "value": value,
        }]})

    if not items:
        elements = [{"tag": "div",
                     "text": {"tag": "lark_md", "content": EMPTY_PENDING}}]

    return _card("预报名清单 %d 项" % len(items),
                 subtitle or "更新于 %s" % now.strftime("%H:%M"),
                 "turquoise", elements,
                 ["仅你可见 · 点「预报名」到点自动报名，结果私聊回报",
                  "先看活动详情，确认感兴趣再点"])
