#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 PyInstaller 构建本机版 EXE；脚本只构建，不自动安装 PyInstaller。

构建会产生两个程序：主界面 tjise-activity-watch.exe 和 grab-worker.exe。
主界面通过后者运行登录、拉列表和报名子任务，避免冻结程序再次解释 .py 文件。

@author LiXinwen
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
OUT = PROJECT / "dist" / "tjise-grab-exe"
WORK = PROJECT / "dist" / ".pyinstaller-work"


def fail_if_missing() -> None:
    if importlib.util.find_spec("PyInstaller") is None:
        raise SystemExit(
            "未找到 PyInstaller；先在当前 Python 环境执行："
            "python -m pip install pyinstaller"
        )


def run_pyinstaller(name: str, source: Path, mode: str, add_ui: bool) -> Path:
    stage = WORK / name
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True, exist_ok=True)
    args = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--name", name, "--distpath", str(stage / "dist"),
        "--workpath", str(stage / "build"), "--specpath", str(stage),
        "--onefile" if mode == "onefile" else "--onedir",
        "--console" if name == "grab-worker" else "--windowed",
    ]
    if name != "grab-worker":
        args += ["--hidden-import", "signup_plan", "--collect-all", "webview"]
        if add_ui:
            args += ["--add-data", str(HERE / "ui") + os.pathsep + "ui"]
    args.append(str(source))
    print("[exe] $ " + " ".join(str(part) for part in args), flush=True)
    result = subprocess.run(args, cwd=str(HERE), text=True)
    if result.returncode != 0:
        raise SystemExit("PyInstaller 构建失败，退出码 %d" % result.returncode)
    if mode == "onefile":
        return stage / "dist" / (name + ".exe")
    return stage / "dist" / name / (name + ".exe")


def prune_output(out: Path) -> None:
    """清掉旧构建产物，但绝不动 out/data。

    data 里可能有上一版 EXE 留下的 session.json / storage_state.json，
    重建后由 runtime_paths 首次启动迁移到统一数据根——先删数据等于替用户
    注销登录。构建失败/文件锁定时同样不会碰 data（删除按条目进行，
    出错直接抛，不吞）。
    """
    if not out.exists():
        return
    for child in out.iterdir():
        if child.name == "data":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def assemble(mode: str) -> Path:
    prune_output(OUT)
    OUT.mkdir(parents=True, exist_ok=True)
    worker = run_pyinstaller("grab-worker", HERE / "grab.py", "onefile", False)
    app = run_pyinstaller("tjise-activity-watch", HERE / "app.py", mode, True)
    if mode == "onefile":
        shutil.copy2(app, OUT / app.name)
    else:
        shutil.copytree(app.parent, OUT, dirs_exist_ok=True)
    shutil.copy2(worker, OUT / "grab-worker.exe")
    shutil.copy2(HERE / "README.md", OUT / "README.md")
    # data 只是占位目录：已有（旧版数据）原样保留，全新构建维持旧布局
    (OUT / "data").mkdir(exist_ok=True)
    return OUT


def main() -> int:
    parser = argparse.ArgumentParser(description="构建天软活动预报名工具 EXE")
    parser.add_argument("--mode", choices=("onedir", "onefile"), default="onedir",
                        help="默认 onedir；onefile 仅在需要单文件入口时使用")
    args = parser.parse_args()
    fail_if_missing()
    output = assemble(args.mode)
    print("[exe] 构建完成：%s" % output, flush=True)
    print("[exe] 首次运行前确认 Windows WebView2、Chrome/Edge 可用。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
