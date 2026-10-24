#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""界面契约的静态测试：不开浏览器，直接用 UTF-8 读源文件核对。

覆盖签到/签退三态契约的关键约定，谁改丢了字段名、筛选语义或保存键，
这里的失败信息会直接指出缺哪一条，避免「界面悄悄退回两态」。

@author LiXinwen
"""
from __future__ import annotations

import unittest
from pathlib import Path

LOCAL = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    path = LOCAL / rel
    self_msg = "契约文件缺失：%s" % path
    assert path.exists(), self_msg
    return path.read_text(encoding="utf-8")


class FilterControlTests(unittest.TestCase):
    """两个筛选控件：全部/需要/无需 三态都必须存在。"""

    def test_index_has_checkin_and_checkout_selects(self):
        html = read("ui/index.html")
        for key in ("checkin", "checkout"):
            self.assertIn('id="%s"' % key, html, "index.html 缺少 #%s 筛选控件" % key)
            self.assertIn('aria-label="%s要求"' % ("签到" if key == "checkin" else "签退"), html,
                          "#%s 需要 aria 名称，状态不能只靠位置表达" % key)
        for key in ("checkin", "checkout"):
            for value, label in (("all", "全部"), ("yes", "需要"), ("no", "无需")):
                self.assertIn('<option value="%s">%s</option>' % (value, label), html,
                              "#%s 缺少「%s」选项（%s）" % (key, label, value))

    def test_app_js_keeps_filter_prefs(self):
        js = read("ui/app.js")
        # 保存与恢复：filter 里的 checkin/checkout 必须进偏好
        self.assertIn('savePrefs({ filter: Object.assign({}, S.filter) })', js,
                      "筛选变化必须保存进偏好（savePrefs filter）")
        for key in ("checkin", "checkout"):
            self.assertIn('%s: ["all", "yes", "no"]' % key, js,
                          "applyPrefs 必须校验 %s 的三态取值" % key)


class TriStateContractTests(unittest.TestCase):
    """tri-state 字段名与 unknown 保留规则：null 不得降级成无需。"""

    def test_grab_normalizes_explicit_aliases_only(self):
        py = read("grab.py")
        self.assertIn("CHECKIN_FLAG_NAMES", py, "grab.py 缺少签到别名列")
        self.assertIn("CHECKOUT_FLAG_NAMES", py, "grab.py 缺少签退别名列")
        # 无法归一化的值必须保持 None，禁止猜成 False
        self.assertIn("return None", py, "_tri_state 对未知值必须返回 None")
        self.assertIn("not in (None, \"\")", py, "空值字段必须当作未知（跳过），不能当无需")
        # list --json 输出必须带两项 tri-state 字段
        self.assertIn('"checkin_required": act.get("checkin_required")', py,
                      "list --json 必须原样透传 checkin_required")
        self.assertIn('"checkout_required": act.get("checkout_required")', py,
                      "list --json 必须原样透传 checkout_required")

    def test_grab_tri_state_function_semantics(self):
        """_tri_state 的三态语义直接在 Python 里验证，不依赖浏览器。"""
        import sys
        sys.path.insert(0, str(LOCAL))
        try:
            import grab
        finally:
            sys.path.pop(0)
        self.assertIs(grab._tri_state(True), True)
        self.assertIs(grab._tri_state(1), True)
        self.assertIs(grab._tri_state("是"), True)
        self.assertIs(grab._tri_state(False), False)
        self.assertIs(grab._tri_state(0), False)
        self.assertIs(grab._tri_state("否"), False)
        # 未知字符串、缺失、空串、未确认数字都必须是 None
        for unknown in (None, "", "maybe", "2", "未知"):
            self.assertIsNone(grab._tri_state(unknown),
                              "未知值 %r 必须归一化为 None" % unknown)
        self.assertIsNone(grab._activity_flag({"qiandao": "随便"}, ("qiandao",)),
                          "未知别名取值必须保持 None")

    def test_app_js_filter_excludes_null_only_for_yes_no(self):
        js = read("ui/app.js")
        self.assertIn("function matchesFlag", js, "app.js 缺少筛选匹配函数 matchesFlag")
        # 需要和无需排除 null；全部保留 null —— 这条规则是文案级别的契约
        self.assertIn("if (value === null) return false;", js,
                      "matchesFlag 必须在需要/无需分支排除 null（未知不得误判）")
        self.assertIn('if (filter === "all") return true;', js,
                      "matchesFilter 的「全部」必须保留未知项")
        # 行内未知文案
        self.assertIn("签到待说明", js, "行内必须显示「签到待说明」而不是留空")
        self.assertIn("签退待说明", js, "行内必须显示「签退待说明」而不是留空")

    def test_app_js_tri_state_normalize(self):
        js = read("ui/app.js")
        self.assertIn("item.checkin_required = triState(item.checkin_required)", js,
                      "列表数据必须经 triState 归一化 checkin_required")
        self.assertIn("item.checkout_required = triState(item.checkout_required)", js,
                      "列表数据必须经 triState 归一化 checkout_required")

    def test_demo_covers_tri_state_and_missing_time(self):
        """demo 样本必须能稳定复现筛选边界：true/false/null、已报名、活动时间缺失。"""
        import sys
        sys.path.insert(0, str(LOCAL))
        try:
            import app as local_app
        finally:
            sys.path.pop(0)
        items = local_app.demo_items()
        flags = {(it["checkin_required"], it["checkout_required"]) for it in items}
        self.assertIn((True, False), flags, "demo 缺少「需要签到」样本")
        self.assertIn((False, True), flags, "demo 缺少「需要签退」样本")
        self.assertIn((None, None), flags, "demo 缺少「签到/签退未知」样本")
        self.assertIn((None, True), flags, "demo 缺少「签到未知但需签退」的混合样本")
        self.assertTrue(any(it["signed"] for it in items), "demo 缺少「已报名」样本")
        missing = [it for it in items if not it["act_start"] or not it["act_end"]]
        self.assertTrue(missing, "demo 缺少「活动时间缺失」样本")
        for it in items:
            self.assertIn("checkin_required", it)
            self.assertIn("checkout_required", it)
            self.assertIn(it["state"], ("已报名", "开放中", "未开放"),
                          "demo 的 state 必须用 list --json 同款中文口径")


class FilterIsolationTests(unittest.TestCase):
    """筛选只影响可见行：全局下一场、冲突校验、计划都基于全量数据。"""

    def test_next_open_uses_all_items(self):
        js = read("ui/app.js")
        self.assertIn("function nextOpen()", js)
        # nextOpen 内遍历的是 S.items（全量），不经过 filtered()
        start = js.index("function nextOpen()")
        body = js[start:js.index("function groupSelected")]
        self.assertIn("S.items.forEach", body, "nextOpen 必须基于全量 S.items，不受筛选影响")
        self.assertNotIn("filtered()", body, "nextOpen 不得使用 filtered() 的结果")

    def test_validation_uses_selected_items(self):
        js = read("ui/app.js")
        start = js.index("function validateSelection()")
        body = js[start:start + 1200]
        self.assertIn("selectedItems()", body, "冲突校验必须基于已选集合，而不是可见行")


class LoginStateContractTests(unittest.TestCase):
    """首次使用 / 登录失效 / 有效登录三态契约（防「首次使用被当成登录失效」回归）。

    「没有登录」= session.json 不存在、无法解析或缺少 token；
    仅有过期 token（present=true 且过期/校验失败）属于登录态失效；
    hours_left=0 不能单独代表首次未登录。
    """

    def test_backend_login_state_fields_in_payloads(self):
        py = read("app.py")
        self.assertIn("def login_state", py, "app.py 必须有三态快照函数 login_state")
        start = py.index("def _do_check")
        body = py[start:py.index("def _do_list", start)]
        for field in ("session_present", "session_expired"):
            self.assertIn('"%s"' % field, py, "登录载荷缺字段 %s" % field)
            self.assertIn(field, body, "check 事件载荷必须带 %s" % field)

    def test_login_state_semantics(self):
        import json
        import sys
        import tempfile
        from datetime import datetime, timedelta
        from unittest import mock

        sys.path.insert(0, str(LOCAL))
        try:
            import app as local_app
        except Exception as exc:
            self.skipTest("无法导入 local/app.py：%s" % exc)
        finally:
            sys.path.pop(0)
        tmp = Path(tempfile.mkdtemp())
        # login_state/token_hours_left 读的是模块级 SESSION_FILE（runtime_paths
        # 统一数据根），不是 HERE：patch 错目标会让隔离失效、摸到真实登录态
        session = tmp / "session.json"
        with mock.patch.object(local_app, "SESSION_FILE", session):
            absent = local_app.login_state()
            self.assertFalse(absent["session_present"], "文件不存在=未登录")
            self.assertIsNone(absent["hours_left"])

            session.write_text("{坏掉的 json", encoding="utf-8")
            unparseable = local_app.login_state()
            self.assertFalse(unparseable["session_present"], "解析失败=未登录")

            fresh = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            session.write_text(json.dumps({"saved_at": fresh}), encoding="utf-8")
            no_token = local_app.login_state()
            self.assertFalse(no_token["session_present"],
                             "缺 token 不得按 saved_at 报成已登录")
            self.assertIsNone(no_token["hours_left"],
                              "空壳会话不得显示有效小时数")

            session.write_text(json.dumps({"token": "t", "saved_at": fresh}), encoding="utf-8")
            valid = local_app.login_state()
            self.assertTrue(valid["session_present"])
            self.assertFalse(valid["session_expired"])
            self.assertGreater(valid["hours_left"], 0)

            stale = (datetime.now() - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S")
            session.write_text(json.dumps({"token": "t", "saved_at": stale}), encoding="utf-8")
            expired = local_app.login_state()
            self.assertTrue(expired["session_present"], "过期=登录过")
            self.assertTrue(expired["session_expired"])
            self.assertEqual(expired["hours_left"], 0.0)

    def test_check_hint_success_beats_login_keyword(self):
        """code==0 的成功行常含「登录」字样（如「登录态还有 17 小时」），不得归类 login。"""
        import sys
        sys.path.insert(0, str(LOCAL))
        try:
            import app as local_app
        except Exception as exc:
            self.skipTest("无法导入 local/app.py：%s" % exc)
        finally:
            sys.path.pop(0)
        self.assertEqual(local_app._check_hint("[OK] 登录态还有 17 小时", 0), "")
        self.assertEqual(local_app._check_hint("[X] 本机还没有登录过", 3), "login")

    def test_app_js_first_use_is_not_bad(self):
        js = read("ui/app.js")
        self.assertIn("S.login.present", js,
                      "paintLogin 必须用 session_present 区分首次使用")
        self.assertIn("S.login.present === false", js,
                      "present=false 必须走未登录分支，不显示登录态失效")
        self.assertIn("S.login.present !== false", js,
                      "bad 分支必须放过 present=false（hours_left=0 不代表首次未登录）")
        self.assertIn('state === "bad" || state === "warn" || state === "ok" ? "重新登录" : "登录"',
                      js, "按钮文案：只有登录过的状态才叫「重新登录」，首次使用叫「登录」")


class StartGateContractTests(unittest.TestCase):
    """检查异常兜底与开始门禁：checking/unknown 不能启动真实计划，demo 可演示。"""

    def test_refresh_catch_leaves_checking(self):
        """check 调用失败（桥接/脚本异常）也必须离开「检查中」并落 unknown。"""
        js = read("ui/app.js")
        start = js.index("function refresh(")
        body = js[start:js.index("function applyActivities", start)]
        self.assertIn("S.login.checking = false", body,
                      "refresh 的 catch 必须清除「检查中」，不得永远停在检查态")
        self.assertIn('S.login.result = "unknown"', body,
                      "check 失败必须落 unknown 结论，给出「点刷新重试」的下一步")
        self.assertIn("S.login.ok = false", body,
                      "旧 ok 不清掉的话 unknown 分支接不住，顶栏会残留「登录有效」")

    def test_start_button_requires_valid_or_demo(self):
        """开始按钮门禁：只有登录校验 valid 且不在检查中，或 demo 模式放行。"""
        js = read("ui/app.js")
        start = js.index("function updateControls(")
        body = js[start:js.index("function paintBanners", start)]
        self.assertIn('S.login.result === "valid"', body,
                      "开始按钮必须要求登录校验 valid（unknown/none/expired 禁用）")
        self.assertIn("!S.login.checking", body,
                      "checking 必须显式禁用开始：新一轮检查不清旧 valid，不排除会放行未确认期")
        self.assertIn("S.demo", body, "demo 模式必须能绕过登录门禁继续视觉演示")

    def test_bootstrap_carries_demo_flag(self):
        py = read("app.py")
        start = py.index("def bootstrap")
        body = py[start:py.index("def help(", start)]
        self.assertIn('"demo"', body, "bootstrap 载荷必须带 demo 标记，前端门禁才能放行演示")

    def test_backend_start_plan_rejects_unconfirmed_login(self):
        """后端最终防线：非 demo 且最近 check 结论不是 valid 时拒绝。"""
        py = read("app.py")
        start = py.index("def start_plan")
        body = py[start:py.index("def cancel_task", start)]
        self.assertIn('result == "valid"', body,
                      "start_plan 必须以「最近 check 结论 valid」为唯一放行形态")
        self.assertIn("DEMO", body, "demo 模式继续可运行演示计划")
        for state in ("checking", "none", "expired"):
            self.assertIn(state, body, "start_plan 门禁必须覆盖 %s" % state)


class BridgeContractTests(unittest.TestCase):
    """桥接方法与事件名：界面和 app.py 的契约字面量。"""

    def test_app_js_calls_bridge_methods(self):
        js = read("ui/app.js")
        for method in ("bootstrap", "check", "list_activities", "login",
                       "start_plan", "cancel_task", "stop_plan",
                       "open_data", "save_prefs", "front_ready"):
            self.assertIn('"%s"' % method, js, "app.js 缺少桥接方法调用：%s" % method)

    def test_app_js_handles_backend_events(self):
        import re
        js = read("ui/app.js")
        self.assertIn("window.onBackend", js, "app.js 必须挂载 window.onBackend")
        for event in ("log", "check", "activities", "done", "plan", "task", "plan_done"):
            self.assertRegex(js, r"\b%s\s*:\s*\(" % event,
                             "window.onBackend 缺少事件处理：%s" % event)

    def test_no_mock_backend_in_production(self):
        """生产页面不得引用原型 mock-backend。"""
        html = read("ui/index.html")
        self.assertNotIn("mock-backend", html, "index.html 不得加载原型 mock-backend.js")
        self.assertNotIn("proto/", html, "index.html 不得引用 design/prototype 的资源")


class NameContractTests(unittest.TestCase):
    """正式名称契约：天软活动预报名工具；「准点」只留在历史说明里。"""

    def test_formal_name_present_everywhere(self):
        for rel in ("../README.md", "README.md", "../docs/UI_SPEC.md",
                    "ui/index.html", "app.py"):
            self.assertIn("天软活动预报名工具", read(rel),
                          "%s 必须包含正式名称「天软活动预报名工具」" % rel)

    def test_window_title_uses_formal_name(self):
        py = read("app.py")
        self.assertGreaterEqual(py.count("天软活动预报名工具"), 2,
                                "app.py 的 help() 标题与 webview 窗口标题都要用正式名称")
        self.assertIn("天软活动预报名工具\", str(UI)", py,
                      "webview.create_window 的窗口标题必须是正式名称")

    def test_runtime_no_zhundian_brand(self):
        """运行时文件里不得再出现「准点」品牌（历史原型称呼）。"""
        for rel in ("ui/index.html", "app.py", "ui/app.js"):
            self.assertNotIn("准点", read(rel),
                             "%s 不得再使用旧品牌「准点」" % rel)

    def test_short_label_for_narrow_window(self):
        html = read("ui/index.html")
        self.assertIn("天软预报名工具", html, "窄窗口短标签「天软预报名工具」必须存在")


class LedAndStopContractTests(unittest.TestCase):
    """LED 点阵与停止二次确认的契约。"""

    def test_index_wires_led_and_stop_dialog(self):
        html = read("ui/index.html")
        self.assertIn('<script src="led.js"></script>', html, "必须在 app.js 之前引入 led.js")
        self.assertIn('id="led"', html, "缺少 LED 点阵容器 #led")
        self.assertIn('id="dlg-stop"', html, "缺少停止确认 dialog #dlg-stop")
        self.assertIn("继续等待", html, "确认弹窗必须有「继续等待」按钮")
        self.assertIn("停止计划", html, "确认弹窗必须有「停止计划」按钮")
        self.assertIn('value="no"', html, "「继续等待」提交 no，Esc 也走 no 通道")

    def test_led_js_is_pure_renderer(self):
        js = read("ui/led.js")
        self.assertIn("renderLed", js, "led.js 必须暴露 renderLed")
        self.assertIn("clearLed", js, "led.js 必须暴露 clearLed")
        self.assertIn("✓", js, "字库要含成功图标")
        self.assertIn("✕", js, "字库要含失败图标")
        for banned in ("fetch(", "XMLHttpRequest", "mock-backend", "pywebview", "onBackend"):
            self.assertNotIn(banned, js, "led.js 只负责渲染，不得含业务/网络逻辑：%s" % banned)

    def test_stop_only_after_confirmation(self):
        js = read("ui/app.js")
        start = js.index("function stopPlan()")
        body = js[start:js.index("function ", start + 10)]
        self.assertIn("dlg-stop", body, "stopPlan 必须先打开确认弹窗")
        self.assertNotIn("stop_plan", body, "stopPlan 本体不得直接调用 stop_plan")
        do_start = js.index("function doStopPlan()")
        do_body = js[do_start:js.index("function ", do_start + 10)]
        self.assertIn('"stop_plan"', do_body, "真正的停止只在确认回调 doStopPlan 里")
        self.assertIn('returnValue === "yes"', js, "只有弹窗回 yes 才停止；Esc/继续等待不触发")


class StopLifecycleContractTests(unittest.TestCase):
    """停止生命周期的静态契约：进程树终止、「正在停止」反馈、重试窗口硬截止。"""

    def test_app_py_kills_process_tree(self):
        py = read("app.py")
        self.assertIn("def _kill_tree", py, "必须有进程树终止 helper")
        self.assertIn("taskkill", py, "Windows 下必须按 taskkill /F /T 终止整棵进程树")
        self.assertIn("canceled or reader_done.is_set()",
                      py, "取消路径的退出条件不得依赖管道 EOF（孙进程可能握着管道）")
        self.assertIn("_terminate_plan_procs", py, "stop_plan 必须主动终止活着的 worker")

    def test_grab_retry_window_is_hard_deadline(self):
        py = read("grab.py")
        self.assertIn("def clamp_wait_ms", py, "重试循环必须有窗口钳制函数")
        self.assertGreaterEqual(py.count("clamp_wait_ms(deadline"), 7,
                                "循环内 reload/expect_response/click/wait 都要钳到剩余窗口")

    def test_grab_finally_never_returns(self):
        """cmd_grab 的 finally 只收尾浏览器：return 会掩盖真实退出码（成功0/登录失败3/报名失败2）。"""
        import ast
        tree = ast.parse(read("grab.py"))
        cmd = next(node for node in tree.body
                   if isinstance(node, ast.FunctionDef) and node.name == "cmd_grab")
        offenders = []
        for node in ast.walk(cmd):
            if isinstance(node, ast.Try) and node.finalbody:
                for stmt in node.finalbody:
                    offenders.extend(sub for sub in ast.walk(stmt)
                                     if isinstance(sub, ast.Return))
        self.assertFalse(offenders, "cmd_grab 的 finally 里不得 return：%r" % offenders)

    def test_app_js_stopping_feedback(self):
        js = read("ui/app.js")
        self.assertIn("S.stopping", js, "停止后必须有「正在停止」的即时反馈状态")
        self.assertIn('"正在停止…"', js, "停止按钮要切换为「正在停止…」")
        self.assertIn("S.stopping = false", js, "plan_done 后必须解除正在停止状态")
        self.assertIn('"正在停止" : "正在处理"', js, "忙碌胶囊要区分停止中/处理中")


class RetryPolicyUiContractTests(unittest.TestCase):
    """重试策略的界面契约：控件、偏好、telemetry 展示与旧桥接不丢。"""

    def test_index_has_policy_controls(self):
        html = read("ui/index.html")
        self.assertIn('id="retry-for"', html, "缺少重试时长下拉 #retry-for")
        self.assertIn('id="max-attempts"', html, "缺少最大提交次数输入 #max-attempts")
        for label in ("2 分钟", "10 分钟", "30 分钟", "1 小时", "直到报名截止"):
            self.assertIn(label, html, "重试时长缺少「%s」档位" % label)
        self.assertIn('value="0"', html, "次数输入必须能表达 0=不限")

    def test_app_js_saves_and_sends_policy(self):
        js = read("ui/app.js")
        self.assertIn("retryPolicy", js, "缺少策略状态 S.retryPolicy")
        self.assertIn("retry_policy", js, "策略必须随偏好保存/恢复（prefs.retry_policy）")
        self.assertIn("setRetryPolicy", js, "缺少策略写入函数 setRetryPolicy")
        # startPlan 必须把策略传给后端
        start = js.index("api(\"start_plan\"")
        call = js[start:start + 200]
        self.assertIn("retry_for_s", call, "start_plan 载荷必须带 retry_for_s")
        self.assertIn("max_attempts", call, "start_plan 载荷必须带 max_attempts")

    def test_app_js_renders_attempts_and_stop_reason(self):
        js = read("ui/app.js")
        self.assertIn("第 \" + task.attempts + \" 次提交", js,
                      "任务卡必须显示第 N 次提交（telemetry）")
        self.assertIn("last_reason", js, "任务卡必须显示最近原因")
        self.assertIn("stopReasonText", js, "缺少停止条件人话函数")
        for word in ("达到最大提交次数", "重试时长到了", "报名已截止", "登录失效"):
            self.assertIn(word, js, "终态文案缺「%s」" % word)
        # 路线图不再用固定 sign_start+120 冒充截止
        self.assertIn("telemetryDeadline", js, "重试截止必须优先用 telemetry")

    def test_backend_contract_strings(self):
        py = read("app.py")
        self.assertIn("--sign-end", py, "worker 命令必须传报名截止")
        self.assertIn("--retry-for", py, "worker 命令必须传重试时长")
        self.assertIn("--max-attempts", py, "worker 命令必须传最大提交次数")
        self.assertIn("def _parse_retry_line", py, "必须解析 [retry] 结构化行")
        grab = read("grab.py")
        self.assertIn("def compute_retry_window", grab, "缺少窗口纯函数")
        self.assertIn("def attempts_allowed", grab, "缺少次数上限判断")
        self.assertIn("sign_end", read("signup_plan.py"), "TaskSpec 必须携带 sign_end")


class StateGapsContractTests(unittest.TestCase):
    """登录到期检查、时钟偏差、断网/睡眠、断点与错误行动按钮的静态契约。"""

    def test_login_expiry_check_present(self):
        js = read("ui/app.js")
        self.assertIn("function paintChecks", js, "缺少检查条 paintChecks")
        self.assertIn("RETRY_WINDOW_S", js, "登录到期必须按「报名窗口 + 重试时长」口径比较")
        self.assertIn("earliestSelectedExecutionEnd", js,
                      "已开放场次必须用本次执行窗口判断登录到期，不得拿历史 sign_start")
        self.assertIn("登录撑得到这次报名窗口", js,
                      "检查条必须能给出登录撑得到/撑不到的结论")

    def test_clock_skew_parsed_from_check_and_log(self):
        js = read("ui/app.js")
        self.assertIn("本机时钟与服务器相差", js, "必须能从日志/check 原话解析时钟偏差")
        self.assertIn("#clock-skew", js, "偏差必须上顶栏徽章（#clock-skew）")
        self.assertIn("clock-pop", js, "必须有时钟详情浮层 #clock-pop")

    def test_online_offline_banners(self):
        js = read("ui/app.js")
        self.assertIn('addEventListener("online"', js, "必须监听 online 恢复")
        self.assertIn('addEventListener("offline"', js, "必须监听 offline 断网")
        self.assertIn("网络断开", js, "断网横幅必须有文字说明")

    def test_sleep_gap_uses_real_interval(self):
        js = read("ui/app.js")
        self.assertIn("SLEEP_GAP_MS", js, "睡眠检测必须有明确阈值常量")
        self.assertIn("lastBeat", js, "必须基于真实心跳间隔（lastBeat 差值）判定睡眠")
        self.assertNotIn("simulateSleep", js, "不得提供模拟睡眠成功的钩子")

    def test_responsive_breakpoints(self):
        css = read("ui/styles.css")
        for width in ("1060px", "900px", "860px"):
            self.assertIn("max-width: %s" % width, css, "缺少 %s 宽度断点" % width)
        self.assertIn("max-height: 720px", css, "缺少 720px 高度断点（矮窗口档）")
        self.assertIn(".c-place, .colhead .h-place { display: none; }", css,
                      "860 断点必须收起地点列")
        self.assertIn(".name-short { display: inline; }", css,
                      "860 断点必须切换为短品牌名")

    def test_error_result_has_action(self):
        js = read("ui/app.js")
        self.assertIn("result-action", js, "失败结论必须带行动按钮（刷新列表/再试一次）")

    def test_check_event_carries_hint(self):
        py = read("app.py")
        self.assertIn('"hint": _check_hint', py, "check 事件必须带可区分的 hint 分类字段")
        self.assertIn("def _check_hint", py, "缺少 hint 分类函数")


class DualModeUiContractTests(unittest.TestCase):
    """双模式契约（静态）：模式文案、开放优先、已截止禁选、路线独立
    checkpoint、统一 deadline、startPlan 载荷与恢复合并不丢运行时字段。"""

    def test_mode_of_and_mode_texts(self):
        """modeOf 三分类与三种正式模式文案必须存在（预报名/立即报名/抢名额/已截止）。"""
        js = read("ui/app.js")
        self.assertIn("function modeOf(", js, "缺少模式分类函数 modeOf")
        self.assertIn('pre_register: "预报名"', js, "缺少预报名文案")
        self.assertIn('instant_signup: "立即报名/抢名额"', js, "缺少立即报名/抢名额文案")
        self.assertIn('closed: "已截止"', js, "缺少已截止文案")
        # 模式只做展示与即时门禁：后端仍是最终权威
        self.assertIn("def classify_signup_mode", read("signup_plan.py"),
                      "后端必须有唯一模式分类口径 classify_signup_mode")

    def test_next_open_prefers_open_activity(self):
        """nextOpen 返回 openId/futureId；存在开放活动时 bestId 优先 openId。"""
        js = read("ui/app.js")
        start = js.index("function nextOpen()")
        body = js[start:js.index("function groupSelected", start)]
        for key in ("openId", "futureId", "bestId"):
            self.assertIn(key, body, "nextOpen 必须返回 %s（开放优先于未来场次）" % key)
        self.assertIn("isEligibleForPlan", body, "已截止活动不得参与下一场")

    def test_closed_activity_cannot_enter_plan(self):
        """已截止禁选：复选框禁用、整行点击拒绝、校验拒绝、统计排除。"""
        js = read("ui/app.js")
        self.assertIn("function isEligibleForPlan(", js,
                      "缺少统一可报名判据 isEligibleForPlan")
        # 复选框与整行点击共用同一份判据
        self.assertIn("check.disabled = S.busy || !isEligibleForPlan(item)", js,
                      "已截止的复选框必须禁用")
        start = js.index("function toggleSelection(")
        body = js[start:js.index("function clearSelection", start)]
        self.assertIn("!isEligibleForPlan(item)", body,
                      "整行点击路径也必须拒绝已截止活动")
        self.assertIn("isEligibleForPlan(item)", body, "复选框 change 路径必须过门禁")
        # validateSelection 明确拒绝已截止
        val_start = js.index("function validateSelection(")
        val_body = js[val_start:val_start + 1600]
        self.assertIn('modeOf(item) === "closed"', val_body,
                      "前端校验必须拒绝已截止活动")
        self.assertIn("报名已截止", val_body, "拒绝文案要说明报名已截止")
        # 统计口径排除已截止
        self.assertIn("S.items.filter(isEligibleForPlan)", js,
                      "可报名统计必须排除已截止")
        # 后端最终防线
        py = read("app.py")
        start_plan = py.index("def start_plan")
        plan_body = py[start_plan:py.index("def cancel_task", start_plan)]
        self.assertIn("sign_end_passed", plan_body,
                      "start_plan 必须拒绝已截止活动（后端最终防线）")
        self.assertIn("报名已截止", plan_body, "拒绝时要返回具体说明")

    def test_start_plan_payload_carries_sign_end_and_mode(self):
        """startPlan 显式传 sign_end 与 signup_mode，后端缺口数据也能拦截。"""
        js = read("ui/app.js")
        start = js.index("const items = selectedItems().map((item) => ({")
        body = js[start:js.index("}));", start)]
        self.assertIn("sign_end:", body, "start_plan 载荷必须带 sign_end")
        self.assertIn("signup_mode:", body, "start_plan 载荷必须带 signup_mode")

    def test_route_uses_independent_station_checkpoints(self):
        """路线图站点独立判断：真实 checkpoint 才 done，历史开放不抢占当前节点。"""
        js = read("ui/app.js")
        start = js.index("function paintRoute()")
        body = js[start:js.index("function routeDeadline", start)]
        for field in ("run_started_at", "page_opened_at", "watch_started_at"):
            self.assertIn(field, body, "路线站点必须用真实 checkpoint：%s" % field)
        self.assertIn("done: runStarted !== null", body,
                      "「本次启动」只有真实 checkpoint 才算 done")
        self.assertIn('done: now >= open', body,
                      "原定开放是时间参考节点（历史开放标 done 但不抢占当前节点）")

    def test_unified_effective_retry_deadline(self):
        """路线/任务卡/LED 共用 effectiveRetryDeadline，不再固定回退 120 秒。"""
        js = read("ui/app.js")
        self.assertIn("function effectiveRetryDeadline(", js, "缺少统一截止函数")
        self.assertIn("function retryDeadline(task) {\n  return effectiveRetryDeadline(task);\n}",
                      js, "任务卡/LED 的旧入口必须并入统一推导")
        rd_start = js.index("function routeDeadline(")
        self.assertIn("effectiveRetryDeadline", js[rd_start:rd_start + 300],
                      "路线图截止必须走统一函数")
        # 推导口径：任务 retry_for_s 与 sign_end 截断，不是写死 120 秒
        ed_start = js.index("function effectiveRetryDeadline(")
        ed_body = js[ed_start:js.index("function retryDeadline", ed_start)]
        self.assertIn("task.retry_for_s", ed_body, "截止推导必须用任务携带的策略时长")
        self.assertIn("toTs(task.sign_end)", ed_body, "截止推导必须被 sign_end 截断")

    def test_plan_merge_keeps_runtime_fields(self):
        """晚到 plan 事件与页面重载恢复不得抹掉已收到的运行时字段。"""
        js = read("ui/app.js")
        self.assertIn("TASK_RUNTIME_FIELDS", js, "缺少运行时字段清单")
        self.assertIn("function mergeTask(", js, "缺少任务合并函数 mergeTask")
        for anchor in ("function handlePlan(", "function handleTask(",
                       "function ingestBootstrapPlan("):
            anchor_at = js.index(anchor)
            body = js[anchor_at:anchor_at + 900]
            self.assertIn("mergeTask(", body,
                          "%s 必须经 mergeTask 合并，不能整对象覆盖" % anchor.strip("function ("))

    def test_task_card_shows_signup_mode(self):
        """混合计划每个任务卡显示自己的模式。"""
        js = read("ui/app.js")
        start = js.index("function taskHtml(")
        body = js[start:js.index("function updateControls(", start)]
        self.assertIn("signup_mode", body, "任务卡必须读取任务的 signup_mode")
        self.assertIn("modeText(mode)", body, "任务卡必须输出模式文案")


class PreopenRouteContractTests(unittest.TestCase):
    """预报名提前准备与真实 checkpoint 的静态契约（反修门禁）。"""

    def test_paint_route_has_no_fake_time_fallbacks(self):
        """paintRoute 不得用 armedAt/open-100/open-5 编造事件时间；缺失显示待发生文案。"""
        js = read("ui/app.js")
        start = js.index("function paintRoute()")
        body = js[start:js.index("function routeDeadline", start)]
        for banned in ("armedAt", "open - 100", "open - 5"):
            self.assertNotIn(banned, body,
                             "路线图站点不得用 %s 生成估算时间冒充事件时间" % banned)
        for needed in ("待启动", "待打开", "待发生"):
            self.assertIn(needed, body, "缺 checkpoint 的站点必须显示「%s」" % needed)
        # 真实 checkpoint 仍是三个执行站点唯一的时间来源
        for field in ("run_started_at", "page_opened_at", "watch_started_at"):
            self.assertIn(field, body, "路线站点必须消费真实字段 %s" % field)

    def test_worker_payload_carries_lead(self):
        """worker 载荷显式携带与 TaskSpec 一致的 --lead；快照携带 lead_s。"""
        py = read("app.py")
        self.assertIn('"--lead", str(max(0, int(spec.lead_s or 0)))', py,
                      "_run_plan_task 必须显式传 --lead（预报名提前准备窗口）")
        sp = read("signup_plan.py")
        self.assertIn("lead_s: int = 100", sp, "TaskSpec 快照必须携带 lead_s（默认 100）")
        self.assertIn('"lead_s": self.lead_s', sp, "as_dict 必须输出 lead_s，页面重载不丢")

    def test_scheduler_prepare_semantics(self):
        """调度器按准备时刻调度：instant 立即、pre_register 提前、批次间不 join。"""
        sp = read("signup_plan.py")
        self.assertIn("def _prepare_ts", sp, "缺少按准备时刻调度的纯函数")
        start = sp.index("def _run(self)")
        body = sp[start:sp.index("def _prepare_ts", start)]
        self.assertIn("_prepare_ts", body, "调度循环必须按 _prepare_ts 排序与分批")
        # 批次循环内不得 join 整批（否则重试会阻塞后续场次按准备时刻启动）
        batch_zone = body[:body.index("for thread in threads")]
        self.assertNotIn("thread.join()", batch_zone,
                         "批次循环内不得 join：重试中的场次不得阻塞后续调度")
        self.assertIn('== "instant_signup"', sp,
                      "已开放场次必须立即启动，不等历史 sign_start")


if __name__ == "__main__":
    unittest.main()
