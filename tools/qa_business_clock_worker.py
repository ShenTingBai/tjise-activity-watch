#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同源候选的内置 worker 离线预算探针（决策 012 返修轮，C01/C02 生产路径收口）。

目的：证明**打包产物里**的 grab-worker（与候选 EXE 同一份源码冻结）能带着真实
ClockClient 进入生产 RetryBudget，并在「重试窗口在启动前已结束」时收成唯一结构化
终态——零鉴权、零列表、零浏览器、零学校请求。这是单测（网络边界一次调用即失败）
之外的实包证据：实包本身不注入任何产品成功桩，也不加测试专用跳过开关。

用法（只在获得运行授权后执行）：

    python -X utf8 tools/qa_business_clock_worker.py \
        --exe dist/release-v1.1.1/tjise-activity-watch-v1.1.1-win-x64.exe \
        --evidence-root D:/@Temp/tjise-beijing-clock-contract-repair-20261026/worker-probe

流程与边界：
1) 计算 exe SHA-256；若发现同目录/上级 manifest.json 则与其中 EXE 指纹核对；
2) 用已安装 PyInstaller 的 CArchiveReader **只读**打开候选 onefile 归档，
   仅把二进制成员 grab-worker.exe 提取到本轮独占目录（绝不读用户数据、不执行主 EXE）；
3) 制作无凭证可信测试快照：``tick_s`` 取本机真实 GetTickCount64（与 worker 同一
   tick 域），``epoch_s`` 固定为 QA 基准并在 source 里标注「非真实授时」；
4) env 显式设 TJISE_DATA_DIR / TJISE_CLOCK_SNAPSHOT / TJISE_CLOCK_OWNER 指向本轮
   独占目录，cwd 也设在该目录（无旧 data、无 config）；
5) 启动 worker 的真实 grab 入口：``--retry-start`` 已过 + 有限窗口 + 明确 activity-id/at，
   断言收成恰一条 ``[retry] kind=done ... stop_reason=retry_duration`` 终态、
   退出码为该终态的正常值 2、无 AttributeError/Traceback；
   「零网络」由生产单测（网络边界一调即败）+ 秒级退出共同证明，实包不再注入桩；
6) 受控超时按进程树收走本轮拥有的子进程；路径/哈希/输出/exit_code 落盘，
   绝不覆盖历史证据根。

@author LiXinwen
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER_MEMBER = "grab-worker.exe"
# 固定 QA 基准 epoch：2026-10-26 20:00:00 +08:00。只作业务锚点，冒充不了真实授时——
# source 字段显式标注，且探针不访问任何授时服务。
QA_EPOCH_S = 1793016000.0
RETRY_START_OFFSET_S = 600.0   # retry_start = QA 基准 - 600 秒（启动前已过）
RETRY_FOR_S = 60.0             # 有限窗口：retry_start + 60 仍早于业务当前时刻
TIMEOUT_S = 90.0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc8_text(epoch: float) -> str:
    """业务锚点 → 学校口径文本（UTC+8），与 time_contract.school_time_text 同式。"""
    import datetime as _dt
    moment = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc) \
        + _dt.timedelta(seconds=epoch + 8 * 3600)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _elapsed_tick_s() -> float:
    """本机真实 GetTickCount64（秒）——与生产 _elapsed_tick 同源，含睡眠、单调。"""
    return float(ctypes.windll.kernel32.GetTickCount64()) / 1000.0


