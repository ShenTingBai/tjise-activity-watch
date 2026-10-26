#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机 UI 的离线浏览器验收：原样加载 local/ui，只注入测试桥。

用法（只在获得运行授权后执行）：

    python -X utf8 tools/qa_runtime_reliability.py \\
        --host 127.0.0.1 --port 8388 \\
        --evidence-root D:/@Temp/tjise-beijing-clock-contract-repair-20261026/browser --run

覆盖面（决策 012 返修后的全量场景）：
- 顶栏唯一时间入口与独立北京时间：boot 前 / 运行中注入 ±8 小时 / ±30 天的
  系统墙钟错时，直接读 #clock-time、等待 LED、任务卡、现在线与登录详情，
  对照测试桥的固定权威 epoch（window.__QA_CLOCK_NOW），不硬编码具体日期；
- 真正执行 page.reload 模拟 F5：同代旧采样不回退、旧代次不覆盖、
  同代不可用不抹锚点、更高代次才更新、F5 后按权威快照恢复；
- holdover 300/900 秒边界、首校时延迟、真实指针点击重新校时、
  失败反馈只落在时间详情，不触发 login/start_plan；
- 全界面文案行为证据：四态登录与未知小时数、首次登录绿色结论不重复、
  忙时胶囊保持登录结论、已报名移出计划不调用学校取消；
- 五视口 980/1120/1280/746/680：正式名称单行、唯一入口、焦点归还、
  详情不越界、未知状态不挤掉帮助与登录操作；
- pageerror / console 错误 / 外部请求一律 0，违例直接判失败。

约束：
- 只绑定 127.0.0.1；端口占用直接报错退出，不杀占用者；
- 生产四文件原样读取，测试页只在 app.js 之前插入 runtime_bridge.js；
- 外部请求全部拦截并记录（页面不允许访问学校 / GitHub）；
- 使用已安装的 Edge（channel=msedge）；装不了就标 blocked，禁止安装依赖；
- 每个场景保存 DOM 断言、viewport、截图绝对路径与失败原因。

@author LiXinwen
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "local" / "ui"
BRIDGE = ROOT / "local" / "tests" / "fixtures" / "runtime_bridge.js"
SCREENSHOT_FILES = {"app.js", "led.js", "styles.css", "index.html"}


def _ui_root() -> Path:
    """当前服务的 UI 目录；指定 --ui-root 时指向复现用的临时副本。

    副本只用于证明「修复前的缺陷确实会被断言抓住」，正式场景永远读生产 UI。
    """
    override = getattr(QaHandler, "ui_root", None)
    return Path(override) if override else UI


def build_test_page() -> str:
    """生产 index.html 原样保留，只在 led.js 之前插入测试桥。"""
    html = (_ui_root() / "index.html").read_text(encoding="utf-8")
    marker = '<script src="led.js"></script>'
    if marker not in html:
        raise RuntimeError("index.html 结构变了：找不到 led.js 脚本标签")
    return html.replace(marker, '<script src="/qa/runtime_bridge.js"></script>\n  ' + marker, 1)


class QaHandler(BaseHTTPRequestHandler):
    server_version = "tjise-qa/1.0"

    def log_message(self, fmt, *args):  # noqa: A003 - 保持静默，证据走报告
        return

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html", "/qa.html"):
                self._send(200, "text/html; charset=utf-8", build_test_page().encode("utf-8"))
                return
            if path == "/qa/runtime_bridge.js":
                self._send(200, "application/javascript; charset=utf-8",
                           BRIDGE.read_bytes())
                return
            name = path.lstrip("/")
            if name in SCREENSHOT_FILES:
                target = _ui_root() / name
                ctype = {"js": "application/javascript; charset=utf-8",
                         "css": "text/css; charset=utf-8",
                         "html": "text/html; charset=utf-8"}[name.rsplit(".", 1)[-1]]
                self._send(200, ctype, target.read_bytes())
                return
            self._send(404, "text/plain; charset=utf-8", b"not found")
        except Exception as exc:  # noqa: BLE001 - QA 服务要把真实原因交回去
            self._send(500, "text/plain; charset=utf-8", str(exc).encode("utf-8"))


class QaServer:
    def __init__(self, host: str, port: int, ui_root=None):
        QaHandler.ui_root = str(ui_root) if ui_root else None
        self.httpd = ThreadingHTTPServer((host, port), QaHandler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       name="qa-http", daemon=True)
        self.host = host
        self.port = self.httpd.server_address[1]

    @property
    def base(self) -> str:
        return "http://%s:%d" % (self.host, self.port)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class Session:
    """一个场景的页面会话：收集断言、截图、控制台与外部请求。"""

    def __init__(self, browser, base: str, evidence: Path, scenario: dict):
        self.browser = browser
        self.base = base
        self.evidence = evidence
        self.scenario = scenario
        self.assertions = []
        self.actions = []
        self.screenshots = []
        self.console = []
        self.page_errors = []
        self.external = []
        width, height = scenario["viewport"]
        context_args = {"viewport": {"width": width, "height": height}}
        if scenario.get("timezone"):
            context_args["timezone_id"] = scenario["timezone"]
        self.context = browser.new_context(**context_args)
        self.page = self.context.new_page()
        self.page.add_init_script(
            "window.__QA_SCENARIO = %s;" % json.dumps(scenario["bridge"]))
        self.page.on("console", self._on_console)
        self.page.on("pageerror", self._on_page_error)
        self.page.route("**/*", self._route)
        self.page.goto(base + "/", wait_until="domcontentloaded")
        self.page.wait_for_function(
            "window.pywebview && window.__QA_CALLS && document.querySelector('#pill')",
            timeout=15000)

    # ---- 采集 -------------------------------------------------------
    def _on_console(self, message):
        if message.type in ("error", "warning"):
            self.console.append({"type": message.type, "text": message.text})

    def _on_page_error(self, error):
        self.page_errors.append(str(error))

    def _route(self, route):
        url = route.request.url
        if url.startswith(self.base) or url.startswith("data:") or url.startswith("about:"):
            route.continue_()
            return
        self.external.append(url)
        route.abort()

    # ---- 断言 -------------------------------------------------------
    def check(self, label: str, ok: bool, detail: str = "") -> None:
        self.assertions.append({"check": label, "ok": bool(ok), "detail": str(detail)})

    def check_contains(self, selector: str, needle: str) -> None:
        text = self.text(selector)
        self.check("%s 含「%s」" % (selector, needle), needle in text, text[:120])

    def check_not_contains(self, selector: str, needle: str) -> None:
        text = self.text(selector)
        self.check("%s 不含「%s」" % (selector, needle), needle not in text, text[:120])

    def shot(self, name: str) -> str:
        path = self.evidence / ("%s-%s.png" % (self.scenario["id"], name))
        self.page.screenshot(path=str(path), full_page=False)
        size = path.stat().st_size if path.exists() else 0
        self.check("截图 %s 非空" % path.name, size > 2000, "bytes=%d" % size)
        self.screenshots.append(str(path.resolve()))
        return str(path)

    # ---- 交互 -------------------------------------------------------
    def text(self, selector: str) -> str:
        return self.page.locator(selector).first.inner_text()

    def count(self, selector: str) -> int:
        return self.page.locator(selector).count()

    def attr(self, selector: str, name: str):
        return self.page.locator(selector).first.get_attribute(name)

    def js(self, expression, arg=None):
        return self.page.evaluate(expression, arg) if arg is not None \
            else self.page.evaluate(expression)

    def wait(self, expression: str, timeout: int = 8000) -> None:
        self.page.wait_for_function(expression, timeout=timeout)

    def set_state(self, **values) -> None:
        self.js("(s) => Object.assign(window.__QA_STATE, s)", values)

    def emit(self, kind: str, payload: dict) -> None:
        self.js("([k, p]) => window.__QA_EMIT(k, p)", [kind, payload])

    def click(self, selector: str) -> None:
        self.page.locator(selector).first.click()

    def pick(self, activity_id: str) -> None:
        """真实 DOM 点击复选框：列表会整块重绘，避免 Playwright 的
        「点击后再校验同一元素」被重绘打断；仍然走生产 change 处理器。"""
        selector = "input.row-check[data-id=%s]" % json.dumps(activity_id)
        self.js("(sel) => document.querySelector(sel).click()", selector)
        self.wait("S.selected.has(%s)" % json.dumps(activity_id))

    def pointer(self, selector: str, label: str) -> dict:
        """用真实 Playwright 指针点一次，并记录几何与命中目标。

        这是 FR07 的核心证据：`is_visible()` 只证明元素在 DOM 里有尺寸，
        不证明它落在滚动容器的可视交集里、也不证明那个坐标真的点得到它。
        """
        locator = self.page.locator(selector).first
        locator.scroll_into_view_if_needed()
        box = locator.bounding_box() or {}
        center = [box.get("x", 0) + box.get("width", 0) / 2,
                  box.get("y", 0) + box.get("height", 0) / 2]
        # 命中目标解析：点到图标/文字子节点也算命中按钮，取最近的带 id 祖先
        hit = self.js(
            "([x, y]) => { const el = document.elementFromPoint(x, y);"
            " if (!el) return { tag: '', id: '', dataId: '' };"
            " const owner = el.closest ? el.closest('[id]') : null;"
            " return { tag: el.tagName, id: owner ? owner.id : '',"
            "          dataId: el.getAttribute('data-id') || '' }; }",
            center)
        record = {"action": label, "selector": selector,
                  "box": {k: round(v, 1) for k, v in box.items()},
                  "center": [round(v, 1) for v in center], "hit": hit}
        locator.click()          # 真实指针，不是 JS 调用处理器
        record["clicked"] = True
        self.actions.append(record)
        return record

    def pointer_pick(self, activity_id: str) -> dict:
        """真实指针勾选：滚到行内再点，最后校验选择真的变了。"""
        selector = "input.row-check[data-id=%s]" % json.dumps(activity_id)
        record = self.pointer(selector, "pick:%s" % activity_id)
        self.wait("S.selected.has(%s)" % json.dumps(activity_id))
        record["selected"] = True
        return record

    def inject(self, script: str) -> None:
        """在页面脚本之前注入数据准备代码（跨 reload 生效）。

        只用于提供「后端本来就会给的数据」，不替换任何生产函数。
        """
        self.page.add_init_script(script)

    def settle(self, timeout: int = 8000) -> None:
        """等首屏 check/list 都落定：空态与指标必须基于真实结论断言。"""
        self.page.wait_for_function(
            "S.listState === 'ready' || S.listState === 'error'", timeout=timeout)
        self.page.wait_for_function("S.login.checking === false", timeout=timeout)

    def close(self) -> None:
        try:
            self.context.close()
        except Exception:
            pass

    # ---- 独立北京时间断言（对照测试桥的固定权威 epoch，不硬编码日期）----
    def qa_now(self) -> float:
        """测试桥的权威业务时刻（秒）。"""
        return float(self.js("window.__QA_CLOCK_NOW()"))

    def wait_business_ready(self, timeout: int = 12000) -> None:
        """等锚点建立且顶栏显示真实时分秒。"""
        self.wait("(S.businessClock.state === 'ready' || S.businessClock.state === 'holdover') "
                  "&& document.querySelector('#clock-time').textContent.includes(':')",
                  timeout)

    def check_clock_text(self, label: str) -> None:
        """顶栏 #clock-time 必须等于权威北京时间（±1 秒进位容差）。"""
        data = self.js("""() => {
          const fmt = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai',
            hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
          const nowS = window.__QA_CLOCK_NOW();
          const at = (d) => fmt.format(new Date((nowS + d) * 1000));
          return { text: document.querySelector('#clock-time').textContent.trim(),
                   candidates: [at(-1), at(0), at(1)],
                   ui: document.querySelector('#clock').dataset.state };
        }""")
        self.check(label, data["text"] in data["candidates"], data)

    def check_unique_clock(self) -> None:
        """顶栏只允许一个时间入口（按钮 + 详情浮层各一个）。"""
        data = self.js("() => ({ clock: document.querySelectorAll('#clock').length,"
                       " pop: document.querySelectorAll('#clock-pop').length,"
                       " kind: document.querySelectorAll('button.clock').length })")
        self.check("顶栏只有一个时间入口",
                   data == {"clock": 1, "pop": 1, "kind": 1}, data)

    def check_topbar_layout(self, tag: str) -> None:
        """正式名称与时钟标签单行，帮助/登录/时钟都可达且无横向滚动。"""
        data = self.js("""() => {
          const brand = document.querySelector('.brand-name');
          const shown = Array.from(brand.children)
            .find((c) => getComputedStyle(c).display !== 'none');
          const fs = parseFloat(getComputedStyle(shown).fontSize) || 13;
          const label = document.querySelector('#clock-label');
          const rect = (sel) => {
            const el = document.querySelector(sel);
            return el ? el.getBoundingClientRect() : { width: 0, right: 0, left: 0 };
          };
          const help = rect('#btn-help');
          const login = rect('#btn-login');
          const clock = rect('#clock');
          return {
            brandLines: Math.round(shown.getBoundingClientRect().height / (fs * 1.35)),
            clockLabelLines: label ? Math.round(label.getBoundingClientRect().height /
              ((parseFloat(getComputedStyle(label).fontSize) || 12) * 1.35)) : 0,
            clockText: document.querySelector('#clock-time').textContent.trim(),
            helpReachable: help.width > 0 && help.right <= window.innerWidth + 1,
            loginReachable: login.width > 0 && login.left < window.innerWidth,
            clockReachable: clock.width > 0 && clock.right <= window.innerWidth + 1,
            noHScroll: document.documentElement.scrollWidth
              <= document.documentElement.clientWidth + 1,
          };
        }""")
        self.check("%s：正式名称单行" % tag, data["brandLines"] <= 1, data)
        self.check("%s：时钟标签单行" % tag, data["clockLabelLines"] <= 1, data)
        self.check("%s：帮助仍可达" % tag, data["helpReachable"], data)
        self.check("%s：登录仍可达" % tag, data["loginReachable"], data)
        self.check("%s：时钟仍可达" % tag, data["clockReachable"], data)
        self.check("%s：无横向滚动" % tag, data["noHScroll"], data)
        return data

    def emit_clock(self, bump: bool = True, **patch) -> None:
        """改桥注入并按生产语义重发权威快照；generation 不前进会被 UI 拒收。"""
        if patch:
            self.set_state(**patch)
        if bump:
            self.js("() => { window.__QA_STATE.clockGen ="
                    " (Number(window.__QA_STATE.clockGen) || 1) + 1; }")
        self.js("() => window.__QA_EMIT('business_clock', window.__QA_CLOCK_STATE())")

    def inject_state(self, **values) -> None:
        """页面脚本前注入桥状态（跨 reload 生效）；用于 boot 前的错时/失效。"""
        self.inject("window.__QA_STATE = Object.assign(window.__QA_STATE || {}, %s);"
                    % json.dumps(values, ensure_ascii=False))

    def inject_wall_skew(self, offset_ms: int) -> None:
        """在页面脚本前把 Date.now 整体偏移（不改系统时间/时区，只影响沙箱）。"""
        self.inject("const __qaRealNow = Date.now.bind(Date);"
                    "Date.now = () => __qaRealNow() + %d;" % offset_ms)

    def skew_wall_now(self, offset_ms: int) -> None:
        """运行中把 Date.now 偏移（不 reload）；performance 单调外推不受影响。"""
        self.js("(off) => { const r = Date.now.bind(Date);"
                "Date.now = () => r() + off; }", offset_ms)

    def check_nowline(self, label: str) -> None:
        """「现在」线必须按北京时间显示（±1 分钟容差）。"""
        data = self.js("""() => {
          const chip = document.querySelector('[data-now]');
          if (!chip) return null;
          const fmt = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai',
            hour: '2-digit', minute: '2-digit', hour12: false });
          const nowS = window.__QA_CLOCK_NOW();
          return { text: chip.textContent.trim(),
                   candidates: [fmt.format(new Date((nowS - 60) * 1000)),
                                fmt.format(new Date(nowS * 1000)),
                                fmt.format(new Date((nowS + 60) * 1000))] };
        }""")
        self.check(label, bool(data) and data["text"] in data["candidates"],
                   data or "页面上没有现在线")

    def calls_count(self, name: str) -> int:
        return int(self.js("window.__QA_CALLS.filter((c) => c.name === '%s').length" % name))

    def hit_in(self, record: dict, selector: str) -> bool:
        """命中坐标必须落在 selector（或其后代）上；子元素 id 不同也算命中。"""
        center = record.get("center") or []
        if len(center) != 2:
            return False
        return bool(self.js(
            "([x, y]) => { const el = document.elementFromPoint(x, y);"
            " return !!el && !!el.closest(%s); }" % json.dumps(selector), center))


