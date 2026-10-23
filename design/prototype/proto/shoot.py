#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给设计文档截图：用本机 Edge 打开每个场景（freeze=1 会在场景跑完后把原型时钟停住），再截窗口内容。

    python design/prototype/proto/shoot.py            # 全部场景 → design/shots/*.png
    python design/prototype/proto/shoot.py armed final # 只截指定场景

只依赖 playwright（local/requirements.txt 里已经有）和本机的 Edge / Chrome。
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

PROTO = Path(__file__).resolve().parent.parent
OUT = PROTO.parent / "shots"

# 场景 id → (文件名, 视口宽, 视口高)。视口 = 窗口 1120×800 去掉 32px 标题栏。
SHOTS = {
    "ready": ("01-ready", 1120, 768),
    "first-run": ("02-first-run", 1120, 768),
    "logging-in": ("03-logging-in", 1120, 768),
    "preview": ("04-preview", 1120, 768),
    "preview-warn": ("05-preview-warn", 1120, 768),
    "preview-open": ("06-preview-open", 1120, 768),
    "armed": ("07-armed", 1120, 768),
    "final": ("08-final", 1120, 768),
    "submitting": ("09-submitting", 1120, 768),
    "success": ("10-success", 1120, 768),
    "fail-full": ("11-fail-full", 1120, 768),
    "fail-login": ("12-fail-login", 1120, 768),
    "switch": ("13-switch", 1120, 768),
    "all": ("14-all", 1120, 768),
    "offline": ("15-offline", 1120, 768),
    "clock-skew": ("16-clock-skew", 1120, 768),
    "expired": ("17-expired", 1120, 768),
    "empty": ("18-empty", 1120, 768),
    "stopped": ("19-stopped", 1120, 768),
    "already": ("20-already", 1120, 768),
    "login-ok": ("21-login-ok", 1120, 768),
    "login-fail": ("22-login-fail", 1120, 768),
    "no-match": ("23-no-match", 1120, 768),
    "sleep": ("24-sleep", 1120, 768),
}
EXTRA = [
    ("armed", "25-armed-980", 980, 668),
    ("preview", "26-preview-980", 980, 668),
    # 150% 缩放时如果窗口尺寸没按 DPI 换算，网页只剩约 746×512
    ("preview-warn", "27-preview-warn-746", 746, 512),
]


def launch(pw):
    for channel in ("msedge", "chrome"):
        try:
            return pw.chromium.launch(channel=channel)
        except Exception:
            continue
    return pw.chromium.launch()


def main(argv: list[str]) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    jobs = [(sid, *SHOTS[sid]) for sid in SHOTS if not argv or sid in argv]
    jobs += [j for j in EXTRA if not argv or j[0] in argv or j[1] in argv]
    errors = 0
    with sync_playwright() as pw:
        browser = launch(pw)
        for sid, name, w, h in jobs:
            ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=1, locale="zh-CN")
            page = ctx.new_page()
            problems: list[str] = []
            page.on("pageerror", lambda e: problems.append(str(e)))
            page.on("console", lambda m: problems.append(m.text) if m.type == "error" else None)
            page.goto((PROTO / "app.html").as_uri() + "?scene=%s&freeze=1" % sid)
            page.wait_for_selector("html[data-scene-ready='1']", timeout=90000)
            page.wait_for_timeout(500)
            page.screenshot(path=str(OUT / (name + ".png")))
            if w == 1120:
                # 单独存一张大屏，给 components.html 的「大屏状态」表用
                (OUT / "board").mkdir(exist_ok=True)
                page.locator("#board").screenshot(path=str(OUT / "board" / (name + ".png")))
            print("%-14s → shots/%s.png%s" % (sid, name, ("  ！" + " | ".join(problems)) if problems else ""))
            errors += len(problems)
            ctx.close()
        browser.close()
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
