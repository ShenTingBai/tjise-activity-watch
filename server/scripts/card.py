"""飞书消息卡片。

用 interactive 卡片代替纯文本，换来标题色、加粗、分隔线和页脚注。
卡片构造失败时上层会退回纯文本，不会因为格式问题丢消息。

两类卡片，边界必须清楚：

- **公共卡片**（digest / pending / open / all / rush / 动态事件）：只写学校公开信息。
  不出现「我报没报」「提交了几次」「抢名额」「碰运气」，按钮只用于切换查询视图。
- **私聊卡片**（grab_pending_card）：只发 owner 私聊，才有预报名 / 取消预报名动作。
"""
import base64
import json
from datetime import datetime

from common import MAX_LIST_ITEMS, business_now, fmt_when

# 卡片里最多列几条，太长了手机上一屏看不完。
# 纯文本兜底共用同一个上限，保证两条通道条数一致。
MAX_ITEMS = MAX_LIST_ITEMS

# ── 公共群状态词 ──────────────────────────────────────────────────────
# 全项目只这五个词，别处不再造同义词。文案要能被单独截图转述：
# 「报名已开放」是事实，「快抢」「还能捡漏」是承诺，公共群不给承诺。
STATUS_PENDING = "待开放报名"
STATUS_OPEN = "报名已开放"
STATUS_OPEN_LONG = "报名已开放一段时间"
STATUS_CLOSED = "报名已结束"
STATUS_UNKNOWN = "报名时间待确认"

# 开放超过 6 小时仍可报名时，用「一段时间」取代「已开放」：
# 刚开的和开了半天的，用户该做的事不一样（前者盯时间，后者直接去详情页看余额）。
OPEN_LONG_MINUTES = 360

_STATUS_BY_KEY = {
    "pending": STATUS_PENDING,
    "open": STATUS_OPEN,
    "closed": STATUS_CLOSED,
    "unknown": STATUS_UNKNOWN,
}


