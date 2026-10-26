#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""界面可靠性的行为回归：驱动生产 app.js 的事件入口，不调用内部 paint 函数。

覆盖：
    R08 每轮 check 用结构化 clock 替换旧状态（warn → ok → unknown）
    R11 旧轮迟到事件不得覆盖新轮；开始请求超时只对账、不重发
    R12 空闲时标题/快捷选择/LED 用同一个 nextOpen 目标
    R13 次数按「已提交 N/M 次」显示，0 明确「不限次数」
    R20 演示模式必须自己显形，不伪装登录有效

每个用例都在独立的 node vm 上下文里跑完整 app.js，入口是真实事件回调
（window.onBackend.*）与真实按钮动作，不直接改写渲染结果。

@author LiXinwen
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from test_ui_contract import run_js  # noqa: E402


class ClockRoundTests(unittest.TestCase):
    """R08：时钟结论逐轮替换，告警能清、未知不伪装。"""

    def test_warn_then_ok_then_unknown(self):
        res = run_js(r"""
window.onBackend.check({ ok: true, result: "valid",
  clock: { source: "http_date", state: "warn", offset_s: 8,
           uncertainty_s: 1.1, checked_at: 1793016000 } });
const warn = { state: S.clock.state, offset: S.clock.offset };
window.onBackend.check({ ok: true, result: "valid",
  clock: { source: "http_date", state: "ok", offset_s: 0,
           uncertainty_s: 1.1, checked_at: 1793016100 } });
const ok = { state: S.clock.state, offset: S.clock.offset };
window.onBackend.check({ ok: true, result: "valid" });
const unknown = { state: S.clock.state, detail: S.clock.detail };
/* 打开时间详情：学校粗测与 NTP 状态都只在详情里出现 */
toggleClockPop();
const popText = document.querySelector("#clock-pop").innerHTML;
out.push({ warn, ok, unknown, popText });
""")
        data = res[0]
        self.assertEqual(data["warn"]["state"], "warn")
        self.assertEqual(data["warn"]["offset"], 8)
        self.assertIn("粗测", data["popText"], "学校粗测结论只进时间详情")
        self.assertEqual(data["ok"]["state"], "ok")
        self.assertEqual(data["unknown"]["state"], "unknown",
                         "没有结构化结论时必须落未知，不能保留上一轮 warn")
        self.assertNotIn("可能偏", data["popText"],
                         "时间详情不得把上一轮粗测当成当前结论")

    def test_unknown_payload_clears_stale_warn(self):
        res = run_js(r"""
window.onBackend.check({ ok: true, result: "valid",
  clock: { source: "http_date", state: "warn", offset_s: 12,
           uncertainty_s: 1.0, checked_at: 1793016000 } });
window.onBackend.check({ ok: false, result: "unknown" });   // 旧载荷，没有 clock
out.push({ state: S.clock.state, offset: S.clock.offset });
""")
        data = res[0]
        self.assertEqual(data["state"], "unknown")
        self.assertIsNone(data["offset"], "旧结论不能当实时测量继续使用")


class StaleRoundTests(unittest.TestCase):
    """R11：旧轮迟到事件不得覆盖新轮。"""

    def test_stale_check_and_list_events_are_dropped(self):
        res = run_js(r"""
S.round.check = "check-new";
S.round.list = "list-new";
S.login.checking = true;
S.listState = "loading";
window.onBackend.check({ request_id: "check-old", ok: true, result: "valid" });
const afterCheck = { checking: S.login.checking, result: S.login.result };
window.onBackend.activities({ request_id: "list-old", ok: true,
  items: [{ id: "x", name: "旧轮活动", sign_start: "2026-10-26 20:00:00",
            sign_end: "2026-10-26 21:00:00", act_start: "2026-10-27 09:00:00",
            act_end: "2026-10-27 11:00:00", signed: false }] });
const afterList = { state: S.listState, count: S.items.length };
window.onBackend.check({ request_id: "check-new", ok: true, result: "valid" });
window.onBackend.activities({ request_id: "list-new", ok: true,
  items: [{ id: "y", name: "新轮活动", sign_start: "2026-10-26 20:00:00",
            sign_end: "2026-10-26 21:00:00", act_start: "2026-10-27 09:00:00",
            act_end: "2026-10-27 11:00:00", signed: false }] });
const settled = { checking: S.login.checking, state: S.listState,
                  names: S.items.map((item) => item.name) };
out.push({ afterCheck, afterList, settled });
""")
        data = res[0]
        self.assertTrue(data["afterCheck"]["checking"], "旧轮 check 不得结束新轮的检查中")
        self.assertIsNone(data["afterCheck"]["result"])
        self.assertEqual(data["afterList"]["count"], 0, "旧轮列表不得写入")
        self.assertEqual(data["afterList"]["state"], "loading")
        self.assertFalse(data["settled"]["checking"])
        self.assertEqual(data["settled"]["state"], "ready")
        self.assertEqual(data["settled"]["names"], ["新轮活动"])


class NextOpenConsistencyTests(unittest.TestCase):
    """R12：有已开放场次时，空闲板面的目标与 LED 必须指向同一场。"""

    def test_open_target_wins_over_future_countdown(self):
        res = run_js(r"""
/* 样本的开放/截止是固定日期，当前时刻也必须固定，不能过23点就让样本自动截止。 */
setBusinessNow(toTs("2026-10-26 20:00:00") * 1000);
window.LED_UI = { renderLed: (el, text) => { S.__led = text; } };
const openItem = { id: "open", name: "已开放活动", sign_start: "2026-10-26 09:00:00",
  sign_end: "2026-10-26 23:00:00", act_start: "2026-10-27 09:00:00",
  act_end: "2026-10-27 11:00:00", signed: false, checkin_required: null,
  checkout_required: null };
const futureItem = { id: "future", name: "未来活动", sign_start: "2026-10-27 20:00:00",
  sign_end: "2026-10-27 21:00:00", act_start: "2026-10-28 09:00:00",
  act_end: "2026-10-28 11:00:00", signed: false, checkin_required: null,
  checkout_required: null };
S.items = [openItem, futureItem];
S.byId = new Map([["open", openItem], ["future", futureItem]]);
const next = nextOpen();
const target = boardTargetItem();
paintLed();
out.push({ best: next.best, bestId: next.bestId, openId: next.openId,
           target: target && target.id,
           led: S.__led,
           metric: document.querySelector("#led-label b").textContent });
""")
        data = res[0]
        self.assertIsNone(data["best"], "已开放时不得再给未来场倒计时")
        self.assertEqual(data["bestId"], "open")
        self.assertEqual(data["target"], "open", "标题目标与快捷选择必须同源")
        self.assertEqual(data["led"], "00:00:00", "已开放不得显示未来场倒计时")
        self.assertEqual(data["metric"], "现在")


class AttemptsDisplayTests(unittest.TestCase):
    """R13：有限次数显示 N/M，0 明确不限。"""

    def test_limited_and_unlimited_copy(self):
        res = run_js(r"""
const limited = submittingDetail({ status: "submitting", attempts: 1, max_attempts: 3 });
const unlimited = submittingDetail({ status: "submitting", attempts: 2, max_attempts: 0 });
const before = submittingDetail({ status: "submitting", attempts: 0, max_attempts: 5 });
out.push({ limited, unlimited, before });
""")
        data = res[0]
        self.assertIn("已提交 1/3 次", data["limited"])
        self.assertIn("已提交 2 次（不限次数）", data["unlimited"])
        self.assertIn("上限 5 次", data["before"])
        self.assertNotIn("第 ", data["limited"], "不能只写「第 N 次」，用户看不到上限")


