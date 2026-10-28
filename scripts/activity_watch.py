"""公共活动动态发现：只读学校公开活动，发现新增 / 转开放 / 字段变化就记一条事件。

与另外两条链路的分工（互不替代）：

- ``run.py`` 固定日报：一天四档，列「当前全部待开放 + 报名已开放」；
- ``scan.py`` 临近提醒：开放前 10 分钟，只提醒时间；
- 本脚本：按白天/夜间固定节奏轮询，负责「学校随时新发布的活动」和「未来转开放」。

产品边界：

- 只读。不发报名请求、不碰任何个人清单、事件里只有公开字段（``fetch.PUBLIC_FIELDS``）；
- 状态与推送去重分开。``state.json`` 记「已经推送过什么」，
  本脚本的状态文件记「这份公开活动什么时候第一次被发现、上次是什么状态」，
  互不覆盖；
- 接口失败或单轮超时不产生活动结束事件，也不清空上一轮成功快照——
  少返回一页不等于活动被取消；
- 首次成功抓取只建基线、不发送通知（``baseline_initialized``）：
  升级那一刻活动库里已有几十条历史活动，不能把它们当新消息推一遍。

用法：:

    python3 scripts/activity_watch.py               # 正常一轮（cron 每分钟唤醒，按 next_poll_at 自己控频）
    python3 scripts/activity_watch.py --dry-run     # 只打印事件，不发送、不写状态
    python3 scripts/activity_watch.py --force       # 忽略排期立刻跑一轮
    python3 scripts/activity_watch.py --now "2026-10-27 13:00"   # 按指定北京时间复盘

退出码：0 正常（含「未到点跳过」）；1 发送失败；2 登录态不可用；3 抓取失败；4 状态文件损坏。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (ACTIVITY_WATCH_FILE, ACTIVITY_WATCH_LOCK,  # noqa: E402
                    WATCH_BACKOFF_SECONDS, WATCH_DAY_START_HOUR,
                    WATCH_NIGHT_START_HOUR, WATCH_STATE_VERSION,
                    WATCH_TIMEOUT_SECONDS, atomic_write_text, business_now,
                    load_session, load_watch_config, try_file_lock)
from fetch import (STATUS_CLOSED, STATUS_OPEN, STATUS_PENDING, STATUS_UNKNOWN,  # noqa: E402
                   classify_public, fetch_all, parse_dt)

# 状态文件与锁由模块级变量持有，测试可以整体替换成临时目录，
# 不必读写真仓库的 data/。
STATE_FILE = ACTIVITY_WATCH_FILE
LOCK_FILE = ACTIVITY_WATCH_LOCK
FMT = "%Y-%m-%d %H:%M:%S"

# 事件类型。new_open 与 opened 的区别对用户很重要：
# 前者是「一发现就已经开放」，后者是「我们看着它开放的」。
EVENT_NEW_PENDING = "new_pending"
EVENT_NEW_OPEN = "new_open"
EVENT_OPENED = "opened"
EVENT_CHANGED = "changed"

# 同一条活动一轮里只留一条事件，取最需要被看到的那种。
_EVENT_RANK = {EVENT_CHANGED: 1, EVENT_NEW_PENDING: 2, EVENT_NEW_OPEN: 3, EVENT_OPENED: 4}

# 指纹覆盖全部公开字段：字段变了就要重算，否则改期会被指纹掩盖。
FINGERPRINT_FIELDS = ("name", "type", "place", "sign_start", "sign_end",
                      "act_start", "act_end", "checkin")

# 只有时间与签到变化才值得单独发一条「改期」通知；
# 名称/地点改字不打扰群，下一档日报自然会用新文案。
TIME_FIELDS = ("sign_start", "sign_end", "act_start", "act_end", "checkin")

FIELD_LABELS = {
    "name": "活动名称",
    "type": "类型",
    "place": "地点",
    "sign_start": "报名开放时间",
    "sign_end": "报名截止时间",
    "act_start": "活动开始时间",
    "act_end": "活动结束时间",
    "checkin": "签到时间",
}

# 连续三轮都抓不到才认为活动真的从活动库里消失了。
# 学校分页偶发少返回一页是常态，一轮没见到就宣布结束会造成假消息。
MISSING_ROUNDS_LIMIT = 3


def _empty_state() -> dict:
    return {
        "version": WATCH_STATE_VERSION,
        "activities": {},
        "pending_events": [],
        "last_run_at": "",
        "last_success_at": "",
        "next_poll_at": "",
        "consecutive_failures": 0,
        "last_error": "",
        # 首次成功抓取只建基线：这两个字段用来区分「还没基线」和「基线已建、
        # 只是当前没有活动」，否则空活动库会被反复当成首次运行。
        "initialized_at": "",
        "baseline_initialized": False,
    }


def load_state(path=None) -> dict:
    """读监测状态。解析失败抛错，绝不静默回到空状态。

    静默清空等于把「哪些活动已经通知过」整片丢掉：下一轮所有活动都会被当成
    首次发现，群里瞬间刷屏，而日志里看不出是缓存坏了。宁可这一轮失败并留下痕迹。
    """
    path = Path(path or STATE_FILE)
    if not path.exists():
        return _empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(
            "activity_watch.json 解析失败（%s）。原文件未改动，请先修复再运行。" % exc
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError(
            "activity_watch.json 结构不是对象。原文件未改动，请先修复再运行。")
    state = _empty_state()
    state.update({k: v for k, v in data.items() if k in state})
    state["activities"] = dict(state.get("activities") or {})
    state["pending_events"] = [e for e in (state.get("pending_events") or [])
                               if isinstance(e, dict) and e.get("id")]
    return state


def save_state(state: dict, path=None) -> None:
    """原子写 0600：轮询进程随时可能被 kill，读方不能看到半截 JSON。"""
    path = Path(path or STATE_FILE)
    atomic_write_text(path, json.dumps(state, ensure_ascii=False, indent=2), mode=0o600)


def fingerprint(item: dict) -> str:
    raw = "|".join(str(item.get(k) or "") for k in FINGERPRINT_FIELDS)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _changed_fields(prev: dict, item: dict) -> list:
    return [k for k in TIME_FIELDS
            if str(prev.get(k) or "").strip() != str(item.get(k) or "").strip()]


def _event(aid: str, kind: str, status: str, row: dict, now: datetime,
           changed=(), prev: dict = None) -> dict:
    """构造一条只含公开字段的事件。

    new_open / opened 同时带「原定开放时间 sign_start」与「本次发现时间
    discovered_at」：用户最想知道的往往不是「现在能不能报」，而是「我错过了多久」。
    """
    event = {
        "id": aid,
        "kind": kind,
        "status": status,
        "name": row.get("name") or "",
        "type": row.get("type") or "",
        "place": row.get("place") or "",
        "sign_start": row.get("sign_start") or "",
        "sign_end": row.get("sign_end") or "",
        "act_start": row.get("act_start") or "",
        "act_end": row.get("act_end") or "",
        "checkin": row.get("checkin") or "",
        "discovered_at": row.get("discovered_at") or now.strftime(FMT),
        "first_status": row.get("first_status") or status,
        "created_at": now.strftime(FMT),
    }
    if changed:
        event["changed_fields"] = list(changed)
        event["previous"] = {k: (prev or {}).get(k) or "" for k in changed}
    return event


def compute_events(previous: dict, rows: list, now: datetime) -> tuple:
    """把上一轮快照与这一轮的公开活动对比，返回 ``(新快照, 事件列表)``。

    纯函数：不读文件、不发消息，时间由调用方传入，测试可以直接喂数据。

    规则：
    - 首次发现待开放 → new_pending；
    - 首次发现已开放 → new_open（带 discovered_at 与原定 sign_start）；
    - 待开放转开放 → opened；
    - 任何状态转 closed → 只更新状态，不发事件（半夜一条「报名已结束」只会打扰）；
    - 时间或签到字段变化 → changed（学校改期必须让人知道）；
    - 本轮没抓到 → 只累加 missing_rounds，连续 MISSING_ROUNDS_LIMIT 轮才移除。
    """
    activities = {k: dict(v) for k, v in (previous or {}).items()}
    events = []
    seen = set()

    for item in rows or []:
        aid = str(item.get("id") or "")
        if not aid:
            continue
        seen.add(aid)
        status = item.get("status") or STATUS_UNKNOWN
        prev = activities.get(aid)
        row = {
            "id": aid,
            "name": item.get("name") or "",
            "type": item.get("type") or "",
            "place": item.get("place") or "",
            "sign_start": item.get("sign_start") or "",
            "sign_end": item.get("sign_end") or "",
            "act_start": item.get("act_start") or "",
            "act_end": item.get("act_end") or "",
            "checkin": item.get("checkin") or "",
            "status": status,
            "fingerprint": fingerprint(item),
            "last_seen_at": now.strftime(FMT),
            "missing_rounds": 0,
        }

        if prev is None:
            row["discovered_at"] = now.strftime(FMT)
            row["first_status"] = status
            row["status_changed_at"] = now.strftime(FMT)
            row["last_event"] = ""
            if status == STATUS_PENDING:
                events.append(_event(aid, EVENT_NEW_PENDING, status, row, now))
                row["last_event"] = EVENT_NEW_PENDING
            elif status == STATUS_OPEN:
                events.append(_event(aid, EVENT_NEW_OPEN, status, row, now))
                row["last_event"] = EVENT_NEW_OPEN
            activities[aid] = row
            continue

        row["discovered_at"] = prev.get("discovered_at") or now.strftime(FMT)
        row["first_status"] = prev.get("first_status") or status
        row["status_changed_at"] = prev.get("status_changed_at") or row["discovered_at"]
        row["last_event"] = prev.get("last_event") or ""

        if status != prev.get("status"):
            row["status_changed_at"] = now.strftime(FMT)
            if status == STATUS_OPEN and prev.get("status") in (STATUS_PENDING,
                                                               STATUS_UNKNOWN):
                events.append(_event(aid, EVENT_OPENED, status, row, now, prev=prev))
                row["last_event"] = EVENT_OPENED
        elif row["fingerprint"] != prev.get("fingerprint"):
            changed = _changed_fields(prev, row)
            if changed:
                events.append(_event(aid, EVENT_CHANGED, status, row, now,
                                     changed=changed, prev=prev))
                row["last_event"] = EVENT_CHANGED
        activities[aid] = row

    # 这一轮没抓到的活动：先记账，不急着判结束。
    for aid, prev in list(activities.items()):
        if aid in seen:
            continue
        row = dict(prev)
        row["missing_rounds"] = int(prev.get("missing_rounds") or 0) + 1
        if row["missing_rounds"] >= MISSING_ROUNDS_LIMIT:
            activities.pop(aid, None)
        else:
            activities[aid] = row

    return activities, events


def merge_pending(existing: list, fresh: list) -> list:
    """同一活动只留一条事件，并把上一轮没发出去的事件并进来。

    发送失败时事件留在 pending_events 等下一轮重试，不能丢；同一条活动
    一轮里出现多种变化时取最需要被看到的那种，避免一条活动在群里连占好几行。
    """
    merged = {}
    for event in list(existing or []) + list(fresh or []):
        aid = str(event.get("id") or "")
        if not aid:
            continue
        kept = merged.get(aid)
        if kept is None:
            merged[aid] = dict(event)
            continue
        if _EVENT_RANK.get(event.get("kind"), 0) >= _EVENT_RANK.get(kept.get("kind"), 0):
            win, lose = dict(event), kept
        else:
            win, lose = kept, dict(event)
        # 最早的发现时刻是事实，不能被后来的事件覆盖
        if lose.get("discovered_at") and lose["discovered_at"] < (win.get("discovered_at")
                                                                 or "9999"):
            win["discovered_at"] = lose["discovered_at"]
        merged[aid] = win
    # 输出按「已开放在前、开放时间早的在前」排，群里第一眼看到的是最该动手的
    order = {STATUS_OPEN: 0, STATUS_PENDING: 1, STATUS_UNKNOWN: 2, STATUS_CLOSED: 3}
    return sorted(merged.values(),
                  key=lambda e: (order.get(e.get("status"), 9),
                                 str(e.get("sign_start") or ""), str(e.get("name") or "")))


def initialize_baseline(state: dict, rows: list, now: datetime) -> dict:
    """首次成功抓取只落快照，不产生任何事件。

    部署或升级的那一刻，活动库里已经有几十条历史活动。若照增量规则算，
    这一轮会把它们全部当成「首次发现」推到群里：一次上线刷屏几十条，
    而且这些活动多数早就开放、甚至已经结束——用户看到的第一条消息
    就是错的。

    所以首轮只建立基线：写 activities（discovered_at = 本轮时间）、
    initialized_at、last_success_at，pending_events 保持为空；
    从第二轮起才按增量规则产生事件。

    快照直接复用 compute_events 的空状态结果，而不是另写一份字段列表：
    两处字段一旦漂移，下一轮算指纹时会认为「什么都变了」，刚建好的基线立刻失效。
    """
    activities, _ = compute_events({}, rows, now)
    state["activities"] = activities
    state["pending_events"] = []
    state["initialized_at"] = now.strftime(FMT)
    state["baseline_initialized"] = True
    return state


def poll_interval(settings: dict, now: datetime) -> int:
    """按业务北京时间选下一轮间隔：白天 3 分钟、夜间 10 分钟。

    不按「有没有活动正在开放」加密。报名窗口中位数 24 小时（见 TIMING 第 4.3 节），
    开放前后那一两分钟多问几次换不到信息，却会把日请求量从 368 抬到 1440；
    真正需要「准点」的是 scan.py 的开放前 10 分钟提醒，那条链路本来就在跑。
    """
    if now.hour >= WATCH_NIGHT_START_HOUR or now.hour < WATCH_DAY_START_HOUR:
        return int(settings.get("night_seconds") or 600)
    return int(settings.get("day_seconds") or 180)


def should_run(state: dict, now: datetime, force: bool = False) -> bool:
    if force:
        return True
    raw = str(state.get("next_poll_at") or "").strip()
    if not raw:
        return True
    due = parse_dt(raw)
    if due is None:
        # 时间戳写坏了就当作到期：一条坏字段不该把监测永久停住
        return True
    return now >= due


def backoff_seconds(failures: int) -> int:
    if failures <= 0:
        return 0
    index = min(int(failures), len(WATCH_BACKOFF_SECONDS)) - 1
    return WATCH_BACKOFF_SECONDS[max(0, index)]


def fetch_public(token: str, timeout: int = WATCH_TIMEOUT_SECONDS) -> list:
    """带单轮超时的抓取。超时与异常一律抛出去，由调用方记失败。

    用线程池而不是信号：cron 场景下「超时」只需要不等了，不必真的杀进程；
    请求层同时设了同样的 socket 超时，线程不会长期滞留。
    """
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FutureTimeout

    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(fetch_all, token, timeout)
    try:
        return future.result(timeout=timeout + 5)
    except FutureTimeout as exc:
        raise TimeoutError("单轮抓取超过 %d 秒" % timeout) from exc
    finally:
        pool.shutdown(wait=False)


def _next_at(settings: dict, now: datetime) -> str:
    wait = poll_interval(settings, now)
    return (now + timedelta(seconds=wait)).strftime(FMT)


def _print_events(events: list) -> None:
    for event in events:
        line = "  %-11s %-8s %s" % (event.get("kind"), event.get("status"),
                                    event.get("name") or "-")
        if event.get("sign_start"):
            line += "｜原定开放 %s" % event["sign_start"]
        if event.get("discovered_at"):
            line += "｜发现 %s" % event["discovered_at"]
        if event.get("changed_fields"):
            line += "｜变化 %s" % "、".join(FIELD_LABELS.get(k, k)
                                            for k in event["changed_fields"])
        print(line)


def _record_failure(state: dict, settings: dict, now: datetime, message: str,
                    code: int, dry: bool = False) -> int:
    """失败只记账：不清空快照、不产生活动结束事件、不推算新增。"""
    failures = int(state.get("consecutive_failures") or 0) + 1
    wait = backoff_seconds(failures) or int(settings.get("day_seconds") or 180)
    state["consecutive_failures"] = failures
    state["last_error"] = str(message)[:200]
    state["last_run_at"] = now.strftime(FMT)
    state["next_poll_at"] = (now + timedelta(seconds=wait)).strftime(FMT)
    if not dry:
        try:
            save_state(state)
        except Exception as exc:
            print("[!] 失败状态没能落盘：%s" % exc)
    print("[x] 第 %d 次连续失败（%s），%d 秒后再试" % (failures, message, wait))
    return code


def _send(events: list, now: datetime):
    """交给 run.py 的公共通知入口：群卡片 + 纯文本兜底，不发私聊。"""
    import run          # 延迟导入：不发送时不必拉起 subprocess/notify
    return run.send_watch_events(events, now)


def _run_once(state: dict, settings: dict, token: str, now: datetime,
              dry: bool) -> int:
    started = time.time()
    try:
        items = fetch_public(token, WATCH_TIMEOUT_SECONDS)
    except PermissionError as exc:
        # 登录态告警由 token_probe / run.py 统一负责，这里只留一行线索
        return _record_failure(state, settings, now, "登录态失效：%s" % exc, 2, dry)
    except Exception as exc:
        return _record_failure(state, settings, now,
                               "%s: %s" % (type(exc).__name__, exc), 3, dry)

    rows = classify_public(items, now)

    # 首次成功抓取只建基线。判据同时看 activities 与 baseline_initialized：
    # 一旦建过基线，即使活动库暂时被清空（连续缺失后全部移除），
    # 也不该把之后的第一条新活动当成「首次运行」静默吞掉。
    bootstrap = not (state.get("activities") or state.get("baseline_initialized"))
    if bootstrap:
        initialize_baseline(state, rows, now)
    else:
        activities, fresh = compute_events(state.get("activities") or {}, rows, now)
        state["activities"] = activities
        state["pending_events"] = merge_pending(state.get("pending_events") or [], fresh)

    state["last_run_at"] = now.strftime(FMT)
    state["last_success_at"] = now.strftime(FMT)
    state["consecutive_failures"] = 0
    state["last_error"] = ""
    state["next_poll_at"] = _next_at(settings, now)

    if bootstrap:
        # 首轮静默：不调用 _send，也不留待推事件。
        # 没有「强制首轮播报」开关——部署脚本一旦误加参数就是一次群刷屏，
        # 需要主动播报时人工跑固定日报就够了。
        if dry:
            print("[i] --dry-run：baseline_initialized，%d 条活动只建基线，不写状态"
                  % len(rows))
            return 0
        save_state(state)
        print("[i] baseline_initialized：%d 条活动只建基线，本轮不发送通知；下次 %s（用时 %.1fs）"
              % (len(rows), state["next_poll_at"], time.time() - started))
        return 0

    pending = state.get("pending_events") or []
    if not pending:
        if not dry:
            save_state(state)
        print("[i] 活动库 %d 条，本轮无新增事件；下次 %s（用时 %.1fs）"
              % (len(rows), state["next_poll_at"], time.time() - started))
        return 0

    if dry:
        # dry-run 不写盘、不发送：反复跑同一批数据应当看到同一批事件
        _print_events(pending)
        print("[i] --dry-run：%d 条事件未发送，状态未写入" % len(pending))
        return 0

    # 先落盘再发送：发送成功才清空 pending_events。
    # 顺序反过来的话，发送途中被 kill 会直接丢事件，用户永远收不到。
    save_state(state)
    ok = _send(pending, now)
    if ok is not True:
        print("[x] 动态事件发送失败，%d 条留到下一轮重试" % len(pending))
        return 1

    state["pending_events"] = []
    save_state(state)
    print("[✓] 已推送 %d 条活动动态；下次 %s（用时 %.1fs）"
          % (len(pending), state["next_poll_at"], time.time() - started))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="公共活动动态发现（只读）")
    ap.add_argument("--dry-run", "--dry", dest="dry", action="store_true",
                    help="只打印事件，不发送、不写状态")
    ap.add_argument("--force", action="store_true", help="忽略排期，立刻跑一轮")
    ap.add_argument("--now", default="", help="按指定北京时间跑（排查与复盘用）")
    args = ap.parse_args(argv)

    now = parse_dt(args.now) or business_now()
    settings = load_watch_config()
    if not settings.get("enabled") and not args.dry:
        print("[i] config.watch.enabled=false，动态监测已关闭；固定日报与临近提醒不受影响")
        return 0

    session = load_session()
    token = (session.get("token") or "").strip()
    if not token:
        print("[i] 没有可用登录态（data/session.json 里没有 token），本轮动态监测跳过")
        return 2

    with try_file_lock(LOCK_FILE) as acquired:
        if not acquired:
            print("[i] 上一轮动态监测还没结束，本轮让路（单飞锁）")
            return 0

        try:
            state = load_state()
        except RuntimeError as exc:
            print("[x] %s" % exc)
            return 4

        if not args.dry and not should_run(state, now, args.force):
            print("[i] 下一轮排期在 %s，本轮跳过" % (state.get("next_poll_at") or "—"))
            return 0

        return _run_once(state, settings, token, now, args.dry)


if __name__ == "__main__":
    sys.exit(main())
