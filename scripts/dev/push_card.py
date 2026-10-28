"""用应用身份往指定群发一张卡片，方便测试按钮回调。

    python3 scripts/dev/push_card.py menu oc_xxxxxxxx
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import notify  # noqa: E402
from card import all_card, menu_card, open_card, pending_card  # noqa: E402


def build(view, now):
    if view == "menu":
        return menu_card(now)
    if view == "open":
        return open_card([], now)
    if view == "all":
        return all_card([], [], now)
    return pending_card([], now)


def main() -> int:
    view = sys.argv[1] if len(sys.argv) > 1 else "menu"
    chat_id = sys.argv[2] if len(sys.argv) > 2 else ""
    if not chat_id:
        print("用法: python3 scripts/dev/push_card.py <menu|pending|open|all> <chat_id>")
        return 2

    cfg = notify.load_config()["notify"]
    app = cfg.get("feishu_app") or {}
    token = notify._tenant_token(app)
    if not token:
        print("[x] 取 token 失败")
        return 1

    card = build(view, datetime.now())
    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
    ok, body = notify._post(url, {
        "receive_id": chat_id,
        "msg_type": "interactive",
        "content": __import__("json").dumps(card, ensure_ascii=False),
    }, token=token)
    print(("✓" if ok else "x"), body[:200])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