def _task(task_id, activity_id, name, status, stop=None, attempts=0, reason="", **extra):
    payload = {"plan_id": "qa-plan", "task_id": task_id, "activity_id": activity_id,
               "name": name, "sign_start": "", "sign_end": "", "status": status,
               "attempts": attempts, "reason": reason}
    if stop:
        payload["stop_reason"] = stop
    payload.update(extra)
    return payload


SCENARIOS = []


def scenario(sid, title, viewport, bridge="valid", settle=True, timezone=None):
    def deco(fn):
        SCENARIOS.append({"id": sid, "title": title, "viewport": viewport,
                          "bridge": bridge, "settle": settle, "timezone": timezone, "run": fn})
        return fn
    return deco


@scenario("demo-mode", "演示模式可开始且策略可见", (1120, 800), "demo")
def _(s: Session):
    s.check_contains("#pill-text", "演示模式")
    s.check_unique_clock()
    s.check_topbar_layout("演示模式 1120")
    s.check("顶栏时钟标注演示时间", s.attr("#clock", "data-state") == "demo"
            and s.text("#clock-label").strip() == "演示时间",
            [s.attr("#clock", "data-state"), s.text("#clock-label")])
    s.check("演示时间入口可访问名称同步", "演示时间" in (s.attr("#clock", "aria-label") or ""),
            s.attr("#clock", "aria-label"))
    s.wait("S.listState === 'ready'")
    s.pick("a-future")
    s.wait("!document.querySelector('#btn-plan').disabled")
    s.check("#演示模式策略控件可见", s.page.locator("#retry-for").is_visible())
    s.check("#演示模式开始按钮可见", s.page.locator("#btn-plan").is_visible())
    s.shot("demo-ready")


@scenario("first-run-unlogged", "首次未登录空态", (980, 760), "unlogged")
def _(s: Session):
    s.wait("document.querySelector('#pill-text').textContent.includes('未登录')")
    s.check_contains("#pill-text", "未登录")
    s.check("#首次使用按钮叫「登录」", s.text("#btn-login-text").strip() == "登录")
    s.check_contains("#empty-title", "登录后查看活动")
    s.check_contains("#empty-text", "点右上角「登录」")
    s.check_unique_clock()
    s.check_topbar_layout("980 首次未登录")
    s.wait_business_ready()
    s.check_clock_text("未登录同样只显独立北京时间")
    s.shot("empty")


@scenario("checking", "正在检查登录态", (1120, 800), "checking", settle=False)
def _(s: Session):
    s.check_contains("#pill-text", "检查登录")
    s.check_contains("#status-detail", "正在检查登录状态")
    s.shot("checking")
    s.wait("document.querySelector('#pill-text').textContent.includes('已登录')", 12000)
    s.check("#pill 已离开检查中", "检查" not in s.text("#pill-text"))
    s.check_contains("#pill-sub", "约剩")


@scenario("unknown-network", "网络未知不诱导重登", (1120, 800), "unknown")
def _(s: Session):
    s.wait("document.querySelector('#pill-text').textContent.includes('登录待确认')")
    s.check_contains("#pill-text", "登录待确认")
    s.check_not_contains("#pill-text", "登录已失效")
    # 列表本轮也失败：空态写真实原因与恢复动作，不复读、不诱导重登
    s.check_contains("#empty-title", "暂时无法加载")
    s.check_contains("#empty-text", "恢复网络后点「刷新」重试")
    s.check_not_contains("#empty-text", "重新登录")
    # 下一轮列表成功返回 0 场：登录未知仍有专属空态，未知小时数不编造
    s.set_state(list="empty")
    s.click("#btn-refresh")
    s.wait("document.querySelector('#empty-title').textContent.includes('暂时无法确认登录状态')",
           8000)
    s.check_contains("#empty-title", "暂时无法确认登录状态")
    s.check_contains("#empty-text", "检查网络")
    s.check("未知状态按钮仍叫「登录」", s.text("#btn-login-text").strip() == "登录")
    s.check("未知小时数不编造", s.text("#pill-sub").strip() == "", s.text("#pill-sub"))
    s.shot("unknown")


@scenario("valid-empty", "登录有效但学校零活动", (1280, 900), "empty")
def _(s: Session):
    s.wait("document.querySelector('#empty-title').textContent.includes('学校当前没有返回活动')")
    s.check_contains("#empty-title", "学校当前没有返回活动")
    s.check_contains("#empty-text", "登录是有效的")
    s.check("#metric-total", s.text("#metric-total").strip() == "0")
    s.shot("empty")


@scenario("long-place", "超长地点两行与大屏展开", (1280, 900), "valid")
def _(s: Session):
    s.wait("document.querySelectorAll('#rows .row').length >= 5")
    s.page.locator('input.row-check[data-id="a-open"]').check()
    s.wait("document.querySelector('#selected-meta .b-place')")
    s.shot("collapsed")
    metrics = s.js("""() => {
      const place = document.querySelector('#selected-meta .b-place');
      const text = place.querySelector('.b-place-text');
      const rect = place.getBoundingClientRect();
      const next = document.querySelector('#board-text').getBoundingClientRect();
      return { lines: Math.round(text.getBoundingClientRect().height / (13 * 1.35)),
               contained: rect.width <= place.parentElement.getBoundingClientRect().width + 1,
               no_overlap: rect.bottom <= next.top + 1 };
    }""")
    s.check("大屏地点默认不超过两行", metrics["lines"] <= 2, metrics)
    s.check("大屏地点不横向溢出", metrics["contained"], metrics)
    s.check("展开前不覆盖下一条", metrics["no_overlap"], metrics)
    s.page.locator("#selected-meta .b-place").focus()
    s.page.keyboard.press("Enter")
    s.wait("document.querySelector('#selected-meta .b-place').getAttribute('aria-expanded') === 'true'")
    expanded = s.js("""() => {
      const place = document.querySelector('#selected-meta .b-place');
      const text = place.querySelector('.b-place-text');
      const next = document.querySelector('#board-text').getBoundingClientRect();
      return { expanded: place.getAttribute('aria-expanded'),
               full: text.scrollHeight <= text.clientHeight + 1,
               no_overlap: place.getBoundingClientRect().bottom <= next.top + 1 };
    }""")
    s.check("Enter 展开后 aria-expanded=true", expanded["expanded"] == "true", expanded)
    s.check("展开后全文可见", expanded["full"], expanded)
    s.check("展开后仍不覆盖下一条", expanded["no_overlap"], expanded)
    s.shot("expanded")
    # 表格地点：聚焦即可展开（与键盘同一路径）
    s.js("document.querySelector('#rows .row .c-place').focus()")
    table = s.js("""() => {
      const cell = document.querySelector('#rows .row .c-place');
      return { focused: document.activeElement === cell,
               expanded: getComputedStyle(cell).webkitLineClamp === 'none' };
    }""")
    s.check("表格地点可聚焦", table["focused"], table)
    s.check("表格地点聚焦后展开全文", table["expanded"], table)
    s.shot("table-place")


@scenario("filters-clear-preserves-policy", "筛选清空保留重试策略", (1120, 800), "valid")
def _(s: Session):
    s.wait("document.querySelectorAll('#rows .row').length >= 5")
    s.pick("a-future")
    s.wait("!document.querySelector('#retry-for').closest('.policy-select').hidden")
    s.page.select_option("#retry-for", "600")
    s.page.fill("#max-attempts", "3")
    s.page.fill("#q", "绝对不存在的活动")
    s.wait("document.querySelector('#empty-title').textContent.includes('没有符合筛选')")
    s.check_contains("#empty-title", "没有符合筛选")
    s.check("#empty-action 是清除筛选", s.text("#empty-action").strip() == "清除筛选")
    s.shot("filtered-empty")
    s.click("#empty-action")
    s.check("#q 已清空", s.page.input_value("#q") == "")
    s.check("#retry-for 保持 600", s.page.input_value("#retry-for") == "600")
    s.check("#max-attempts 保持 3", s.page.input_value("#max-attempts") == "3")
    s.check("#rows 恢复", s.count("#rows .row") >= 5)
    s.shot("cleared")


@scenario("preopen-and-instant", "预报名与立即报名同批", (1280, 900), "valid")
def _(s: Session):
    s.wait("document.querySelectorAll('#rows .row').length >= 5")
    # a-open 与 a-long 举办时间不冲突：一立即、一预报名，才是真实的混合批次
    s.pick("a-open")
    s.pick("a-long")
    s.wait("!document.querySelector('#btn-plan').disabled")
    s.click("#btn-plan")
    s.wait("window.__QA_CALLS.some((c) => c.name === 'start_plan')")
    s.emit("plan", {"plan_id": "qa-plan", "status": "running", "tasks": []})
    s.emit("task", _task("t1", "a-long", "创新创业训练计划宣讲", "waiting",
                         signup_mode="pre_register", sign_start="2026-10-27 20:00:00"))
    s.emit("task", _task("t2", "a-open", "校园歌手大赛决赛", "submitting",
                         signup_mode="instant_signup", attempts=1, last_reason="已发起"))
    s.wait("document.querySelectorAll('#plan-tasks .task-row').length === 2")
    s.check_contains("#plan-tasks", "预报名")
    s.check_contains("#plan-tasks", "立即报名")
    s.check("#开始按钮锁定", s.page.locator("#btn-plan").is_disabled())
    s.shot("running")


