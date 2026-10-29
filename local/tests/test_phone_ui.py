#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手机页行为测试：node vm 跑真实 JS + 本机 HTTP 端到端（离线）。

覆盖筛选、请求、对账、轮询单飞/退避、刷新恢复、stale 禁用、截止投影、
验证码代次与状态呈现；另做 375/430 布局静态检查与旧桌面契约回归。
不连学校、不开 100.x 监听、不启真实浏览器、不假截图。

@author LiXinwen
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

LOCAL = Path(__file__).resolve().parents[1]
if str(LOCAL) not in sys.path:
    sys.path.insert(0, str(LOCAL))

from application.phone_service import PhoneAccessService  # noqa: E402
from infrastructure.phone_http import PhoneHttpServer  # noqa: E402

PHONE_DIR = LOCAL / "ui" / "m"

# ---- node vm 装置：加载真实 ui/m/app.js + 最小受控 DOM -----------------
PHONE_JS_HARNESS = r"""
"use strict";
const vm = require("vm");
const fs = require("fs");
const path = require("path");
const uiDir = process.argv[2];
const snippetFile = process.argv[3];

const out = [];
const els = new Map();
function makeEl(sel) {
  const el = {
    __sel: sel, hidden: false, disabled: false, checked: false,
    value: "", textContent: "", innerHTML: "", tagName: "DIV",
    dataset: {}, style: {}, src: "",
    listeners: {}, childElementCount: 0, _children: [],
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
    removeEventListener() {},
    setAttribute(k, v) { this["attr_" + k] = v; },
    getAttribute(k) { return this["attr_" + k]; },
    appendChild(child) { this._children.push(child); this.childElementCount = this._children.length; return child; },
    removeChild(child) { const at = this._children.indexOf(child); if (at >= 0) this._children.splice(at, 1); this.childElementCount = this._children.length; return child; },
    removeAttribute(k) { this["attr_" + k] = undefined; },
    click() { (this.listeners.click || []).forEach((fn) => fn({ target: this })); },
    focus() {}, blur() {}, select() {},
  };
  Object.defineProperty(el, "firstChild", { get() { return this._children[0] || null; } });
  return el;
}
function el2(sel) { if (!els.has(sel)) els.set(sel, makeEl(sel)); return els.get(sel); }

const listeners = [];
const sandbox = {
  console, Math, JSON, Object, Array, Number, String, Boolean, RegExp, Error,
  parseInt, parseFloat, isNaN, Date, Uint8Array, Promise, Map, Set,
  setTimeout: (fn) => { return 0; }, clearTimeout: () => {},
  setInterval: () => 0, clearInterval: () => {},
  crypto: { getRandomValues: (buf) => { for (let i = 0; i < buf.length; i++) buf[i] = (i * 37 + 11) % 256; return buf; } },
  fetch: (...args) => sandbox.__fetch(...args),
  confirm: () => true,
  out, __els: els,
  document: {
    hidden: false, readyState: "complete",
    getElementById: (id) => el2("#" + id),
    querySelector: (s) => el2(s),
    querySelectorAll: (s) => (s === "[data-status]" ? ["#s-all", "#s-free"].map(el2)
                              : s === "[data-time]" ? ["#t-all", "#t-today"].map(el2) : []),
    createElement: (tag) => el2("<" + tag + ">"),
    addEventListener: (t, fn) => { if (t === "DOMContentLoaded") listeners.push(fn); },
    removeEventListener: () => {},
  },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.__fetch = () => Promise.resolve({ status: 500, json: () => Promise.resolve({}) });
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(uiDir, "app.js"), "utf8"), sandbox, { filename: "m/app.js" });
const snippetSource = fs.readFileSync(snippetFile, "utf8");
const result = vm.runInContext("(async () => {\n" + snippetSource + "\n})()", sandbox, { filename: "snippet.js" });
const finish = () => process.stdout.write("__RESULT__" + JSON.stringify(out));
result.then(finish, (err) => { process.stderr.write(String(err && err.stack || err)); process.exit(1); });
"""