class StartReconcileTests(unittest.TestCase):
    """R11：开始请求失联时只对账，不重发；已接纳要恢复停止入口。"""

    def test_accepted_reconcile_restores_plan(self):
        res = run_js(r"""
window.pywebview = { api: {
  start_request_state: () => Promise.resolve({ ok: true, state: "accepted", plan_id: "p1" }),
  bootstrap: () => Promise.resolve({ plan: { plan_id: "p1", status: "running", tasks: [] } }),
} };
const flush = async () => { for (let i = 0; i < 8; i += 1) await Promise.resolve(); };
S.startRequest = { id: "rid", state: "submitting", timer: null, reconcile: 0 };
S.starting = true;
reconcileStart("rid");
await flush();
out.push({ planId: S.plan.id, busy: S.busy, starting: S.starting,
           state: S.startRequest.state,
           stopDisabled: document.querySelector("#btn-stop").disabled });
""")
        data = res[0]
        self.assertEqual(data["planId"], "p1")
        self.assertTrue(data["busy"], "已接纳必须回到运行态")
        self.assertFalse(data["starting"], "对账成功后释放开始标志")
        self.assertEqual(data["state"], "accepted")
        self.assertFalse(data["stopDisabled"], "必须有停止入口")

    def test_unknown_reconcile_keeps_start_blocked(self):
        res = run_js(r"""
window.pywebview = { api: {
  start_request_state: () => Promise.resolve({ ok: true, state: "unknown" }),
} };
const flush = async () => { for (let i = 0; i < 8; i += 1) await Promise.resolve(); };
S.startRequest = { id: "rid", state: "submitting", timer: null, reconcile: 0 };
S.starting = true;
reconcileStart("rid");
await flush();
out.push({ starting: S.starting, state: S.startRequest.state,
           title: document.querySelector("#result").innerHTML,
           disabled: document.querySelector("#btn-plan").disabled });
""")
        data = res[0]
        self.assertTrue(data["starting"], "没有结论时不得开放第二次开始")
        self.assertEqual(data["state"], "unknown")
        self.assertIn("结果未确认", data["title"])
        self.assertTrue(data["disabled"])


class DemoModeTests(unittest.TestCase):
    """R20：演示模式显式标识，不伪装成登录有效。"""

    def test_demo_pill_is_explicit(self):
        res = run_js(r"""
S.demo = true;
setLogin(null, null, null);
paintLogin();
out.push({ text: document.querySelector("#pill-text").textContent,
           state: document.querySelector("#pill").dataset.state });
""")
        data = res[0]
        self.assertEqual(data["text"], "演示模式")
        self.assertEqual(data["state"], "demo")


class LoginPresentTests(unittest.TestCase):
    """C07 回归：session_present 的 null 不能被 !! 成「首次未登录」。"""

    def test_null_is_unknown_not_false(self):
        res = run_js(r"""
out.push({
  demo: loginPresent({ session_present: null }, true),
  nullPlain: loginPresent({ session_present: null }, false),
  missing: loginPresent({}, false),
  yes: loginPresent({ session_present: true }, false),
  no: loginPresent({ session_present: false }, false),
});
""")
        data = res[0]
        # undefined 经 JSON 序列化会丢键：用「键不存在」表达未知
        self.assertNotIn("demo", data, "演示模式的会话必须是未知，不能判成未登录")
        self.assertNotIn("nullPlain", data)
        self.assertNotIn("missing", data)
        self.assertTrue(data["yes"])
        self.assertFalse(data["no"])


class ReconcileTerminalTests(unittest.TestCase):
    """C05：15 秒内就结束的计划不得被对账复活成运行中。"""

    def test_finished_plan_restores_terminal_not_busy(self):
        res = run_js(r"""
window.pywebview = { api: {
  start_request_state: () => Promise.resolve({
    ok: true, state: "accepted", plan_id: "p1", running: false, finished: true,
    snapshot: { status: "completed", summary: { total: 1, counts: { success: 1 } },
                tasks: [{ task_id: "t1", activity_id: "a1", name: "活动",
                          status: "success", attempts: 1 }] },
  }),
  bootstrap: () => Promise.resolve({}),
} };
S.startRequest = { id: "rid", state: "submitting", timer: null, reconcile: 0 };
S.starting = true;
reconcileStart("rid");
for (let i = 0; i < 8; i += 1) await Promise.resolve();
out.push({ busy: S.busy, starting: S.starting, status: S.plan.status,
           tasks: S.plan.tasks.size,
           title: document.querySelector("#result").innerHTML });
""")
        data = res[0]
        self.assertFalse(data["busy"], "已结束的计划不得保持运行态")
        self.assertFalse(data["starting"])
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["tasks"], 1)
        self.assertIn("完成", data["title"])


class SkippedRefreshTests(unittest.TestCase):
    """C06：去重跳过要接续原 ID，并把已完成结果直接落地。"""
    def test_skipped_check_rebinds_original_id(self):
        res = run_js(r"""
S.round.check = "new";
S.login.checking = true;
settleSynchronous({ ok: true, queued: true, skipped: "已在检查登录态",
                    request_id: "orig" }, "check");
out.push({ round: S.round.check, checking: S.login.checking });
""")
        data = res[0]
        self.assertEqual(data["round"], "orig", "必须接续原请求 ID")
        self.assertTrue(data["checking"], "原请求还在途，继续等它")

    def test_skipped_with_finished_result_applies(self):
        res = run_js(r"""
S.round.check = "new";
S.login.checking = true;
settleSynchronous({ ok: true, skipped: "已在检查登录态", request_id: "orig",
  result: { request_id: "orig", ok: true, result: "valid", hours_left: 5,
            session_present: true } }, "check");
out.push({ round: S.round.check, checking: S.login.checking,
           result: S.login.result });
""")
        data = res[0]
        self.assertEqual(data["round"], "orig")
        self.assertFalse(data["checking"], "已完成的终态必须当场落地")
        self.assertEqual(data["result"], "valid")


SELECTED_SNIPPET = r"""
window.LED_UI = { renderLed: (el, text, tone) => { S.__led = [String(text), String(tone)]; } };
const __labels = [];
const __realLabel = paintLedLabel;
paintLedLabel = (prefix, value) => { __labels.push(String(prefix)); return __realLabel(prefix, value); };
window.onBackend.plan({ plan_id: "p1", status: "ready", tasks: [] });
"""