@scenario("mixed-success-waiting", "单场成功不抢整屏结论", (1280, 900), "valid")
def _(s: Session):
    s.wait("document.querySelectorAll('#rows .row').length >= 5")
    s.pick("a-open")
    s.pick("a-long")
    s.wait("!document.querySelector('#btn-plan').disabled")
    s.click("#btn-plan")
    s.wait("window.__QA_CALLS.some((c) => c.name === 'start_plan')")
    s.emit("plan", {"plan_id": "qa-plan", "status": "running", "tasks": []})
    s.emit("task", _task("t1", "a-long", "创新创业训练计划宣讲", "success",
                         stop="success", attempts=1, reason="报名成功"))
    s.emit("task", _task("t2", "a-open", "校园歌手大赛决赛", "waiting",
                         signup_mode="instant_signup"))
    s.wait("document.querySelectorAll('#plan-tasks .task-row').length === 2")
    s.check("#运行中不弹整屏成功", s.page.locator("#result").is_hidden())
    s.shot("single-success")
    s.emit("plan_done", {"plan_id": "qa-plan", "status": "partial",
                         "summary": {"total": 2, "counts": {"success": 1, "waiting": 1}}})
    s.wait("!document.querySelector('#result').hidden")
    s.check_contains("#result", "部分完成")
    s.shot("partial-done")


@scenario("unknown-outcomes", "未确认与鉴权未知贯穿界面", (1120, 800), "valid")
def _(s: Session):
    s.wait("document.querySelectorAll('#rows .row').length >= 5")
    s.pick("a-open")
    s.pick("a-long")
    s.wait("!document.querySelector('#btn-plan').disabled")
    s.click("#btn-plan")
    s.wait("window.__QA_CALLS.some((c) => c.name === 'start_plan')")
    s.emit("plan", {"plan_id": "qa-plan", "status": "running", "tasks": []})
    s.emit("task", _task("t1", "a-long", "创新创业训练计划宣讲", "failed",
                         stop="unconfirmed", attempts=3, reason="响应丢失"))
    s.emit("task", _task("t2", "a-open", "校园歌手大赛决赛", "failed",
                         stop="auth_unavailable", attempts=0, reason="接口异常：URLError"))
    s.emit("plan_done", {"plan_id": "qa-plan", "status": "failed",
                         "summary": {"total": 2, "counts": {"failed": 2}}})
    s.wait("!document.querySelector('#result').hidden")
    s.check_contains("#result", "需确认")
    s.check_not_contains("#result", "都没抢到")
    s.check_not_contains("#result", "登录失效")
    s.check_contains("#plan-tasks", "待核对")
    s.check_contains("#plan-tasks", "本场没有提交")
    s.check("#LED 不亮失败叉", s.attr("#led", "data-tone") != "red")
    # FR06：待核对与未提交必须分列，不能拿 unknown 总数当「待核对」
    s.check_contains("#result", "待核对 1 场")
    s.check_contains("#result", "未提交 1 场")
    s.check_not_contains("#result", "待核对 2 场")
    shadow = s.js("getComputedStyle(document.querySelector('#board')).boxShadow")
    s.actions.append({"action": "board-shadow", "value": shadow})
    s.check("#大屏不画确定失败红框", "255, 91, 72" not in shadow, shadow)
    s.check("#大屏框是中性琥珀", "185, 131, 25" in shadow, shadow)
    s.check("#大屏状态为 attention", s.js(
        "document.querySelector('#board').dataset.planState") == "attention",
        s.js("document.querySelector('#board').dataset.planState"))
    s.shot("unknown-summary")


@scenario("reconcile-hang", "对账永不返回的 5 秒出口", (1120, 800), "reconcile_hang")
def _(s: Session):
    s.wait("document.querySelectorAll('#rows .row').length >= 5")
    s.pick("a-future")
    s.wait("!document.querySelector('#btn-plan').disabled")
    s.click("#btn-plan")
    s.wait("window.__QA_CALLS.some((c) => c.name === 'start_plan')")
    # 触发生产对账入口（真实按钮要在 15 秒后才走这里，QA 直接驱动同一函数）
    s.js("""() => {
      S.startRequest = { id: 'qa-hang', state: 'submitting', timer: null,
                         queryTimer: null, reconcile: 0, gen: 0 };
      S.starting = true;
      reconcileStart('qa-hang');
    }""")
    s.shot("reconciling")
    s.wait("document.querySelector('#result').textContent.includes('结果未确认')", 9000)
    s.check_contains("#result", "5 秒")
    s.check("#开始仍锁定", s.page.locator("#btn-plan").is_disabled())
    calls = s.js("window.__QA_CALLS.filter((c) => c.name === 'start_request_state').length")
    s.click("#result-action")
    s.wait("window.__QA_CALLS.filter((c) => c.name === 'start_request_state').length > %d" % calls)
    s.check("#重新确认只读", s.js(
        "window.__QA_CALLS.filter((c) => c.name === 'start_plan').length") == 1)
    s.shot("reconfirm")


@scenario("skipped-terminal-first", "终态先于 skipped ack", (1120, 800), "skipped")
def _(s: Session):
    s.wait("document.querySelectorAll('#rows .row').length >= 5")
    s.js("""() => {
      window.__QA_STATE.nextCheckSkipped = true;
      refresh(false);                    // 新一轮 ID 先发
      window.__QA_EMIT('check', { request_id: 'qa-old-check', ok: true,
        result: 'valid', session_present: true, hours_left: 6 });
    }""")
    s.wait("document.querySelector('#pill-text').textContent.includes('已登录')", 5000)
    s.check("#旧终态被接续重放", "检查" not in s.text("#pill-text"))
    s.check("#未干等 100 秒", s.js("S.login.checking === false"))
    s.shot("replayed")


@scenario("component-banner", "组件横幅与下一轮恢复", (1120, 800), "component")
def _(s: Session):
    s.wait("document.querySelector('#banners').textContent.includes('程序组件缺失')")
    s.check_contains("#banners", "程序组件缺失")
    s.check_contains("#empty-title", "活动列表未加载")
    s.shot("banner")
    s.set_state(list="ok", check="valid")
    s.click("#btn-refresh")
    s.wait("!document.querySelector('#banners').textContent.includes('程序组件缺失')")
    s.check("#横幅已撤销", "程序组件缺失" not in s.text("#banners"))
    s.check("#列表恢复", s.count("#rows .row") >= 5)
    s.shot("recovered")


@scenario("keyboard-dialogs", "键盘、弹窗与引导", (1120, 800), "valid")
def _(s: Session):
    s.wait("document.querySelectorAll('#rows .row').length >= 5")
    s.page.keyboard.press("Control+f")
    s.check("#Ctrl+F 聚焦搜索", s.js("document.activeElement && document.activeElement.id === 'q'"))
    s.page.keyboard.press("Escape")
    s.check("时钟入口可访问名称同步状态", "北京时间" in (s.attr("#clock", "aria-label") or "")
            and "详情" in (s.attr("#clock", "aria-label") or ""),
            s.attr("#clock", "aria-label"))
    s.click("#clock")
    s.wait("!document.querySelector('#clock-pop').hidden", 4000)
    esc_state = s.js("() => ({ hidden: document.querySelector('#clock-pop').hidden,"
                     " open: S.clockPopOpen,"
                     " active: document.activeElement ? document.activeElement.id : '' })")
    s.page.keyboard.press("Escape")
    esc_after = s.js("() => ({ hidden: document.querySelector('#clock-pop').hidden,"
                     " open: S.clockPopOpen,"
                     " active: document.activeElement ? document.activeElement.id : '' })")
    s.check("Esc 关闭时间详情且焦点归还", esc_after["hidden"] is True
            and esc_after["active"] == "clock", [esc_state, esc_after])
    s.click("#btn-help")
    s.wait("document.querySelector('#help').open === true")
    s.shot("help")
    s.page.keyboard.press("Escape")
    s.wait("document.querySelector('#help').open === false")
    s.check("#Esc 关闭后焦点回入口", s.js(
        "document.activeElement && document.activeElement.id === 'btn-help'"))
    s.click("#btn-help")
    s.wait("document.querySelector('#help').open === true")
    s.click("#btn-help-tour")
    s.wait("document.querySelector('#tour').hidden === false")
    s.check_contains("#tour-card", "第 1/6 步")
    s.shot("tour")
    s.click("#tour-skip")
    s.wait("document.querySelector('#tour').hidden === true")
    s.check("#重看引导按设计关闭帮助面板", s.js("document.querySelector('#help').open === false"))
    s.check("#跳过引导后焦点回入口", s.js(
        "document.activeElement && document.activeElement.id === 'btn-help'"))


@scenario("owner-reuse-cross-round", "同 owner 结果收口新刷新轮次", (1120, 800), "valid")
def _(s: Session):
    s.wait("document.querySelectorAll('#rows .row').length >= 5")
    # roundA：真发一轮 check，但后端卡住不回（owner 就是这一轮自己发的 ID）
    s.set_state(check="hang")
    s.js("() => refresh(false)")
    owner = s.js("S.round.check")
    s.check("#owner 是本轮自己发出的 ID", str(owner).startswith("check-"), owner)
    # 终态先到（后端 _do_check 还没走到 finally，在途标记仍在）
    s.js("""(rid) => window.onBackend.check({ request_id: rid, ok: true,
      result: 'valid', session_present: true, hours_left: 6,
      clock: { source: 'http_date', state: 'ok', offset_s: 0,
               uncertainty_s: 1, rtt_s: 0.2, checked_at: 1 } })""", owner)
    s.wait("S.login.checking === false && S.login.result === 'valid'")
    count_js = ("(needle) => Array.from(document.querySelectorAll('#log .lg'))"
                ".filter((el) => el.textContent.indexOf(needle) >= 0).length")
    before = s.js(count_js, "已复用本机登录态")
    s.actions.append({"action": "log-before", "count": before})

    # roundB：新一轮先置「检查中」，后端回 skipped + 同一 owner 的既有终态
    s.set_state(check="valid", nextCheckSkipped=True, skipCheckId=owner,
                skipCheckResult={"request_id": owner, "ok": True, "result": "valid",
                                 "session_present": True, "hours_left": 6})
    s.js("() => refresh(false)")
    s.wait("S.login.checking === false", 6000)
    s.check("#新轮次不再干等 100 秒", s.js("S.login.checking === false"))
    s.check("#新轮次接续原 owner", s.js("S.round.check") == owner, s.js("S.round.check"))
    s.check("#登录结论保留", s.js("S.login.result") == "valid", s.js("S.login.result"))
    s.check("#新轮 pending 已收口", s.js("S.runtime.checkPending === false"))
    after = s.js(count_js, "已复用本机登录态")
    s.actions.append({"action": "log-after", "count": after})
    s.check("#同一句日志不重复", after == before, [before, after])

    # 旧轮迟到 ack 不得收口后来那一轮：C 的 ack 慢回，D 一直挂着
    s.js("""() => {
      window.__QA_STATE.delayCheckMs = 900;
      refresh(false);                        // roundC
      window.__QA_STATE.check = 'hang';      // roundD 永不回
      refresh(false);                        // roundD
    }""")
    s.page.wait_for_timeout(1500)
    s.check("#旧 ack 不清新轮 pending", s.js("S.runtime.checkPending === true"),
            s.js("[S.runtime.checkPending, S.runtime.listPending, S.round.gen.check]"))
    s.check("#新一轮仍是检查中", s.js("S.login.checking === true"))
    s.shot("owner-reuse")


@scenario("stopped-unconfirmed", "停止保留已提交与待核对", (1280, 900), "valid")
def _(s: Session):
    s.wait("document.querySelectorAll('#rows .row').length >= 5")
    s.pick("a-open")
    s.pick("a-long")
    s.wait("!document.querySelector('#btn-plan').disabled")
    s.click("#btn-plan")
    s.wait("window.__QA_CALLS.some((c) => c.name === 'start_plan')")
    s.emit("plan", {"plan_id": "qa-plan", "status": "running", "tasks": []})
    s.emit("task", _task("t1", "a-long", "创新创业训练计划宣讲", "success",
                         stop="success", attempts=1, reason="报名成功"))
    s.emit("task", _task("t2", "a-open", "校园歌手大赛决赛", "failed",
                         stop="unconfirmed", attempts=3,
                         reason="已按你的要求停止；最后一次提交没有拿到确定回应",
                         locally_stopped=True))
    s.emit("plan_done", {"plan_id": "qa-plan", "status": "stopped",
                         "summary": {"total": 2, "counts": {"success": 1, "failed": 1}}})
    s.wait("!document.querySelector('#result').hidden")
    s.check_contains("#result strong", "已停止")
    s.check_contains("#result", "待核对")
    s.check_contains("#result", "已报名 1 场")
    s.check_contains("#plan-tasks", "已提交 3 次")
    s.check_not_contains("#result", "都没抢到")
    shadow = s.js("getComputedStyle(document.querySelector('#board')).boxShadow")
    s.check("#停止带未知不画红框", "255, 91, 72" not in shadow, shadow)
    s.check("#LED 不亮失败叉", s.attr("#led", "data-tone") != "red")
    s.shot("stopped-unconfirmed")


