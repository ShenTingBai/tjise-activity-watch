"""临近开放提醒：把「马上要开放报名」的活动单独说一声。

由 cron 每 5 分钟跑一次，**全天运行**（部分活动在 00:00 开闸，
只在白天跑会让这类活动既进不了清单也收不到提醒）。
没有命中就静默退出，不打扰群。

这条提醒只做一件事：把开放时刻说清楚。它不承诺名额、不代表机器人会去报名，
公共群里的每一次推送都必须是「可查证的时间信息」而不是「动作承诺」。

职责分工：本脚本只做 10 分钟窗口的临近提醒；「学校新发布的活动」和
「未来转开放」由 activity_watch.py 负责，两者互不替代，也不共用状态键。

退出码：0 正常（无论有没有推）；1 推送失败；2 登录态问题；3 抓取/接口异常
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import business_now, load_session, load_state, save_state, state_lock  # noqa: E402
from fetch import fetch_all, gender_mismatch, parse_dt   # noqa: E402
from notify import send                                  # noqa: E402
from render import pending_lines, trim                   # noqa: E402

# 报名开放前多少分钟内提醒。5 分钟扫描一次，实际提前量在 5-10 分钟之间。
LEAD_MINUTES = 10

# 一次扫描可能命中十几条（00:00 开闸那种）。分成 5 条一批有两个原因：
# 1) 一条消息里塞太多会刷屏，临近提醒本来就是要快看的；
# 2) 每批独立「锁内发送 → 成功才落盘」，失败的那批下个周期还能重试，
#    不会因为一条推送失败把整轮命中都拖下水。
BATCH_SIZE = 5


def send_rush_card(items, now, fallback_text: str):
    """临近提醒优先发卡片。

    卡片里的「▸ 09:15 开放」比纯文本的缩进行更好扫。卡片构造或发送出任何问题
    都退回纯文本——样式可以降级，提醒不能丢。
    """
    try:
        from card import rush_card
        from notify import send_card
        return send_card(rush_card(items, now, LEAD_MINUTES), fallback_text)
    except Exception as exc:
        print("[!] 卡片不可用，改用纯文本：%s: %s" % (type(exc).__name__, exc))
        return send(fallback_text)


def main() -> int:
    """state.json 损坏时不能静默继续——那会让同一批活动被重复提醒。"""
    try:
        return _scan()
    except RuntimeError as exc:
        print(f"[x] {exc}")
        return 4


def _scan() -> int:
    session = load_session()
    token = (session.get("token") or "").strip()
    if not token:
        print("[i] 没有可用登录态（data/session.json 里没有 token），本轮扫描跳过")
        return 2

    try:
        items = fetch_all(token)
    except PermissionError as exc:
        # 登录态问题由 run.py 统一报警，这里只留一行日志供排查
        print(f"[i] 登录态失效（{exc}），本轮扫描跳过")
        return 2
    except Exception as exc:
        # 不要把异常吞掉：cron 直接跑本脚本，日志是我们唯一的线索。
        # 但也不要往群里发技术错误——公共群只该看到活动信息，
        # 接口报错、traceback 留给 data/scan.log 和管理员。
        print(f"[x] 抓取失败：{type(exc).__name__}: {exc}")
        return 3

    now = business_now()
    gender = session.get("gender", "")

    # 这里只用「时间窗 + 性别」筛候选，不预读 reminded_signup：
    # 真正要不要发，留给每批在锁内重新读 state 再判断。
    due = []
    for it in items:
        # 公用群：不按本人是否已报名过滤，别人还要抢
        if gender_mismatch(it["name"], gender):
            continue
        end = parse_dt(it["sign_end"])
        if end and now > end:
            continue
        start = parse_dt(it["sign_start"])
        if not start:
            continue
        minutes = (start - now).total_seconds() / 60.0
        if 0 < minutes <= LEAD_MINUTES:
            due.append(it)

    if not due:
        return 0

    due.sort(key=lambda x: x["sign_start"])

    failed = False
    for offset in range(0, len(due), BATCH_SIZE):
        batch = due[offset:offset + BATCH_SIZE]
        # 「判断有没有提醒过 → 发送 → 写 reminded_signup」必须在同一把锁里：
        # 否则两个重叠的 cron 进程会读到同一批活动，各发一条。
        # 发送放在锁内，代价只是这一批的网络耗时，换来的是不会重复提醒。
        with state_lock():
            state = load_state()
            reminded = dict(state.get("reminded_signup") or {})
            fresh = [it for it in batch if reminded.get(it["id"]) != it["sign_start"]]
            if not fresh:
                continue                      # 别的进程刚提醒过，让给它

            lines = ["⏰ 即将开放报名"]
            lines += pending_lines(fresh, now, limit=BATCH_SIZE)
            lines.append("只提醒开放时间；报名请在活动详情页自行完成。")
            msg = trim(lines)

            print(msg)
            # 卡片与清单同一套版式；卡片发不出去时 send_card 自己退回这条纯文本
            result = send_rush_card(fresh, now, msg)
            if result is not True:
                # 发送失败就整批回滚：不预写占位，下个扫描周期重试这一批
                failed = True
                break

            for it in fresh:
                reminded[it["id"]] = it["sign_start"]
            cutoff = now - timedelta(days=2)
            state["reminded_signup"] = {
                k: v for k, v in reminded.items()
                if (parse_dt(v) or datetime.min) >= cutoff
            }
            save_state(state)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