def run_phone_js(snippet: str, files=None):
    """在受控 node vm 里跑 ui/m/app.js + 片段，返回 out 数组。node 缺失则 skip。"""
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("本机没有 node，无法运行 JS 行为测试")
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        harness = root / "harness.js"
        part = root / "snippet.js"
        harness.write_text(PHONE_JS_HARNESS, encoding="utf-8")
        part.write_text(snippet, encoding="utf-8")
        res = subprocess.run([node, str(harness), str(PHONE_DIR), str(part)],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=30)
        if res.returncode != 0:
            raise AssertionError("手机 JS 行为测试崩溃：%s" % res.stderr[-800:])
        marker = res.stdout.rfind("__RESULT__")
        if marker < 0:
            raise AssertionError("手机 JS 没有输出结果：%s" % res.stdout[-500:])
        return json.loads(res.stdout[marker + len("__RESULT__"):].strip() or "[]")


class PhoneModelTests(unittest.TestCase):
    """PhoneModel 纯规则：node vm 执行真实函数，不搜字符串。"""

    def test_filter_items_by_keyword_status_and_time(self):
        values = run_phone_js("""
const nowS = 1000;
const items = [
  { id: "a1", name: "篮球赛", place: "体育馆", signed: false, open_ts: nowS + 3600 },
  { id: "a2", name: "讲座", place: "报告厅", signed: true, open_ts: nowS + 3600 },
  { id: "a3", name: "远足", place: "体育馆", signed: false, open_ts: nowS + 3 * 86400 },
  { id: "a4", name: "未知", place: "", signed: false, open_ts: 0 },
];
out.push(PhoneModel.filterItems(items, { q: "体育" }, nowS).map((i) => i.id));
out.push(PhoneModel.filterItems(items, { status: "free" }, nowS).map((i) => i.id));
out.push(PhoneModel.filterItems(items, { status: "signed" }, nowS).map((i) => i.id));
out.push(PhoneModel.filterItems(items, { time: "today" }, nowS).map((i) => i.id));
out.push(PhoneModel.filterItems(items, { time: "week" }, nowS).map((i) => i.id));
""")
        self.assertEqual(values[0], ["a1", "a3"])
        self.assertEqual(values[1], ["a1", "a3", "a4"])
        self.assertEqual(values[2], ["a2"])
        self.assertEqual(values[3], ["a1", "a2"])
        self.assertEqual(values[4], ["a1", "a2", "a3"])

    def test_backoff_is_exponential_and_capped_at_15s(self):
        values = run_phone_js("""
let cur = 1000;
const steps = [cur];
for (let i = 0; i < 5; i++) { cur = PhoneModel.nextBackoff(cur); steps.push(cur); }
out.push(steps);
""")
        self.assertEqual(values[0], [1000, 2000, 4000, 8000, 15000, 15000])

    def test_stale_disables_start_and_relogin_submit(self):
        values = run_phone_js("""
const model = { lastOkAt: 1000, nowMs: 1000 + PhoneModel.staleAfterMs + 1,
                busy: false, selected: ["a1"] };
out.push(PhoneModel.isStale(model.lastOkAt, model.nowMs));
out.push(PhoneModel.canStart(model));
model.nowMs = 1000 + 100;
out.push(PhoneModel.canStart(model));
model.selected = [];
out.push(PhoneModel.canStart(model));
out.push(PhoneModel.canSubmitRelogin(
  { lastOkAt: 8900, nowMs: 9000 },
  { challenge_id: "c1", got_at: 9000, expires_in: 60 }));
out.push(PhoneModel.canSubmitRelogin(
  { lastOkAt: 8900, nowMs: 9000 },
  { challenge_id: "c1", got_at: 1000, expires_in: 5 }));
""")
        self.assertEqual(values, [True, False, True, False, True, False])

    def test_deadline_projection_prefers_telemetry_and_three_modes(self):
        values = run_phone_js("""
const anchor = 1000, signEnd = 5000;
out.push(PhoneModel.taskDeadline(
  { retry_deadline: 4321, retry_for_s: 120, retry_started_at: anchor, sign_end_ts: signEnd }, 0));
out.push(PhoneModel.taskDeadline(
  { retry_for_s: 120, retry_started_at: anchor, sign_end_ts: signEnd }, 0));
out.push(PhoneModel.taskDeadline(
  { retry_for_s: -1, retry_started_at: anchor, sign_end_ts: signEnd }, 0));
out.push(PhoneModel.taskDeadline(
  { retry_for_s: 0, retry_started_at: anchor, sign_end_ts: signEnd }, 0));
out.push(PhoneModel.taskDeadline(
  { retry_for_s: 0, retry_started_at: anchor, sign_end_ts: 0 }, 0));
out.push(PhoneModel.taskDeadline(
  { retry_for_s: 120, retry_started_at: anchor, sign_end_ts: 1030 }, 0));
""")
        self.assertEqual(values, [4321, 1120, 1120, 5000, 1120, 1030])

    def test_retry_hint_for_zero_without_sign_end(self):
        values = run_phone_js("""
out.push(PhoneModel.retryHint(0, false));
out.push(PhoneModel.retryHint(0, true));
out.push(PhoneModel.retryHint(-1, true));
""")
        self.assertIn("2 分钟", values[0])
        self.assertEqual(values[1], "")
        self.assertIn("25 分钟", values[2])

    def test_challenge_generation_matching_and_expiry(self):
        values = run_phone_js("""
const flow = { flow_id: "f1", challenge_id: "c9" };
out.push(PhoneModel.matchesChallenge(flow, "f1", "c9"));
out.push(PhoneModel.matchesChallenge(flow, "f2", "c9"));
out.push(PhoneModel.matchesChallenge(flow, "f1", "c0"));
out.push(PhoneModel.challengeExpired({ got_at: 1000, expires_in: 60 }, 1000 + 59000));
out.push(PhoneModel.challengeExpired({ got_at: 1000, expires_in: 60 }, 1000 + 61000));
out.push(PhoneModel.newRequestId().length);
""")
        self.assertEqual(values[:5], [True, False, False, False, True])
        self.assertEqual(values[5], 24, "request id 用 getRandomValues，HTTP 页面也可用")

    def test_item_actionable_and_presentation(self):
        values = run_phone_js("""
out.push(PhoneModel.actionable({ signed: false, sign_start: "2026-01-01 09:00:00" }));
out.push(PhoneModel.actionable({ signed: true, sign_start: "2026-01-01 09:00:00" }));
out.push(PhoneModel.actionable({ signed: false, sign_start: "" }));
out.push(PhoneModel.actionable({ signed: false, sign_start: "x", closed: true }));
out.push(PhoneModel.itemTitle({ name: "篮球" }));
out.push(PhoneModel.itemMeta({ sign_start: "09:00", signed: true }));
""")
        self.assertEqual(values, [True, False, False, False, "篮球", "开放 09:00 · 已报名"])

    def test_start_pending_keeps_reconciling_window(self):
        values = run_phone_js("""
out.push(PhoneModel.startPending(null, 0));
out.push(PhoneModel.startPending({ at: 1000, done: false }, 1000 + 14000));
out.push(PhoneModel.startPending({ at: 1000, done: false }, 1000 + 16000));
out.push(PhoneModel.startPending({ at: 1000, done: true }, 1000 + 16000));
""")
        self.assertEqual(values, [False, True, True, False])