@scenario("f5-terminal-restore", "F5 恢复完整终态", (1120, 800), "valid")
def _(s: Session):
    s.inject("window.__QA_PLAN = " + json.dumps({
        "plan_id": "qa-f5",
        "status": "failed",
        "summary": {"total": 2, "counts": {"failed": 2}},
        "tasks": [
            {"task_id": "t1", "activity_id": "a-long", "name": "创新创业训练计划宣讲",
             "status": "failed", "stop_reason": "unconfirmed", "attempts": 4,
             "reason": "响应丢失"},
            {"task_id": "t2", "activity_id": "a-open", "name": "校园歌手大赛决赛",
             "status": "failed", "stop_reason": "auth_unavailable", "attempts": 0,
             "reason": "接口异常：URLError"},
        ],
    }, ensure_ascii=False))
    s.page.reload(wait_until="domcontentloaded")
    s.settle()
    s.wait("S.plan.id === 'qa-f5'")
    s.wait("!document.querySelector('#result').hidden")
    s.check("#F5 后不是忙碌态", s.js("S.busy === false"))
    s.check("#F5 后结论框在", s.js("document.querySelector('#result').hidden === false"))
    s.check_contains("#result", "待核对 1 场")
    s.check_contains("#result", "未提交 1 场")
    s.check_contains("#plan-tasks", "已提交 4 次")
    s.check("#F5 后 LED 不亮失败叉", s.attr("#led", "data-tone") != "red")
    shadow = s.js("getComputedStyle(document.querySelector('#board')).boxShadow")
    s.check("#F5 后整屏中性", "255, 91, 72" not in shadow, shadow)
    s.check("#F5 后 doneSelection 已设", s.js(
        "S.plan.doneSelection === selectionSignature()"))
    s.check("#F5 不重发 start_plan", s.js(
        "window.__QA_CALLS.filter((c) => c.name === 'start_plan').length") == 0)
    s.shot("f5-restored")
    # 用户真的改变选择后才回预览
    s.pointer_pick("a-future")
    s.check("#改变选择后回预览", s.js(
        "document.querySelector('#board').dataset.planState === 'idle'"),
        s.js("document.querySelector('#board').dataset.planState"))
    s.shot("f5-after-select")


@scenario("stale-plan-events", "旧计划事件不劫持运行中的计划", (1120, 800), "valid")
def _(s: Session):
    s.wait("document.querySelectorAll('#rows .row').length >= 5")
    s.pick("a-open")
    s.wait("!document.querySelector('#btn-plan').disabled")
    # 真实点击「开始报名计划」：B（qa-plan，代次 2）经生产 startPlan 路径接纳
    s.click("#btn-plan")
    s.wait("S.busy === true && S.plan.id === 'qa-plan'")
    s.emit("task", _task("t1", "a-open", "校园歌手大赛决赛", "submitting", attempts=2))
    # 注入旧 A（qa-plan-old，代次 1）迟到的 plan/task/plan_done 三类事件
    s.js("() => window.__QA_EMIT_STALE()")
    s.page.wait_for_timeout(200)
    s.check("#旧事件不改当前计划 ID", s.js("S.plan.id") == "qa-plan", s.js("S.plan.id"))
    s.check("#B 仍运行、忙态未被解除", s.js("S.busy === true && S.plan.status === 'running'"),
            s.js("[S.busy, S.plan.status]"))
    s.check("#旧任务不得混入 B 的任务表", s.js("S.plan.tasks.has('t-old')") is False,
            list(s.js("Array.from(S.plan.tasks.keys())")))
    s.check("#B 的任务次数保留", s.js("(S.plan.tasks.get('t1')||{}).attempts") == 2,
            s.js("(S.plan.tasks.get('t1')||{}).attempts"))
    s.check("#旧事件不弹终态结果框", s.js("document.querySelector('#result').hidden"),
            s.text("#result")[:80])
    # 真实指针点击停止并确认：stop_plan 实参必须是 B 的 plan_id（P1）
    record = s.pointer("#btn-stop", "stop")
    s.check("#停止按钮真实命中", _hit_id(record, "btn-stop"), record.get("hit"))
    s.wait("document.querySelector('#dlg-stop').open === true")
    s.pointer("#dlg-stop button[value='yes']", "stop-confirm")
    s.wait("S.busy === false", 8000)
    stops = s.js("window.__QA_CALLS.filter((c) => c.name === 'stop_plan')"
                 ".map((c) => String(c.args[0]))")
    s.check("#停止调用指向 B", stops == ["qa-plan"], stops)
    s.check_contains("#result strong", "已停止")
    s.check("#旧计划 ID 不得出现在屏上", s.js("S.plan.id") == "qa-plan", s.js("S.plan.id"))
    s.shot("stale-plan-events")


@scenario("stale-after-done", "旧计划事件不改已完成结论", (1120, 800), "valid")
def _(s: Session):
    s.wait("document.querySelectorAll('#rows .row').length >= 5")
    s.pick("a-future")
    s.wait("!document.querySelector('#btn-plan').disabled")
    s.click("#btn-plan")
    s.wait("S.busy === true && S.plan.id === 'qa-plan'")
    s.emit("task", _task("t1", "a-future", "人工智能前沿讲座", "failed",
                         stop="unconfirmed", attempts=3, reason="响应丢失"))
    s.emit("plan_done", {"plan_id": "qa-plan", "status": "failed",
                         "summary": {"total": 1, "counts": {"failed": 1}}})
    s.wait("!document.querySelector('#result').hidden")
    s.check_contains("#result", "待核对 1 场")
    before_html = s.js("document.querySelector('#result').innerHTML")
    before_led = s.attr("#led", "data-tone")
    # B 已结束后注入旧 A 的完成与进度事件：结论/LED/边框/忙态保持 B
    s.js("() => window.__QA_EMIT_STALE()")
    s.page.wait_for_timeout(200)
    s.check("#已完成结论不被旧完成事件改写",
            s.js("document.querySelector('#result').innerHTML") == before_html,
            s.js("document.querySelector('#result').innerHTML")[:120])
    s.check("#LED 保持 B 的结论", s.attr("#led", "data-tone") == before_led,
            [before_led, s.attr("#led", "data-tone")])
    s.check("#已完成计划不得被旧进度复活", s.js("S.busy === false"), s.js("S.busy"))
    s.check("#屏上计划仍是 B", s.js("S.plan.id") == "qa-plan", s.js("S.plan.id"))
    s.check("#旧任务的卡不得混进来", s.js("S.plan.tasks.has('t-old')") is False,
            list(s.js("Array.from(S.plan.tasks.keys())")))
    # F5 后同样成立：权威快照（带接纳代次）建立归属，旧事件不再覆盖
    s.inject("window.__QA_PLAN = " + json.dumps({
        "plan_id": "qa-plan", "plan_generation": 2, "status": "failed",
        "summary": {"total": 1, "counts": {"failed": 1}},
        "tasks": [{"task_id": "t1", "activity_id": "a-future",
                   "name": "人工智能前沿讲座", "status": "failed",
                   "stop_reason": "unconfirmed", "attempts": 3, "reason": "响应丢失"}],
    }, ensure_ascii=False))
    s.page.reload(wait_until="domcontentloaded")
    s.settle()
    s.wait("S.plan.id === 'qa-plan' && S.busy === false")
    s.js("() => window.__QA_EMIT_STALE()")
    s.page.wait_for_timeout(200)
    s.check("#F5 后旧事件不改结论", s.js("S.busy === false && S.plan.id === 'qa-plan'"),
            s.js("[S.plan.id, S.busy, S.plan.status]"))
    s.check_contains("#result", "待核对 1 场")
    s.shot("stale-after-done")


@scenario("early-events-before-ack", "新计划事件早于 ack 且 ack 不抹次数", (1120, 800), "valid")
def _(s: Session):
    s.wait("document.querySelectorAll('#rows .row').length >= 5")
    s.pick("a-future")
    s.wait("!document.querySelector('#btn-plan').disabled")
    # ack 延迟 600ms 回：B 的 plan/task 事件先于 ack 到达（生产真实时序）
    s.set_state(earlyEventsBeforeAck=True, earlyAckDelayMs=600)
    s.click("#btn-plan")
    s.wait("S.plan.id === 'qa-plan' && S.plan.tasks.has('t1')", 8000)
    s.check("#早于 ack 的事件已接纳新计划", s.js("S.plan.id") == "qa-plan",
            s.js("S.plan.id"))
    s.check("#早到的事件已建任务表", s.js("S.plan.tasks.size") == 1,
            s.js("S.plan.tasks.size"))
    s.wait("S.startRequest.state === 'settled' && S.busy === true", 8000)
    s.check("#ack 后忙态成立", s.js("S.busy === true"), s.js("S.busy"))
    s.check("#ack 不清已收任务", s.js("S.plan.tasks.size") == 1,
            s.js("S.plan.tasks.size"))
    s.check("#ack 不抹已收到的提交次数",
            s.js("(S.plan.tasks.get('t1')||{}).attempts") == 2,
            s.js("(S.plan.tasks.get('t1')||{}).attempts"))
    s.shot("early-events-before-ack")


@scenario("early-terminal-before-ack", "终态早于开始 ack：ack 不复活已结束计划", (1120, 800), "valid")
def _(s: Session):
    s.wait("document.querySelectorAll('#rows .row').length >= 5")
    s.pick("a-future")
    s.wait("!document.querySelector('#btn-plan').disabled")
    # ack 延迟 600ms；30ms 后先推 plan/task/plan_done(completed)
    s.set_state(earlyTerminalBeforeAck=True, earlyAckDelayMs=600)
    s.click("#btn-plan")
    s.wait("S.plan.status === 'completed' && S.busy === false", 8000)
    s.check_contains("#result", "已报名 1 场")
    s.wait("S.startRequest.state === 'settled'", 8000)
    s.page.wait_for_timeout(300)
    s.check("#迟到的 ack 不复活忙态", s.js("S.busy === false"), s.js("S.busy"))
    s.check("#开始标志已释放", s.js("S.starting === false"), s.js("S.starting"))
    s.check("#终态结论保持", s.js("S.plan.status") == "completed",
            s.js("S.plan.status"))
    s.check("#次数保留", s.js("(S.plan.tasks.get('t1')||{}).attempts") == 1,
            s.js("(S.plan.tasks.get('t1')||{}).attempts"))
    s.check("#不重发开始", s.js(
        "window.__QA_CALLS.filter((c) => c.name === 'start_plan').length") == 1,
        s.js("window.__QA_CALLS.filter((c) => c.name === 'start_plan').length"))
    s.check("#LED 保持成功结论", s.attr("#led", "data-tone") != "red",
            s.attr("#led", "data-tone"))
    s.shot("early-terminal-before-ack")


@scenario("clock-skew-boot-8h", "开机前本机错 +8 小时仍显北京时间", (1120, 800), "valid")
def _(s: Session):
    s.inject_wall_skew(8 * 3600 * 1000)
    s.page.reload(wait_until="domcontentloaded")
    s.settle()
    s.wait_business_ready()
    s.check_clock_text("顶栏时间不随本机 +8 小时漂移")
    s.check_unique_clock()
    s.wait("document.querySelectorAll('#rows .row').length >= 5")
    s.check_nowline("现在线 = 权威北京时间（本机 +8h）")
    s.check_contains("#pill-sub", "约剩")
    # 等待 LED 与任务卡：等待倒计时按业务时钟推进，不因错时显示待校准
    s.pick("a-future")
    s.click("#btn-plan")
    s.wait("S.busy === true && S.plan.id === 'qa-plan'")
    s.emit("plan", {"plan_id": "qa-plan", "status": "running", "tasks": []})
    s.emit("task", _task("t1", "a-future", "人工智能前沿讲座", "waiting",
                         signup_mode="pre_register",
                         sign_start=s.js("S.byId.get('a-future').sign_start")))
    s.wait("document.querySelector('#btn-stop').disabled === false")
    metric = s.js("() => document.querySelector('#countdown').textContent")
    s.check("倒计时按北京时间推进（非待校准）",
            "待校准" not in metric and "同时开放" in metric, metric)
    s.check_contains("#led-label", "同时开放 1 场，开放")
    s.check("LED 倒计时是数字不是占位", s.js(
        "() => { const t = document.querySelector('#led-label').textContent;"
        " return t.indexOf('--:--') < 0 && t.indexOf('待校准') < 0; }"), s.text("#led-label"))
    s.shot("skew-boot-8h")


@scenario("clock-skew-boot-30d", "开机前本机错 -30 天仍显北京时间", (1120, 800), "valid")
def _(s: Session):
    s.inject_wall_skew(-30 * 86400 * 1000)
    s.page.reload(wait_until="domcontentloaded")
    s.settle()
    s.wait_business_ready()
    s.check_clock_text("顶栏时间不随本机 -30 天漂移")
    s.wait("document.querySelectorAll('#rows .row').length >= 5")
    s.check("#错日期下列表仍有可报名行",
            s.js("S.items.some((i) => modeOf(i) !== 'closed' && !i.signed)"),
            s.js("S.items.map((i) => modeOf(i))"))
    s.check_nowline("现在线 = 权威北京时间（本机 -30d）")
    s.pick("a-future")
    s.wait("!document.querySelector('#btn-plan').disabled", 6000)
    s.check("开始按钮未被错日期禁死", s.js("!document.querySelector('#btn-plan').disabled"),
            s.text("#btn-plan"))
    s.shot("skew-boot-30d")


