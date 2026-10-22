"""定时任务入口：抓取 → 生成简报 →（可选）推送 QQ 群。

     python scripts/run.py            # 正常跑一轮，会推送
     python scripts/run.py --dry      # 只抓不推送
     python scripts/run.py --dry --push-preview   # 只抓，并打印将要推送的文案
     python scripts/run.py --with-grab   # 07:00 那轮：群里正常推 + 私聊发预报名卡片

退出码：0 正常；1 推送失败；2 登录失效；3 抓取/解析失败；
        4 状态文件损坏；5 未配置推送通道（抓到了但没地方发）
"""

import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (MAX_LIST_ITEMS, REPORTS, ROOT, fmt_when, load_state,  # noqa: E402
                    save_state, state_lock, update_state)
from render import digest_items, digest_lines, trim                    # noqa: E402

# 同一个时间窗内如果已经推过，就不再重复推（本机 + 云端双保险时用）
GUARD_MINUTES = 60


def build_message(summary: dict) -> str:
    """群里的公共清单：报名中的排前面，再排待开放的，合计最多 8 项。

    只用公共字段（名称/类型/地点/时间），不带任何「我报没报」的信息——
    群里六个人看到的是同一份。
    """
    pending = summary.get("pending", [])
    open_count = int(summary.get("open_count") or 0)
    if not pending and not open_count:
        return "📋 待开放报名 0 项\n\n暂时没有待开放报名的活动。"

    if open_count:
        head = "📋 可报名 %d 项 · 待开放 %d 项" % (open_count, len(pending))
    else:
        head = "📋 待开放报名 %d 项" % len(pending)

    lines = [head, ""]
    lines += digest_lines(digest_items(summary), datetime.now())
    lines.append("开放前 10 分钟会单独提醒。" if pending
                 else "名额随时会满，看到合适的先报名。")
    return trim(lines)


LATE_CHECKIN_NOTE = ("\n\n🌙 晚归签到 22:45–23:59\n"
                     "晚归的同学记得在 23:59 前完成。")


def _mark_announced(items) -> None:
    """推送成功后才写「已推送」集合，保留 120 天，避免无限增长。"""
    if not items:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    cutoff = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d")

    def _upd(st):
        # 不迁移旧版 seen。seen 的语义是「抓取过」，announced 是「推送成功过」，
        # 把抓取过的当成推送过的，会让那些真正没发出去的活动永久失去「新增」标记
        # ——届时用户看到清单里没有 🆕，会以为没有新活动。
        ann = dict(st.get("announced") or {})
        for it in items:
            ann[str(it.get("id") or "")] = today
            ann[it["name"]] = today
        st["announced"] = {k: v for k, v in ann.items() if k and v >= cutoff}
        st.pop("seen", None)      # 旧字段，语义已废弃，直接丢掉
        return st

    update_state(_upd)


def alert(text: str) -> None:
    """异常时也往群里喊一声，避免静默失败。"""
    try:
        from notify import send
        send(text)
    except Exception as exc:
        print(f"[!] 告警发送失败：{exc}")


def send_grab_card(summary: dict) -> None:
    """私聊推一张带「预报名」按钮的清单。

    只在 07:00 那轮发（cron 带 --with-grab），避免一天四张重复卡片。
    发不出去不算本轮失败：群里那张已经发出去了，这里失败只记日志，
    否则一次私聊通道故障会把整轮推送判成失败、进而放弃写「已推送」集合。

    已经报上名的活动不进这张卡片：卡片上的每个按钮都必须能点出真实动作，
    给一个「已报名」的场次挂「预报名」只会让人怀疑是不是会重复报名。
    """
    pending = [it for it in summary.get("pending", []) if not it.get("signed")]
    if not pending:
        return
    try:
        import grab_card
        import watchlist
        from card import grab_pending_card
        from notify import send_private_card

        queued = {e.get("id") for e in watchlist.load()
                  if e.get("status") in watchlist.ACTIVE}
        mid = send_private_card(grab_pending_card(pending, datetime.now(), queued))
        if not mid:
            print("[!] 预报名卡片没发出去（私聊通道不可用？）")
            return
        # 存快照：之后点按钮要靠 message_id 原地把「预报名」换成「取消预报名」
        grab_card.save(mid, pending)
    except Exception as exc:
        print("[!] 预报名卡片构造/发送失败：%s" % exc)