class UnknownOutcomeUITests(unittest.TestCase):
    """F04：未确认/鉴权未知贯穿任务卡、计划总结与大屏（与 stopReasonText 同源）。"""

    def _run(self, tasks, status, counts):
        body = SELECTED_SNIPPET
        for task in tasks:
            body += "window.onBackend.task(%s);\n" % _js(task)
        body += ("window.onBackend.plan_done({ plan_id: \"p1\", status: %s, "
                 "summary: { total: %d, counts: %s } });\n"
                 % (_js(status), len(tasks), _js(counts)))
        body += r"""
out.push({
  cards: document.querySelector("#plan-tasks").innerHTML,
  result: document.querySelector("#result").innerHTML,
  led: S.__led,
  labels: __labels,
});
"""
        return run_js(body)[0]

    def test_unconfirmed_is_pending_not_failure(self):
        data = self._run(
            [{"plan_id": "p1", "task_id": "t1", "activity_id": "a1", "name": "讲座",
              "sign_start": "2026-10-26 09:00:00", "sign_end": "2026-10-26 23:00:00",
              "status": "failed", "stop_reason": "unconfirmed", "attempts": 3,
              "reason": "本机监督到期，已收走本场进程"}],
            "failed", {"failed": 1})
        self.assertIn("待核对", data["cards"])
        self.assertIn("3 次", data["cards"])
        self.assertNotIn("没抢到", data["cards"])
        self.assertIn("待核对", data["result"])
        self.assertIn("没拿到确定回应", data["result"])
        self.assertIn("不会自动重发", data["result"])
        self.assertNotIn("没抢到", data["result"])
        self.assertNotIn("都没抢到", data["result"])
        self.assertTrue(any("1 场待核对" in text for text in data["labels"]),
                        "大屏必须与任务卡同一分类：%s" % data["labels"])
        self.assertEqual(data["led"], ["--:--:--", "off"], "待核对不得亮失败叉")

    def test_auth_unavailable_does_not_push_relogin(self):
        data = self._run(
            [{"plan_id": "p1", "task_id": "t1", "activity_id": "a1", "name": "讲座",
              "sign_start": "2026-10-26 09:00:00", "sign_end": "2026-10-26 23:00:00",
              "status": "failed", "stop_reason": "auth_unavailable", "attempts": 0,
              "reason": "接口异常：URLError"}],
            "failed", {"failed": 1})
        self.assertIn("本场没有提交", data["cards"])
        self.assertNotIn("没抢到", data["cards"])
        self.assertIn("未提交", data["result"])
        self.assertIn("登录状态无法确认", data["result"])
        self.assertNotIn("登录失效", data["result"])
        self.assertNotIn("请重新登录", data["result"])
        self.assertIn("检查网络", data["result"])
        self.assertEqual(data["led"], ["--:--:--", "off"])

    def test_mixed_success_and_unknown_is_partial(self):
        data = self._run(
            [{"plan_id": "p1", "task_id": "t1", "activity_id": "a1", "name": "成功场",
              "sign_start": "2026-10-26 09:00:00", "sign_end": "2026-10-26 23:00:00",
              "status": "success", "stop_reason": "success", "attempts": 1, "reason": "报名成功"},
             {"plan_id": "p1", "task_id": "t2", "activity_id": "a2", "name": "待核对场",
              "sign_start": "2026-10-26 09:00:00", "sign_end": "2026-10-26 23:00:00",
              "status": "failed", "stop_reason": "unconfirmed", "attempts": 2,
              "reason": "响应丢失"}],
            "partial", {"success": 1, "failed": 1})
        self.assertIn("待核对", data["result"])
        self.assertIn("已报名 1 场", data["result"])
        self.assertIn("待核对 1 场", data["result"])
        self.assertNotIn("都没抢到", data["result"])

    def test_confirmed_failure_and_unknown_are_counted_separately(self):
        data = self._run(
            [{"plan_id": "p1", "task_id": "t1", "activity_id": "a1", "name": "名额满",
              "sign_start": "2026-10-26 09:00:00", "sign_end": "2026-10-26 23:00:00",
              "status": "failed", "stop_reason": "max_attempts", "attempts": 5,
              "reason": "报名人数已满"},
             {"plan_id": "p1", "task_id": "t2", "activity_id": "a2", "name": "待核对场",
              "sign_start": "2026-10-26 09:00:00", "sign_end": "2026-10-26 23:00:00",
              "status": "failed", "stop_reason": "unconfirmed", "attempts": 1,
              "reason": "响应丢失"}],
            "failed", {"failed": 2})
        self.assertIn("报名未成功 1 场", data["result"])
        self.assertIn("待核对 1 场", data["result"])
        self.assertIn("待核对", data["result"])
        self.assertTrue(any("待核对" in text for text in data["labels"]))
        self.assertEqual(data["led"], ["--:--:--", "off"])

    def test_all_confirmed_failures_keep_definite_copy(self):
        data = self._run(
            [{"plan_id": "p1", "task_id": "t1", "activity_id": "a1", "name": "场一",
              "sign_start": "2026-10-26 09:00:00", "sign_end": "2026-10-26 23:00:00",
              "status": "failed", "stop_reason": "retry_duration", "attempts": 2,
              "reason": "名额已满"},
             {"plan_id": "p1", "task_id": "t2", "activity_id": "a2", "name": "场二",
              "sign_start": "2026-10-26 09:00:00", "sign_end": "2026-10-26 23:00:00",
              "status": "failed", "stop_reason": "sign_end", "attempts": 1,
              "reason": "报名已截止"}],
            "failed", {"failed": 2})
        self.assertIn("报名计划没有成功", data["result"])
        self.assertIn("报名未成功 2 场", data["result"])
        self.assertNotIn("待核对", data["result"])
        self.assertEqual(data["led"], ["✕", "red"], "全部确认失败才允许亮失败结论")

    def test_stopped_plan_keeps_its_own_copy(self):
        data = self._run(
            [{"plan_id": "p1", "task_id": "t1", "activity_id": "a1", "name": "场一",
              "sign_start": "2026-10-26 09:00:00", "sign_end": "2026-10-26 23:00:00",
              "status": "canceled", "stop_reason": "user_stopped", "attempts": 0,
              "reason": "用户取消了这一场"}],
            "stopped", {"canceled": 1})
        self.assertIn("已停止", data["result"])
        self.assertNotIn("待核对", data["result"])
        self.assertIn("保留原状态", data["result"])
        self.assertEqual(data["led"], ["--:--:--", "off"])

    def test_f5_restore_keeps_same_conclusion_and_attempts(self):
        res = run_js(r"""
window.LED_UI = { renderLed: (el, text, tone) => { S.__led = [String(text), String(tone)]; } };
ingestBootstrapPlan({ plan: { plan_id: "p9", status: "failed", tasks: [
  { task_id: "t1", activity_id: "a1", name: "待核对场",
    sign_start: "2026-10-26 09:00:00", sign_end: "2026-10-26 23:00:00",
    status: "failed", stop_reason: "unconfirmed", attempts: 4,
    reason: "响应丢失" } ] } });
const task = S.plan.tasks.get("t1");
out.push({ attempts: task.attempts, stop: task.stop_reason,
           cards: document.querySelector("#plan-tasks").innerHTML });
""")
        data = res[0]
        self.assertEqual(data["attempts"], 4, "恢复后次数不能归零")
        self.assertEqual(data["stop"], "unconfirmed")
        self.assertIn("4 次", data["cards"])
        self.assertIn("待核对", data["cards"])


def _js(value):
    import json
    return json.dumps(value, ensure_ascii=False)


TIMER_HARNESS = r"""
const __timers = [];
let __seq = 1;
setTimeout = (fn, ms) => { __timers.push({ id: __seq, fn: fn, ms: ms }); return __seq++; };
clearTimeout = (id) => {
  const at = __timers.findIndex((t) => t.id === id);
  if (at >= 0) __timers.splice(at, 1);
};
const __fire = (ms) => {
  const hit = __timers.find((t) => t.ms === ms);
  if (hit) { clearTimeout(hit.id); hit.fn(); }
  return !!hit;
};
const __flush = async (n = 6) => { for (let i = 0; i < n; i += 1) await Promise.resolve(); };
"""


