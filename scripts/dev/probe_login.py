"""探查登录页结构：是否可自动登录（有没有验证码、字段名是什么）。"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import HOME_URL  # noqa: E402

# 登录页就是站点首页，地址跟着 common.SCHOOL_ORIGIN 走
START = HOME_URL

JS = """() => {
  const inputs = Array.from(document.querySelectorAll('input')).map(i => ({
    type: i.type, name: i.name, id: i.id, ph: i.placeholder,
    cls: (i.className || '').slice(0, 40), visible: !!(i.offsetWidth || i.offsetHeight)
  }));
  const imgs = Array.from(document.querySelectorAll('img')).map(i => ({
    src: (i.src || '').slice(0, 120), w: i.width, h: i.height
  })).filter(i => i.w > 0 && i.w < 300);
    const buttons = Array.from(document.querySelectorAll('button, .btn, [class*=login]'))
    .map(b => ({ text: (b.innerText || '').trim().slice(0, 20),
                 cls: (b.className || '').slice(0, 40) }))
    .filter(b => b.text);
  return { title: document.title, url: location.href, inputs, imgs,
           buttons, text: (document.body.innerText || '').slice(0, 400),
           iframes: Array.from(document.querySelectorAll('iframe')).map(f => f.src) };
}"""


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, channel="chrome")
        page = b.new_page()
        page.goto(START, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        info = page.evaluate(JS)
        print("最终地址:", info["url"])
        print("标题:", info["title"])
        print("\n输入框:")
        for i in info["inputs"]:
            if i["visible"]:
                print("  ", i)
        print("\n小图（可能是验证码）:")
        for i in info["imgs"]:
            print("  ", i)
        print("\n按钮:")
        for x in info["buttons"][:12]:
            print("  ", x)
        b.close()


if __name__ == "__main__":
    sys.exit(main())
