"""一致性自检：状态文件、锁、并发、token 缓存、卡片构造。"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

STATE = ROOT / "data" / "state.json"
LOCK = ROOT / "data" / ".state.lock"
TOKEN_CACHE = ROOT / "data" / "token_cache.json"


def parse_ok():
    try:
        with open(str(STATE), encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        return {"error": str(exc)}


def check_token_cache():
    """第一次应真实申请，第二次应命中缓存（耗时明显更短）。"""
    import notify
    app = notify.load_config()["notify"]["feishu_app"]

    if TOKEN_CACHE.exists():
        TOKEN_CACHE.unlink()

    t0 = time.time()
    tok1 = notify._tenant_token(app, force=True)
    cost1 = time.time() - t0

    t0 = time.time()
    tok2 = notify._tenant_token(app)
    cost2 = time.time() - t0

    ok = bool(tok1) and tok1 == tok2 and TOKEN_CACHE.exists()
    print("token 申请: %.3fs，缓存命中: %.3fs，一致: %s"
          % (cost1, cost2, tok1 == tok2))
    print("  缓存文件:", "存在" if TOKEN_CACHE.exists() else "缺失")
    return ok


def check_cards():
    import card
    from datetime import datetime
    now = datetime.now()
    problems = []

    for name, payload in (("menu", card.menu_card(now)),
                          ("pending", card.pending_card([], now)),
                          ("open", card.open_card([], now)),
                          ("digest", card.digest_card({"pending": [], "new_count": 0,
                                                       "open_count": 0,
                                                       "generated_at": "test"}))):
        try:
            json.dumps(payload)
        except Exception as exc:
            problems.append("%s 序列化失败: %s" % (name, exc))
            continue
        size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        if size > 30000:
            problems.append("%s 超过 30KB 限制（%d）" % (name, size))
        print("  卡片 %-8s %d 字节" % (name, size))

    return not problems, problems


def check_parity():
    """推送卡片与 @ 回复卡片必须同构：只允许差一个按钮区。"""
    import card
    from datetime import datetime

    now = datetime.now()
    items = [
        {"name": "测试活动A", "type": "实践学习", "place": "教学A301",
         "sign_start": "2026-10-21 12:00:00", "sign_end": "2026-10-21 17:59:00",
         "act_start": "2026-10-21 18:30:00"},
        {"name": "测试活动B", "type": "宿舍文化", "place": "",
         "sign_start": "2026-10-22 09:00:00", "sign_end": "2026-10-22 12:00:00",
         "act_start": "2026-10-22 14:00:00"},
    ]
    summary = {"pending": items, "new_count": 0, "open_count": 0,
               "generated_at": now.strftime("%Y-%m-%d %H:%M")}

    push = card.digest_card(summary)
    reply = card.pending_card(items, now)

    def strip(c):
        return {
            "title": c["header"]["title"]["content"],
            "subtitle": c["header"].get("subtitle", {}).get("content", ""),
            "template": c["header"]["template"],
            "elements": [e for e in c["elements"] if e.get("tag") != "action"],
        }

    a, b = strip(push), strip(reply)
    diff = [k for k in a if a[k] != b[k]]
    if diff:
        print("✗ 推送与回复不一致：", ", ".join(diff))
        for k in diff:
            print("    推送:", str(a[k])[:120])
            print("    回复:", str(b[k])[:120])
        return False

    print("✓ 推送卡片与 @ 回复同构（差异仅按钮区）")
    print("  标题:", a["title"], "｜副标题:", a["subtitle"],
          "｜元素数:", len(a["elements"]))
    return True


def check_limit_parity():
    """卡片和纯文本兜底在同一批数据下列出的条数必须一致。

    曾经卡片限 8 条、文本限 12 条：条目一多，兜底文案比卡片多出 4 条，
    用户会以为卡片漏推了。现在两边共用 common.MAX_LIST_ITEMS。
    """
    import card
    import render
    from common import MAX_LIST_ITEMS
    from datetime import datetime

    now = datetime.now()
    items = [{"name": "测试活动%02d" % i, "type": "实践学习", "place": "教学A301",
              "sign_start": "2026-10-21 12:00:00", "sign_end": "2026-10-21 17:59:00",
              "act_start": "2026-10-21 18:30:00"}
             for i in range(MAX_LIST_ITEMS + 4)]

    c = card.pending_card(items, now)
    card_n = len([e for e in c["elements"] if e.get("tag") == "div"])
    card_more = any("另有" in str(e) for e in c["elements"] if e.get("tag") == "note")

    lines = render.pending_lines(items, now)
    text_n = sum(1 for ln in lines if ln.startswith(render.BULLET))
    text_more = any("另有" in ln for ln in lines)

    ok = True
    if card_n != MAX_LIST_ITEMS:
        print("✗ 卡片列出 %d 条，应为 %d" % (card_n, MAX_LIST_ITEMS))
        ok = False
    if text_n != card_n:
        print("✗ 条数不一致：卡片 %d 条 / 文本 %d 条" % (card_n, text_n))
        ok = False
    if not (card_more and text_more):
        print("✗ 超出上限时应提示「另有 N 项」：卡片 %s / 文本 %s"
              % (card_more, text_more))
        ok = False
    if ok:
        print("✓ 卡片与文本同限 %d 条，超出均提示「另有 %d 项」"
              % (card_n, len(items) - MAX_LIST_ITEMS))
    return ok


def main():
    ok = True
    print("锁文件存在:", LOCK.exists())

    d = parse_ok()
    if "error" in d:
        print("✗ state.json 解析失败:", d["error"])
        return 1
    print("state 键:", sorted(d.keys()))
    print("  seen:", len(d.get("seen", [])),
          " reminded:", len(d.get("reminded_signup", {})))

    # 并发跑 4 个进程，看会不会写坏
    procs = [subprocess.Popen([sys.executable, str(ROOT / "scripts" / "scan.py")],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
             for _ in range(4)]
    for p in procs:
        p.wait()

    d2 = parse_ok()
    if "error" in d2:
        print("✗ 并发后 state.json 损坏:", d2["error"])
        return 1
    print("✓ 并发 4 个进程后仍可解析，seen:", len(d2.get("seen", [])))

    print("\n== token 缓存 ==")
    if not check_token_cache():
        ok = False
        print("✗ token 缓存异常")

    print("\n== 卡片 ==")
    cards_ok, problems = check_cards()
    if not cards_ok:
        ok = False
        for p in problems:
            print("  ✗", p)

    print("\n== 推送 / 回复一致性 ==")
    if not check_parity():
        ok = False
    if not check_limit_parity():
        ok = False

    print("\n" + ("== 自检通过 ==" if ok else "== 自检未通过 =="))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
