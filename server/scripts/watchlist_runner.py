"""每分钟跑一次：把到点的预报名交给 grab_signup.py。

为什么不用一次性 systemd timer（以前是那样）：每场活动都要人工 systemd-run，
人一忘就漏，而且「谁排了这一场」散落在 systemd 的状态里。现在清单是唯一入口，
runner 每分钟对齐一次，漏跑顶多晚一分钟。

为什么用 systemd-run 而不是直接 Popen：抓取进程要脱离 runner 的生命周期。
runner 是 cron 拉起的、一分钟就退出，如果它直接起子进程，cron 收尾或 bot 重启
都可能把正在等的抓取连带杀掉；交给 systemd 之后，谁重启都不影响那一场。

用法：
    python3 scripts/watchlist_runner.py            # 正常跑（cron 每分钟）
    python3 scripts/watchlist_runner.py --dry-run  # 只说会做什么，不拉起
"""
from __future__ import annotations

import argparse
import fcntl
import os
import shlex
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import watchlist as wl  # noqa: E402

ROOT = wl.ROOT
# 抓取脚本在登录目录（要能用那边的 playwright 环境），可用环境变量覆盖
GRAB = os.environ.get("GRAB_SCRIPT") or "/root/tjise-login/grab_signup.py"
STATUS_DIR = Path(os.environ.get("GRAB_STATUS_DIR") or (ROOT / "data"))
EARLY = 20          # 允许比「开放前 lead 秒」晚多少秒拉起（留给 cron 抖动）
LATE = 300          # 开放后还允许拉起多久：迟一点也可能挤进名额
LOCK = ROOT / "data" / "watchlist.lock"
PYTHON = sys.executable or "/usr/bin/python3"


def unit_name(entry: dict) -> str:
    return "tjise-grab-" + str(entry["id"])[:8]


def status_file(entry: dict) -> Path:
    return STATUS_DIR / ("grab-%s.status" % str(entry["id"])[:8])


def unit_active(unit: str) -> bool:
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", unit],
                              timeout=10).returncode == 0
    except Exception:
        return False


def launch(entry: dict, dry: bool) -> bool:
    """把这一场交给 systemd；退出码写进状态文件，供下一轮判定成败。"""
    unit = unit_name(entry)
    code_path = status_file(entry)
    log_path = STATUS_DIR / "grab_signup.log"
    try:
        code_path.unlink()
    except FileNotFoundError:
        pass

    inner = ("%s %s --activity-id %s --at %s --name %s --lead %s --window %s "
             ">> %s 2>&1; echo $? > %s"
             % (shlex.quote(PYTHON), shlex.quote(GRAB), shlex.quote(str(entry["id"])),
                shlex.quote(str(entry.get("open_at") or "")),
                shlex.quote(str(entry.get("name") or "活动")),
                shlex.quote(str(entry.get("lead") or wl.DEFAULT_LEAD)),
                shlex.quote(str(entry.get("window") or wl.DEFAULT_WINDOW)),
                shlex.quote(str(log_path)), shlex.quote(str(code_path))))

    if dry:
        print("[dry] 会拉起 %s：%s" % (unit, entry.get("name") or entry["id"]))
        return True

    try:
        proc = subprocess.run(
            ["systemd-run", "--unit=" + unit, "--collect",
             "--description=预报名 %s" % (entry.get("name") or entry["id"]),
             "/bin/bash", "-lc", inner],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, timeout=30)
    except Exception as exc:
        print("[x] %s 拉起异常：%s" % (unit, exc))
        return False

    if proc.returncode != 0:
        print("[x] %s systemd-run 失败：%s"
              % (unit, (proc.stdout or "").strip()[:200]))
        return False
    print("[✓] 已拉起 %s（%s，开放 %s）"
          % (unit, entry.get("name") or entry["id"], entry.get("open_at")))
    return True


def read_code(entry: dict) -> str:
    try:
        return status_file(entry).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except Exception:
        return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="预报名 runner")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(str(LOCK), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("[i] 上一轮还没结束，跳过")
            return 0

        now = datetime.now()
        entries = wl.load()
        changed = False          # 有状态变化才刷新卡片，别每分钟 PATCH 一次
        for e in entries:
            status = e.get("status")
            if status == "queued":
                try:
                    open_at = wl.parse_at(e.get("open_at"))
                except ValueError:
                    wl.mark(e["id"], status="failed", result="开放时间无法解析",
                            finished_at=wl.now_str())
                    changed = True
                    print("[x] %s 开放时间无法解析，已标记失败" % (e.get("name") or e["id"]))
                    continue
                lead = float(e.get("lead") or wl.DEFAULT_LEAD)
                window = float(e.get("window") or wl.DEFAULT_WINDOW)
                start = open_at - timedelta(seconds=lead)

                if now < start - timedelta(seconds=EARLY):
                    continue
                if now > open_at + timedelta(seconds=window + LATE):
                    wl.mark(e["id"], status="missed", result="runner 没来得及拉起",
                            finished_at=wl.now_str())
                    changed = True
                    print("[!] %s 已过报名窗口，标记错过" % (e.get("name") or e["id"]))
                    continue

                if args.dry_run:
                    launch(e, True)
                    continue

                wl.mark(e["id"], status="running", started_at=wl.now_str(),
                        attempts=int(e.get("attempts") or 0) + 1)
                if not launch(e, False):
                    wl.mark(e["id"], status="failed", result="拉起失败",
                            finished_at=wl.now_str())
                    changed = True

            elif status == "running":
                if unit_active(unit_name(e)):
                    continue
                code = read_code(e)
                changed = True
                ok = code == "0"
                wl.mark(e["id"],
                        status="done" if ok else "failed",
                        result=("exit=%s" % code) if code else "没有留下退出码",
                        finished_at=wl.now_str())
                print("[%s] %s %s"
                      % ("✓" if ok else "x", e.get("name") or e["id"],
                         "报名成功" if ok else "未成功（详见私聊/抓取日志）"))

        if changed and not args.dry_run:
            wl._refresh_card()
    return 0


if __name__ == "__main__":
    sys.exit(main())
