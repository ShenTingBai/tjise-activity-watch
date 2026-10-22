"""预报名清单：先记下「想报哪个活动」，到点由 watchlist_runner.py 自动抢。

和 state.json 分开放：state.json 记的是「抓取/推送进度」，属于机器状态；
watchlist.json 记的是「人的意愿」。两者的生命周期和回滚方式都不一样，
混在一起会让「清空去重状态」和「取消某个预报名」互相牵连。

命令行（机器人按钮回调也走这里）：
    python3 scripts/watchlist.py add --id <活动id> --name <名称> --at "2026-10-23 19:00:00"
    python3 scripts/watchlist.py remove --id <活动id>
    python3 scripts/watchlist.py list

状态流转：queued（排队）→ running（已拉起抓取）→ done / failed / missed。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 测试时可以指到临时文件，避免污染真实清单
FILE = Path(os.environ.get("WATCHLIST_FILE") or (ROOT / "data" / "watchlist.json"))
FMT = "%Y-%m-%d %H:%M:%S"
DEFAULT_LEAD = 100.0      # 提前多少秒打开详情页
DEFAULT_WINDOW = 120.0    # 开放后最多尝试多少秒
ACTIVE = ("queued", "running")


def now_str() -> str:
    return datetime.now().strftime(FMT)


def parse_at(text: str) -> datetime:
    """只接受绝对时间：相对说法（「明天」「今晚」）在定时任务里没有可靠语义。"""
    raw = str(text or "").strip()
    for fmt in (FMT, "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError("时间格式不对：%r（要 YYYY-MM-DD HH:MM:SS）" % raw)


def load() -> list:
    """读清单。文件不存在算空清单；解析失败抛错，绝不静默当成空。"""
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception as exc:
        # 读坏了不静默清空：那是真人点出来的「预报名」，丢了就再也不会报名
        raise RuntimeError("watchlist.json 读取失败：%s" % exc)
    entries = data.get("entries") if isinstance(data, dict) else data
    return [e for e in (entries or []) if isinstance(e, dict) and e.get("id")]


def save(entries: list) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_name(FILE.name + ".tmp")
    tmp.write_text(json.dumps({"entries": entries}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.chmod(str(tmp), 0o600)
    os.replace(str(tmp), str(FILE))


def find(entries: list, activity_id: str):
    for e in entries:
        if e.get("id") == activity_id:
            return e
    return None


def _refresh_card() -> None:
    """非点击场景的卡片刷新（runner 抢完一场后调用）。

    点击按钮引起的翻转不走这里：飞书要求按钮回调直接回传新卡片，
    客户端当场替换。如果点击时再用 PATCH 改一次，两次更新会互相覆盖，
    表现就是「刚变成已预报名，一眨眼又变回预报名」。
    """
    try:
        import grab_card
        grab_card.refresh()
    except Exception as exc:
        print("[!] 卡片刷新失败：%s" % exc)


def add(activity_id: str, name: str, open_at: str,
        lead: float = DEFAULT_LEAD, window: float = DEFAULT_WINDOW) -> dict:
    """同一活动重复点「预报名」按覆盖处理，不叠出两条。"""
    start = parse_at(open_at)          # 先校验，避免把写坏的时间存进去
    # 老卡片留到第二天再点的情况：窗口已经过去了，排进去只会在下一分钟被标「错过」，
    # 还会给用户发一条莫名其妙的失败提醒。直接拒绝，并说清原因。
    if datetime.now() > start + timedelta(seconds=float(window)):
        raise RuntimeError(
            "这场报名窗口已经结束（%s 开放），预报名不适用" % open_at)
    entries = load()
    entry = find(entries, activity_id)
    if entry is None:
        entry = {"id": activity_id, "attempts": 0, "created_at": now_str()}
        entries.append(entry)
    entry.update({
        "name": name or entry.get("name") or "活动",
        "open_at": open_at,
        "lead": float(lead),
        "window": float(window),
        "status": "queued",
        "result": "",
        "started_at": "",
        "finished_at": "",
    })
    save(entries)
    return entry


def remove(activity_id: str) -> bool:
    entries = load()
    kept = [e for e in entries if e.get("id") != activity_id]
    if len(kept) == len(entries):
        return False
    save(kept)
    return True


def mark(activity_id: str, **fields) -> dict:
    entries = load()
    entry = find(entries, activity_id)
    if entry is None:
        return {}
    entry.update(fields)
    save(entries)
    return entry


def _fmt_line(e: dict) -> str:
    status = {"queued": "排队", "running": "抓取中", "done": "已报名",
              "failed": "失败", "missed": "错过"}.get(e.get("status"), e.get("status") or "?")
    line = "%s  %s  %s" % (status, e.get("open_at", "?"), e.get("name") or "活动")
    if e.get("result"):
        line += "  (%s)" % e["result"]
    return line


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="预报名清单")
    sub = ap.add_subparsers(dest="cmd")

    p_add = sub.add_parser("add", help="加入预报名")
    p_add.add_argument("--id", required=True)
    p_add.add_argument("--name", default="活动")
    p_add.add_argument("--at", required=True, help="报名开放时间 YYYY-MM-DD HH:MM:SS")
    p_add.add_argument("--lead", type=float, default=DEFAULT_LEAD)
    p_add.add_argument("--window", type=float, default=DEFAULT_WINDOW)

    for name in ("remove", "cancel"):
        p_rm = sub.add_parser(name, help="取消预报名")
        p_rm.add_argument("--id", required=True)

    sub.add_parser("list", help="看清单")

    args = ap.parse_args(argv)
    if not args.cmd:
        args.cmd = "list"

    if args.cmd == "add":
        try:
            entry = add(args.id, args.name, args.at, args.lead, args.window)
        except (ValueError, RuntimeError) as exc:
            print("[x] %s" % exc)
            return 1
        print("已加入预报名：%s  %s（提前 %d 秒开页面）"
              % (entry["name"], entry["open_at"], int(entry["lead"])))
        return 0

    if args.cmd in ("remove", "cancel"):
        if remove(args.id):
            print("已取消预报名：%s" % args.id)
            return 0
        print("清单里没有这条：%s" % args.id)
        return 1

    entries = load()
    if not entries:
        print("清单是空的。")
        return 0
    order = {"queued": 0, "running": 1, "done": 2, "failed": 3, "missed": 4}
    entries.sort(key=lambda e: (order.get(e.get("status"), 9), e.get("open_at") or ""))
    for e in entries:
        print(_fmt_line(e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