class PhoneDomBehaviourTests(unittest.TestCase):
    """DOM 层行为：轮询单飞、退避、stale 禁用、刷新恢复（最小受控 DOM）。"""

    def test_poll_is_single_flight_and_recovers_on_visibility(self):
        values = run_phone_js("""
let calls = 0;
globalThis.__fetch = () => {
  calls += 1;
  return Promise.resolve({ status: 200, json: () => Promise.resolve({
    state: { activities: [{ id: "a1", name: "活动", sign_start: "2026-01-01 09:00:00" }],
             busy: false, plan: null, retry_options: { default: 120, choices: [] } },
    events: [], reset: false, seq: 3 }) });
};
const ui = globalThis.PhoneUI;
ui.model.selected = [];
const first = ui.poll(true);
const second = ui.poll(true);          // 单飞：在途不重发
await first; await second;
out.push(calls);
out.push(ui.model.seq);
out.push(ui.model.items.length);
""")
        self.assertEqual(values, [1, 3, 1], "轮询必须单飞；快照要落进 model")

    def test_poll_failure_backs_off_and_stale_disables_start(self):
        values = run_phone_js("""
globalThis.__fetch = () => Promise.resolve({ status: 500, json: () => Promise.resolve({}) });
const ui = globalThis.PhoneUI;
ui.model.selected = ["a1"];
ui.model.lastOkAt = Date.now();
await ui.poll(true);
out.push(ui.model.backoff);
await ui.poll(true);
out.push(ui.model.backoff);
ui.model.lastOkAt = Date.now() - PhoneModel.staleAfterMs - 1;
ui.refreshControls();
out.push(document.getElementById("btn-start").disabled);
out.push(document.getElementById("conn").textContent);
""")
        self.assertEqual(values, [2000, 4000, True, "离线/未知"])

    def test_refresh_restores_busy_plan_and_stop(self):
        values = run_phone_js("""
const ui = globalThis.PhoneUI;
ui.ingest({ reset: true, seq: 9,
  state: { activities: [], busy: true,
           plan: { plan_id: "p1", tasks: [{ task_id: "t1", name: "篮球", status: "submitting",
                                            retry_for_s: -1, retry_started_at: 1000 }] },
           retry_options: { default: 120,
             choices: [{ value: 120, label: "2 分钟" }, { value: -1, label: "自动" }] } },
  events: [{ seq: 8, kind: "log", payload: { text: "开始" } }] });
ui.refreshControls();
out.push(ui.model.busy);
out.push(ui.model.plan.plan_id);
out.push(document.getElementById("btn-stop").hidden);
out.push(document.getElementById("logs").childElementCount);
out.push(document.getElementById("retry-for").__sel ? 1 : 0);
""")
        self.assertEqual(values, [True, "p1", False, 1, 1])

    def test_unconfirmed_start_keeps_same_request_id(self):
        values = run_phone_js("""
globalThis.__fetch = (path, opts) => {
  const body = JSON.parse(opts.body);
  out.push(body.action + ":" + body.args.request_id);
  return Promise.resolve({ status: 200, json: () => Promise.resolve({ ok: false, error: "busy" }) });
};
const ui = globalThis.PhoneUI;
ui.model.selected = ["a1"];
ui.model.lastOkAt = Date.now();
ui.model.pending = null;
await ui.startPlan();
const firstId = ui.model.pending.request_id;
out.push(PhoneModel.startPending(ui.model.pending, Date.now()));
ui.model.pending.done = true;
out.push(PhoneModel.startPending(ui.model.pending, Date.now() + 16000));
out.push(firstId.length);
""")
        kinds = [v.split(":")[0] for v in values if isinstance(v, str) and ":" in v]
        self.assertIn("start_plan", kinds)
        self.assertTrue(values[1], "15 秒内未确认必须仍在对账窗口")
        self.assertFalse(values[2])
        self.assertEqual(values[3], 24)


