#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验 design/README.md 里引用的代码行。

文档里的引用写成 `路径:行号`「原文片段」，片段必须原样出现在基线提交的那一行里。
基线里没有的文件（例如还没提交的草稿）退回去读工作区，并在输出里注明。

    python design/check_refs.py      # 全部对得上退出 0；有对不上的逐条列出并退出 1

@author LiXinwen
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DOC = HERE / "README.md"
BASE = "a66016a"
REF = re.compile(r"`([\w./-]+):(\d+)`「([^」]+)」")


def lines_of(path: str) -> tuple[list[str] | None, str]:
    res = subprocess.run(["git", "show", "%s:%s" % (BASE, path)], cwd=str(ROOT),
                         capture_output=True)
    if res.returncode == 0:
        return res.stdout.decode("utf-8", "replace").splitlines(), BASE
    local = ROOT / path
    if local.exists():
        return local.read_text(encoding="utf-8").splitlines(), "工作区"
    return None, ""


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    text = DOC.read_text(encoding="utf-8")
    cache: dict[str, tuple[list[str] | None, str]] = {}
    total = bad = 0
    for m in REF.finditer(text):
        path, no, snippet = m.group(1), int(m.group(2)), m.group(3)
        total += 1
        if path not in cache:
            cache[path] = lines_of(path)
        lines, src = cache[path]
        if lines is None:
            bad += 1
            print("[X] %s 不存在" % path)
            continue
        line = lines[no - 1] if 0 < no <= len(lines) else ""
        if snippet not in line:
            bad += 1
            print("[X] %s:%d（%s）里没有「%s」\n    这一行实际是：%s" % (path, no, src, snippet, line.strip()))
    worktree = sorted(p for p, (_, src) in cache.items() if src == "工作区")
    print("共 %d 处引用，%d 处对得上，%d 处对不上。基线 %s%s"
          % (total, total - bad, bad, BASE,
             ("；按工作区核对：" + "、".join(worktree)) if worktree else ""))
    return 1 if bad or not total else 0


if __name__ == "__main__":
    raise SystemExit(main())