class StartReconcileQueryTests(unittest.TestCase):
    """F05：对账自身有 5 秒期限；旧代次与旧请求的迟到回复不得落地。"""

    def test_never_settled_query_times_out_at_5s(self):
        res = run_js(TIMER_HARNESS + r"""
window.pywebview = { api: {
  start_request_state: () => new Promise(() => {}),
  bootstrap: () => Promise.resolve({}),
} };
S.startRequest = { id: "rid", state: "submitting", timer: null,
                   queryTimer: null, reconcile: 0, gen: 0 };
S.starting = true;
reconcileStart("rid");
const fired = __fire(5000);
out.push({ fired: fired, state: S.startRequest.state, starting: S.starting,
           html: document.querySelector("#result").innerHTML });
""")
        data = res[0]
        self.assertTrue(data["fired"], "5 秒期限必须真的挂上计时器")
        self.assertEqual(data["state"], "unknown")
        self.assertTrue(data["starting"], "对账超时也必须锁住重复启动")
        self.assertIn("结果未确认", data["html"])
        self.assertIn("5 秒", data["html"])
        self.assertIn("重新确认", data["html"])

    def test_reconfirm_is_read_only(self):
        res = run_js(TIMER_HARNESS + r"""
const calls = { start_plan: 0, query: 0 };
window.pywebview = { api: {
  start_request_state: () => { calls.query += 1; return new Promise(() => {}); },
  start_plan: () => { calls.start_plan += 1; return Promise.resolve({ ok: true }); },
  bootstrap: () => Promise.resolve({}),
} };
S.startRequest = { id: "rid", state: "submitting", timer: null,
                   queryTimer: null, reconcile: 0, gen: 0 };
S.starting = true;
reconcileStart("rid");
__fire(5000);
const btn = document.querySelector("#result").querySelector("#result-action");
btn.click();
out.push({ calls: calls, gen: S.startRequest.gen, starting: S.starting });
""")
        data = res[0]
        self.assertEqual(data["calls"]["query"], 2, "重新确认只做只读查询")
        self.assertEqual(data["calls"]["start_plan"], 0, "绝不重发 start_plan")
        self.assertEqual(data["gen"], 2)
        self.assertTrue(data["starting"])

    def test_old_generation_reply_is_ignored(self):
        res = run_js(TIMER_HARNESS + r"""
const pending = [];
window.pywebview = { api: {
  start_request_state: () => new Promise((resolve) => pending.push(resolve)),
  bootstrap: () => Promise.resolve({}),
} };
S.startRequest = { id: "rid", state: "submitting", timer: null,
                   queryTimer: null, reconcile: 0, gen: 0 };
S.starting = true;
reconcileStart("rid");            // 第 1 代
__fire(5000);                     // 超时 -> unknown
reconcileStart("rid");            // 第 2 代
pending[0]({ ok: true, state: "accepted", plan_id: "p-old", running: true });
await __flush();
const afterOld = { plan: S.plan.id, state: S.startRequest.state };
pending[1]({ ok: false, state: "rejected", text: "本机拒绝" });
await __flush();
out.push({ afterOld: afterOld, plan: S.plan.id, state: S.startRequest.state,
           starting: S.starting, html: document.querySelector("#result").innerHTML });
""")
        data = res[0]
        self.assertFalse(data["afterOld"]["plan"], "旧代次的 accepted 不得建计划")
        self.assertEqual(data["afterOld"]["state"], "reconciling")
        self.assertEqual(data["state"], "rejected", "当前代次的结论必须落地")
        self.assertFalse(data["starting"])

    def test_late_accept_does_not_replace_later_plan(self):
        res = run_js(TIMER_HARNESS + r"""
const pending = [];
window.pywebview = { api: {
  start_request_state: () => new Promise((resolve) => pending.push(resolve)),
  bootstrap: () => Promise.resolve({}),
} };
S.plan = { id: "p-later", status: "running", tasks: new Map(), armedAt: 0 };
S.startRequest = { id: "rid", state: "submitting", timer: null,
                   queryTimer: null, reconcile: 0, gen: 0 };
S.starting = true;
reconcileStart("rid");            // 第 1 代
__fire(5000);                     // 超时 -> unknown
reconcileStart("rid");            // 第 2 代（当前查询）
pending[0]({ ok: true, state: "accepted", plan_id: "p-old", running: true });
await __flush();
out.push({ plan: S.plan.id, state: S.startRequest.state, pending: pending.length });
""")
        data = res[0]
        self.assertEqual(data["plan"], "p-later", "迟到的旧 plan 不得替换当前计划")


class RefreshRoundRaceTests(unittest.TestCase):
    """F05：check/list 终态先于 skipped ack 时必须暂存重放，不能干等 100/130 秒。"""

    def test_check_terminal_before_skipped_ack_replays(self):
        res = run_js(TIMER_HARNESS + r"""
S.round.check = "new";
S.login.checking = true;
window.onBackend.check({ request_id: "old", ok: true, result: "valid",
  session_present: true, hours_left: 5, request_id2: "" });
const cachedSize = S.round.cache.length;
/* 去重 ack 后到：接续回旧 owner，必须立刻重放暂存终态 */
settleSynchronous({ ok: true, skipped: "已在检查登录态", request_id: "old" }, "check");
await __flush();
out.push({ cachedSize: cachedSize, round: S.round.check, checking: S.login.checking,
           result: S.login.result, left: S.round.cache.length });
""")
        data = res[0]
        self.assertEqual(data["cachedSize"], 1, "终态先到必须先暂存")
        self.assertEqual(data["round"], "old")
        self.assertFalse(data["checking"], "重放后必须离开检查中")
        self.assertEqual(data["result"], "valid")
        self.assertEqual(data["left"], 0, "重放后缓存清空")

    def test_check_ack_before_terminal_still_applies(self):
        res = run_js(TIMER_HARNESS + r"""
window.pywebview = { api: {} };
S.login.checking = true;
settleSynchronous({ ok: true, skipped: "已在检查登录态", request_id: "old" }, "check");
window.onBackend.check({ request_id: "old", ok: true, result: "valid",
  session_present: true, hours_left: 7 });
await __flush();
out.push({ round: S.round.check, checking: S.login.checking,
           result: S.login.result });
""")
        data = res[0]
        self.assertEqual(data["round"], "old")
        self.assertFalse(data["checking"])
        self.assertEqual(data["result"], "valid")

    def test_skipped_ack_with_result_applies_without_cache(self):
        res = run_js(TIMER_HARNESS + r"""
S.login.checking = true;
settleSynchronous({ ok: true, skipped: "已在检查登录态", request_id: "old",
  result: { request_id: "old", ok: true, result: "valid", session_present: true } }, "check");
await __flush();
out.push({ checking: S.login.checking, result: S.login.result,
           cached: S.round.cache.length });
""")
        data = res[0]
        self.assertFalse(data["checking"])
        self.assertEqual(data["result"], "valid")
        self.assertEqual(data["cached"], 0)

    def test_list_terminal_before_skipped_ack_replays(self):
        res = run_js(TIMER_HARNESS + r"""
S.round.list = "new-list";
S.listState = "loading";
window.onBackend.activities({ ok: true, request_id: "old-list", items: [
  { id: "a1", name: "活动一", sign_start: "2026-10-27 09:00:00",
    sign_end: "2026-10-27 23:00:00", act_start: "2026-10-28 09:00:00",
    act_end: "2026-10-28 11:00:00", signed: false } ] });
const cached = S.round.cache.length;
settleSynchronous({ ok: true, skipped: "已经在拉活动列表", request_id: "old-list" }, "list");
await __flush();
out.push({ cached: cached, round: S.round.list, state: S.listState,
           items: S.items.length });
""")
        data = res[0]
        self.assertEqual(data["cached"], 1)
        self.assertEqual(data["round"], "old-list")
        self.assertEqual(data["state"], "ready")
        self.assertEqual(data["items"], 1, "列表终态必须当场重放")

    def test_old_terminal_does_not_clear_current_timer(self):
        res = run_js(TIMER_HARNESS + r"""
S.round.check = "new";
S.login.checking = true;
S.round.checkTimer = setTimeout(() => { S.login.checking = false; }, 100000);
window.onBackend.check({ request_id: "old", ok: true, result: "valid" });
const armed = __timers.filter((t) => t.ms === 100000).length;
__fire(100000);
out.push({ armed: armed, checking: S.login.checking });
""")
        data = res[0]
        self.assertEqual(data["armed"], 1, "旧轮终态不得清掉新一轮的兜底计时器")
        self.assertFalse(data["checking"])

    def test_duplicate_terminal_is_idempotent(self):
        res = run_js(TIMER_HARNESS + r"""
S.round.check = "rid";
window.onBackend.check({ request_id: "rid", ok: true, result: "valid" });
window.onBackend.check({ request_id: "rid", ok: false, result: "expired" });
out.push({ result: S.login.result, ok: S.login.ok });
""")
        data = res[0]
        self.assertEqual(data["result"], "valid", "同一终态重复到达只生效一次")
        self.assertTrue(data["ok"])

    def test_cache_bound_and_expiry(self):
        res = run_js(TIMER_HARNESS + r"""
for (let i = 0; i < 20; i += 1) cacheRoundPayload("check", "id" + i, { n: i });
const size = S.round.cache.length;
const first = S.round.cache[0];
first.at = Date.now() - 200000;      // 超过 130 秒
const expired = takeRoundPayload("check", first.id);
const alive = takeRoundPayload("check", "id19");
out.push({ size: size, expired: expired === null, alive: alive });
""")
        data = res[0]
        self.assertEqual(data["size"], 16, "缓存最多 16 项")
        self.assertTrue(data["expired"], "超过 130 秒的终态不得重放")
        self.assertIsNotNone(data["alive"])