def main() -> int:
    dry = "--dry" in sys.argv
    force = "--force" in sys.argv      # 手动触发时忽略防重复窗口
    late_checkin = "--late-checkin" in sys.argv   # 21:30 那轮附带晚归签到提醒
    with_open = "--with-open" in sys.argv         # 末尾附「仍在报名中」的条数
    with_grab = "--with-grab" in sys.argv         # 07:00 那轮私聊推预报名卡片
    py = sys.executable

    proc = subprocess.run([py, str(ROOT / "scripts" / "fetch.py")],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, encoding="utf-8",
                          cwd=str(ROOT))
    out = (proc.stdout or "") + (proc.stderr or "")

    if proc.returncode != 0:
        print(out.strip())
        if proc.returncode == 2:
            print("\nACTION: 登录态失效，需要重新运行 login.py")
            if not dry:
                alert("【学生活动速递·异常】\n登录态已失效，今天抓不到活动了。\n\n"
                      "处理办法：\n"
                      "  1) 本地运行 python scripts/login.py 重新登录\n"
                      "  2) 把新的登录态同步到服务器（见 deploy/sync_token.sh）\n"
                      "  3) 或直接触发服务器续期脚本")
        elif proc.returncode == 3:
            if not dry:
                alert("【学生活动速递·异常】\n接口返回异常，抓取失败。\n"
                      "请在服务器上看： tail -20 ~/tjise-activity-watch/data/cron.log")
        return proc.returncode

    summary = None
    if "SUMMARY_JSON_BEGIN" in out:
        blob = out.split("SUMMARY_JSON_BEGIN", 1)[1].split("SUMMARY_JSON_END", 1)[0]
        try:
            summary = json.loads(blob.strip())
        except json.JSONDecodeError:
            summary = None

    if summary is None:
        print(out.strip())
        print("\n[x] 没能解析出结构化摘要。")
        return 3

    # 「新增」以「已成功推送过的集合」为基线，而不是上一次抓取结果。
    # 这样即使上一轮抓取或推送失败，也不会漏标、不会重复标。
    try:
        _state = load_state()
    except RuntimeError as exc:
        print(f"\n[x] {exc}")
        alert("【学生活动速递·异常】\nstate.json 读取失败，本轮已终止，不会推送。\n\n"
              f"{exc}\n\n处理办法：检查 data/state.json，损坏的话从备份恢复。")
        return 4
    announced = set(_state.get("announced") or {})
    for it in summary.get("pending", []):
        it["is_new"] = (str(it.get("id") or "") not in announced) and (it["name"] not in announced)

    # 只保留一条渲染路径：卡片和纯文本兜底用同一份数据、同样的字段顺序。
    # 不做「无变化精简」——那会让卡片和兜底文案长得不一样，反而制造差异。
    msg = build_message(summary)
    if with_open:
        shown_open = sum(1 for it in digest_items(summary)[:MAX_LIST_ITEMS]
                         if it.get("kind") == "open")
        rest_open = max(0, int(summary.get("open_count") or 0) - shown_open)
        if rest_open:
            msg += ("\n\n另有 %d 项还能报名（名额可能已满），"
                    "@元小元 说「报名中」可看。" % rest_open)
    if late_checkin:
        msg += LATE_CHECKIN_NOTE
    print(msg)

    if dry:
        print("\n[i] --dry 模式，未推送。")
        print(f"[i] 日报：{REPORTS / 'latest.md'}")
        return 0

    # 先在锁内「占位」，再发消息。这样两个进程同时到点也只有一个能发出去。
    prev_ts = 0.0
    with state_lock():
        st = load_state()
        last_ts = float(st.get("last_push_ts") or 0)
        if not force and time.time() - last_ts < GUARD_MINUTES * 60:
            mins = int((time.time() - last_ts) / 60)
            print(f"\n[i] {mins} 分钟前刚推送过，本次跳过（防重复）。手动触发请加 --force。")
            return 0
        prev_ts = last_ts
        st["last_push_ts"] = time.time()
        save_state(st)
        claim_ts = st["last_push_ts"]

    def release_claim() -> None:
        """把本轮占位还回去，只影响「仍然是本轮占下的那个时间戳」。

        如果占位等待期间有人带 --force 手动推过一轮，last_push_ts 已经更新，
        这里就不能再回滚，否则会把刚推送过的防重复窗口抹掉。
        """
        with state_lock():
            current = load_state()
            if float(current.get("last_push_ts") or 0) != claim_ts:
                return
            current["last_push_ts"] = prev_ts
            save_state(current)

    from notify import send as send_text
    from notify import send_card
    card = None
    try:
        from card import digest_card
        card = digest_card(summary, late_checkin=late_checkin, with_open=with_open)
    except Exception as exc:
        print("[!] 卡片构造失败，改用纯文本：%s" % exc)

    try:
        result = send_card(card, msg) if card else send_text(msg)
    except Exception as exc:
        # 发送过程抛异常也要释放占位，否则后面一小时都不会再尝试
        print(f"[x] 推送异常：{type(exc).__name__}: {exc}")
        release_claim()
        return 1

    # 只有明确的 True 才算发出去了。False / None / 任何非法返回值都不写成功集合：
    # 一旦误写，这条活动以后就不会再标「新增」，等于永久漏推。
    if result is not True:
        release_claim()
        if result is None:
            # 抓到了却没地方发，属于配置缺失。返回非 0，让 cron 日志/systemd 能看出来，
            # 否则每天四轮都「成功」却一条消息都收不到，是最难发现的一种故障。
            print("\n[x] 未配置推送通道，本轮消息没有发出去。")
            print("    在 config.json 的 notify 里填 webhook_url，或改用飞书应用机器人。")
            return 5
        if result is not False:
            print(f"\n[x] 推送返回值不是 True/False/None：{result!r}")
        print("\nACTION: 推送失败，请检查 config.json 的 notify 配置")
        return 1

    # 只有真正发出去了，才把「已推送」记下来。
    # 但清单最多显示 MAX_LIST_ITEMS 项（且报名中的占用了前面的位置）：
    # 只把用户真正看见的那几条待开放活动记为已推送，
    # 否则没显示出来的活动下次也不会再标「新增」，等于静默漏推。
    visible = digest_items(summary)[:MAX_LIST_ITEMS]
    _mark_announced([it for it in visible if it.get("kind") == "pending"])
    if with_grab:
        send_grab_card(summary)
    print(f"[i] 日报已写入 {REPORTS / 'latest.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
