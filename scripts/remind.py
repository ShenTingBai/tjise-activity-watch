"""私人提醒：今天要参加的活动（已报名 + 活动在今天）。

只走私聊，绝不进群——报名状态属于个人数据。

用法：
    python3 scripts/remind.py          # 有活动就私聊推送，没有就静默退出
    python3 scripts/remind.py --dry    # 只打印不推送
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import load_session  # noqa: E402
from fetch import fetch_all, mine_items, parse_dt  # noqa: E402
from notify import send_private, send_private_card  # noqa: E402


def main() -> int:
    dry = "--dry" in sys.argv

    session = load_session()
    token = (session.get("token") or "").strip()
    if not token:
        print("[x] 没有可用登录态（data/session.json 里没有 token），今天的私人提醒没发出去")
        return 2

    try:
        items = fetch_all(token)
    except PermissionError as exc:
        # 只记一行日志、不重复报警：登录态告警由 run.py 与 token_probe 统一负责
        print(f"[x] 登录态失效（{exc}），今天的私人提醒没发出去；恢复后不补发")
        return 2
    except Exception as exc:
        # 吞掉异常等于静默失败：cron 只把 stdout 写进 remind.log，日志是唯一线索
        print(f"[x] 抓取失败：{type(exc).__name__}: {exc}")
        return 3

    now = datetime.now()
    today = now.date()

    todays = []
    for it in mine_items(items, now):
        start = parse_dt(it.get("act_start"))
        if start and start.date() == today:
            todays.append(it)

    if not todays:
        return 0

    todays.sort(key=lambda x: x["act_start"] or "")
    lines = ["🎟 今天（%s）有 %d 个活动要参加" % (now.strftime("%m-%d"), len(todays)), ""]
    for it in todays:
        lines.append("▸ %s" % (it["act_start"][11:16]))
        lines.append("　" + it["name"])
        meta = [it.get("type") or "其他"]
        if it.get("place"):
            meta.append(str(it["place"]))
        lines.append("　" + "｜".join(meta))
        lines.append("")

    msg = "\n".join(lines).rstrip()
    print(msg)

    if dry:
        print("\n[i] --dry，未推送")
        return 0

    card = None
    try:
        from card import remind_card
        card = remind_card(todays, now)
    except Exception as exc:
        print("[!] 卡片构造失败，改用纯文本：%s" % exc)

    ok = send_private_card(card, msg) if card else send_private(msg)
    # send_private_card 成功时返回 message_id 或 True；False/None 才算没发出去
    return 0 if ok not in (False, None) else 1


if __name__ == "__main__":
    sys.exit(main())