class CrossRoundConsumptionTests(unittest.TestCase):
    """FR04：请求幂等与刷新轮次分成两层——同 owner 可以收口新轮次，副作用仍幂等。"""

    LOG_COUNT = ("() => document.querySelector('#log').childElementCount")

    def test_same_owner_terminal_replays_in_new_round(self):
        res = run_js(TIMER_HARNESS + r"""
const logs = () => document.querySelector('#log').childElementCount;
S.round.check = "owner-1";
bumpRoundGeneration("check");
window.onBackend.check({ request_id: "owner-1", ok: true, result: "valid",
  session_present: true, hours_left: 5 });
const first = logs();
/* 新一轮：代次递增、本轮 id 换新，但后端回的还是同一个 owner */
bumpRoundGeneration("check");
S.round.check = "check-new";
S.login.checking = true;
settleSynchronous({ ok: true, queued: true, skipped: "已在检查登录态",
  request_id: "owner-1",
  result: { request_id: "owner-1", ok: true, result: "valid",
            session_present: true, hours_left: 5 } }, "check");
await __flush();
out.push({ first: first, after: logs(), checking: S.login.checking,
           round: S.round.check, result: S.login.result });
""")
        data = res[0]
        self.assertFalse(data["checking"], "新一轮必须立刻用同一 owner 的既有结果收口")
        self.assertEqual(data["round"], "owner-1")
        self.assertEqual(data["result"], "valid")
        self.assertEqual(data["after"], data["first"],
                         "状态要恢复，但同一句日志不能写两遍")

    def test_list_owner_replays_in_new_round(self):
        res = run_js(TIMER_HARNESS + r"""
const row = { id: "a1", name: "活动", sign_start: "2026-10-26 09:00:00",
  sign_end: "2026-10-26 23:00:00", act_start: "", act_end: "", place: "报告厅",
  signed: false };
S.round.list = "L1";
bumpRoundGeneration("list");
applyActivities({ request_id: "L1", ok: true, items: [row] }, false);
const first = S.items.length;
bumpRoundGeneration("list");
S.round.list = "L2";
S.listState = "loading";
settleSynchronous({ ok: true, queued: true, skipped: "已经在拉活动列表",
  request_id: "L1", result: { request_id: "L1", ok: true, items: [row] } }, "list");
await __flush();
out.push({ first: first, items: S.items.length, state: S.listState,
           round: S.round.list });
""")
        data = res[0]
        self.assertEqual(data["first"], 1)
        self.assertEqual(data["state"], "ready", "新一轮不能停在加载中干等 130 秒")
        self.assertEqual(data["round"], "L1")
        self.assertEqual(data["items"], 1)

    def test_duplicate_terminal_in_same_round_applies_once(self):
        res = run_js(TIMER_HARNESS + r"""
const logs = () => document.querySelector('#log').childElementCount;
S.round.check = "dup";
bumpRoundGeneration("check");
const emit = () => window.onBackend.check({ request_id: "dup", ok: true,
  result: "valid", session_present: true, hours_left: 5 });
emit();
const first = logs();
emit();
out.push({ first: first, after: logs(), cache: S.round.cache.length });
""")
        data = res[0]
        self.assertEqual(data["first"], 1)
        self.assertEqual(data["after"], 1, "同一轮同一终态只能生效一次")
        self.assertEqual(data["cache"], 0, "本轮事件不该被当成旧轮暂存")

    def test_old_ack_does_not_settle_new_round(self):
        res = run_js(TIMER_HARNESS + r"""
const pending = [];
window.pywebview = { api: {
  check: (rid) => new Promise((resolve) => pending.push([rid, resolve])),
  list_activities: () => new Promise(() => {}),
  bootstrap: () => Promise.resolve({}),
} };
refresh(false);                 // 第 1 轮
refresh(false);                 // 第 2 轮
pending[0][1]({ ok: true, queued: true, request_id: pending[0][0] });
await __flush();
out.push({ pending: S.runtime.checkPending, checking: S.login.checking,
           gen: S.round.gen.check });
""")
        data = res[0]
        self.assertTrue(data["pending"], "旧轮 ack 不得清掉新一轮的 pending")
        self.assertTrue(data["checking"], "新一轮仍在检查中")
        self.assertGreaterEqual(data["gen"], 2)


