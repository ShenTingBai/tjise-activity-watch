"""预报名卡片的快照与原地刷新。

发出去的那张卡片得能「自己变」：点「预报名」，同一个按钮变成「取消预报名」；
已经报上的、报名窗口过去的，那一项直接从卡上撤掉。
飞书只支持按 message_id 整卡覆盖，所以发送时把列出的条目存一份快照，
点按钮时按快照重建成新卡再 PATCH 回去。

快照只留卡片要用的公共字段：不存 token，也不存「我报了什么」之外的任何个人数据。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAP = ROOT / "data" / "grab_card.json"

FIELDS = ("id", "name", "type", "place", "sign_start", "sign_end",
          "act_start", "is_new", "signed")
FMT = "%Y-%m-%d %H:%M:%S"


def _dt(text):
    raw = str(text or "").strip()
    for fmt in (FMT, "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def save(message_id: str, items: list, sent_at: datetime = None) -> dict:
    """记下这张卡片：消息 ID + 当时列出的条目（只留卡片要用的字段）。"""
    snap = {
        "message_id": str(message_id or ""),
        "sent_at": (sent_at or datetime.now()).strftime(FMT),
        "items": [{k: it.get(k) for k in FIELDS} for it in items],
    }
    SNAP.parent.mkdir(parents=True, exist_ok=True)
    tmp = SNAP.with_name(SNAP.name + ".tmp")
    tmp.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(str(tmp), 0o600)
    os.replace(str(tmp), str(SNAP))
    return snap


def load() -> dict:
    try:
        snap = json.loads(SNAP.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return snap if isinstance(snap, dict) else {}


def live_items(snap: dict = None, now: datetime = None) -> list:
    """还能点的条目：已报名、报名已截止、活动已开始的一律撤掉。

    撤掉而不是标成「已报名」：卡片上多一行就少一分「点了会不会重复报名」的
    犹豫，留下的每一行都必须是能点的。
    """
    now = now or datetime.now()
    items = []
    for it in (snap or load()).get("items") or []:
        if not it.get("id") or it.get("signed"):
            continue
        # 报名已经开始的，预报名没有意义：点下去只会排一个「开放时间在过去」的任务，
        # runner 立刻判错过。真要抢就当场抢，不能混在「等开放」的卡片里。
        sign_start = _dt(it.get("sign_start"))
        if sign_start and sign_start <= now:
            continue
        sign_end = _dt(it.get("sign_end"))
        if sign_end and now > sign_end:
            continue
        act_start = _dt(it.get("act_start"))
        if act_start and act_start <= now:
            continue
        items.append(it)
    return items


def refresh(now: datetime = None) -> bool:
    """按当前清单状态重建并覆盖那张卡片；没有快照就什么都不做。

    用于「非点击」引起的状态变化（runner 抢完一场）。点击引起的翻转不走这里：
    飞书要求按钮回调直接回传新卡片，客户端当场替换，比 PATCH 稳。
    """
    snap = load()
    mid = snap.get("message_id") or ""
    if not mid:
        return False
    card = card_json(now)
    try:
        from notify import update_card
        return update_card(mid, card)
    except Exception as exc:
        print("[!] 预报名卡片刷新失败：%s" % exc)
        return False


def card_json(now: datetime = None) -> dict:
    """按快照 + 当前清单重建整张卡片（不发送）。"""
    import watchlist
    from card import grab_pending_card

    now = now or datetime.now()
    queued = {e.get("id") for e in watchlist.load()
              if e.get("status") in watchlist.ACTIVE}
    return grab_pending_card(live_items(now=now), now, queued)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "card":
        # 机器人按钮回调要拿到整张新卡片，退回给飞书由客户端当场替换
        print(json.dumps(card_json(), ensure_ascii=False))
    else:
        print("refresh:", refresh())