def _check_manifest(exe: Path, exe_sha: str, notes: list) -> None:
    """发现 manifest 就核对 EXE 指纹；没有 manifest 时只落哈希，不虚构来源。"""
    for candidate in (exe.parent / "manifest.json", exe.parent.parent / "manifest.json"):
        if candidate.is_file():
            try:
                manifest = json.loads(candidate.read_text(encoding="utf-8"))
            except ValueError as exc:
                notes.append("manifest %s 解析失败：%s" % (candidate, exc))
                return
            files = manifest.get("files") or manifest.get("assets") or {}
            entry = files.get(exe.name) or {}
            if not isinstance(entry, dict) or not entry:
                notes.append("manifest %s 没有 %s 条目" % (candidate.name, exe.name))
                return
            recorded = str(entry.get("sha256") or "")
            if recorded and recorded.lower() != exe_sha:
                raise SystemExit("[X] exe 哈希与 manifest 不符：%s != %s"
                                 % (exe_sha, recorded))
            notes.append("manifest %s 核对一致（source_dirty=%s，head=%s）"
                         % (candidate.name, manifest.get("source_dirty"),
                            manifest.get("head") or manifest.get("commit")))
            return
    notes.append("未发现 manifest.json，仅记录 exe 实测哈希")


def _extract_worker(exe: Path, dest_dir: Path, notes: list) -> Path:
    """从候选 onefile 归档里只读提取 grab-worker.exe（不执行主 EXE）。"""
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except ImportError as exc:
        raise SystemExit("[X] 需要 PyInstaller（随打包环境提供）：%s" % exc)
    reader = CArchiveReader(str(exe))
    toc = getattr(reader, "toc", None) or {}
    names = set(toc.keys())
    if WORKER_MEMBER not in names:
        raise SystemExit("[X] 候选归档里没有内置 %s（成员数 %d）" % (WORKER_MEMBER, len(names)))
    data = reader.extract(WORKER_MEMBER)
    if not data:
        raise SystemExit("[X] 提取 %s 得到空内容" % WORKER_MEMBER)
    dest = dest_dir / WORKER_MEMBER
    dest.write_bytes(data)
    worker_sha = _sha256(dest)
    notes.append("提取 %s → %s（sha256=%s，%d 字节）"
                 % (WORKER_MEMBER, dest, worker_sha, len(data)))
    return dest