class BootstrapTerminalViewTests(unittest.TestCase):
    """FR05：F5 只恢复 data 不够，必须恢复结果框/LED/边框/次数/动作。"""

    def _snapshot(self, status, tasks):
        return {"plan_id": "p9", "status": status, "summary": {"total": len(tasks)},
                "tasks": tasks}

    def test_terminal_snapshot_restores_result_box_and_led(self):
        snap = self._snapshot("stopped", [
            {"task_id": "t1", "activity_id": "a1", "name": "场一", "status": "failed",
             "stop_reason": "unconfirmed", "attempts": 3,
             "sign_start": "2026-10-26 09:00:00", "sign_end": "2026-10-26 23:00:00",
             "reason": "响应丢失"},
            {"task_id": "t2", "activity_id": "a2", "name": "场二", "status": "canceled",
             "stop_reason": "user_stopped", "attempts": 0,
             "sign_start": "2026-10-26 09:00:00", "sign_end": "2026-10-26 23:00:00"},
        ])
        res = run_js("const snap = " + _js(snap) + ";" + r"""
window.LED_UI = { renderLed: (el, text, tone) => { S.__led = [String(text), String(tone)]; } };
const __labels = [];
const __realLabel = paintLedLabel;
paintLedLabel = (prefix, value) => { __labels.push(String(prefix)); return __realLabel(prefix, value); };
ingestBootstrapPlan({ plan: snap });
out.push({
  busy: S.busy, status: S.plan.status,
  doneSel: S.plan.doneSelection === selectionSignature(),
  board: boardPlanState(),
  html: document.querySelector("#result").innerHTML,
  labels: __labels,
  cards: document.querySelector("#plan-tasks").innerHTML,
});
""")
        data = res[0]
        self.assertFalse(data["busy"], "已结束的计划不得恢复成运行中")
        self.assertEqual(data["status"], "stopped")
        self.assertTrue(data["doneSel"], "恢复时必须设好选择签名，否则 LED/边框回落预览")
        self.assertEqual(data["board"], "attention", "含未知的停止不能画确定失败红框")
        self.assertIn("已停止", data["html"])
        self.assertIn("待核对 1 场", data["html"])
        self.assertTrue(any("已停止" in text and "待核对" in text for text in data["labels"]),
                        "LED 必须同时说清「已停止」和待核对场次：%s" % data["labels"])
        self.assertIn("3 次", data["cards"], "恢复后已提交次数不能归零")

    def test_running_snapshot_restores_busy_and_progress(self):
        snap = self._snapshot("running", [
            {"task_id": "t1", "activity_id": "a1", "name": "场一", "status": "waiting",
             "sign_start": "2099-01-01 09:00:00", "sign_end": "2099-01-01 10:00:00"},
        ])
        res = run_js("const snap = " + _js(snap) + ";" + r"""
ingestBootstrapPlan({ plan: snap });
out.push({ busy: S.busy, status: S.plan.status, tasks: S.plan.tasks.size,
           result: document.querySelector("#result").innerHTML });
""")
        data = res[0]
        self.assertTrue(data["busy"], "运行态快照必须恢复进度与停止入口")
        self.assertEqual(data["tasks"], 1)
        self.assertNotIn("报名计划", data["result"], "运行态不该冒出终态结论")

    def test_same_plan_id_advances_to_terminal(self):
        running = self._snapshot("running", [])
        done = self._snapshot("completed", [
            {"task_id": "t1", "activity_id": "a1", "name": "场一", "status": "success",
             "stop_reason": "success", "attempts": 1,
             "sign_start": "2026-10-26 09:00:00", "sign_end": "2026-10-26 23:00:00"},
        ])
        res = run_js("const running = " + _js(running) + "; const done = " + _js(done) + ";" + r"""
ingestBootstrapPlan({ plan: running });
const busyFirst = S.busy;
ingestBootstrapPlan({ plan: done });
out.push({ busyFirst: busyFirst, busy: S.busy, status: S.plan.status,
           html: document.querySelector("#result").innerHTML });
""")
        data = res[0]
        self.assertTrue(data["busyFirst"])
        self.assertFalse(data["busy"], "同一 plan_id 推进到终态不能因为「已存在」被跳过")
        self.assertEqual(data["status"], "completed")
        self.assertIn("完成", data["html"])

    def test_restore_never_starts_a_plan(self):
        snap = self._snapshot("failed", [])
        res = run_js("const snap = " + _js(snap) + ";" + r"""
const calls = [];
window.pywebview = { api: {
  start_plan: (...args) => { calls.push(args); return Promise.resolve({ ok: true }); },
  bootstrap: () => Promise.resolve({}),
} };
ingestBootstrapPlan({ plan: snap });
out.push({ calls: calls.length, status: S.plan.status });
""")
        data = res[0]
        self.assertEqual(data["calls"], 0, "恢复显示不得重发 start_plan")
        self.assertEqual(data["status"], "failed")


class BoardStateSemanticsTests(unittest.TestCase):
    """FR06：整屏视觉与卡片/总结/LED 同一分类，确认失败才用失败色。"""

    def _run_plan(self, tasks, status):
        body = ["window.onBackend.plan({ plan_id: \"p1\", status: \"running\", tasks: [] });"]
        for task in tasks:
            # plan_id 必须随任务一起带：handleTask 用它对账，缺了会被当成别的计划
            body.append("window.onBackend.task(%s);" % _js(dict(task, plan_id="p1")))
        body.append("window.onBackend.plan_done(%s);" % _js(
            {"plan_id": "p1", "status": status, "summary": {"total": len(tasks)}}))
        body.append(r"""out.push({ board: document.querySelector("#board").dataset.planState,
  html: document.querySelector("#result").innerHTML,
  led: document.querySelector("#led-label").textContent });""")
        return run_js(chr(10).join(body))[0]

    def test_unconfirmed_only_is_neutral(self):
        data = self._run_plan(
            [{"task_id": "t1", "activity_id": "a1", "name": "场一", "status": "failed",
              "stop_reason": "unconfirmed", "attempts": 2, "reason": "响应丢失"}],
            "failed")
        self.assertEqual(data["board"], "attention")
        self.assertIn("待核对 1 场", data["html"])
        self.assertIn("有 1 场待核对", data["html"])
        self.assertNotIn("待核对 2 场", data["html"])

    def test_auth_unavailable_is_counted_separately(self):
        data = self._run_plan(
            [{"task_id": "t1", "activity_id": "a1", "name": "场一", "status": "failed",
              "stop_reason": "unconfirmed", "attempts": 2, "reason": "响应丢失"},
             {"task_id": "t2", "activity_id": "a2", "name": "场二", "status": "failed",
              "stop_reason": "auth_unavailable", "attempts": 0, "reason": "URLError"}],
            "failed")
        self.assertIn("待核对 1 场", data["html"])
        self.assertIn("未提交 1 场", data["html"])
        self.assertNotIn("待核对 2 场", data["html"], "未提交的场次不能算成待核对")
        self.assertEqual(data["board"], "attention")

    def test_confirmed_failure_keeps_red(self):
        data = self._run_plan(
            [{"task_id": "t1", "activity_id": "a1", "name": "场一", "status": "failed",
              "stop_reason": "max_attempts", "attempts": 5, "reason": "名额已满"}],
            "failed")
        self.assertEqual(data["board"], "failed", "确认失败仍用确定失败视觉")
        self.assertIn("报名未成功 1 场", data["html"])

    def test_stopped_with_unknown_keeps_stopped_title(self):
        data = self._run_plan(
            [{"task_id": "t1", "activity_id": "a1", "name": "场一", "status": "success",
              "stop_reason": "success", "attempts": 1, "reason": "报名成功"},
             {"task_id": "t2", "activity_id": "a2", "name": "场二", "status": "failed",
              "stop_reason": "unconfirmed", "attempts": 3, "reason": "停止前没拿到回应"}],
            "stopped")
        self.assertIn("已停止", data["html"], "停止是主结论，不能被未知覆盖")
        self.assertIn("待核对", data["html"], "停止前的未知场次要并列说清")
        self.assertIn("已报名 1 场", data["html"])
        self.assertEqual(data["board"], "attention")


