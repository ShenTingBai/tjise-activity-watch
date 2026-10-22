"""登录 / 同步 token。

用法：
    python scripts/login.py          # 会话还有效时静默同步；失效则开窗口让你登录
    python scripts/login.py --force  # 强制开窗口重新登录

站点把登录态存在 localStorage 的 xgUniToken 里。本脚本把 token 和用户信息
写到 data/session.json，fetch.py 之后直接带 token 调接口，不用再开浏览器。
"""
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import HOME_URL, PROFILE, save_session  # noqa: E402

# 首页地址跟着 common.SCHOOL_ORIGIN 走，不要在本地再硬编码一份学校域名
START_URL = HOME_URL

READ_JS = """() => {
  const get = (k) => { try { return localStorage.getItem(k) || ''; } catch (e) { return ''; } };
  let user = {};
  try { user = JSON.parse(get('user') || '{}'); } catch (e) { user = {}; }
  return { token: get('xgUniToken') || get('token'), user };
}"""


def build_session(got: dict) -> dict:
    user = got.get("user") or {}
    return {
        "token": (got.get("token") or "").strip(),
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "name": user.get("xm", ""),
        "student_no": user.get("xh", ""),
        "gender": user.get("xb", ""),
    }


def try_silent_sync() -> dict | None:
    """用已保存的浏览器配置静默读一次 token。"""
    if not PROFILE.exists():
        return None
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE), channel="chrome", headless=True,
                viewport={"width": 1280, "height": 860})
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(START_URL, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(2500)
            got = page.evaluate(READ_JS) or {}
            ctx.close()
            session = build_session(got)
            return session if session["token"] else None
    except Exception:
        return None


def interactive_login() -> dict | None:
    PROFILE.mkdir(parents=True, exist_ok=True)
    print("[i] 正在启动 Chrome，请在窗口里选「我是学生」并完成登录", flush=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), channel="chrome", headless=False,
            viewport={"width": 1280, "height": 860},
            args=["--disable-blink-features=AutomationControlled"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(START_URL, wait_until="domcontentloaded", timeout=60_000)
        print(f"[i] 已打开：{page.url}", flush=True)

        deadline = time.time() + 900
        session = None
        while time.time() < deadline:
            time.sleep(2)
            try:
                got = page.evaluate(READ_JS) or {}
            except Exception:
                continue
            if (got.get("token") or "").strip():
                session = build_session(got)
                break

        if session:
            try:
                page.screenshot(path=str(PROFILE.parent / "login-ok.png"),
                                full_page=False)
            except Exception:
                pass
        ctx.close()
        return session


def main() -> int:
    force = "--force" in sys.argv

    if not force:
        session = try_silent_sync()
        if session:
            save_session(session)
            # 日志里不出现姓名、学号和 token：这些输出会被重定向进文件或被转发
            print("[OK] 会话仍然有效，已同步到 data/session.json", flush=True)
            return 0
        print("[i] 本地没有可用的登录态，需要手动登录一次。", flush=True)

    session = interactive_login()
    if not session:
        print("[x] 15 分钟内没检测到登录态，已退出。请重新运行本脚本。", flush=True)
        return 2

    save_session(session)
    print("[OK] 已保存会话到 data/session.json", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
