"""探索：打开首页 → 点「学生活动」→ 打印接口与页面文本，用来确定活动列表的位置。"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import HOME_URL  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "data" / "chrome-profile"
# 学生首页路由由共享的 HOME_URL 拼出，换学校时不用在这里再改一次域名
HOME = f"{HOME_URL}#/stuWorker/student/home/index"
OUT = ROOT / "data" / "explore"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    api_hits = []

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), channel="chrome", headless=True,
            viewport={"width": 1400, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_resp(r):
            if "/api/" in r.url:
                try:
                    ct = (r.headers or {}).get("content-type", "")
                    size = len(r.body()) if "json" in ct else 0
                except Exception:
                    ct, size = "", 0
                api_hits.append({"url": r.url, "method": r.request.method,
                                 "post": r.request.post_data, "ct": ct, "size": size})

        page.on("response", on_resp)
        page.goto(HOME, wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(5000)

        print("== 首页 URL:", page.url)
        print("== 首页接口:")
        for h in api_hits:
            print(f"   {h['method']:4} {h['url'][:110]}  [{h['size']}B]")

        api_hits.clear()
        # 尝试点击「学生活动」入口
        clicked = False
        for sel in ["text=学生活动", "text=活动"]:
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible():
                    loc.click(timeout=5000)
                    clicked = True
                    print(f"\n== 已点击入口：{sel}")
                    break
            except Exception as exc:
                print(f"   点击 {sel} 失败：{exc}")
        if not clicked:
            print("\n[!] 没找到「学生活动」入口，截图留证")
        page.wait_for_timeout(5000)

        print("== 当前 URL:", page.url)
        print("== 该页接口:")
        for h in api_hits:
            print(f"   {h['method']:4} {h['url'][:110]}  [{h['size']}B]")
            if h.get("post"):
                print(f"        post: {h['post'][:200]}")

        txt = page.inner_text("body")
        (OUT / "activity-page.txt").write_text(txt, encoding="utf-8")
        print("\n== 页面文本（前 1200 字）:")
        print(txt[:1200])
        page.screenshot(path=str(OUT / "activity-page.png"), full_page=True)
        print(f"\n== 截图：{OUT / 'activity-page.png'}")
        ctx.close()


if __name__ == "__main__":
    main()