class PlanEventOwnershipTests(unittest.TestCase):
    """P1：旧计划事件不得改写当前计划；合法新计划事件可早于 ack。

    全部通过 window.onBackend.plan/task/plan_done 驱动生产 handler，
    载荷带后端接纳时附加的 plan_generation（生产桥接事件都有该字段）。
    """

    def test_stale_plan_events_cannot_hijack_running_plan(self):
        """B 运行中注入旧 A 的 plan/task/plan_done：B 的忙态/任务/停止入口不变。"""
        res = run_js(r"""
window.onBackend.plan({ plan_id: "pb", plan_generation: 2, status: "running",
  tasks: [{ task_id: "t1", activity_id: "a1", name: "B场一", status: "running" }] });
const calls = [];
window.pywebview = { api: {
  stop_plan: (pid) => { calls.push(pid); return Promise.resolve({ ok: true }); } } };
window.onBackend.plan({ plan_id: "pa", plan_generation: 1, status: "ready", tasks: [] });
window.onBackend.task({ plan_id: "pa", plan_generation: 1, task_id: "tA",
                        activity_id: "aA", name: "A场一", status: "success" });
window.onBackend.plan_done({ plan_id: "pa", plan_generation: 1,
                             status: "completed", summary: {} });
out.push({ id: S.plan.id, status: S.plan.status, busy: S.busy,
           tasks: Array.from(S.plan.tasks.keys()) });
doStopPlan();
out.push({ calls: calls });
""")
        data = res[0]
        self.assertEqual(data["id"], "pb", "旧计划事件不得替换当前计划")
        self.assertEqual(data["status"], "running", "旧计划事件不得结束运行中的 B")
        self.assertTrue(data["busy"], "B 仍在运行：忙态不得被旧事件解除")
        self.assertEqual(data["tasks"], ["t1"], "旧计划任务不得混入 B 的任务表")
        self.assertEqual(res[1]["calls"], ["pb"],
                         "停止按钮实际调用必须仍指向 B 的 plan_id")

    def test_stale_done_cannot_replace_finished_plan_conclusion(self):
        """B 已完成后再注入 A 的完成：结果框/LED/边框/忙态保持 B 的结论。"""
        res = run_js(r"""
window.onBackend.plan({ plan_id: "pb", plan_generation: 2, status: "running",
  tasks: [{ task_id: "t1", activity_id: "a1", name: "B场一", status: "success" }] });
window.onBackend.plan_done({ plan_id: "pb", plan_generation: 2,
                             status: "completed", summary: { total: 1 } });
const doneHtml = document.querySelector("#result").innerHTML;
const doneBoard = document.querySelector("#board").dataset.planState;
window.onBackend.plan_done({ plan_id: "pa", plan_generation: 1,
                             status: "failed", summary: {} });
out.push({ id: S.plan.id, busy: S.busy, board: document.querySelector("#board").dataset.planState,
           html: document.querySelector("#result").innerHTML,
           unchanged: document.querySelector("#result").innerHTML === doneHtml,
           doneBoard: doneBoard });
""")
        data = res[0]
        self.assertEqual(data["id"], "pb", "旧完成事件不得把屏上计划换成 A")
        self.assertTrue(data["unchanged"], "B 的结果框结论不得被 A 改写")
        self.assertFalse(data["busy"], "B 已结束：忙态应保持解除")
        self.assertEqual(data["board"], data["doneBoard"], "整屏状态保持 B 的终态")

    def test_duplicate_done_keeps_first_conclusion(self):
        """同计划重复终态：不把已定结论改成后来重复包的另一状态。"""
        res = run_js(r"""
window.onBackend.plan({ plan_id: "p1", plan_generation: 1, status: "running",
  tasks: [{ task_id: "t1", activity_id: "a1", name: "场一", status: "failed" }] });
window.onBackend.plan_done({ plan_id: "p1", plan_generation: 1,
                             status: "failed", summary: {} });
const firstHtml = document.querySelector("#result").innerHTML;
window.onBackend.plan_done({ plan_id: "p1", plan_generation: 1,
                             status: "completed", summary: {} });
out.push({ status: S.plan.status, busy: S.busy,
           unchanged: document.querySelector("#result").innerHTML === firstHtml });
""")
        data = res[0]
        self.assertTrue(data["unchanged"], "重复终态不得改写已定结论")
        self.assertFalse(data["busy"], "重复终态不得重新拉起忙态")

    def test_stale_progress_cannot_regress_terminal_plan(self):
        """同计划终态后收到旧进度事件：不回退终态、不复活忙态（含被拒的开始）。"""
        res = run_js(r"""
window.onBackend.plan({ plan_id: "pa", plan_generation: 1, status: "running", tasks: [] });
window.onBackend.plan_done({ plan_id: "pa", plan_generation: 1,
                             status: "failed", summary: {} });
S.startRequest = { id: "rid-x", state: "rejected", timer: null,
                   queryTimer: null, reconcile: 0, gen: 0 };
window.onBackend.plan({ plan_id: "pa", plan_generation: 1, status: "running", tasks: [] });
window.onBackend.plan_done({ plan_id: "pa", plan_generation: 1,
                             status: "completed", summary: {} });
out.push({ id: S.plan.id, status: S.plan.status, busy: S.busy });
""")
        data = res[0]
        self.assertEqual(data["status"], "failed", "旧进度/重复终态不得回退已定结论")
        self.assertFalse(data["busy"], "被拒的开始请求不得被旧事件复活成运行态")

    def test_new_plan_events_before_ack_are_adopted_and_ack_keeps_telemetry(self):
        """B 事件早于 ack：按代次接纳；后到 ack 不清任务、不抹已收次数。"""
        res = run_js(r"""
window.onBackend.plan({ plan_id: "pa", plan_generation: 1, status: "running", tasks: [] });
window.onBackend.plan_done({ plan_id: "pa", plan_generation: 1,
                             status: "stopped", summary: {} });
/* 新 B：事件先于 start_plan ack 到达 */
window.onBackend.plan({ plan_id: "pb", plan_generation: 2, status: "running",
  tasks: [{ task_id: "t1", activity_id: "a1", name: "B场一", status: "running",
            attempts: 3 }] });
/* ack 后到：不得清掉已收任务，也不得把已收到的提交次数抹回 0 */
adoptPlan("rid-b", "pb",
          { status: "running", tasks: [{ task_id: "t1", activity_id: "a1", attempts: 0 }] },
          false, 2);
const task = S.plan.tasks.get("t1") || {};
out.push({ id: S.plan.id, busy: S.busy, tasks: S.plan.tasks.size, attempts: task.attempts });
""")
        data = res[0]
        self.assertEqual(data["id"], "pb", "更新代次的事件应被接纳为新计划")
        self.assertTrue(data["busy"], "B 运行中：忙态保持")
        self.assertEqual(data["tasks"], 1, "后到 ack 不得清掉已收任务")
        self.assertEqual(data["attempts"], 3, "后到 ack 不得抹掉已收到的提交次数")

    def test_f5_restore_survives_stale_events(self):
        """F5 以 bootstrap 权威快照建立归属后，旧计划事件不得覆盖。"""
        res = run_js(r"""
ingestBootstrapPlan({ plan: { plan_id: "pb", plan_generation: 2, status: "completed",
  summary: { total: 1 },
  tasks: [{ task_id: "t1", activity_id: "a1", name: "B场一", status: "success" }] } });
const html = document.querySelector("#result").innerHTML;
window.onBackend.plan_done({ plan_id: "pa", plan_generation: 1,
                             status: "failed", summary: {} });
window.onBackend.plan({ plan_id: "pa", plan_generation: 1, status: "running", tasks: [] });
out.push({ id: S.plan.id, busy: S.busy, status: S.plan.status,
           unchanged: document.querySelector("#result").innerHTML === html });
""")
        data = res[0]
        self.assertEqual(data["id"], "pb", "F5 恢复的计划不得被旧事件替换")
        self.assertTrue(data["unchanged"], "F5 恢复的结论不得被旧事件改写")
        self.assertFalse(data["busy"])
        self.assertEqual(data["status"], "completed")