@scenario("wall-jump-during-plan", "运行中系统墙钟跳 30 天不影响业务时钟", (1120, 800), "valid")
def _(s: Session):
    s.settle()
    s.wait_business_ready()
    before = s.js("""() => {
      const fmt = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai',
        hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
      return { text: document.querySelector('#clock-time').textContent.trim(),
               expect: fmt.format(new Date(window.__QA_CLOCK_NOW() * 1000)) };
    }""")
    s.check("注入前顶栏已显北京时间", before["text"] == before["expect"], before)
    # 运行中注入 +30 天：不 reload，等 1 秒 tick 重绘后顶栏仍为北京时间
    s.skew_wall_now(30 * 86400 * 1000)
    s.page.wait_for_timeout(1400)
    s.check_clock_text("运行中 +30 天后顶栏仍为北京时间")
    s.wait("document.querySelectorAll('#rows .row').length >= 5")
    s.pick("a-future")
    s.click("#btn-plan")
    s.wait("S.busy === true && S.plan.id === 'qa-plan'")
    s.emit("plan", {"plan_id": "qa-plan", "status": "running", "tasks": []})
    s.emit("task", _task("t1", "a-future", "人工智能前沿讲座", "waiting",
                         signup_mode="pre_register",
                         sign_start=s.js("S.byId.get('a-future').sign_start")))
    s.wait("document.querySelector('#btn-stop').disabled === false")
    metric = s.js("() => document.querySelector('#countdown').textContent")
    s.check("错日期下倒计时仍按北京时间", "待校准" not in metric and "同时开放" in metric, metric)
    s.check_nowline("运行中错时后现在线仍为北京时间")
    s.emit("task", _task("t1", "a-future", "人工智能前沿讲座", "failed",
                         stop="unconfirmed", attempts=1, reason="响应丢失"))
    s.emit("plan_done", {"plan_id": "qa-plan", "status": "failed",
                         "summary": {"total": 1, "counts": {"failed": 1}}})
    s.wait("!document.querySelector('#result').hidden")
    s.check_contains("#result", "待核对 1 场")
    s.shot("wall-jump-during-plan")


@scenario("clock-skew-runtime-minus8h", "运行中系统墙钟回拨 8 小时不回显旧时刻", (1120, 800), "valid")
def _(s: Session):
    s.settle()
    s.wait_business_ready()
    s.pick("a-future")
    s.click("#btn-plan")
    s.wait("S.busy === true && S.plan.id === 'qa-plan'")
    s.emit("plan", {"plan_id": "qa-plan", "status": "running", "tasks": []})
    s.emit("task", _task("t1", "a-future", "人工智能前沿讲座", "waiting",
                         signup_mode="pre_register",
                         sign_start=s.js("S.byId.get('a-future').sign_start")))
    s.wait("document.querySelector('#btn-stop').disabled === false")
    s.skew_wall_now(-8 * 3600 * 1000)
    s.page.wait_for_timeout(1400)
    s.check_clock_text("运行中 -8 小时后顶栏仍为北京时间")
    metric = s.js("() => document.querySelector('#countdown').textContent")
    s.check("回拨后等待倒计时不凭空变长（按北京时间）",
            "待校准" not in metric and "同时开放" in metric, metric)
    s.check_contains("#led-label", "同时开放 1 场，开放")
    s.check("回拨后忙时胶囊保持登录结论", s.text("#pill-text").strip() == "已登录",
            s.text("#pill-text"))
    s.shot("skew-runtime-minus8h")


@scenario("clock-unavailable-start", "校时不可用：开始禁用单一原因、恢复即解禁", (1120, 800), "valid")
def _(s: Session):
    s.inject_state(clockUnavailable=True)
    s.inject_wall_skew(30 * 86400 * 1000)
    s.page.reload(wait_until="domcontentloaded")
    s.settle()
    s.wait("document.querySelector('#clock').dataset.state === 'unavailable'", 8000)
    s.check("顶栏时间显示待校准", s.text("#clock-time").strip() == "待校准",
            s.text("#clock-time"))
    s.check_unique_clock()
    s.pick("a-future")
    s.check("#时钟不可用时开始禁用", s.page.locator("#btn-plan").is_disabled())
    s.check("#禁用原因单一可见", s.text("#btn-plan").strip() == "北京时间未校准",
            s.text("#btn-plan"))
    s.check("#登录校验与时钟正交（仍有效）", s.js("S.login.result") == "valid",
            s.js("S.login.result"))
    s.check_contains("#pill-text", "已登录")
    # 恢复：真实指针点开时间详情并点击「重新校准时间」
    rec = s.pointer("#clock", "open-clock-pop")
    s.check("#时钟入口真实命中", s.hit_in(rec, "#clock"), rec.get("hit"))
    s.wait("!document.querySelector('#clock-pop').hidden", 4000)
    s.set_state(clockUnavailable=False)
    rec2 = s.pointer("#clock-pop-sync", "resync")
    s.check("#重新校准按钮真实命中", s.hit_in(rec2, "#clock-pop-sync"), rec2.get("hit"))
    s.wait("document.querySelector('#clock').dataset.state === 'ready'", 12000)
    s.wait_business_ready()
    s.check_clock_text("恢复后顶栏为权威北京时间")
    s.check("#恢复校时后开始解禁", s.js("!document.querySelector('#btn-plan').disabled"))
    s.check("#恢复过程不触发登录", s.calls_count("login") == 0, s.calls_count("login"))
    s.check("#恢复过程不触发开始计划", s.calls_count("start_plan") == 0,
            s.calls_count("start_plan"))
    s.shot("clock-unavailable-start")


@scenario("clock-sync-failure-feedback", "重新校时失败：反馈只在时间详情", (1120, 800), "valid")
def _(s: Session):
    s.inject_state(clockUnavailable=True)
    s.page.reload(wait_until="domcontentloaded")
    s.settle()
    s.wait("document.querySelector('#clock').dataset.state === 'unavailable'", 8000)
    s.pointer("#clock", "open-clock-pop")
    s.wait("!document.querySelector('#clock-pop').hidden", 4000)
    s.check_contains("#clock-pop-status", "时间校准失败")
    s.check("详情说明不可用原因", "暂时无法获取北京时间" in s.text("#clock-pop-feedback"),
            s.text("#clock-pop-feedback"))
    s.check("失败态按钮叫重试校准", s.text("#clock-pop-sync").strip() == "重试校准",
            s.text("#clock-pop-sync"))
    s.pointer("#clock-pop-sync", "resync-fail")
    s.wait("S.businessClock.syncState === 'idle' && document.querySelector('#clock-pop-feedback')"
           ".textContent.includes('本次校准未成功')", 8000)
    s.check("#失败反馈只在详情（不占结果框）", s.js("document.querySelector('#result').hidden"),
            s.text("#result")[:80])
    s.check("#失败后开始仍禁用", s.page.locator("#btn-plan").is_disabled())
    s.check("#失败不触发登录", s.calls_count("login") == 0, s.calls_count("login"))
    s.check("#失败不触发开始计划", s.calls_count("start_plan") == 0,
            s.calls_count("start_plan"))
    s.shot("clock-sync-failure")


@scenario("clock-holdover-expiry", "holdover 仍可用、过期转待校准并可恢复", (1120, 800), "valid")
def _(s: Session):
    s.settle()
    s.wait_business_ready()
    s.check_clock_text("holdover 前顶栏为权威北京时间")
    # 注入 420 秒授时年龄：超过 300 秒 → holdover，仍可显示
    s.emit_clock(clockAgeS=420)
    s.wait("document.querySelector('#clock').dataset.state === 'holdover'", 8000)
    s.check("holdover 仍显示时间", ":" in s.text("#clock-time"), s.text("#clock-time"))
    s.check("holdover 顶栏标题说明沿用上次校准",
            "使用上次校准结果" in (s.attr("#clock", "title") or ""), s.attr("#clock", "title"))
    s.pointer("#clock", "open-pop-holdover")
    s.wait("!document.querySelector('#clock-pop').hidden", 4000)
    s.check_contains("#clock-pop-status", "使用上次校准结果")
    s.check("holdover 详情给出解释", len(s.text(".pop-explain").strip()) > 0,
            s.text(".pop-explain"))
    s.page.keyboard.press("Escape")
    s.wait("document.querySelector('#clock-pop').hidden === true", 4000)
    s.check("Esc 关详情后焦点回时钟入口",
            s.js("document.activeElement && document.activeElement.id === 'clock'"),
            s.js("document.activeElement && document.activeElement.id"))
    # 过期：905 秒年龄 → unavailable，显示待校准并禁用开始
    s.emit_clock(clockAgeS=905)
    s.wait("document.querySelector('#clock').dataset.state === 'unavailable'", 8000)
    s.check("过期后显示待校准", s.text("#clock-time").strip() == "待校准",
            s.text("#clock-time"))
    s.pick("a-future")
    s.check("过期后开始禁用", s.page.locator("#btn-plan").is_disabled())
    s.check("过期禁用原因单一", s.text("#btn-plan").strip() == "北京时间未校准",
            s.text("#btn-plan"))
    # 恢复：年龄归零 + 更高代次 → 重新可读
    s.emit_clock(clockAgeS=0)
    s.wait("document.querySelector('#clock').dataset.state === 'ready'", 10000)
    s.wait_business_ready()
    s.check_clock_text("恢复后顶栏为权威北京时间")
    s.check("恢复后开始解禁", s.js("!document.querySelector('#btn-plan').disabled"))
    s.shot("clock-holdover-expiry")


@scenario("clock-first-sync-delay", "首校时延迟期间不误导且完成后自动恢复", (1120, 800), "valid")
def _(s: Session):
    s.inject_state(clockInitialUnavailable=True)
    s.page.reload(wait_until="domcontentloaded")
    s.settle()
    s.wait("document.querySelector('#clock').dataset.state === 'syncing'", 8000)
    s.check("首校时未完成显示校准中", s.text("#clock-time").strip() == "校准中…",
            s.text("#clock-time"))
    s.pick("a-future")
    s.check("首校时期间开始禁用", s.page.locator("#btn-plan").is_disabled())
    s.check("禁用原因单一可见", s.text("#btn-plan").strip() == "北京时间未校准",
            s.text("#btn-plan"))
    s.check("等待期不触发登录", s.calls_count("login") == 0, s.calls_count("login"))
    s.check("等待期不触发开始计划", s.calls_count("start_plan") == 0,
            s.calls_count("start_plan"))
    # 后台首校时完成：生产语义 = business_clock 事件 + 更高代次
    s.set_state(clockInitialUnavailable=False)
    s.emit_clock()
    s.wait("document.querySelector('#clock').dataset.state === 'ready'", 10000)
    s.wait_business_ready()
    s.check_clock_text("首校时完成后显示北京时间")
    s.check("首校时完成后开始解禁", s.js("!document.querySelector('#btn-plan').disabled"))
    s.shot("clock-first-sync-delay")


@scenario("clock-status-after-reload", "F5 后锚点恢复且旧代次/旧采样/同代误报不倒退", (1120, 800), "valid")
def _(s: Session):
    s.settle()
    s.wait_business_ready()
    s.check_clock_text("F5 前顶栏时间 = 权威北京时间")
    anchor0 = s.js("S.businessClock.anchorEpochMs")
    # 同代旧采样（锚点早 100 秒）：不得把显示时间拖回去
    s.js("""() => {
      const nowS = window.__QA_CLOCK_NOW();
      window.onBackend.business_clock({ schema: 1, owner_id: 'qa-clock',
        generation: S.businessClock.gen, state: 'ready',
        epoch_s: nowS - 100, tick_s: 0, sampled_tick_s: 0,
        uncertainty_s: 0.2, source: 'qa-ntp', age_s: 0, reason: '旧采样' });
    }""")
    s.page.wait_for_timeout(300)
    s.check("同代旧采样不回退锚点",
            s.js("S.businessClock.anchorEpochMs") >= anchor0 - 2000,
            [anchor0, s.js("S.businessClock.anchorEpochMs")])
    s.check_clock_text("旧采样后顶栏仍为权威北京时间")
    # 更低代次：不得覆盖
    s.js("""() => window.onBackend.business_clock({ schema: 1, owner_id: 'qa-clock',
      generation: 0, state: 'ready', epoch_s: 1000, tick_s: 0,
      sampled_tick_s: 0, uncertainty_s: 0.2, source: 'old', age_s: 0, reason: 'old' })""")
    s.page.wait_for_timeout(200)
    s.check("旧代次不回写锚点",
            s.js("S.businessClock.anchorEpochMs") >= anchor0 - 2000,
            [anchor0, s.js("S.businessClock.anchorEpochMs")])
    # 同代 unavailable 误报：不得抹掉已建立的可读锚点
    s.js("""() => window.onBackend.business_clock({ schema: 1, owner_id: 'qa-clock',
      generation: S.businessClock.gen, state: 'unavailable', epoch_s: null,
      tick_s: null, sampled_tick_s: null, uncertainty_s: 0, source: 'qa-ntp',
      age_s: null, reason: '暂时无法获取北京时间（同代误报）' })""")
    s.page.wait_for_timeout(200)
    s.check("同代误报不抹锚点",
            s.js("S.businessClock.state") in ("ready", "holdover")
            and s.js("S.businessClock.anchorEpochMs") >= anchor0 - 2000,
            s.js("S.businessClock.state"))
    s.check_clock_text("同代误报后仍显示北京时间")
    # 更高代次 holdover：接受并更新
    s.emit_clock(clockAgeS=420)
    s.wait("document.querySelector('#clock').dataset.state === 'holdover'", 8000)
    s.check("更高代次更新为沿用上次校准", True)
    s.check("代次单调不减", s.js("S.businessClock.gen") >= 2, s.js("S.businessClock.gen"))
    # F5：bootstrap 权威快照重建锚点，旧代次事件不得污染
    s.page.reload(wait_until="domcontentloaded")
    s.settle()
    s.wait_business_ready()
    s.check_clock_text("F5 后锚点按权威快照恢复")
    s.check("F5 后时钟可读", s.js("S.businessClock.state") in ("ready", "holdover"),
            s.js("S.businessClock.state"))
    s.shot("clock-status-after-reload")


