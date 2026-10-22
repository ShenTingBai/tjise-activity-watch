# -*- coding: utf-8 -*-
"""去重基线的行为测试。

核心约定：**「已推送」必须在推送成功后才写入**。
这个测试验证两个方向，缺一个都不算通过：

  不重复推送 —— 推送成功后，同一批活动下次不再标 🆕
  不漏推     —— 推送失败（没写标记）时，下次仍然标 🆕

只跑逻辑，不真的往群里发消息。会临时改写 state.json，结束时一定还原。
用法：python tests/test_dedup.py
"""
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

STATE = ROOT / "data" / "state.json"
BACKUP = ROOT / "data" / ".state.json.deduptest"


def fetch_pending():
    """跑一次真实抓取，返回待开放列表（含 is_new 标记）。"""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "fetch.py")],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        universal_newlines=True, encoding="utf-8", cwd=str(ROOT))
    out = proc.stdout or ""
    if proc.returncode != 0 or "SUMMARY_JSON_BEGIN" not in out:
        raise RuntimeError("抓取失败：\n" + out[-800:])
    blob = out.split("SUMMARY_JSON_BEGIN", 1)[1].split("SUMMARY_JSON_END", 1)[0]
    return json.loads(blob.strip()).get("pending", [])


def read_state():
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}


def write_state(st):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    if not STATE.exists():
        print("[x] 没有 state.json，先跑一次 run.py 再说。")
        return 1

    shutil.copy2(STATE, BACKUP)
    failed = 0

    def check(label, got, want):
        nonlocal failed
        ok = got == want
        print(("  ✓ " if ok else "  ✗ ") + label
              + ("" if ok else f"（得到 {got}，期望 {want}）"))
        if not ok:
            failed += 1

    try:
        print("== 去重基线 ==")

        # 场景 0：基线清空，第一次见到这批活动
        st = read_state()
        st.pop("announced", None)
        st.pop("seen", None)
        write_state(st)

        items = fetch_pending()
        print(f"  待开放 {len(items)} 项")
        if not items:
            print("  [!] 当前没有待开放活动，跳过断言（逻辑仍可回归）")
            return 0

        check("基线为空时，全部标记为新增",
              sum(1 for i in items if i["is_new"]), len(items))

        # 关键：抓取本身绝不能写入「已推送」
        after_fetch = read_state()
        check("抓取后未写入 announced（推送失败也不会漏）",
              len(after_fetch.get("announced") or {}), 0)

        # 场景 1：推送成功 → 下次不再标新（不重复推送）
        import run as run_mod
        run_mod._mark_announced(items)

        items2 = fetch_pending()
        check("推送成功后，同一批不再标新增",
              sum(1 for i in items2 if i["is_new"]), 0)

        # 场景 2：新增一条从没推过的 → 必须还能被认出来（不漏推）
        st = read_state()
        victim = items2[0]
        st["announced"].pop(str(victim.get("id") or ""), None)
        st["announced"].pop(victim["name"], None)
        write_state(st)

        items3 = fetch_pending()
        same = [i for i in items3 if i["name"] == victim["name"]]
        check("被移出已推送集合的活动，重新判定为新增",
              bool(same) and same[0]["is_new"], True)

        # 场景 3：幂等 —— 重复写入不产生副作用
        run_mod._mark_announced(items3)
        before = len(read_state().get("announced") or {})
        run_mod._mark_announced(items3)
        run_mod._mark_announced(items3)
        after = len(read_state().get("announced") or {})
        check("重复写入幂等（条数不膨胀）", after, before)

        # 场景 4：过期清理有界（120 天 TTL）
        st = read_state()
        st["announced"]["__已过期的老活动__"] = "2000-01-01"
        write_state(st)
        run_mod._mark_announced(items3)
        check("超过 120 天的记录被清理",
              "__已过期的老活动__" in (read_state().get("announced") or {}), False)

        print("\n" + ("== 通过 ==" if failed == 0 else f"== 失败 {failed} 项 =="))
        return 0 if failed == 0 else 1

    finally:
        shutil.copy2(BACKUP, STATE)
        if BACKUP.exists():
            BACKUP.unlink()          # 服务器 Python 可能 <3.8，不用 missing_ok
        print(f"[i] 已还原 state.json（{datetime.now():%H:%M:%S}）")


if __name__ == "__main__":
    sys.exit(main())
