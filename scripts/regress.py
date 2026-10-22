"""回归测试：把每条命令都跑一遍，检查退出码与关键输出。

    python3 scripts/regress.py
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
CASES = []


def run(args, expect_rc=0, must_contain=(), must_not_contain=()):
    p = subprocess.run([PY] + args, cwd=str(ROOT), stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, universal_newlines=True,
                       encoding="utf-8", timeout=180)
    out = p.stdout or ""
    problems = []
    if p.returncode != expect_rc:
        problems.append("退出码 %d（期望 %d）" % (p.returncode, expect_rc))
    for s in must_contain:
        if s not in out:
            problems.append("缺少 %r" % s)
    for s in must_not_contain:
        if s in out:
            problems.append("不该出现 %r" % s)
    CASES.append((args, problems, out))
    return out, problems


def main():
    fails = 0

    # 1. 清单（默认，无变化时应精简）
    run(["scripts/run.py", "--dry"], must_contain=["待开放"])

    # 2. 21:30 口径
    run(["scripts/run.py", "--dry", "--late-checkin", "--with-open"],
        must_contain=["待开放", "晚归签到"])

    # 3. 冲刺扫描（当前无临近活动，应静默）
    out, _ = run(["scripts/scan.py"])
    if out.strip():
        CASES[-1][1].append("本应静默却输出了内容")

    # 4. 私人提醒（今天无已报名活动，应静默）
    out, _ = run(["scripts/remind.py", "--dry"])
    if "Traceback" in out:
        CASES[-1][1].append("出现异常")

    # 5. 文本视图
    for view in ["pending", "open", "all", "types", "mine"]:
        run(["scripts/query.py", view])

    # 6. 卡片视图（必须是合法 JSON，且带标题）
    for view in ["pending", "open", "all", "menu", "mine"]:
        out, problems = run(["scripts/query.py", view, "--format", "card"])
        if not problems:
            try:
                card = json.loads(out)
                if not card.get("header", {}).get("title"):
                    problems.append("卡片缺标题")
            except Exception as exc:
                problems.append("卡片 JSON 解析失败: %s" % exc)

    # 7. 防重复：把上次推送时间设成现在，非 --force 应被拦下（不依赖真实历史）
    sys.path.insert(0, str(ROOT / "scripts"))
    import time as _time
    from common import load_state, save_state

    st = load_state()
    prev_ts = st.get("last_push_ts")
    st["last_push_ts"] = _time.time()
    save_state(st)
    try:
        out, problems = run(["scripts/run.py"], must_contain=["跳过"])
        if "已写入" in out:
            problems.append("防重复未生效，实际推送了")
    finally:
        st = load_state()
        st["last_push_ts"] = prev_ts or 0
        save_state(st)

    # 8. 状态自检（含 token 缓存、卡片大小、并发、版式一致性）
    run(["scripts/selfcheck.py"], must_contain=["自检通过"])

    # 9. 去重基线（不漏推 / 不重复推送 / 幂等 / TTL）
    run(["tests/test_dedup.py"], must_contain=["通过"])

    # 10. 推送成败判定（HTTP 2xx 但业务码失败必须算失败）
    run(["tests/test_notify.py"], must_contain=["通过"])

    # 汇总
    print("=" * 60)
    for args, problems, _ in CASES:
        name = " ".join(args[:3])
        if problems:
            fails += 1
            print("✗ %s" % name)
            for p in problems:
                print("    - " + p)
        else:
            print("✓ %s" % name)
    print("=" * 60)
    print("共 %d 项，失败 %d 项" % (len(CASES), fails))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
