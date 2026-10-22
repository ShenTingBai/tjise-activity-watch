#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打一条 Release：构建两个 zip，打 tag，用 gh 上传。

    python tools/release.py v1.0.0                 # 完整发布（会先跑绿色版构建）
    python tools/release.py v1.0.0 --skip-portable # 复用 dist/ 里已有的绿色版

产物：

    dist/tjise-grab-portable-<tag>.zip   本机版绿色版（舍友点 Release 下载这个）
    dist/tjise-server-<tag>.zip          服务器版源码（不含 config/data）

tag 若已存在会复用；Release 若已存在会改用 `gh release upload --clobber` 覆盖附件。

@author LiXinwen
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
GH = Path(os.environ.get("GH_EXE", r"D:/Env/gh/bin/gh.exe"))
PROXY = "http://127.0.0.1:7897"     # api.github.com 在本机要走代理，git push 不用

# 服务器版打包时排除的东西：密钥、运行时数据、产物
SERVER_EXCLUDE = {"config.json", "data", "dist", "reports",
                  "__pycache__", "node_modules"}


def run(cmd: list[str], cwd: Path = ROOT, env: dict | None = None) -> str:
    res = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, env=env,
                         encoding="utf-8", errors="replace")
    if res.returncode != 0:
        raise SystemExit("命令失败：%s\n%s\n%s"
                         % (" ".join(cmd), res.stdout[-1500:], res.stderr[-1500:]))
    return res.stdout


def build_portable() -> None:
    print("[1/4] 构建绿色版 ...")
    run([sys.executable, str(ROOT / "local" / "make_portable.py")])


def zip_portable(tag: str) -> Path:
    src = DIST / "tjise-grab-portable"
    if not src.is_dir():
        raise SystemExit("没找到 %s，先运行 local/make_portable.py" % src)
    out = DIST / ("tjise-grab-portable-%s.zip" % tag)
    out.unlink(missing_ok=True)
    shutil.make_archive(str(out.with_suffix("")), "zip",
                        root_dir=str(DIST), base_dir="tjise-grab-portable")
    return out


def zip_server(tag: str) -> Path:
    out = DIST / ("tjise-server-%s.zip" % tag)
    out.unlink(missing_ok=True)
    staging = DIST / (".server-staging-" + tag)
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(
        ROOT / "server", staging,
        ignore=shutil.ignore_patterns(*SERVER_EXCLUDE))
    # 服务器版单独带一份运维文档，克隆下来就能查
    shutil.copytree(ROOT / "docs", staging / "docs")
    shutil.copy2(ROOT / "README.md", staging / "README.md")
    shutil.copy2(ROOT / "CHANGELOG.md", staging / "CHANGELOG.md")
    shutil.make_archive(str(out.with_suffix("")), "zip",
                        root_dir=str(DIST), base_dir=staging.name)
    shutil.rmtree(staging, ignore_errors=True)
    return out


def ensure_tag(tag: str) -> None:
    if run(["git", "tag", "-l", tag]).strip():
        print("   tag %s 已存在，复用" % tag)
        return
    run(["git", "tag", "-a", tag, "-m", "tjise-activity-watch %s" % tag])
    run(["git", "push", "--quiet", "origin", tag])
    print("   已创建并推送 tag %s" % tag)


def release_notes(tag: str) -> str:
    """取 CHANGELOG 里最新一节，省得每次手写。"""
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    buf: list[str] = []
    for i, line in enumerate(lines):
        if line.startswith("## "):
            if buf:
                break
            buf.append(line)
            continue
        if buf:
            buf.append(line)
    body = "\n".join(buf).strip()
    return (body or tag) + "\n\n---\n\n"


def publish(tag: str, assets: list[Path]) -> None:
    env = dict(os.environ, HTTPS_PROXY=PROXY)
    exists = subprocess.run([str(GH), "release", "view", tag],
                            capture_output=True, text=True, env=env,
                            encoding="utf-8", errors="replace")
    if exists.returncode == 0:
        print("   Release %s 已存在，覆盖附件" % tag)
        run([str(GH), "release", "upload", tag, *[str(a) for a in assets],
             "--clobber"], env=env)
        return
    notes_file = DIST / (".notes-" + tag + ".md")
    notes_file.write_text(release_notes(tag), encoding="utf-8")
    run([str(GH), "release", "create", tag,
         "--title", "tjise-activity-watch %s" % tag,
         "--notes-file", str(notes_file),
         *[str(a) for a in assets]], env=env)
    notes_file.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="打 Release")
    ap.add_argument("tag", help="例如 v1.0.0")
    ap.add_argument("--skip-portable", action="store_true", help="复用已有绿色版目录")
    ap.add_argument("--no-branches", action="store_true", help="不更新 local/server 分发分支")
    args = ap.parse_args()
    tag = args.tag if args.tag.startswith("v") else "v" + args.tag

    if args.skip_portable:
        print("[1/4] 跳过绿色版构建")
    else:
        build_portable()

    print("[2/4] 打两个 zip ...")
    portable = zip_portable(tag)
    server = zip_server(tag)
    for p in (portable, server):
        print("   %s  %.1f MB" % (p.name, p.stat().st_size / 1024 / 1024))

    print("[3/4] tag ...")
    ensure_tag(tag)

    print("[4/4] 上传 Release ...")
    publish(tag, [portable, server])

    if not args.no_branches:
        print("[附] 更新分发分支 local / server ...")
        run([sys.executable, str(ROOT / "tools" / "split_branches.py")])

    print("\n完成：https://github.com/ShenTingBai/tjise-activity-watch/releases/tag/%s" % tag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