@scenario("clock-sampled-age-uncertainty", "采样误差与年龄分离：age400/unc0.47 仍可用", (1120, 800), "valid")
def _(s: Session):
    s.settle()
    s.emit_clock(clockAgeS=400, clockUncertaintyS=0.47)
    s.wait("document.querySelector('#clock').dataset.state === 'holdover'", 8000)
    s.check("400 秒旧锚点仍显示时间", ":" in s.text("#clock-time"), s.text("#clock-time"))
    s.pick("a-future")
    s.check("可用快照不提前禁用开始", s.js("!document.querySelector('#btn-plan').disabled"))
    s.check("业务时刻仍可读", s.js("businessNowMs() !== null"))
    s.pointer("#clock", "open-age400")
    s.wait("!document.querySelector('#clock-pop').hidden", 4000)
    pop = s.js("""() => {
      const pop = document.querySelector('#clock-pop');
      return { state: pop.dataset.state,
               text: pop.textContent,
               rows: Array.from(pop.querySelectorAll('.facts div')).map((d) => d.textContent) };
    }""")
    s.check("浮层状态为沿用上次校准", pop["state"] == "holdover", pop["state"])
    s.check("详情误差约 0.47 秒（不叠加采样前漂移）",
            any("0.47" in row for row in pop["rows"]), pop["rows"])
    s.check("详情不显示成 0.51 秒", "0.51" not in pop["text"], pop["text"][-200:])
    s.shot("age400-unc047")


@scenario("clock-f5-old-calibration-age", "F5 接入旧锚点不刷新最近校准", (1120, 800), "valid")
def _(s: Session):
    s.inject_state(clockAgeS=600)
    s.page.reload(wait_until="domcontentloaded")
    s.settle()
    s.wait("document.querySelector('#clock').dataset.state === 'holdover'", 8000)
    s.pointer("#clock", "open-f5-age")
    s.wait("!document.querySelector('#clock-pop').hidden", 4000)
    text = s.text("#clock-pop")
    s.check("F5 后显示真实校准年龄", "10 分钟前" in text, text[:400])
    s.check("F5 不把旧锚点说成刚刚", "刚刚" not in text, text[:400])
    s.check("权威 age 保持 600 秒",
            abs(s.js("S.businessClock.sampleAgeMs") - 600000) < 1,
            s.js("S.businessClock.sampleAgeMs"))
    s.shot("f5-old-age")


@scenario("clock-unknown-known-open-time", "校时失败不把已知开放时间说成待公布", (1120, 800), "valid")
def _(s: Session):
    s.inject_state(clockUnavailable=True)
    s.page.reload(wait_until="domcontentloaded")
    s.settle()
    s.wait("document.querySelector('#clock').dataset.state === 'unavailable'", 8000)
    s.wait("document.querySelectorAll('#rows .row').length >= 5")
    data = s.js("""() => {
      const row = (id) => document.querySelector('.row[data-id="' + id + '"]');
      const unknown = document.querySelector('.gsec[data-g="unknown"]');
      return {
        futureState: row('a-future').querySelector('.c-state').textContent.trim(),
        futureTime: row('a-future').querySelector('.c-time').textContent.trim(),
        futureInUnknown: unknown
          ? unknown.textContent.includes('人工智能前沿讲座') : false,
        noSignInUnknown: unknown
          ? unknown.textContent.includes('开放时间未公布') : false,
        signedState: row('a-signed').querySelector('.c-state').textContent.trim(),
        board: document.querySelector('#selected').textContent.trim(),
        boardText: document.querySelector('#board-text').textContent,
        boardButtons: document.querySelectorAll('#board-text [data-open-clock]').length,
        metricFree: document.querySelector('#metric-free').textContent.trim(),
        metricTotal: document.querySelector('#metric-total').textContent.trim(),
      };
    }""")
    s.check("已知开放时间保留", ":" in data["futureTime"], data["futureTime"])
    s.check("已知开放时间标待校准", "待校准" in data["futureState"], data["futureState"])
    s.check("不把已知开放时间塞进待公布组", not data["futureInUnknown"], data)
    s.check("真正缺开放时间的仍在待公布组", data["noSignInUnknown"], data)
    s.check("已报名事实保留", "已报名" in data["signedState"], data["signedState"])
    s.check("大屏只报时间待校准", data["board"] == "时间待校准", data["board"])
    s.check("大屏不断言没有可报名场次",
            "没有可报名的场次" not in data["boardText"], data["boardText"])
    s.check("大屏恢复入口唯一", data["boardButtons"] == 1, data["boardButtons"])
    s.check("可报名计数不伪造", data["metricFree"] == "待确认", data["metricFree"])
    s.check("列表总数仍真实", data["metricTotal"].isdigit(), data["metricTotal"])
    s.shot("unknown-clock-known-time")
    # 恢复：选中一项并改策略，校时恢复后原地重算且不丢选择/策略
    s.pick("a-future")
    s.js("() => { S.retryPolicy = { retry_for_s: 600, max_attempts: 2 }; }")
    s.set_state(clockUnavailable=False)
    s.emit_clock(clockAgeS=0, clockUncertaintyS=0.2)
    s.wait("document.querySelector('#clock').dataset.state === 'ready'", 10000)
    s.wait_business_ready()
    after = s.js("""() => ({
      metricFree: document.querySelector('#metric-free').textContent.trim(),
      countdown: document.querySelector('#countdown').textContent.trim(),
      selected: Array.from(S.selected), policy: S.retryPolicy,
      futureState: document.querySelector('.row[data-id="a-future"] .c-state')
        .textContent.trim(),
    })""")
    s.check("恢复后计数真实", after["metricFree"].isdigit(), after["metricFree"])
    s.check("恢复后倒计时重算", "待校准" not in after["countdown"], after["countdown"])
    s.check("恢复后不丢选择", after["selected"] == ["a-future"], after["selected"])
    s.check("恢复后不丢重试策略",
            after["policy"] == {"retry_for_s": 600, "max_attempts": 2}, after["policy"])
    s.check("恢复后行状态重算", "待校准" not in after["futureState"], after["futureState"])
    s.shot("recovered")


@scenario("clock-failure-icon-color", "校时失败状态图形与顶栏同为警告色", (1120, 800), "valid")
def _(s: Session):
    s.settle()
    s.wait_business_ready()
    s.pointer("#clock", "open-normal")
    s.wait("!document.querySelector('#clock-pop').hidden", 4000)
    probe = """() => ({
      color: getComputedStyle(
        document.querySelector('#clock-pop-status .state-icon')).color,
      state: document.querySelector('#clock-pop').dataset.state,
      label: document.querySelector('#clock-pop-status').textContent.trim() })"""
    ready = s.js(probe)
    s.check("正常态浮层图形成功色", ready["state"] == "ready", ready)
    s.page.keyboard.press("Escape")
    s.set_state(clockUnavailable=True)
    s.emit_clock(clockAgeS=0, clockUncertaintyS=0.2)
    s.wait("document.querySelector('#clock').dataset.state === 'unavailable'", 8000)
    s.pointer("#clock", "open-failure")
    s.wait("!document.querySelector('#clock-pop').hidden", 4000)
    failed = s.js(probe)
    s.check("失败态浮层图形警告色", failed["state"] == "unavailable", failed)
    s.check("失败与正常状态图形颜色不同", failed["color"] != ready["color"],
            [ready, failed])
    s.check("失败态标签为时间校准失败", "时间校准失败" in failed["label"], failed["label"])
    s.shot("failure-icon-color")


@scenario("clock-five-states", "时钟状态渲染：校准中/失败/holdover/ready", (1120, 800), "valid")
def _(s: Session):
    s.settle()
    s.wait_business_ready()
    s.shot("ready")
    # 从真实 bootstrap 接入首轮进行中的后端快照，不直接改生产 S 状态。
    s.inject_state(clockInitialUnavailable=True)
    s.page.reload(wait_until="domcontentloaded")
    s.settle()
    s.wait("document.querySelector('#clock').dataset.state === 'syncing'", 4000)
    s.check("校准中顶栏文案", s.text("#clock-time").strip() == "校准中…",
            s.text("#clock-time"))
    s.pointer("#clock", "open-syncing")
    s.wait("!document.querySelector('#clock-pop').hidden", 4000)
    s.check_contains("#clock-pop-status", "正在校准")
    s.shot("syncing")
    s.page.keyboard.press("Escape")
    s.emit_clock(clockInitialUnavailable=False, clockAgeS=400, clockUncertaintyS=0.3)
    s.wait("document.querySelector('#clock').dataset.state === 'holdover'", 8000)
    s.shot("holdover")
    s.set_state(clockUnavailable=True)
    s.emit_clock(clockAgeS=0, clockUncertaintyS=0.2)
    s.wait("document.querySelector('#clock').dataset.state === 'unavailable'", 8000)
    s.shot("failure")


@scenario("clock-state-demo", "演示时间浮层中性色", (1120, 800), "demo")
def _(s: Session):
    s.settle()
    s.wait("document.querySelector('#clock').dataset.state === 'demo'", 8000)
    s.pointer("#clock", "open-demo")
    s.wait("!document.querySelector('#clock-pop').hidden", 4000)
    demo = s.js("""() => ({
      state: document.querySelector('#clock-pop').dataset.state,
      label: document.querySelector('#clock-pop-status').textContent.trim() })""")
    s.check("演示浮层状态为 demo", demo["state"] == "demo", demo)
    s.check("演示标签", "演示时间" in demo["label"], demo["label"])
    s.shot("demo")


def _cached_terminal_wall_jump(s: Session, offset_ms: int, tag: str) -> None:
    """终态先暂存 → OS 墙钟跳变 → skipped ack 到达：必须仍立即重放。"""
    s.wait("document.querySelectorAll('#rows .row').length >= 5")
    s.js("""(off) => {
      window.__QA_STATE.nextCheckSkipped = true;
      S.login.checking = true;
      S.login.result = null;
      refresh(false);
      window.__QA_EMIT('check', { request_id: 'qa-old-check', ok: true,
        result: 'valid', session_present: true, hours_left: 6 });
      const real = Date.now.bind(Date);
      Date.now = () => real() + off;
    }""", offset_ms)
    s.wait("S.login.result === 'valid'", 5000)
    s.check("墙钟%s后终态仍立即重放" % tag, s.js("S.login.checking === false"))
    s.check("重放后暂存清空", s.js("S.round.cache.length") == 0,
            s.js("S.round.cache.length"))
    s.check("重放不重复发起检查",
            s.js("window.__QA_CALLS.filter((c) => c.name === 'check').length") <= 2)
    s.shot("wall-jump-%s" % tag)


@scenario("clock-cached-terminal-wall-forward", "暂存终态跨 OS 前移 8 小时仍重放", (1120, 800), "skipped")
def _(s: Session):
    _cached_terminal_wall_jump(s, 8 * 3600 * 1000, "forward")


@scenario("clock-cached-terminal-wall-back", "暂存终态跨 OS 回拨 30 天仍重放", (1120, 800), "skipped")
def _(s: Session):
    _cached_terminal_wall_jump(s, -30 * 86400 * 1000, "back")


