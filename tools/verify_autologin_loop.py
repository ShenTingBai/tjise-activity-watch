#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""受控验证「探针判失效 → timer 自动重登」整条闭环。

为什么需要它：2026-10-22 之前的两次恢复都是人工 `--force` 跑的，只能证明代码路径通，
证不了「没人看着的时候它会自己醒过来」。这个脚本把落盘时序真实跑一遍并留时间线。

用法（服务器 root，脚本放在 /root/tjise-login/ 与 auto_login.py 作伴）：

    python3 /root/tjise-login/verify_autologin_loop.py            # 完整验证，最多等 12 分钟
    python3 /root/tjise-login/verify_autologin_loop.py --dry-run  # 只打印计划，不改任何文件

代价与回滚（务必先读）：

- 会把 `data/session.json` 的 token 临时换成无效值，这几分钟里机器人推不出去；
- 原文件先备份到 `/root/tjise-login/backup/`，超时未自愈会自动还原；
- 会消耗一次真实的学校登录（一次密码尝试 + 一次验证码）；
- 连续 2 次探针失败会触发一次 owner 私聊告警，登录恢复后下一轮探针自动清零；
- 时间线写 `/root/tjise-login/backup/loop-<yyyymmdd-HHMMSS>.log`。

判据：auto_login.log 先出现「开始自动登录」，随后出现「[OK] 登录成功」→ 闭环通过。

@author LiXinwen
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

APP_DIR = pathlib.Path("/root/tjise-activity-watch")
LOGIN_DIR = pathlib.Path("/root/tjise-login")
SESSION = APP_DIR / "data" / "session.json"
PROBE = APP_DIR / "scripts" / "token_probe.py"
PROBE_LOG = APP_DIR / "data" / "token_probe.log"
AUTOLOGIN_LOG = LOGIN_DIR / "auto_login.log"     # 找不到时下面会回落到 app 的 data/
BACKUP_DIR = LOGIN_DIR / "backup"
POLL_SEC = 10


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _fp(token: str) -> str:
    """只留指纹，日志里绝不出现 token 原文。"""
    return hashlib.sha1(token.encode()).hexdigest()[:8]


def _log(line: str, fh=None) -> None:
    text = "%s %s" % (_now(), line)
    print(text, flush=True)
    if fh is not None:
        fh.write(text + "\n")
        fh.flush()


def _autologin_log() -> pathlib.Path:
    if AUTOLOGIN_LOG.exists():
        return AUTOLOGIN_LOG
    alt = APP_DIR / "data" / "auto_login.log"
    return alt if alt.exists() else AUTOLOGIN_LOG


def _owner_env() -> dict:
    """从 crontab 里捞出 owner open_id，让探针的告警路径与 cron 一致。"""
    env = dict(os.environ)
    try:
        out = subprocess.run(["crontab", "-l"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace").stdout
        m = re.search(r"PRIVATE_OWNER_OPEN_ID=(\S+)", out)
        if m:
            env.setdefault("PRIVATE_OWNER_OPEN_ID", m.group(1))
    except Exception:
        pass
    return env


def _run_probe(env: dict) -> None:
    """按 cron 的写法跑：stdout/stderr 追加进 token_probe.log（探针自己不写文件）。"""
    with open(PROBE_LOG, "a", encoding="utf-8") as fh:
        subprocess.run([sys.executable, str(PROBE)], cwd=str(APP_DIR),
                       stdout=fh, stderr=fh, env=env, check=False)


def _last_probe_line() -> str:
    try:
        with open(PROBE_LOG, "r", encoding="utf-8") as f:
            lines = [ln.rstrip() for ln in f if ln.strip()]
        return lines[-1] if lines else "(空)"
    except Exception as exc:
        return "(读取失败 %s)" % type(exc).__name__


def main() -> int:
    ap = argparse.ArgumentParser(description="受控验证自动重登闭环")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不改文件")
    ap.add_argument("--timeout", type=int, default=720, help="等 timer 自愈的秒数，默认 720")
    args = ap.parse_args()

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    report = BACKUP_DIR / ("loop-%s.log" % stamp)

    with open(report, "a", encoding="utf-8") as fh:
        _log("=== 自动重登闭环验证开始 ===", fh)
        session = json.loads(SESSION.read_text(encoding="utf-8"))
        old_fp = _fp(str(session.get("token", "")))
        _log("当前 token 指纹=%s，探针末行=%s" % (old_fp, _last_probe_line()), fh)

        if not PROBE.exists():
            _log("[x] 找不到 %s" % PROBE, fh)
            return 1

        if args.dry_run:
            _log("dry-run：将依次做「备份 session.json → 写入无效 token → 按 cron 写法跑探针 "
                 "→ 最多等 %d 秒 timer 自愈 → 复探一次确认 ok」" % args.timeout, fh)
            _log("报告会写到 %s" % report, fh)
            return 0

        backup = BACKUP_DIR / ("session.json.pre-loop-%s" % stamp)
        shutil.copy2(SESSION, backup)
        backup.chmod(0o600)
        _log("已备份 session.json → %s" % backup, fh)

        session["token"] = "0" * 32
        SESSION.write_text(json.dumps(session, ensure_ascii=False), encoding="utf-8")
        SESSION.chmod(0o600)
        _log("已写入无效 token（指纹 %s），开始等 timer 自己醒" % _fp(session["token"]), fh)

        env = _owner_env()
        log_path = _autologin_log()
        offset = log_path.stat().st_size if log_path.exists() else 0
        _log("从 %s 的第 %d 字节开始跟踪" % (log_path, offset), fh)

        _run_probe(env)
        _log("探针已跑，末行=%s" % _last_probe_line(), fh)

        saw_start = False
        saw_ok = False
        new_fp = ""
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            time.sleep(POLL_SEC)
            if not log_path.exists():
                continue
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(offset)
                chunk = f.read()
            for line in chunk.splitlines():
                if "开始自动登录" in line and not saw_start:
                    saw_start = True
                    _log("观察到：%s" % line.strip(), fh)
                m = re.search(r"\[OK\] 登录成功.*fp=([0-9a-f]{8})", line)
                if m:
                    saw_ok = True
                    new_fp = m.group(1)
                    _log("观察到：%s" % line.strip(), fh)
            if saw_ok:
                break
        # 只看新增内容：offset 之后的行才算本轮结果，不拿历史记录冒充成功
        if not saw_ok:
            _log("[x] 超时 %d 秒仍未自愈，回滚 session.json" % args.timeout, fh)
            shutil.copy2(backup, SESSION)
            SESSION.chmod(0o600)
            _log("已还原备份，结束（退出码 1）", fh)
            return 1

        # 复探一次：确认新 token 真被接口接受（而不是只看日志写了 [OK]）
        _run_probe(env)
        line = _last_probe_line()
        _log("复探结果=%s" % line, fh)
        ok = "result=ok" in line
        now_fp = _fp(str(json.loads(SESSION.read_text(encoding="utf-8")).get("token", "")))
        _log("session 当前指纹=%s（原 %s，登录后 %s）" % (now_fp, old_fp, new_fp), fh)
        verdict = ("闭环通过：timer 自己判失效并完成了登录"
                   if (saw_start and ok and now_fp == new_fp)
                   else "闭环存疑：日志有 [OK] 但复探未 ok 或指纹不符")
        _log(verdict, fh)
        _log("=== 结束，报告 %s ===" % report, fh)
        return 0 if verdict.startswith("闭环通过") else 1


if __name__ == "__main__":
    raise SystemExit(main())