def _kill_tree(pid: int) -> None:
    """只收本轮自己启动的进程树；失败不掩盖主结论。"""
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, timeout=15)
    except Exception:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def run_probe(exe: Path, evidence: Path) -> dict:
    report = {"ok": False, "exe": str(exe.resolve()), "started_at":
              time.strftime("%Y-%m-%dT%H:%M:%S")}
    notes = report["notes"] = []
    if not exe.is_file():
        raise SystemExit("[X] 候选不存在：%s" % exe)
    exe_sha = _sha256(exe)
    report["exe_sha256"] = exe_sha
    _check_manifest(exe, exe_sha, notes)

    # 本轮独占目录：提取物、快照、数据根、cwd 都在这里，互不混用旧数据
    work = Path(tempfile.mkdtemp(prefix="tjise-worker-probe-", dir=str(evidence)))
    report["work_dir"] = str(work)
    worker = _extract_worker(exe, work, notes)

    tick = _elapsed_tick_s()
    snapshot = {"schema": 1, "owner_id": "qa-clock-probe", "generation": 1,
                "state": "ready", "epoch_s": QA_EPOCH_S, "tick_s": tick,
                "sampled_tick_s": tick, "uncertainty_s": 0.1,
                "source": "QA 固定基准（非真实授时，仅离线探针）",
                "age_s": 0.0, "reason": "北京时间已同步（QA 探针快照）"}
    snap_path = work / "qa-clock-snapshot.json"
    snap_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    report["snapshot"] = {"path": str(snap_path), "epoch_s": QA_EPOCH_S,
                          "tick_s": tick, "sha256": _sha256(snap_path)}

    retry_start = QA_EPOCH_S - RETRY_START_OFFSET_S
    args = [
        str(worker), "grab",
        "--activity-id", "qa-act-1",
        "--name", "QA内置预算探针",
        "--at", _utc8_text(retry_start),
        "--retry-start", repr(retry_start),
        "--retry-for", repr(RETRY_FOR_S),
        "--max-attempts", "1",
        "--lead", "100",
        "--window", repr(RETRY_FOR_S),
    ]
    data_dir = work / "data-root"
    env = dict(os.environ)
    env.update({
        "TJISE_DATA_DIR": str(data_dir),
        "TJISE_CLOCK_SNAPSHOT": str(snap_path),
        "TJISE_CLOCK_OWNER": "qa-clock-probe",
        "PYTHONIOENCODING": "utf-8",
    })
    report["command"] = args
    report["env"] = {"TJISE_DATA_DIR": str(data_dir),
                     "TJISE_CLOCK_SNAPSHOT": str(snap_path),
                     "TJISE_CLOCK_OWNER": "qa-clock-probe",
                     "cwd": str(work)}
    started = time.monotonic()
    proc = subprocess.Popen(args, cwd=str(work), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        out, _ = proc.communicate(timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        out, _ = proc.communicate()
        report["timed_out"] = True
    elapsed = time.monotonic() - started
    text = (out or b"").decode("utf-8", errors="replace")
    report["elapsed_s"] = round(elapsed, 2)
    report["exit_code"] = proc.returncode
    report["output"] = text
    (work / "worker-output.log").write_text(text, encoding="utf-8")

    terminals = [line for line in text.splitlines() if "[retry] kind=done" in line]
    report["terminal_lines"] = terminals
    assertions = report["assertions"] = []
    assertions.append({"check": "退出码=2（retry_duration 终态正常值）",
                       "ok": proc.returncode == 2, "actual": proc.returncode})
    assertions.append({"check": "恰一条结构化终态", "ok": len(terminals) == 1,
                       "actual": len(terminals)})
    assertions.append({"check": "终态 stop_reason=retry_duration",
                       "ok": len(terminals) == 1 and "stop_reason=retry_duration" in terminals[0],
                       "actual": terminals})
    assertions.append({"check": "无 AttributeError/Traceback",
                       "ok": "AttributeError" not in text and "Traceback" not in text,
                       "actual": ("found" if ("AttributeError" in text
                                              or "Traceback" in text) else "clean")})
    # 预算启动前已过：必须零鉴权零列表零浏览器（秒级退出 + 无这些阶段的人话/日志）
    staged = ["登录", "拉取活动", "报名成功", "浏览器", "Chrome", "playwright"]
    assertions.append({"check": "未进入鉴权/列表/浏览器阶段",
                       "ok": all(word not in text for word in staged),
                       "actual": [w for w in staged if w in text]})
    assertions.append({"check": "秒级退出（预算前置即收口）",
                       "ok": elapsed < 60.0, "actual": round(elapsed, 2)})
    # 数据根只允许出现探针自己的日志/时钟读数，绝不许出现登录态文件
    produced = sorted(p.name for p in data_dir.rglob("*")) if data_dir.is_dir() else []
    report["data_root_files"] = produced
    assertions.append({"check": "数据根无登录态/存储态文件",
                       "ok": not any(n in produced for n in
                                     ("session.json", "storage_state.json")),
                       "actual": produced})
    report["ok"] = all(item["ok"] for item in assertions)
    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="候选内置 worker 离线预算探针")
    parser.add_argument("--exe", required=True, help="候选主 EXE 路径")
    parser.add_argument("--evidence-root", required=True,
                        help="本轮独占证据目录（不覆盖历史根）")
    args = parser.parse_args(argv)

    exe = Path(args.exe)
    evidence = Path(args.evidence_root)
    if evidence.exists() and any(evidence.iterdir()):
        # 允许重跑：本轮根内建带时间戳的子目录，历史报告不覆盖
        evidence = evidence / time.strftime("run-%Y%m%d-%H%M%S")
    evidence.mkdir(parents=True, exist_ok=True)

    report = run_probe(exe, evidence)
    report_path = evidence / "worker-probe-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print("[i] 报告：%s" % report_path.resolve())
    for note in report["notes"]:
        print("[i] %s" % note)
    for item in report["assertions"]:
        print("[%s] %s（%s）" % ("OK" if item["ok"] else "X",
                                 item["check"], item["actual"]))
    if report["ok"]:
        print("[OK] 内置 worker 进入真实 ClockClient → RetryBudget，过期预算唯一终态")
        return 0
    print("[X] 探针断言未全过")
    return 1


if __name__ == "__main__":
    sys.exit(main())