@scenario("legacy-session-valid-no-estimate", "旧会话有效但不编造估算小时数", (1120, 800), "legacy_session")
def _(s: Session):
    s.settle()
    s.wait("document.querySelector('#pill-text').textContent.includes('已登录')", 10000)
    s.check_contains("#pill-text", "已登录")
    s.check("旧会话不编「约剩 N 小时」", s.text("#pill-sub").strip() == "",
            s.text("#pill-sub"))
    s.check_not_contains("#status-detail", "约剩")
    s.check("旧会话不被催促重登", "重新登录" not in s.text("#status-detail"),
            s.text("#status-detail"))
    s.wait("document.querySelectorAll('#rows .row').length >= 5")
    # 时间详情里的登录剩余：未知（以学校校验为准），不拿旧 saved_at 冒充。
    # 浮层每秒重绘会把 dd 短暂换新：原子读 + 短重试，避免抓到重建瞬间。
    s.pointer("#clock", "open-pop-legacy")
    s.wait("!document.querySelector('#clock-pop').hidden", 4000)
    pop_ok = False
    pop_state = {}
    for _ in range(6):
        pop_state = s.js(
            "() => ({ hidden: document.querySelector('#clock-pop').hidden,"
            " left: document.querySelector('#clock-pop-left')"
            " ? document.querySelector('#clock-pop-left').textContent : 'NO-NODE' })")
        if (not pop_state["hidden"]) and "未知" in pop_state["left"] \
                and "约剩" not in pop_state["left"]:
            pop_ok = True
            break
        s.page.wait_for_timeout(400)
    s.check("详情登录剩余显示未知", pop_ok, pop_state)
    s.page.keyboard.press("Escape")
    s.wait("document.querySelector('#clock-pop').hidden === true", 4000)
    s.shot("legacy-session")


@scenario("login-flow-first-success", "首次登录成功只报一次绿色结论", (1120, 800), "unlogged")
def _(s: Session):
    s.settle()
    s.check_contains("#pill-text", "未登录")
    s.set_state(loginDone=True, loginOk=True, check="valid")
    rec = s.pointer("#btn-login", "login")
    s.check("#登录按钮真实命中", s.hit_in(rec, "#btn-login"), rec.get("hit"))
    s.wait("document.querySelector('#result').textContent.includes('登录成功')", 8000)
    s.check_contains("#result strong", "登录成功")
    s.check("#成功结论不染红", "is-bad" not in (s.attr("#result", "class") or ""),
            s.attr("#result", "class"))
    s.wait("S.login.checking === false", 12000)
    s.check_contains("#pill-text", "已登录")
    s.check_contains("#pill-sub", "约剩")
    s.check("#只发起一次登录", s.calls_count("login") == 1, s.calls_count("login"))
    s.check("#登录后排队校验登录态", s.calls_count("check") >= 1, s.calls_count("check"))
    red_lines = s.js("""() => Array.from(document.querySelectorAll('#log .lg'))
      .filter((el) => el.textContent.indexOf('登录没完成') >= 0
                   || el.textContent.indexOf('登录没能开始') >= 0
                   || el.textContent.indexOf('报名未成功') >= 0).length""")
    s.check("#首次登录没有红色失败复读", red_lines == 0, red_lines)
    s.shot("login-success")


@scenario("login-four-states", "四态登录与未知小时数准确", (1120, 800), "valid")
def _(s: Session):
    s.settle()
    # 1) 有效：已登录 + 估算小时数
    s.wait("document.querySelector('#pill-text').textContent.includes('已登录')")
    s.check_contains("#pill-sub", "约剩")
    s.check("有效时按钮叫重新登录", s.text("#btn-login-text").strip() == "重新登录")
    # 2) 失效：明确叫重新登录
    s.set_state(check="expired")
    s.click("#btn-refresh")
    s.wait("document.querySelector('#pill-text').textContent.includes('登录已失效')", 8000)
    s.check("失效按钮叫重新登录", s.text("#btn-login-text").strip() == "重新登录")
    s.check("失效说明给动作", "重新登录" in s.text("#status-detail"), s.text("#status-detail"))
    # 3) 未知：不诱导重登、不编小时数
    s.set_state(check="unknown")
    s.click("#btn-refresh")
    s.wait("document.querySelector('#pill-text').textContent.includes('登录待确认')", 8000)
    s.check_contains("#pill-text", "登录待确认")
    s.check_not_contains("#pill-text", "登录已失效")
    s.check("未知说明给恢复动作", "刷新" in s.text("#status-detail")
            and "先不用重新登录" in s.text("#status-detail"), s.text("#status-detail"))
    s.check("未知小时数留空", s.text("#pill-sub").strip() == "", s.text("#pill-sub"))
    # 4) 未登录：引导首次登录
    s.set_state(check="none")
    s.click("#btn-refresh")
    s.wait("document.querySelector('#pill-text').textContent.includes('未登录')", 8000)
    s.check("未登录按钮叫登录", s.text("#btn-login-text").strip() == "登录")
    s.shot("login-four-states")


@scenario("busy-pill-login-state", "忙时胶囊保持登录结论且操作禁用", (1120, 800), "valid")
def _(s: Session):
    s.settle()
    s.pick("a-future")
    s.click("#btn-plan")
    s.wait("S.busy === true")
    s.check_contains("#pill-text", "已登录")
    s.check("#忙时登录按钮禁用", s.page.locator("#btn-login").is_disabled())
    s.check("#忙时刷新保持可用（恢复路径不封死）",
            s.js("!document.querySelector('#btn-refresh').disabled"))
    s.check("#忙时不拿「正在处理」顶替登录结论",
            "正在" not in s.text("#pill-text"), s.text("#pill-text"))
    s.emit("plan", {"plan_id": "qa-plan", "status": "running", "tasks": []})
    s.emit("task", _task("t1", "a-future", "人工智能前沿讲座", "waiting",
                         signup_mode="pre_register",
                         sign_start=s.js("S.byId.get('a-future').sign_start")))
    s.wait("document.querySelector('#btn-stop').disabled === false")
    s.check_contains("#pill-text", "已登录")
    s.shot("busy-pill")
    s.emit("plan_done", {"plan_id": "qa-plan", "status": "stopped",
                         "summary": {"total": 1, "counts": {"canceled": 1}}})
    s.wait("S.busy === false", 8000)
    s.check_contains("#pill-text", "已登录")
    s.check("#停止后登录按钮恢复可用", s.js("!document.querySelector('#btn-login').disabled"))


@scenario("signed-clear-no-cancel", "已报名移出计划不调用学校取消、不自动重开", (1120, 800), "valid")
def _(s: Session):
    s.settle()
    s.wait("document.querySelectorAll('#rows .row').length >= 5")
    s.pick("a-open")
    s.click("#btn-plan")
    s.wait("S.busy === true && S.plan.id === 'qa-plan'")
    s.emit("plan", {"plan_id": "qa-plan", "status": "running", "tasks": []})
    s.emit("task", _task("t1", "a-open", "校园歌手大赛决赛", "success",
                         stop="success", attempts=1, reason="报名成功"))
    s.emit("plan_done", {"plan_id": "qa-plan", "status": "completed",
                         "summary": {"total": 1, "counts": {"success": 1}}})
    s.wait("!document.querySelector('#result').hidden")
    s.check_contains("#plan-tasks", "报名成功")
    clears_before = s.calls_count("cancel_task")
    s.check("#清空按钮已恢复可用", s.js("!document.querySelector('#btn-clear-plan').disabled"))
    s.click("#btn-clear-plan")
    s.wait("S.busy === false && S.plan.status === 'idle'", 6000)
    s.check("移出计划不调用学校取消", s.calls_count("cancel_task") == clears_before == 0,
            [clears_before, s.calls_count("cancel_task")])
    s.check("移出计划不重发开始", s.calls_count("start_plan") == 1, s.calls_count("start_plan"))
    s.check("移出后大屏回预览",
            s.js("document.querySelector('#board').dataset.planState") == "idle",
            s.js("document.querySelector('#board').dataset.planState"))
    s.check("移出后不再显示已报名任务卡", s.count("#plan-tasks .task-row") == 0,
            s.count("#plan-tasks .task-row"))
    s.shot("signed-clear")


def _topbar_matrix(s: Session, tag: str) -> None:
    """登录三态 × 时钟两态组合：名称单行、操作可达、未知态不挤掉入口。"""
    s.settle()
    for check_mode, clock_bad, pill_needle in (
            ("valid", False, "已登录"), ("unknown", False, "登录待确认"),
            ("none", True, "未登录")):
        s.set_state(check=check_mode, clockUnavailable=clock_bad)
        s.js("() => refresh(false)")
        s.wait("document.querySelector('#pill-text').textContent.includes('%s')" % pill_needle,
               10000)
        s.emit_clock()
        s.wait("document.querySelector('#clock').dataset.state === '%s'"
               % ("unavailable" if clock_bad else "ready"), 8000)
        g = s.check_topbar_layout("%s %s×%s" % (tag, pill_needle,
                                                "时钟不可用" if clock_bad else "时钟正常"))
        if clock_bad:
            s.check("%s：未知时钟显示待校准" % tag, g["clockText"] == "待校准", g["clockText"])
            s.check("%s：不可用时开始禁用" % tag, s.page.locator("#btn-plan").is_disabled())
        else:
            s.check("%s：正常时钟显示真实时刻" % tag, ":" in g["clockText"], g["clockText"])
    s.shot("topbar-matrix-%s" % tag)


@scenario("topbar-matrix-1120", "顶栏登录×时钟组合（1120）", (1120, 800), "valid")
def _(s: Session):
    _topbar_matrix(s, "1120")


@scenario("topbar-matrix-980", "顶栏登录×时钟组合（980）", (980, 760), "valid")
def _(s: Session):
    _topbar_matrix(s, "980")


@scenario("topbar-matrix-1280", "顶栏登录×时钟组合（1280）", (1280, 900), "valid")
def _(s: Session):
    _topbar_matrix(s, "1280")


def _hit_id(record: dict, expect: str) -> bool:
    """命中目标校验：那个坐标真的落在预期元素（或它的子节点）上，没被遮挡。"""
    hit = record.get("hit") or {}
    return str(hit.get("id") or "") == str(expect)


def _narrow(s: Session, width: int, height: int):
    s.wait("document.querySelectorAll('#rows .row').length >= 5")
    s.check_unique_clock()
    s.check_topbar_layout("窄窗 %d" % width)
    s.wait_business_ready()
    s.check_clock_text("窄窗顶栏仍显独立北京时间")
    s.check("#无横向滚动", s.js("document.documentElement.scrollWidth <= "
                                "document.documentElement.clientWidth + 1"),
            s.js("[document.documentElement.scrollWidth, document.documentElement.clientWidth]"))

    # FR07：活动区必须有真实可操作高度，不能只剩表头
    geo = s.js("""() => {
      const list = document.querySelector('#rows');
      const table = document.querySelector('.table');
      const lr = list.getBoundingClientRect();
      const tr = table.getBoundingClientRect();
      const rows = Array.from(document.querySelectorAll('#rows .row'));
      const first = rows[0] ? rows[0].getBoundingClientRect() : null;
      /* 可见带 = 列表矩形 ∩ 表格矩形 ∩ 视口：list 自己的 height 可能是被
         父级 overflow:hidden 裁掉之前的值，不能当作「用户看得到多少」。 */
      const top = Math.max(lr.top, tr.top, 0);
      const bottom = Math.min(lr.bottom, tr.bottom, window.innerHeight);
      const visibleH = Math.max(0, bottom - top);
      const overlap = first
        ? Math.max(0, Math.min(bottom, first.bottom) - Math.max(top, first.top)) : 0;
      return { listH: Math.round(lr.height), tableH: Math.round(tr.height),
               visibleH: Math.round(visibleH), rows: rows.length,
               scrollH: Math.round(list.scrollHeight),
               firstRowOverlap: Math.round(overlap),
               pageScrollable: document.documentElement.scrollHeight
                 > document.documentElement.clientHeight + 1 };
    }""")
    s.actions.append({"action": "list-geometry", **geo})
    # 短窗允许整页纵向滚动：要求「现在就看得见」或「滚一下就能看见」，
    # 两者都不成立才是把列表挤没了（修复前 visibleH=11 且不可滚动）。
    s.check("#列表有可操作高度", geo["listH"] >= 120, geo)
    s.check("#列表可达（视口内或页面可滚动）",
            geo["visibleH"] >= 120 or geo["pageScrollable"], geo)
    s.check("#首行可达", geo["firstRowOverlap"] >= 20 or geo["pageScrollable"], geo)

    # 真实指针：两行各自滚到可见处再点，命中目标必须是本行复选框
    first = s.pointer_pick("a-future")
    s.check("#指针命中第一行", (first["hit"] or {}).get("dataId") == "a-future", first["hit"])
    s.check("#第一行选择已改变", s.js("S.selected.has('a-future')"), list(s.js("Array.from(S.selected)")))
    second = s.pointer_pick("a-long")
    s.check("#滚动后仍可选中第二行", s.js("S.selected.has('a-long')"),
            list(s.js("Array.from(S.selected)")))
    s.shot("narrow-%d" % width)

    # 开始 → 停止：两个关键操作在短窗里也必须真的点得到
    planning = s.pointer("#btn-plan", "start")     # pointer() 自己就点击
    s.check("#开始按钮命中", _hit_id(planning, "btn-plan"), planning["hit"])
    s.wait("S.busy === true")
    # 只有计划事件到了才有路线与停止入口：先补真实形状的 plan/task 载荷
    sign_start = s.js("S.byId.get('a-future').sign_start")
    s.emit("plan", {"plan_id": "qa-plan", "status": "running", "tasks": []})
    s.emit("task", _task("t1", "a-future", "人工智能前沿讲座", "waiting",
                         signup_mode="pre_register", sign_start=sign_start))
    s.wait("document.querySelector('#btn-stop').disabled === false")
    s.wait("document.querySelector('#route-line').hidden === false")
    stop = s.pointer("#btn-stop", "stop")
    s.check("#停止按钮命中", _hit_id(stop, "btn-stop"), stop["hit"])
    s.wait("document.querySelector('#dlg-stop').open === true")
    s.check("#停止确认框可打开", True)
    s.page.locator("#dlg-stop button[value='no']").first.click()
    s.wait("document.querySelector('#dlg-stop').open === false")
    s.shot("narrow-%d-running" % width)

    # 帮助入口同样要能到达并关闭
    help_rec = s.pointer("#btn-help", "help")
    s.check("#帮助按钮命中", _hit_id(help_rec, "btn-help"), help_rec["hit"])
    s.wait("document.querySelector('#help').open === true")
    s.page.keyboard.press("Escape")
    s.wait("document.querySelector('#help').open === false")
    s.shot("narrow-%d-help-closed" % width)


