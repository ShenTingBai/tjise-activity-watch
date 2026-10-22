#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把服务器版 / 本机版拆成两条「分发分支」。

main 上是两版并存（server/ 与 local/），这里生成两条根目录即成品的分支：

    local   —— 根目录就是本机版（舍友点 Code → Download ZIP 直接能用）
    server  —— 根目录就是服务器版（含 docs/，部署时对着它 rsync 即可）

这两条分支是**只读快照**，每次发布时由脚本重建并强推，不要在上面直接改代码：
改了下次发布就被覆盖。改代码永远在 main。

用法：

    python tools/split_branches.py            # 重建并推送
    python tools/split_branches.py --dry-run  # 只在本地建，不推送

@author LiXinwen
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SHARED = ["README.md", "CHANGELOG.md", ".gitattributes"]
SNAPSHOTS = {
    # 分支名: (要铺到根目录的源目录, 额外单文件)
    "local": ("local", SHARED),
    "server": ("server", SHARED + ["docs"]),
}


def run(cmd: list[str], cwd: Path) -> str:
    res = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    if res.returncode != 0:
        raise SystemExit("命令失败：%s\n%s\n%s"
                         % (" ".join(cmd), res.stdout[-1200:], res.stderr[-1200:]))
    return res.stdout


def copy_tree(src: Path, dst: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dst / src.name,
                        ignore=shutil.ignore_patterns(
                            "config.json", "data", "dist", "reports",
                            "__pycache__", "node_modules", "*.pyc"))
    elif src.exists():
        shutil.copy2(src, dst / src.name)


def build_branch(name: str, subdir: str, extras: list[str], dry: bool) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp) / "repo"
        run(["git", "clone", "--no-hardlinks", "--quiet", str(ROOT), str(tmp)], ROOT)
        run(["git", "checkout", "--orphan", name, "--quiet"], tmp)
        # 清空索引与工作区（orphan 分支会保留原文件的未跟踪状态）
        for entry in tmp.iterdir():
            if entry.name == ".git":
                continue
            shutil.rmtree(entry, ignore_errors=True) if entry.is_dir() else entry.unlink()
        run(["git", "rm", "-rf", "--quiet", "."], tmp)

        copy_tree(ROOT / subdir, tmp)
        for extra in extras:
            copy_tree(ROOT / extra, tmp)

        # 本机版分支补一句「这是分发快照」的说明，避免有人直接在分支上提 PR
        note = tmp / "SNAPSHOT.md"
        note.write_text(
            "# 这是自动生成的分发快照\n\n"
            "内容由 main 分支的 `%s/` 目录拆出，每次发布重建一次并强制推送。\n"
            "**不要在这条分支上改代码**——改动会在下次发布时被覆盖。\n"
            "改代码请提到 main，再运行 `python tools/split_branches.py`。\n"
            % subdir, encoding="utf-8")

        run(["git", "add", "-A"], tmp)
        if run(["git", "status", "--porcelain"], tmp).strip():
            run(["git", "commit", "--quiet", "-m",
                 "chore(dist): 由 main 的 %s/ 生成的快照" % subdir], tmp)
        head = run(["git", "rev-parse", "--short", "HEAD"], tmp).strip()
        print("[%s] 本地快照 "%name + head)
        if dry:
            print("   dry-run，不推送")
            return
        run(["git", "push", "--quiet", "--force", "origin",
             "HEAD:refs/heads/%s" % name], tmp)
        print("   已推送 origin/%s" % name)


def main() -> int:
    ap = argparse.ArgumentParser(description="生成服务器版/本机版分发分支")
    ap.add_argument("--dry-run", action="store_true", help="只建不推")
    ap.add_argument("--only", choices=sorted(SNAPSHOTS), help="只处理一条分支")
    args = ap.parse_args()

    names = [args.only] if args.only else list(SNAPSHOTS)
    for name in names:
        subdir, extras = SNAPSHOTS[name]
        build_branch(name, subdir, extras, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
