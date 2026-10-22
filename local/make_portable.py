#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本机版打包成「绿色版」：解压即用，宿主机不用装 Python。

用法（在装过 playwright 的机器上跑一次）：

    python make_portable.py            # 产物在 ../dist/tjise-grab-portable/
    python make_portable.py --keep     # 保留上次的 python/ 目录，只换应用文件

做法：官方嵌入式 Python（含 tkinter 的补齐件）+ 在这台机器上 pip 装 playwright/pillow，
再把 grab.py / gui.py 拷进去。浏览器不打包——Windows 10/11 自带 Edge，
grab.py 会按 Chrome → Edge → 自带内核的顺序找，省掉 150MB+。

@author LiXinwen
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT_ROOT = HERE.parent / "dist"
NAME = "tjise-grab-portable"

PY_VERSION = "%d.%d.%d" % sys.version_info[:3]
EMBED_SOURCES = [
    "https://mirrors.huaweicloud.com/python/{v}/python-{v}-embed-amd64.zip",
    "https://www.python.org/ftp/python/{v}/python-{v}-embed-amd64.zip",
]
GET_PIP = "https://bootstrap.pypa.io/get-pip.py"
PIP_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
PIP_PACKAGES = ["playwright>=1.40", "pillow>=9.0"]

# 应用本体：脚本 + 双击入口 + 说明
APP_FILES = [
    "grab.py", "gui.py", "install.bat", "_py.bat", "1-登录.bat",
    "2-抢活动.bat", "3-检查登录和活动.bat", "4-图形界面.bat",
    "requirements.txt", "config.example.json", "README.md", "gui-preview.png",
]

PY_DIR_NAME = "python"          # 便携解释器放在这里
LAUNCHER = "启动.bat"


def log(msg: str) -> None:
    print("[portable] " + msg, flush=True)


def download(url: str, dest: Path) -> bool:
    log("下载 %s" % url)
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, dest.open("wb") as fh:
            shutil.copyfileobj(resp, fh)
        return True
    except Exception as exc:
        log("  失败：%s: %s" % (type(exc).__name__, exc))
        return False