class TerminalBeforeAckTests(unittest.TestCase):
    """终态早于开始 ack：ack 只证明「已接纳」，不得把已结束计划重新置忙。

    全部驱动生产 startPlan/adoptPlan 与真实 onBackend 事件；start_plan 回执用
    受控 Promise 挂起，终态事件先落地、最后才 resolve ack。
    """

    ITEM = {"id": "a1", "name": "活动一", "sign_start": "2026-10-27 09:00:00",
            "sign_end": "2099-01-01 23:00:00",
            "act_start": "2099-01-02 09:00:00", "act_end": "2099-01-02 11:00:00",
            "signed": False}

    def _harness(self):
        return r"""
window.onBackend.activities({ ok: true, request_id: "l1", items: [
  { id: "a1", name: "活动一", sign_start: "2026-10-27 09:00:00",
    sign_end: "2099-01-01 23:00:00", act_start: "2099-01-02 09:00:00",
    act_end: "2099-01-02 11:00:00", signed: false } ] });
S.selected.add("a1");
"""

    def test_ack_does_not_revive_finished_plan(self):
        """start → 终态事件 → 原 ack：busy 不得复活，结论/次数/任务保持。"""
        cases = [
            ("completed", "success", "success", 1),
            ("failed", "failed", "max_attempts", 5),
            ("failed", "failed", "unconfirmed", 3),
            ("stopped", "canceled", "unconfirmed", 2),
        ]
        for plan_status, task_status, stop, attempts in cases:
            with self.subTest(plan=plan_status, stop=stop):
                res = run_js(self._harness() + r"""
let resolveAck = null;
let startCalls = 0;
window.pywebview = { api: {
  start_plan: () => { startCalls += 1;
    return new Promise((res) => { resolveAck = res; }); } } };
startPlan();
const flush = async () => { for (let i = 0; i < 8; i += 1) await Promise.resolve(); };
await flush();
const submitting = { busy: S.busy, starting: S.starting };
window.onBackend.plan({ plan_id: "p9", plan_generation: 1, status: "running", tasks: [] });
window.onBackend.task({ plan_id: "p9", plan_generation: 1, task_id: "t1",
                        activity_id: "a1", name: "活动一", status: %s,
                        stop_reason: %s, attempts: %d });
window.onBackend.plan_done({ plan_id: "p9", plan_generation: 1, status: %s,
                             summary: { total: 1 } });
const afterDone = { busy: S.busy, status: S.plan.status,
                    result: document.querySelector("#result").innerHTML };
resolveAck({ ok: true, queued: true, plan_id: "p9", plan_generation: 1 });
await flush();
out.push({ submitting: submitting, afterDone: afterDone,
           after: { busy: S.busy, starting: S.starting, status: S.plan.status,
                    id: S.plan.id, attempts: (S.plan.tasks.get("t1")||{}).attempts },
           startCalls: startCalls });
""" % (json_str(task_status), json_str(stop), attempts, json_str(plan_status)))
                data = res[0]
                self.assertFalse(data["submitting"]["busy"],
                                 "ack 未回、事件未到：此时还不忙（对照）")
                self.assertTrue(data["submitting"]["starting"],
                                "提交中开始标志保持，不放行第二次开始")
                self.assertFalse(data["afterDone"]["busy"], "plan_done 后必须解锁")
                self.assertFalse(data["after"]["busy"],
                                 "迟到的 ack 不得把已结束计划重新置忙")
                self.assertFalse(data["after"]["starting"])
                self.assertEqual(data["after"]["status"], plan_status,
                                 "ack 不得改写终态结论")
                self.assertEqual(data["after"]["id"], "p9")
                self.assertEqual(data["after"]["attempts"], attempts,
                                 "ack 不得抹掉已收次数")
                self.assertEqual(data["startCalls"], 1, "start_plan 恰好调用一次")

    def test_ack_while_still_submitting_keeps_busy(self):
        """对照：事件仍处提交中时 ack 到达，busy 必须为 true（既有路径不回退）。"""
        res = run_js(self._harness() + r"""
let resolveAck = null;
window.pywebview = { api: {
  start_plan: () => new Promise((res) => { resolveAck = res; }) } };
startPlan();
const flush = async () => { for (let i = 0; i < 8; i += 1) await Promise.resolve(); };
await flush();
window.onBackend.plan({ plan_id: "p9", plan_generation: 1, status: "running", tasks: [] });
window.onBackend.task({ plan_id: "p9", plan_generation: 1, task_id: "t1",
                        activity_id: "a1", name: "活动一", status: "submitting",
                        attempts: 1 });
resolveAck({ ok: true, queued: true, plan_id: "p9", plan_generation: 1 });
await flush();
out.push({ busy: S.busy, status: S.plan.status, tasks: S.plan.tasks.size,
           attempts: (S.plan.tasks.get("t1")||{}).attempts });
""")
        data = res[0]
        self.assertTrue(data["busy"], "计划仍在提交中：ack 后忙态成立")
        self.assertEqual(data["tasks"], 1, "ack 不清已收任务")
        self.assertEqual(data["attempts"], 1, "ack 不抹已收次数")

    def test_reconcile_old_running_snapshot_cannot_revive_terminal(self):
        """adoptPlan 收到同 plan_id 旧 running 快照：终态/忙态/次数不回退。"""
        res = run_js(self._harness() + r"""
window.onBackend.plan({ plan_id: "p9", plan_generation: 1, status: "running", tasks: [] });
window.onBackend.task({ plan_id: "p9", plan_generation: 1, task_id: "t1",
                        activity_id: "a1", name: "活动一", status: "success",
                        stop_reason: "success", attempts: 2 });
window.onBackend.plan_done({ plan_id: "p9", plan_generation: 1,
                             status: "completed", summary: { total: 1 } });
const before = { busy: S.busy, status: S.plan.status,
                 result: document.querySelector("#result").innerHTML };
S.startRequest = { id: "rid", state: "reconciling", timer: null,
                   queryTimer: null, reconcile: 0, gen: 0 };
adoptPlan("rid", "p9",
          { status: "running",
            tasks: [{ task_id: "t1", activity_id: "a1", name: "活动一",
                      status: "queued", attempts: 0 }] },
          false, 1);
const flush = async () => { for (let i = 0; i < 8; i += 1) await Promise.resolve(); };
await flush();
out.push({ before: before, after: { busy: S.busy, status: S.plan.status,
           taskStatus: (S.plan.tasks.get("t1")||{}).status,
           attempts: (S.plan.tasks.get("t1")||{}).attempts,
           unchanged: document.querySelector("#result").innerHTML === before.result } });
""")
        data = res[0]
        self.assertFalse(data["before"]["busy"], "plan_done 后已解锁（对照）")
        self.assertFalse(data["after"]["busy"],
                         "旧 running 快照不得复活已终态的计划")
        self.assertEqual(data["after"]["status"], "completed",
                         "旧 running 快照不得改写终态结论")
        self.assertEqual(data["after"]["taskStatus"], "success",
                         "旧 queued 快照不得把已成功场次拖回排队")
        self.assertEqual(data["after"]["attempts"], 2, "次数只增不减")
        self.assertTrue(data["after"]["unchanged"], "终态呈现保持")


def json_str(value):
    import json
    return json.dumps(value, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