def _dt(text):
    """轻量时间解析：卡片只做展示，不引 fetch（那会把 urllib 拖进纯渲染路径）。"""
    raw = str(text or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def status_of(item: dict, now: datetime = None) -> str:
    """活动的公开状态（pending / open / closed / unknown）。

    优先用事件里已经算好的 status：展示与判定必须同一个来源，
    否则会出现「事件说已开放、卡片说待开放」这种自相矛盾的消息。
    """
    now = now or business_now()
    raw = str(item.get("status") or "").strip()
    if raw in _STATUS_BY_KEY:
        return raw
    end = _dt(item.get("sign_end"))
    if end and now > end:
        return "closed"
    act_start = _dt(item.get("act_start"))
    if act_start and act_start <= now:
        return "closed"
    start = _dt(item.get("sign_start"))
    if not start:
        return "unknown"
    return "pending" if start > now else "open"


def status_label(item: dict, now: datetime = None) -> str:
    """给用户看的状态词。已开放太久的不再叫「刚开放」。"""
    now = now or business_now()
    status = status_of(item, now)
    if status == "open":
        start = _dt(item.get("sign_start"))
        if start and (now - start).total_seconds() >= OPEN_LONG_MINUTES * 60:
            return STATUS_OPEN_LONG
        return STATUS_OPEN
    return _STATUS_BY_KEY[status]


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


# 两条通道共用的文案常量，改一处两边同时生效。
# 「只提醒」三个字是有意的：公共群不承诺机器人会去报名，也不承诺还有名额。
BASE_FOOTER = "开放前 10 分钟会单独提醒，报名请自行前往"
EMPTY_PENDING = "暂时没有待开放报名的活动。"
BASE_TRAILER = "只提供公开活动信息；报名请在活动详情页自行完成"


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

    清单把「报名已开放」排在前面：它比待开放更需要当下处理；
    以前只推待开放的活动，已经开放报名的会整片从群里消失。

    这批文案同时是 selfcheck 的「推送/回复同构」基准：除按钮区外，
    digest_card 与 pending_card 必须逐字段一致。
    """
    from render import digest_items

    now = business_now()
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
        extra.append("另有 %d 项报名已开放，@元小元 说「报名中」可看" % rest_open)
    if late_checkin:
        extra.append("🌙 晚归签到 22:45–23:59，别忘签")

    gen = summary.get("generated_at", "")
    subtitle = "更新于 %s" % gen[11:16] if len(gen) >= 16 else None

    footer = [BASE_FOOTER] if pending_count else []
    footer += extra

    return _card(title, subtitle, "blue", elements, footer)


def open_card(items, now, type_filter=""):
    title = "报名已开放 %d 项" % len(items)
    if type_filter:
        title += "（%s）" % type_filter
    elements = _items_to_elements(
        items, _open_md, now, empty_text="现在没有报名已开放的活动。")
    elements.append(_buttons("open"))
    return _card(title, "更新于 %s" % now.strftime("%H:%M"), "orange", elements,
                 ["报名已开放不等于还有名额，以活动详情页为准",
                  "回复「待报名」看还没开放的活动"])


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
    """临近开放提醒：还有几分钟开放报名。

    只报时间，不承诺名额、不暗示「快来抢」——公共群的这条消息
    就是一份可查证的时间表，用户自己决定去不去。
    """
    elements = _items_to_elements(items, _pending_md, now)
    return _card("⏰ 即将开放报名",
                 "%d 分钟内 · 更新于 %s" % (int(lead_minutes), now.strftime("%H:%M")),
                 "orange", elements,
                 ["只提醒开放时间；报名请在活动详情页自行完成"])


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
    title = "待开放 %d 项 · 报名已开放 %d 项" % (len(pending), len(open_now))
    if type_filter:
        title += "（%s）" % type_filter
    elements = [{"tag": "div",
                 "text": {"tag": "lark_md", "content": "**待开放报名**"}}]
    elements += _items_to_elements(pending, _pending_md, now,
                                   empty_text="暂时没有待开放的活动。")
    elements.append({"tag": "hr"})
    elements.append({"tag": "div",
                     "text": {"tag": "lark_md",
                              "content": "**报名已开放**（是否还有名额以详情页为准）"}})
    elements += _items_to_elements(open_now, _open_md, now,
                                   empty_text="现在没有报名已开放的活动。")
    elements.append(_buttons("all"))
    return _card(title, "更新于 %s" % now.strftime("%H:%M"), "blue", elements,
                 [BASE_TRAILER])


def _event_md(event: dict, now) -> str:
    """一条动态事件的卡片正文。

    与原定开放时间和本次发现时间同时出现：只给一个时刻，
    用户没法判断自己是刚赶上，还是已经晚了半天。
    """
    lines = ["**▸ %s**　%s" % (status_label(event, now), event.get("name") or "活动")]
    meta = [event.get("type") or "其他"]
    if event.get("place"):
        meta.append(str(event["place"]))
    lines.append("｜".join(meta))

    tail = []
    if event.get("sign_start"):
        tail.append("原定开放 %s" % fmt_when(event["sign_start"], now))
    if event.get("kind") in ("new_open", "opened"):
        tail.append("本次发现 %s" % fmt_when(event.get("discovered_at"), now))
    if event.get("sign_end"):
        tail.append("截止 %s" % fmt_when(event["sign_end"], now))
    if event.get("act_start"):
        tail.append("活动 %s" % fmt_when(event["act_start"], now))
    if event.get("checkin"):
        tail.append(str(event["checkin"]))
    if tail:
        lines.append("｜".join(tail))

    changed = event.get("changed_fields") or []
    if changed:
        from activity_watch import FIELD_LABELS      # 局部导入，避免模块级循环
        lines.append("变化：" + "、".join(FIELD_LABELS.get(k, k) for k in changed))
    return "\n".join(lines)


def public_event_card(events, now=None):
    """公共群动态卡片：只读信息 + 查询按钮。

    按钮只有「待报名 / 报名中 / 全部」三个查询视图：群里任何人点下去
    只会拿到一份新的公开清单，不会触发预报名、报名或重试。
    这张卡片也因此不需要任何 owner 身份。
    """
    now = now or business_now()
    events = list(events or [])
    elements = _items_to_elements(events, _event_md, now,
                                  empty_text="最近没有新的活动动态。")
    elements.append(_buttons(""))
    return _card("活动动态 %d 项" % len(events),
                 "更新于 %s" % now.strftime("%H:%M"), "turquoise", elements,
                 [BASE_TRAILER])


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