def stage_python(out: Path, keep: bool) -> Path:
    """准备 python/ 目录：嵌入式解释器 + tkinter + pip + 依赖。"""
    py = out / PY_DIR_NAME
    stamp = py / ".ready"
    if keep and stamp.exists():
        log("复用已有解释器：%s" % py)
        return py
    if py.exists():
        shutil.rmtree(py)
    py.mkdir(parents=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        zpkg = tmp / "python-embed.zip"
        ok = False
        for tpl in EMBED_SOURCES:
            if download(tpl.format(v=PY_VERSION), zpkg):
                ok = True
                break
        if not ok:
            raise SystemExit("嵌入式 Python 下载失败：请检查网络或代理")
        with zipfile.ZipFile(zpkg) as zf:
            zf.extractall(py)

    write_pth(py)

    copy_tkinter(py)
    install_pip(py)
    pip_install(py)
    stamp.write_text("ok\n", encoding="utf-8")
    return py


def write_pth(py: Path) -> None:
    """写 ._pth（复用已有解释器时也要重写一遍）。

    必须带 "."：有 ._pth 时 Python 不再自动把当前目录放进 sys.path，
    而 gui.py 要 import 同目录的 grab.py，启动器又是 cd 到本目录再跑。
    "import site" 一行也不能少，否则 pip 装的 playwright / PIL 找不到。
    """
    pth = next(py.glob("python*._pth"), None)
    if pth is None:
        raise SystemExit("没找到 ._pth，嵌入式包结构变了")
    lines = ["python%s.zip" % "".join(str(n) for n in sys.version_info[:2]),
             "..", ".", "Lib", "Lib\\site-packages", "import site"]
    pth.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    log("写好 ._pth（应用目录 .. 与解释器目录 . 都在路径里）")


def copy_tkinter(py: Path) -> None:
    """嵌入式包不带 tkinter，从本机同版本 Python 里借一套过来。"""
    base = Path(sys.base_prefix)
    log("补齐 tkinter（来自 %s）" % base)
    (py / "Lib").mkdir(exist_ok=True)
    src_pkg = base / "Lib" / "tkinter"
    dst_pkg = py / "Lib" / "tkinter"
    if dst_pkg.exists():
        shutil.rmtree(dst_pkg)
    shutil.copytree(src_pkg, dst_pkg, ignore=shutil.ignore_patterns("__pycache__"))
    for dll in ("_tkinter.pyd", "tcl86t.dll", "tk86t.dll", "zlib1.dll"):
        src = base / "DLLs" / dll
        if src.exists():
            shutil.copy2(src, py / dll)
    src_tcl = base / "tcl"
    if src_tcl.exists():
        shutil.copytree(src_tcl, py / "tcl",
                        ignore=shutil.ignore_patterns("*.exe", "demos", "doc"))


def install_pip(py: Path) -> None:
    exe = py / "python.exe"
    with tempfile.TemporaryDirectory() as tmp:
        gp = Path(tmp) / "get-pip.py"
        if not download(GET_PIP, gp):
            raise SystemExit("get-pip.py 下载失败")
        run([str(exe), str(gp), "--no-warn-script-location", "-i", PIP_INDEX])
    run([str(exe), "-m", "pip", "--version"])


def pip_install(py: Path) -> None:
    exe = py / "python.exe"
    log("安装依赖：%s" % ", ".join(PIP_PACKAGES))
    run([str(exe), "-m", "pip", "install", "--no-warn-script-location",
         "--disable-pip-version-check", "-i", PIP_INDEX] + PIP_PACKAGES)


def prune(py: Path) -> None:
    """删掉装完就用不上的东西：pip 本体、tcl 的 tix/头文件/示例、测试包、缓存。

    pip 只在构建期用一次；绿色版里留着它白占 12MB，删掉不影响运行。
    """
    drop = [
        py / "Lib" / "site-packages" / "pip",
        py / "tcl" / "tix8.4.3",
        py / "tcl" / "tcl8",
        py / "Lib" / "tkinter" / "test",
    ]
    for path in drop:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            log("  删除 %s" % path.relative_to(py))
    for pat in ("tcl/*.lib", "tcl/*.sh", "tcl/*.h"):
        for path in py.glob(pat):
            path.unlink(missing_ok=True)
    for path in list(py.rglob("pip-*.dist-info")):
        shutil.rmtree(path, ignore_errors=True)
    for path in list(py.rglob("__pycache__")):
        shutil.rmtree(path, ignore_errors=True)


def run(cmd: list[str]) -> None:
    log("$ " + " ".join(Path(c).name if i == 0 else c for i, c in enumerate(cmd)))
    # pip 的下载缓存默认落在 %LOCALAPPDATA%，这台机器上会被解析到工作目录，
    # 于是每次构建都在仓库里丢一个 datacachepip/；钉死在 dist 下，跑完就删。
    env = dict(os.environ, PIP_CACHE_DIR=str(OUT_ROOT / ".pip-cache"))
    res = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if res.returncode != 0:
        tail = (res.stdout or "")[-800:] + (res.stderr or "")[-800:]
        raise SystemExit("命令失败（%d）：\n%s" % (res.returncode, tail))


def copy_app(out: Path) -> None:
    log("拷贝应用文件")
    for name in APP_FILES:
        src = HERE / name
        if not src.exists():
            log("  跳过（不存在）：%s" % name)
            continue
        shutil.copy2(src, out / name)
    (out / "data").mkdir(exist_ok=True)


def write_launcher(out: Path) -> None:
    body = """@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "%~dp0python\\pythonw.exe" (
    start "" "%~dp0python\\pythonw.exe" "%~dp0gui.py"
) else (
    echo 便携版解释器缺失，请重新解压整个目录。
    pause
)
"""
    (out / LAUNCHER).write_text(body, encoding="utf-8", newline="\r\n")
    log("写好启动器：%s" % LAUNCHER)


def main() -> int:
    ap = argparse.ArgumentParser(description="打包本机版绿色版")
    ap.add_argument("--keep", action="store_true", help="复用上次的解释器目录")
    args = ap.parse_args()

    out = OUT_ROOT / NAME
    out.mkdir(parents=True, exist_ok=True)
    log("目标目录：%s" % out)
    py = stage_python(out, args.keep)
    write_pth(py)
    prune(py)
    copy_app(out)
    write_launcher(out)

    shutil.rmtree(OUT_ROOT / ".pip-cache", ignore_errors=True)
    total = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    log("完成，%.1f MB" % (total / 1024 / 1024))
    log("自检：%s" % (out / PY_DIR_NAME / "python.exe"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
