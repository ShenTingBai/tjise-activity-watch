#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 PyInstaller 构建本机版 EXE；脚本只构建，不自动安装 PyInstaller。

构建产物是**独立可用**的主界面 tjise-activity-watch.exe：先构建 grab-worker（onefile），
再把它作为普通数据资源与 VERSION 一起打进主程序资源根——冻结态 app.py 按
RESOURCE_ROOT/grab-worker.exe 解析，单 EXE 拷到任何空目录都能自足，
不再要求 worker 躺在 EXE 旁边（旧外置 worker 定位已被用户 2026-10-25 决定取代）。
资源落点两种模式同源：onedir 在 <exe>/_internal/（PyInstaller 6.x 的 _MEIPASS），
onefile 在运行期解包根；主程序内执行的是完整既有 worker，不重新分析其依赖。

版本号来自仓库根的 VERSION（唯一来源），并随 --add-data 打进冻结资源根，
运行时 app.py 按 RESOURCE_ROOT/VERSION 读到同一个值。

@author LiXinwen
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
OUT = PROJECT / "dist" / "tjise-grab-exe"
WORK = PROJECT / "dist" / ".pyinstaller-work"
VERSION_FILE = PROJECT / "VERSION"


def read_version() -> str:
    """构建前读 VERSION 并校验格式：版本不合法就不构建，不产出版本对不上号的包。"""
    try:
        text = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit("[exe] 读不到 VERSION（%s）：%s" % (VERSION_FILE, exc))
    if not re.fullmatch(r"\d+\.\d+\.\d+", text):
        raise SystemExit("[exe] VERSION 格式非法：%r（只允许 major.minor.patch）" % text)
    return text


def fail_if_missing() -> None:
    if importlib.util.find_spec("PyInstaller") is None:
        raise SystemExit(
            "未找到 PyInstaller；先在当前 Python 环境执行："
            "python -m pip install pyinstaller"
        )


def run_pyinstaller(name: str, source: Path, mode: str, add_ui: bool,
                    extra_data: "list[tuple[Path, str]] | None" = None) -> Path:
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
        # 版本单一来源：同一份根 VERSION 放进冻结资源根，app.py 用 RESOURCE_ROOT 读
        for src, dest in (extra_data or []):
            args += ["--add-data", str(src) + os.pathsep + dest]
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
    # 先构建现有 worker（onefile），再把它当普通数据资源随 VERSION 一起进主程序
    # 资源根：不用 --add-binary（那会让 PyInstaller 重新分析 worker 依赖），
    # 主程序冻结态按 RESOURCE_ROOT/grab-worker.exe 解析——单 EXE 与 onedir 同源自足
    worker = run_pyinstaller("grab-worker", HERE / "grab.py", "onefile", False)
    # --add-data 对文件源按「dest/源文件名」归档：dest 必须是 "."，
    # 让 worker 以 basename（恰为 grab-worker.exe）落在资源根；
    # 写成 dest="grab-worker.exe" 会解包成同名目录，冻结态解析直接 worker_missing
    app = run_pyinstaller("tjise-activity-watch", HERE / "app.py", mode, True,
                          extra_data=[(worker, "."), (VERSION_FILE, ".")])
    if mode == "onefile":
        shutil.copy2(app, OUT / app.name)
    else:
        shutil.copytree(app.parent, OUT, dirs_exist_ok=True)
    # worker 已内置进主程序资源根：OUT 不再放置外置 grab-worker.exe，
    # 构建失败会在这里之前直接退出，绝不退回「外置 worker 入口」假称独立
    shutil.copy2(HERE / "README.md", OUT / "README.md")
    # 发行说明 / 预览 / 协议：与 Release ZIP 的清单保持同一套来源
    shutil.copy2(PROJECT / "CHANGELOG.md", OUT / "CHANGELOG.md")
    if (HERE / "gui-preview.png").is_file():
        shutil.copy2(HERE / "gui-preview.png", OUT / "gui-preview.png")
    if (PROJECT / "LICENSE").is_file():
        shutil.copy2(PROJECT / "LICENSE", OUT / "LICENSE")
    # data 只是占位目录：已有（旧版数据）原样保留，全新构建维持旧布局
    (OUT / "data").mkdir(exist_ok=True)
    return OUT


def main() -> int:
    parser = argparse.ArgumentParser(description="构建天软活动预报名工具 EXE")
    parser.add_argument("--mode", choices=("onedir", "onefile"), default="onedir",
                        help="默认 onedir；onefile 仅在需要单文件入口时使用")
    args = parser.parse_args()
    version = read_version()
    print("[exe] 版本（VERSION 唯一来源）：%s" % version, flush=True)
    fail_if_missing()
    output = assemble(args.mode)
    print("[exe] 构建完成：%s" % output, flush=True)
    print("[exe] 主 EXE 已内置 grab-worker：单文件可独立运行（无需外置 worker）。",
          flush=True)
    print("[exe] 首次运行前确认 Windows WebView2、Chrome/Edge 可用。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