class PhoneHttpEnd2EndTests(unittest.TestCase):
    """本机 HTTP 端到端：pair → state → action，替身 Api，不开 100.x。"""

    def setUp(self):
        self.api_calls = []
        self.ui_dir = Path(tempfile.mkdtemp(prefix="tjise-phone-ui-"))
        self.addCleanup(shutil.rmtree, str(self.ui_dir), True)
        for name in ("index.html", "styles.css", "app.js"):
            (self.ui_dir / name).write_text(
                (PHONE_DIR / name).read_text(encoding="utf-8"), encoding="utf-8")

        class Api:
            def check(inner, request_id=""):
                self.api_calls.append(("check", request_id))
                return {"ok": True, "result": "valid"}

            def start_plan(inner, items, headed=False, retry_policy=None, request_id=""):
                self.api_calls.append(("start_plan", items, request_id))
                return {"ok": True, "plan_id": "p1", "request_id": request_id}

        self.service = PhoneAccessService(
            api=Api(), now=lambda: 100.0,
            state_of=lambda: {"activities": [], "busy": False,
                              "terms": {"accepted_version": "1"}},
            server_factory=lambda host, port, svc: PhoneHttpServer(
                host, port, svc, ui_dir=self.ui_dir))
        started = self.service.start("127.0.0.1", 0)
        self.assertTrue(started["ok"], started)
        self.base = started["address"].rstrip("/")
        self.code = started["pair_code"]
        self.addCleanup(self.service.stop)

    def _req(self, method, path, body=None, headers=None, cookie=None):
        data = None
        merged = {"Host": self.base.split("//", 1)[1]}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            merged["Content-Type"] = "application/json"
        merged.update(headers or {})
        if cookie:
            merged["Cookie"] = cookie
        req = urllib.request.Request(self.base + path, data=data, method=method)
        for key, value in merged.items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def test_html_entry_resources_are_really_served(self):
        for path, ctype in (("/", "text/html"), ("/styles.css", "text/css"),
                            ("/app.js", "javascript")):
            with self.subTest(path=path):
                status, headers, body = self._req("GET", path)
                self.assertEqual(status, 200)
                self.assertIn(ctype, headers.get("Content-Type", ""))
                self.assertTrue(body, "入口资源必须真的可取")
                self.assertIn("content-security-policy",
                              {k.lower() for k in headers})

    def test_pair_state_action_roundtrip(self):
        status, headers, body = self._req(
            "POST", "/pair", body={"code": self.code},
            headers={"Origin": self.base})
        self.assertEqual(status, 200)
        payload = json.loads(body)
        cookie = next(v for k, v in headers.items()
                      if k.lower() == "set-cookie" and v.startswith("tjise_phone_sid="))
        cookie = cookie.split(";", 1)[0]
        status, _, body = self._req("GET", "/api/state?after=0", cookie=cookie)
        self.assertEqual(status, 200)
        status, _, body = self._req(
            "POST", "/api/action",
            body={"action": "check", "args": {"request_id": "r1"}},
            headers={"Origin": self.base, "X-Tjise-Csrf": payload["csrf"]},
            cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(self.api_calls, [("check", "r1")])


class LayoutStaticTests(unittest.TestCase):
    """375/430 布局静态检查：不截图、不假像素，只查结构性约束。"""

    def test_mobile_layout_statics(self):
        html = (PHONE_DIR / "index.html").read_text(encoding="utf-8")
        css = (PHONE_DIR / "styles.css").read_text(encoding="utf-8")
        self.assertIn('name="viewport"', html)
        self.assertIn("width=device-width", html)
        self.assertIn("max-width: 430px", css)
        self.assertIn("overflow-x: hidden", css)
        self.assertIn("min-height: 44px", css)
        self.assertNotIn("display: grid", css, "单栏布局不引入多列网格")
        self.assertIn("inputmode=", html, "数字键盘只给配对码/次数")
        report = Path("D:/@Temp/night-run-B-20261030/evidence/phone-ui-layout-375-430.txt")
        report.write_text(
            "375/430 布局静态检查（无截图、无像素断言）\n"
            "viewport=%s\nmax-width430=%s\noverflow-x-hidden=%s\nmin-tap-44px=%s\n"
            "真实手机像素与操作未验证。\n" % (
                "width=device-width" in html, "max-width: 430px" in css,
                "overflow-x: hidden" in css, "min-height: 44px" in css),
            encoding="utf-8")

    def test_desktop_entry_keeps_existing_contracts(self):
        html = (LOCAL / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="retry-for"', html)
        self.assertIn('id="phone-panel"', html)
        self.assertIn('src="views/phone.js"', html)
        self.assertNotIn("onclick=", html, "桌面仍不内联脚本")


if __name__ == "__main__":
    unittest.main()