@scenario("narrow-746", "压力视口 746x512", (746, 512), "valid")
def _(s: Session):
    _narrow(s, 746, 512)


@scenario("narrow-680", "压力视口 680x480", (680, 480), "valid")
def _(s: Session):
    _narrow(s, 680, 480)


def run_scenarios(base: str, evidence: Path, only: str | None) -> dict:
    from playwright.sync_api import sync_playwright

    report = {"ok": False, "base": base, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "scenarios": [], "failures": []}
    selected = [item for item in SCENARIOS if not only or item["id"] == only]
    if only and not selected:
        raise SystemExit("没有这个场景：%s" % only)

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="msedge", headless=True)
        except Exception as exc:  # noqa: BLE001 - 环境缺失按 blocked 处理
            report["blocked"] = "无法启动 Edge/Playwright：%s" % exc
            return report
        try:
            for item in selected:
                scenario_report = {"id": item["id"], "title": item["title"],
                                   "viewport": item["viewport"], "assertions": [],
                                   "actions": [], "screenshots": [], "console": [],
                                   "page_errors": [], "external_requests": []}
                session = None
                print("[i] 场景 %s …（%dx%d）" % (item["id"], item["viewport"][0],
                                                  item["viewport"][1]))
                try:
                    session = Session(browser, base, evidence, item)
                    if item.get("settle", True):
                        session.settle()
                    item["run"](session)
                    scenario_report["assertions"] = session.assertions
                    scenario_report["actions"] = session.actions
                    scenario_report["screenshots"] = session.screenshots
                    scenario_report["console"] = session.console
                    scenario_report["page_errors"] = session.page_errors
                    scenario_report["external_requests"] = session.external
                    failed = [a for a in session.assertions if not a["ok"]]
                    scenario_report["ok"] = (not failed
                                             and not session.page_errors
                                             and not session.console
                                             and not session.external)
                    if failed:
                        report["failures"].extend(
                            "%s: %s" % (item["id"], a["check"]) for a in failed)
                    if session.page_errors:
                        report["failures"].append("%s: pageerror %s"
                                                  % (item["id"], session.page_errors[0]))
                    if session.external:
                        report["failures"].append("%s: 外部请求 %s"
                                                  % (item["id"], session.external[0]))
                except Exception as exc:  # noqa: BLE001 - 场景失败要写进报告
                    scenario_report["ok"] = False
                    scenario_report["error"] = "%s: %s" % (type(exc).__name__, exc)
                    report["failures"].append("%s: %s" % (item["id"], exc))
                    if session is not None:
                        try:
                            session.shot("failure")
                        except Exception:
                            pass
                        scenario_report["screenshots"] = session.screenshots
                        scenario_report["assertions"] = session.assertions
                        scenario_report["actions"] = session.actions
                        scenario_report["console"] = session.console
                        scenario_report["page_errors"] = session.page_errors
                        scenario_report["external_requests"] = session.external
                finally:
                    if session is not None:
                        session.close()
                report["scenarios"].append(scenario_report)
                print("[%s] %s（断言 %d）" % ("OK" if scenario_report["ok"] else "X",
                                              item["id"], len(scenario_report["assertions"])))
        finally:
            browser.close()
    report["ok"] = not report["failures"]
    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    return report


@scenario("cold-clock-progress-failure-recovery", "真实boot校准中、失败与点击恢复", (1120, 800))
def _(s: Session):
    s.inject_state(clockInitialUnavailable=True)
    s.page.reload(wait_until="domcontentloaded")
    s.settle()
    s.wait("document.querySelector('#clock').dataset.state === 'syncing'")
    s.check("真实bootstrap显示校准中", s.text("#clock-time").strip() == "校准中…")
    s.check("未知LED不误标待公布", "待公布" not in s.text("#led-label"), s.text("#led-label"))
    s.pointer("#clock", "open-first-sync")
    s.check_contains("#clock-pop-status", "正在校准")
    s.shot("real-boot-syncing")
    s.emit_clock(clockInitialUnavailable=False, clockUnavailable=True)
    s.wait("document.querySelector('#clock').dataset.state === 'unavailable'")
    s.check_contains("#clock-pop-status", "时间校准失败")
    s.shot("first-attempt-failed")
    s.set_state(clockUnavailable=False)
    before = s.calls_count("sync_clock")
    s.pointer("#clock-pop-sync", "retry-first-clock")
    s.wait("document.querySelector('#clock').dataset.state === 'ready'")
    s.check("真实点击发起一次校准", s.calls_count("sync_clock") == before + 1)
    s.check_clock_text("恢复后显示北京时间")
    s.shot("retry-recovered")


@scenario("log-business-time", "日志只使用业务时间或worker源时间", (1120, 800))
def _(s: Session):
    s.wait_business_ready()
    data = s.js("""() => {
      const RealDate=Date;
      globalThis.Date=class extends RealDate {
        constructor(...args){super(...(args.length?args:[0]));} static now(){return 0;}
      };
      const before=clockParts(businessNowMs()).time;
      addLog('[i] 日志业务时刻验证');
      const row=document.querySelector('#log').lastElementChild.textContent;
      const after=clockParts(businessNowMs()).time;
      const source='2026-10-26 19:59:00 [i] worker源事件';
      addLog(source);
      return {before,after,row,source,worker:document.querySelector('#log').lastElementChild.textContent};
    }""")
    s.check("日志时间不使用错误OS", any(data["row"].startswith(t + "  ")
                                       for t in [data["before"], data["after"]]), data)
    s.check("worker源时间不被重新盖章", data["worker"] == data["source"], data)
    s.emit_clock(clockUnavailable=True)
    unknown = s.js("""() => { addLog('[i] 未校准日志');
      return document.querySelector('#log').lastElementChild.textContent; }""")
    s.check("缺时钟日志明确待校准", unknown.startswith("待校准  "), unknown)
    s.shot("log-time-dom")


@scenario("preferences-busy-recovery", "设置保存超时有界重试与恢复", (1120, 800))
def _(s: Session):
    s.pick("a-future")       # 与实际使用一致：选择活动后才展示计划设置控件
    s.set_state(prefsBusy=True)
    before = s.calls_count("save_prefs")
    s.pointer("label:has(#headed)", "change-setting-while-locked")
    s.wait("S.saveFailures === 2", 6000)
    s.check("最多一次自动重试", s.calls_count("save_prefs") == before + 2)
    s.check("失败只提示一次", s.js("Array.from(document.querySelectorAll('#log .lg')).filter(x=>x.textContent.includes('设置暂未保存')).length") == 1)
    s.check("会话设置与待保存值保留", s.js("S.prefs.headed === true && S.savePending.headed === true"))
    s.shot("save-busy")
    s.set_state(prefsBusy=False)
    s.pointer("label:has(#headed)", "change-after-lock-release")
    s.wait("window.__QA_STATE.savedPrefs && window.__QA_STATE.savedPrefs.headed === false")
    s.wait("Object.keys(S.savePending).length === 0 && !S.saveBusy")
    s.check("恢复时较新值优先", s.js("S.prefs.headed === false"))
    s.shot("save-recovered")


def _today_in_os_timezone(s: Session):
    s.emit_clock(clockEpochS=1792947600, clockAgeS=0)  # 北京2026-10-26 01:00
    s.js("""() => {
      const items=[{id:'today',name:'北京时间今天',sign_start:'2026-10-26 22:00:00',
        act_start:'2026-10-28 10:00:00',act_end:'2026-10-28 11:00:00'},
        {id:'tomorrow',name:'北京时间明天',sign_start:'2026-10-27 01:00:00',
        act_start:'2026-10-29 10:00:00',act_end:'2026-10-29 11:00:00'}];
      bumpRoundGeneration('list'); S.round.list='qa-timezone';
      window.__QA_EMIT('activities',{request_id:'qa-timezone',ok:true,items});
    }""")
    s.page.locator("#time").select_option("today")
    s.wait("document.querySelectorAll('#rows .row').length === 1")
    s.check("今天按北京日期过滤", s.page.locator("#rows .row").first.get_attribute("data-id") == "today")
    s.check_clock_text("非北京OS时区仍显示北京时间")
    s.shot("today-filter")


scenario("today-filter-utc", "UTC系统时区下的北京今天", (1120, 800), timezone="UTC")(_today_in_os_timezone)
scenario("today-filter-los-angeles", "洛杉矶系统时区下的北京今天", (1120, 800), timezone="America/Los_Angeles")(_today_in_os_timezone)


@scenario("manual-clock-failure-same-generation", "校准失败及时收尾并保留可用旧时间", (1120, 800))
def _(s: Session):
    s.wait_business_ready()
    s.pointer("#clock", "open-clock-before-retry")
    old_gen = s.js("S.businessClock.gen")
    old_round = s.js("S.businessClock.syncRound")
    s.set_state(clockSyncFails=True)
    s.pointer("#clock-pop-sync", "retry-with-valid-anchor")
    s.wait("S.businessClock.syncState === 'idle' && S.businessClock.syncError.includes('未成功')", 4000)
    s.check("失败不伪造授时代次", s.js("S.businessClock.gen") == old_gen)
    s.check("失败仍有本次尝试编号", s.js("S.businessClock.syncRound") == old_round + 1)
    s.check_contains("#clock-pop-feedback", "仍使用上次校准结果")
    s.check("仍可读取有效旧时间", s.js("businessNowMs() !== null"))
    s.check("不占用报名结论", s.js("document.querySelector('#result').hidden"))
    s.page.wait_for_timeout(250)   # 覆盖结果早于 sync_clock ack：迟到ack不重启等待
    s.check("迟到回执不复活校准等待", s.js("S.businessClock.syncState === 'idle'"))
    s.shot("same-generation-failure")
    s.set_state(clockSyncFails=False)
    s.pointer("#clock-pop-sync", "retry-after-failure")
    s.wait("S.businessClock.gen > %d && S.businessClock.syncState === 'idle'" % old_gen)
    s.check("新成功清除旧错误", s.js("S.businessClock.syncError === ''"))
    s.check_clock_text("恢复成功后北京时间准确")
    s.shot("retry-success")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="本机 UI 离线验收")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--ui-root", default=None,
                        help="复现/对照用的 UI 目录（默认读生产 local/ui）")
    parser.add_argument("--port", type=int, default=8382)
    parser.add_argument("--evidence-root", default="")
    parser.add_argument("--run", action="store_true", help="真正启动浏览器执行")
    parser.add_argument("--scenario", default="", help="只跑一个场景")
    args = parser.parse_args(argv)

    if args.host not in ("127.0.0.1", "localhost"):
        print("[X] 只允许绑定回环地址，收到 %r" % args.host)
        return 2
    if not (UI / "index.html").exists() or not BRIDGE.exists():
        print("[X] 缺少 UI 文件或测试桥：%s" % BRIDGE)
        return 2
    evidence = Path(args.evidence_root or (ROOT / "dist" / "qa-evidence"))
    evidence.mkdir(parents=True, exist_ok=True)

    try:
        server = QaServer(args.host, args.port, ui_root=args.ui_root)
    except OSError as exc:
        print("[X] 端口 %d 无法绑定（可能被占用）：%s" % (args.port, exc))
        return 2
    server.start()
    print("[i] QA 服务：%s（证据目录 %s）" % (server.base, evidence))
    try:
        if not args.run:
            print("[i] 未传 --run：只校验服务与资源，不启动浏览器。")
            return 0
        try:
            report = run_scenarios(server.base, evidence, args.scenario or None)
        except ImportError as exc:
            print("[blocked] 没有 Playwright：%s" % exc)
            return 3
        report_path = evidence / "qa-report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        print("[i] 报告：%s" % report_path.resolve())
        if report.get("blocked"):
            print("[blocked] %s" % report["blocked"])
            return 3
        passed = [item for item in report["scenarios"] if item.get("ok")]
        print("[i] 场景 %d/%d 通过，截图 %d 张"
              % (len(passed), len(report["scenarios"]),
                 sum(len(item["screenshots"]) for item in report["scenarios"])))
        if report["failures"]:
            for line in report["failures"]:
                print("[X] %s" % line)
            return 1
        print("[OK] 全部场景通过")
        return 0
    finally:
        server.stop()


if __name__ == "__main__":
    sys.exit(main())
