#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""界面契约的静态测试：不开浏览器，直接用 UTF-8 读源文件核对。

覆盖签到/签退三态契约的关键约定，谁改丢了字段名、筛选语义或保存键，
这里的失败信息会直接指出缺哪一条，避免「界面悄悄退回两态」。

@author LiXinwen
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

LOCAL = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    path = LOCAL / rel
    self_msg = "契约文件缺失：%s" % path
    assert path.exists(), self_msg
    if rel == "ui/app.js":
        # 结构重构后界面脚本按层拆成多文件：契约断言读「前端脚本联合源码」，
        # 加载序取自 index.html（led.js 是独立字库渲染器，不含业务契约文本）。
        return read_ui_scripts()
    return path.read_text(encoding="utf-8")


def read_ui_scripts() -> str:
    """index.html 加载序的全部业务脚本拼接（led.js 除外）。"""
    html = (LOCAL / "ui" / "index.html").read_text(encoding="utf-8")
    parts = []
    for src in re.findall(r'<script src="([^"]+)"></script>', html):
        if src == "led.js":
            continue
        parts.append((LOCAL / "ui" / src).read_text(encoding="utf-8"))
    return "\n".join(parts)


def help_dialog(html: str) -> str:
    """截取更多 dialog 元素本体（不含其前注释），供禁词等逐字断言使用。"""
    start = html.index('<dialog class="dlg dlg-help"')
    end = html.index("</dialog>", start) + len("</dialog>")
    return html[start:end]


# 帮助载荷隔离求值用的注入常量：证明 help() 用的是外部版本来源，不是写死值
HELP_TEST_VERSION = "9.9.9-test"
HELP_TEST_URL = "https://example.invalid/releases/latest"


def load_backend_help() -> dict:
    """只取 app.py 里 Api.help 这一个方法，在隔离命名空间里求值后调用。

    不 import app.py：它的顶层会 import webview 并经 runtime_paths 触碰数据根，
    与本计划「不安装依赖、不读真实登录文件」的边界冲突。这里用 ast 抽出
    FunctionDef，单独编译并注入 APP_VERSION / RELEASES_LATEST_URL，
    因此拿到的是**真实方法的返回值**，不是源码子串的推测。
    """
    import ast

    tree = ast.parse(read("app.py"))
    node = next(item for item in ast.walk(tree)
                if isinstance(item, ast.FunctionDef) and item.name == "help"
                and [arg.arg for arg in item.args.args][:1] == ["self"])
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"APP_VERSION": HELP_TEST_VERSION, "RELEASES_LATEST_URL": HELP_TEST_URL}
    exec(compile(module, "<Api.help>", "exec"), namespace)  # noqa: S102 - 只编译受控片段
    return namespace["help"](None)


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
        # 行内未知文案：详情未回/已回但没数据分开说，绝不写成「无需」
        self.assertIn("签到时间待确认", js, "行内必须显示「签到时间待确认」而不是留空")
        self.assertIn("签退时间待确认", js, "行内必须显示「签退时间待确认」而不是留空")
        self.assertIn("签到信息同步中", js, "详情未返回时必须显示「签到信息同步中」")

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
        # check 事件载荷整理已下沉 application.login_service：三态字段随载荷迁移
        svc = read("application/login_service.py")
        for field in ("session_present", "session_expired"):
            self.assertIn('"%s"' % field, svc, "登录载荷缺字段 %s" % field)

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
        prefs = tmp / "ui.json"
        with mock.patch.object(local_app, "SESSION_FILE", session), \
             mock.patch.object(local_app, "PREFS", prefs):
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
            # 决策 012：无 v2 锚点不报数——旧 saved_at 不能冒充准确小时数
            self.assertIsNone(valid["hours_left"])

            stale = (datetime.now() - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S")
            session.write_text(json.dumps({"token": "t", "saved_at": stale}), encoding="utf-8")
            expired = local_app.login_state()
            self.assertTrue(expired["session_present"], "过期=登录过")
            # 旧墙钟文本不改判失效：有效性只由学校真实校验决定
            self.assertFalse(expired["session_expired"])
            self.assertIsNone(expired["hours_left"])

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


class LayerBoundaryContractTests(unittest.TestCase):
    """分层结构契约：呈现归 views，controller 不直接读写 DOM；加载序即依赖序。"""

    VIEW_BEFORE_DEPENDENT = (
        ("views/terms.js", "controllers/terms.js"),
        ("views/update.js", "controllers/update.js"),
        ("views/update.js", "views/help.js"),
    )

    def test_script_load_order_puts_views_before_dependents(self):
        html = read("ui/index.html")
        scripts = re.findall(r'<script src="([^"]+)"></script>', html)
        for view, dependent in self.VIEW_BEFORE_DEPENDENT:
            self.assertIn(view, scripts, "缺少 %s" % view)
            self.assertIn(dependent, scripts, "缺少 %s" % dependent)
            self.assertLess(scripts.index(view), scripts.index(dependent),
                            "%s 必须先于 %s 加载（经典 script 顺序即依赖序）"
                            % (view, dependent))

    def test_terms_and_update_controllers_do_not_render_dom(self):
        """条款/更新 controller 只做状态编排与桥接效果，DOM 查询与呈现都在 views。"""
        banned = (
            "$(", "document.", "querySelector", ".textContent", ".innerHTML",
            ".dataset.", "showModal", ".hidden = ", ".disabled = ", ".focus(",
            "classList", "scrollTop", "setAttribute", ".close()", "createElement",
        )
        for rel in ("ui/controllers/terms.js", "ui/controllers/update.js"):
            js = read(rel)
            for token in banned:
                self.assertNotIn(token, js,
                                 "%s 不得直接呈现 DOM：%s" % (rel, token))


class NameContractTests(unittest.TestCase):
    """正式名称契约：天软活动预报名工具；「准点」只留在历史说明里。"""

    def test_formal_name_present_everywhere(self):
        for rel in ("../README.md", "README.md", "../docs/UI_SPEC.md",
                    "ui/index.html", "app.py"):
            self.assertIn("天软活动预报名工具", read(rel),
                          "%s 必须包含正式名称「天软活动预报名工具」" % rel)

    def test_window_title_uses_formal_name(self):
        py = read("app.py")
        self.assertIn('"title": "使用帮助"', py, "帮助标题描述用途，正式名称仍由窗口标题表达")
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
        self.assertIn("继续计划", html, "确认弹窗必须有「继续计划」按钮")
        self.assertIn("停止计划", html, "确认弹窗必须有「停止计划」按钮")
        self.assertIn('value="no"', html, "「继续计划」提交 no，Esc 也走 no 通道")

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
        # 树终止 helper 已下沉 infrastructure.process_tree；任务监督的
        # 取消收尾契约在 application.plan_service（app.py 保留 stop_plan 入口）
        tree_src = read("infrastructure/process_tree.py")
        self.assertIn("def kill_tree", tree_src, "必须有进程树终止 helper")
        self.assertIn("taskkill", tree_src, "Windows 下必须按 taskkill /F /T 终止整棵进程树")
        plan_src = read("application/plan_service.py")
        self.assertIn("canceled or reader_done.is_set()",
                      plan_src, "取消路径的退出条件不得依赖管道 EOF（孙进程可能握着管道）")
        py = read("app.py")
        self.assertIn("_terminate_plan_procs", py, "stop_plan 必须主动终止活着的 worker")

    def test_grab_retry_window_is_hard_deadline(self):
        """所有阻塞必须压在同一个单调预算上（R03/R06）。

        旧实现按墙钟 deadline 逐个钳制，墙钟回拨就会把窗口拉长；现在
        reload/click/expect_response/goto 的超时都必须来自 RetryBudget，
        由单调截止与绝对截止取更紧者。行为证据在 test_worker_boundaries。
        """
        import ast
        source = read("grab.py")
        self.assertNotIn("def clamp_wait_ms", source, "旧的墙钟钳制 helper 必须移除")
        self.assertIn("RetryBudget(", source, "重试窗口必须用单调预算")
        tree = ast.parse(source)
        cmd = next(node for node in tree.body
                   if isinstance(node, ast.FunctionDef) and node.name == "cmd_grab")
        offenders = []
        for node in ast.walk(cmd):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"reload", "click", "expect_response", "goto"}:
                continue
            timeout = next((kw.value for kw in node.keywords if kw.arg == "timeout"), None)
            text = ast.unparse(timeout) if timeout is not None else "<none>"
            if "budget.timeout_ms" not in text:
                offenders.append((node.func.attr, text))
        self.assertFalse(offenders, "阻塞调用必须用预算超时：%r" % offenders)

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
        self.assertIn("S.login.present === false ? \"未登录\" : \"已登录\"", js,
                      "忙碌胶囊只表达登录状态，不替换成处理中")


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
        # R13：有限次数必须显示 N/M；0=不限要明说，不能只写「第 N 次」
        self.assertIn('"已提交 " + attempts + "/" + cap + " 次"', js,
                      "任务卡必须显示已提交 N/M 次（telemetry）")
        self.assertIn("（不限次数）", js, "不限次数必须明说，不能只给一个孤立数字")
        self.assertIn("last_reason", js, "任务卡必须显示最近原因")
        self.assertIn("stopReasonText", js, "缺少停止条件人话函数")
        for word in ("达到最大提交次数", "重试时长到了", "报名已截止", "登录失效"):
            self.assertIn(word, js, "终态文案缺「%s」" % word)
        # 路线图不再用固定 sign_start+120 冒充截止
        self.assertIn("telemetryDeadline", js, "重试截止必须优先用 telemetry")

    def test_backend_contract_strings(self):
        # worker 命令行已随任务监督下沉 application.plan_service
        plan_src = read("application/plan_service.py")
        self.assertIn("--sign-end", plan_src, "worker 命令必须传报名截止")
        self.assertIn("--retry-for", plan_src, "worker 命令必须传重试时长")
        self.assertIn("--max-attempts", plan_src, "worker 命令必须传最大提交次数")
        py = read("app.py")
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
        # R15：登录剩余只是估算，不能给「一定撑得到」的保证句
        self.assertIn("预计可覆盖本次窗口", js, "检查条必须用「预计」而不是保证语气")
        self.assertNotIn("登录撑得到这次报名窗口", js,
                         "不得再用保证式结论：登录有效性只由学校校验决定")

    def test_clock_skew_structured_and_not_text_scraped(self):
        """R08：学校 HTTP 粗测只认结构化载荷，逐轮替换，不再从文本猜。"""
        js = read("ui/app.js")
        self.assertIn("function applyClock", js, "必须有一个结构化学校时间入口")
        self.assertIn("S.clock = {", js, "学校粗测必须按状态（ok/warn/unknown）整份替换")
        self.assertIn("clock-pop", js, "必须有时钟详情浮层 #clock-pop")
        self.assertIn("粗测", js, "必须明确这是粗测、仅作诊断")
        self.assertNotIn("#clock-skew", js, "顶栏不再放长状态徽章")
        self.assertNotIn("本机时钟与服务器相差", js,
                         "不得再从人读文本解析偏差：失败文本不能变成确定结论")
        self.assertNotIn("S.clock.checked =", js, "旧的 checked 布尔已被状态机取代")

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
        # check 事件载荷整理已下沉 application.login_service
        svc = read("application/login_service.py")
        self.assertIn('"hint": check_hint', svc, "check 事件必须带可区分的 hint 分类字段")
        py = read("app.py")
        self.assertIn("def _check_hint", py, "缺少 hint 分类函数")


class DualModeUiContractTests(unittest.TestCase):
    """双模式契约（静态）：模式文案、开放优先、已截止禁选、路线独立
    checkpoint、统一 deadline、startPlan 载荷与恢复合并不丢运行时字段。"""

    def test_mode_of_and_mode_texts(self):
        """modeOf 三分类与三种正式模式文案必须存在（预报名/立即报名/已截止）。"""
        js = read("ui/app.js")
        self.assertIn("function modeOf(", js, "缺少模式分类函数 modeOf")
        self.assertIn('pre_register: "预报名"', js, "缺少预报名文案")
        self.assertIn('instant_signup: "立即报名"', js, "缺少立即报名文案")
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
        # 后端最终防线：拒绝策略在 domain.plan_rules，start_plan 仍走这条门禁
        rules = read("domain/plan_rules.py")
        self.assertIn("sign_end_passed", rules,
                      "start_plan 必须拒绝已截止活动（后端最终防线）")
        self.assertIn("报名已截止", rules, "拒绝时要返回具体说明")
        py = read("app.py")
        start_plan = py.index("def start_plan")
        plan_body = py[start_plan:py.index("def cancel_task", start_plan)]
        self.assertIn("closed_items_error", plan_body,
                      "start_plan 必须经 closed_items_error 过已截止门禁")

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
        self.assertIn("done: openDone", body,
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
        self.assertIn("taskModePrefix(task)", body, "任务卡必须使用统一的模式文案")
        cards = run_js(r"""
const task = { task_id: "mode", name: "模式样本", status: "waiting",
  sign_start: "2099-01-01 09:00:00" };
out.push(taskHtml({ ...task, signup_mode: "pre_register" }));
out.push(taskHtml({ ...task, signup_mode: "instant_signup" }));
""")
        self.assertIn("预报名 · ", cards[0])
        self.assertIn("立即报名 · ", cards[1])


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
        for needed in ("待启动", "待打开", "待开始"):
            self.assertIn(needed, body, "缺 checkpoint 的站点必须显示「%s」" % needed)
        # 真实 checkpoint 仍是三个执行站点唯一的时间来源
        for field in ("run_started_at", "page_opened_at", "watch_started_at"):
            self.assertIn(field, body, "路线站点必须消费真实字段 %s" % field)

    def test_worker_payload_carries_lead(self):
        """worker 载荷显式携带与 TaskSpec 一致的 --lead；快照携带 lead_s。"""
        # worker 命令行已随任务监督下沉 application.plan_service
        plan_src = read("application/plan_service.py")
        self.assertIn('"--lead", str(max(0, int(spec.lead_s or 0)))', plan_src,
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


class TourAndLifecycleContractTests(unittest.TestCase):
    """六步可重播引导 + 生命周期提示的静态契约（决策 009 / UI_SPEC §5.11）。"""

    def test_index_has_six_tour_targets(self):
        """六个稳定高亮锚点：登录/列表/筛选/选择/开始/运行。"""
        html = read("ui/index.html")
        for key in ("login", "list", "filters", "select", "start", "run"):
            self.assertIn('data-tour-target="%s"' % key, html,
                          "缺少第 %s 步的高亮锚点 data-tour-target" % key)

    def test_index_has_tour_overlay_and_replay_entry(self):
        """引导遮罩结构 + 两处重播入口（「更多」与说明底部，共用一条路径）。"""
        html = read("ui/index.html")
        for needle in ('id="tour"', 'id="tour-card"', 'id="tour-spot"',
                       'id="tour-skip"', 'id="tour-prev"', 'id="tour-next"',
                       'id="tour-step"', 'id="tour-dots"'):
            self.assertIn(needle, html, "引导结构缺 %s" % needle)
        self.assertIn('id="btn-help-tour"', html, "「更多」里必须有「重看引导」入口")
        self.assertIn('id="btn-help-tour"', html,
                      "说明底部必须有重播快捷入口（与更多共用同一条处理路径）")
        self.assertIn("重看引导", html, "重播入口要有可读文字，不能只靠图标")
        self.assertIn('id="help"', html, "必须有独立 dialog #help")
        self.assertIn("跳过引导", html, "必须有跳过按钮")

    def test_app_js_tour_definition_and_version(self):
        """TOUR_VERSION=2（内容实质变化）、六步定义、完成/跳过存储键与立即保存。"""
        js = read("ui/app.js")
        self.assertIn("const TOUR_VERSION = 2", js,
                      "六步文案已纠正「选中即执行」，内容版本必须升到 2")
        self.assertGreaterEqual(js.count("target: "), 6, "TOUR_STEPS 必须定义六步")
        for key in ("tour_version", "tour_completed_at", "tour_skipped_at"):
            self.assertIn(key, js, "引导状态键 %s 必须写入偏好" % key)
        self.assertIn("savePrefsNow", js, "完成/跳过必须立即落盘，不等防抖")
        # 六步标题齐全（顺序在数组里固定）
        for title in ("登录账号", "查看活动", "筛选活动", "选择活动", "设置并开始", "查看结果"):
            self.assertIn('title: "%s"' % title, js, "TOUR_STEPS 缺「%s」步骤" % title)

    def test_tour_steps_do_not_promise_selection_starts(self):
        """选择不等于开始：引导必须写清「选择活动不会自动开始报名」。"""
        js = read("ui/app.js")
        steps = js[js.index("TOUR_STEPS = ["):js.index("const S = {")]
        self.assertNotIn("选中后马上执行", steps,
                         "引导不得暗示选中即执行")
        self.assertIn("选择活动不会自动开始报名", steps, "第 2 步要写清选择不等于开始")
        self.assertIn("再选择「开始报名计划」", steps, "第 5 步要写清真正的开始动作")
        # 第 5 步讲停止条件，第 6 步讲任务/计划结果分层与停止语义
        self.assertIn("工具停止尝试", steps, "第 5 步要讲清三种停止条件")
        self.assertIn("整块大屏的结论要等整个计划结束", steps,
                      "第 6 步要区分任务结果与计划结果")
        self.assertIn("停止计划不会取消已成功的报名", read("ui/index.html"),
                      "停止确认要说明不撤销学校报名")

    def test_tour_never_triggers_backend_actions(self):
        """引导只做说明：开始/推进/跳过引导的代码不得调用任何桥接动作。"""
        js = read("ui/app.js")
        start = js.index("function tourShouldAutoStart()")
        end = js.index("/* ---------------------------------------------------------------- 事件绑定 */")
        body = js[start:end]
        for banned in ("api(\"login\"", "api(\"start_plan\"", "api(\"check\"",
                       "api(\"list_activities\"", "toggleSelection(", "startPlan()"):
            self.assertNotIn(banned, body,
                             "引导逻辑不得代替用户执行动作：%s" % banned)

    def test_tour_skip_pause_and_narrow_fallback(self):
        """跳过后同版不再自动弹；窄窗（≤980）与目标缺失退回底部说明条；运行中暂停。"""
        js = read("ui/app.js")
        self.assertIn("tourShouldAutoStart", js, "自动开始必须有判定函数")
        self.assertIn("seen >= TOUR_VERSION", js,
                      "自动开始只按已看过的内容版本判定（旧时间戳不得拦截新版）")
        self.assertIn("window.innerWidth > 980", js, "窄窗（≤980）必须退回底部说明条")
        self.assertIn('classList.toggle("is-bottom"', js, "回退态必须挂 is-bottom 类")
        self.assertIn("tourPauseNow", js, "计划运行中必须暂停引导")
        self.assertIn("tourResumeNow", js, "计划结束后必须可恢复引导")
        # Esc 等同跳过
        esc_at = js.index('if (event.key === "Escape")')
        esc_body = js[esc_at:esc_at + 400]
        self.assertIn("tourSkip()", esc_body, "Esc 在引导打开时必须先走跳过")

    def test_chrome_login_window_text_only(self):
        """第 1 步必须说明浏览器登录窗口是系统窗口、引导无法高亮。"""
        js = read("ui/app.js")
        first_step = js[js.index("TOUR_STEPS = ["):js.index('{ target: "list"')]
        self.assertIn("浏览器", first_step, "登录步骤不得硬编码 Chrome")
        self.assertNotIn("Chrome", first_step, "登录步骤不得硬编码 Chrome")

    def test_lifecycle_display_and_estimate_reminder(self):
        """生命周期提示：离线时长 abnormal 带「约」；登录估算同源且只是提醒；valid 不被估算压成失效。"""
        js = read("ui/app.js")
        self.assertIn("S.lifecycle = info.lifecycle || null", js,
                      "bootstrap 的 lifecycle 快照必须消费")
        self.assertIn("上次退出至本次启动", js, "时间详情必须显示上一周期的固定离线间隔")
        self.assertIn("约 ", js, "abnormal/估算值必须带「约」")
        self.assertIn("以学校校验为准", js, "登录估算必须注明以学校校验为准")
        # R4：浮层剩余与胶囊同源（S.login 锚点），不得读冻结的生命周期快照
        pop_body = js[js.index("function toggleClockPop("):js.index("function paintClockPopLeft")]
        self.assertNotIn("life.login", pop_body,
                         "浮层登录剩余必须用当前锚点，不得用 bootstrap 冻结的旧快照")
        self.assertIn("S.login.expireAt", pop_body, "浮层剩余必须与胶囊同一来源")
        self.assertIn('S.login.result === "valid" && typeof h === "number" && h <= 0',
                      js, "校验有效但估算到点必须单独分支（不得落失效分支）")
        # 估算不参与开始门禁：门禁仍只看 result === "valid"
        gate = js[js.index("function updateControls("):js.index("function paintBanners")]
        self.assertNotIn("estimated_expires_at", gate,
                         "生命周期估算不得进入开始按钮门禁")
        self.assertNotIn("lifecycle", gate, "生命周期字段不得参与门禁")

    def test_releases_latest_link_is_user_triggered(self):
        """GitHub releases/latest：只由用户点击打开，启动时不自动联网。"""
        html = read("ui/index.html")
        self.assertNotIn("<a ", help_dialog(html),
                         "发布页不得变成 WebView 内导航链接")
        js = read("ui/app.js")
        self.assertIn("const RELEASES_LATEST_URL =", js, "外链地址必须是固定常量")
        self.assertIn('api("open_latest_release")', js, "打开外链必须走后端动作（点击触发）")
        handler = js[js.index('$("#btn-help-latest").addEventListener'):
                     js.index('$("#btn-help-data").addEventListener')]
        self.assertIn("RELEASES_LATEST_URL", handler,
                      "打不开时要把固定地址摆成可选文本，让用户能自己复制")
        # 启动路径（boot/refresh）不得自动请求外链
        boot_body = js[js.index("async function boot()"):js.index("function applyPrefs")]
        self.assertNotIn("open_latest_release", boot_body, "启动时不得自动打开/请求 GitHub")


    def test_tour_does_not_gate_login_or_start(self):
        """引导未完成/进行中都不影响登录与 start_plan 门禁（门禁只看登录四态）。

        断言锁 S.tour 引导状态（而非 "tour" 子串——#btn-help-tour 的
        disabled 引用是设置功能，不是引导门禁）。
        """
        js = read("ui/app.js")
        gate = js[js.index("function updateControls("):js.index("function paintBanners")]
        self.assertNotIn("S.tour", gate,
                         "开始按钮门禁不得引用引导状态")
        auto = js[js.index("function tourShouldAutoStart()"):js.index("function startTour")]
        self.assertNotIn("S.login", auto,
                         "引导自动开始不读登录状态：未登录也要能看引导")
        # 引导也不改变登录按钮：paintLogin 的四态分支里没有引导概念
        paint = js[js.index("function paintLogin()"):js.index("function setBusy")]
        self.assertNotIn("tour", paint, "登录胶囊渲染与引导互不影响")

    def test_settings_dialog_shows_version(self):
        """「更多」显示后端版本（VERSION 唯一来源），帮助载荷带版本与外链；
        旧说明弹窗版本位 id 不得残留。"""
        js = read("ui/app.js")
        anchor = js.index('$("#help-version").textContent')
        self.assertIn("info.version", js[anchor:anchor + 160],
                      "版本必须来自 bootstrap.version（唯一来源）")
        self.assertIn('$("#help-version")', js, "版本必须填入更多弹窗唯一位置")
        html = read("ui/index.html")
        self.assertIn('id="help-version"', html, "设置弹窗必须有版本位")
        self.assertEqual(html.count('id="help-version"'), 1, "当前版本只出现一个真实节点")
        # 只锁字符串字面量：注释里提及历史候选号不算硬编码
        self.assertNotIn('"1.1.0"', js, "界面代码不得写死候选版本号字符串")
        self.assertNotIn("'1.1.0'", js, "界面代码不得写死候选版本号字符串")
        py = read("app.py")
        self.assertIn('"version": APP_VERSION', py, "bootstrap/help 用 APP_VERSION")
        self.assertIn('"latest_url": RELEASES_LATEST_URL', py,
                      "help 载荷要带最新版本入口信息")

    def test_unknown_attendance_copy_stays(self):
        """未知签到/签退必须说清「同步中/待确认」，且仍被需要/无需筛选排除。"""
        js = read("ui/app.js")
        html = read("ui/index.html")
        self.assertIn("签到信息同步中", js)
        self.assertIn("签到时间待确认", js)
        self.assertIn("无需签到", js, "明确无需必须有独立文案，不能与未知混用")
        self.assertNotIn("签到待说明", js, "旧「待说明」口径已细分，不得回退")
        for key in ("checkin", "checkout"):
            self.assertIn('id="%s"' % key, html)


# --------------------------------------------------------------------- 行为测试装置
# 在 node vm 里加载生产 app.js + 最小 DOM 桩，执行代码片段并取回断言输出。
# 不开浏览器、不装依赖：node 是项目既有工具链（服务器机器人同用）。
# 桩的边界：querySelector 按选择器缓存同一元素；focus 记录到 focused；
# document 只捕获 keydown 监听（__dispatchKey 合成按键）；定时器只记录不调度。

JS_HARNESS = r"""
"use strict";
const vm = require("vm");
const fs = require("fs");
const path = require("path");
const uiDir = process.argv[2];
const snippetFile = process.argv[3];

const out = [];
const focused = [];
const els = new Map();
const keydownListeners = [];

function makeEl(sel) {
  const el = {
    __sel: sel, hidden: false, disabled: false, checked: false,
    value: "", textContent: "", title: "", innerHTML: "", returnValue: "",
    tagName: "DIV", open: false,
    dataset: {}, style: {}, _cls: new Set(),
    listeners: {},
  };
  el.classList = {
    toggle(c, on) {
      if (on === undefined) { if (el._cls.has(c)) el._cls.delete(c); else el._cls.add(c); }
      else if (on) el._cls.add(c); else el._cls.delete(c);
    },
    add(c) { el._cls.add(c); },
    remove(c) { el._cls.delete(c); },
    contains(c) { return el._cls.has(c); },
  };
  el.addEventListener = (type, fn) => { (el.listeners[type] = el.listeners[type] || []).push(fn); };
  el.removeEventListener = () => {};
  el.setAttribute = (k, v) => { el["attr_" + k] = v; };
  el.getAttribute = (k) => el["attr_" + k];
  el.focus = () => { sandbox.document.activeElement = el; focused.push(sel); };
  el.blur = () => { if (sandbox.document.activeElement === el) sandbox.document.activeElement = null; };
  el.select = () => {};
  el.click = () => (el.listeners.click || []).forEach((fn) => fn({ target: el, preventDefault() {} }));
  el.closest = () => null;
  el.contains = () => true;
  /* <dialog> 原生方法桩：设置/说明/停止确认的 open 状态靠它们翻转 */
  el.showModal = () => { el.open = true; };
  el.close = () => { el.open = false; };
  /* 子节点按真实语义记账：addLog 的「副作用有没有重复」需要数得出来 */
  el._children = [];
  el.appendChild = (child) => {
    el._children.push(child);
    el.childElementCount = el._children.length;
    return child;
  };
  el.removeChild = (child) => {
    const at = el._children.indexOf(child);
    if (at >= 0) el._children.splice(at, 1);
    el.childElementCount = el._children.length;
    return child;
  };
  el.insertBefore = (child) => { el._children.unshift(child); el.childElementCount = el._children.length; return child; };
  el.childElementCount = 0;
  el.childNodes = [];
  Object.defineProperty(el, "firstChild", {
    get() { return el._children[0] || null; },
  });
  el.getBoundingClientRect = () => ({ top: 100, left: 100, bottom: 140, right: 460, width: 360, height: 40 });
  el.getClientRects = () => [1];
  el.offsetWidth = 360;
  el.offsetHeight = 40;
  el.querySelector = (s) => el2(sel + " " + s);
  el.querySelectorAll = (s) => [el2(sel + " " + s)];
  return el;
}

function el2(sel) {
  if (!els.has(sel)) els.set(sel, makeEl(sel));
  return els.get(sel);
}

/* 单调钟独立于 Date：初值与 Date.now 同源（既有用例仍可用 Date.now 伪造
 * 「很久以前」的缓存时间），用例可再用 __advanceMono 单独推进经过时长，
 * 不会用 Date 假跳变同时推进 performance 导致红例失真。 */
const monoState = { base: Date.now(), offset: 0 };
const sandbox = {
  console, Date, Math, JSON, Promise, Object, Array, Number, String, Boolean, RegExp, Error,
  isNaN, parseInt, parseFloat,
  performance: { now: () => monoState.base + monoState.offset },
  __advanceMono: (ms) => { monoState.offset += Number(ms) || 0; },
  __monoValue: () => monoState.base + monoState.offset,
  innerWidth: 1120, innerHeight: 800,
  setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
  navigator: { onLine: true },
  /* window.addEventListener/removeEventListener：app.js 顶层注册 resize/online/offline 用 */
  addEventListener: () => {}, removeEventListener: () => {},
  out, focused, __els: els,
  requestAnimationFrame: () => 0,
  __dispatchKey: (ev) => {
    const e = Object.assign(
      { ctrlKey: false, shiftKey: false, altKey: false, defaultPrevented: false,
        target: { closest: () => null, tagName: "BODY", blur() {} } }, ev);
    e.preventDefault = () => { e.defaultPrevented = true; };
    keydownListeners.slice().forEach((fn) => fn(e));
    return e;
  },
  document: {
    hidden: false, activeElement: null,
    querySelector: (s) => el2(s),
    querySelectorAll: (s) => [el2(s)],
    createElement: (tag) => el2("<" + tag + ">"),
    createTextNode: (text) => ({ nodeType: 3, textContent: String(text) }),
    addEventListener: (t, fn) => { if (t === "keydown") keydownListeners.push(fn); },
    removeEventListener: () => {},
  },
};
sandbox.window = sandbox;
sandbox.document.body = el2("body");
vm.createContext(sandbox);
/* 结构重构后界面按层拆成多脚本：按 index.html 的加载序喂进同一个沙箱，
 * 与真实 WebView 顺序一致。led.js 不进沙箱——字库渲染器只在真实 DOM 有
 * 意义，且历史行为就是 LED_UI 缺席时 ledSet 回退纯文本层。 */
const html = fs.readFileSync(path.join(uiDir, "index.html"), "utf8");
const scripts = Array.from(html.matchAll(/<script src="([^"]+)"><\/script>/g))
  .map((m) => m[1]).filter((src) => src !== "led.js");
for (const src of scripts) {
  vm.runInContext(fs.readFileSync(path.join(uiDir, src), "utf8"), sandbox, { filename: src });
}
/* 决策 012：node 桩显式注入可信假时钟（锚定 Date.now），不以 Date.now 作默认成功桩 */
vm.runInContext(
  "ingestBusinessClock({ schema: 1, owner_id: 'harness-clock', generation: 1," +
  " state: 'ready', epoch_s: Date.now() / 1000, tick_s: 0, sampled_tick_s: 0," +
  " uncertainty_s: 0, source: 'harness', age_s: 0, reason: '测试注入' });" +
  /* 用例把「业务现在」定到任意时刻：注入新一代快照，不再改 Date.now（墙钟不是权威） */
  "globalThis.setBusinessNow = (ms) => ingestBusinessClock({" +
  " schema: 1, owner_id: 'harness-clock'," +
  " generation: (S.businessClock.gen || 0) + 1, state: 'ready'," +
  " epoch_s: ms / 1000, tick_s: 0, sampled_tick_s: 0, uncertainty_s: 0," +
  " source: 'harness', age_s: 0, reason: '测试注入' });",
  sandbox, { filename: "clock-inject.js" });
/* 片段统一包成 async IIFE：顶层 await 可用（受控 Promise 次序），同步片段
 * 返回的 promise 立即完成，输出时序不变 */
const snippetSource = fs.readFileSync(snippetFile, "utf8");
const snippetResult = vm.runInContext(
  "(async () => {\n" + snippetSource + "\n})()",
  sandbox, { filename: "snippet.js" });
const finishHarness = () => process.stdout.write("__RESULT__" + JSON.stringify(out));
snippetResult.then(finishHarness, (err) => { process.stderr.write(String(err)); process.exit(1); });
"""


def run_js(snippet: str, env: dict | None = None):
    """在受控 JS 上下文里执行片段，返回 out 数组（list）。node 缺失时 skip。

    env 用于按用例注入 TZ（例如证明学校时间解析不随本机时区漂移）；
    默认不改进程环境。
    """
    import json
    import os
    import shutil
    import subprocess
    import tempfile
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("本机没有 node，无法运行 JS 行为测试")
    with tempfile.TemporaryDirectory() as name:
        harness = Path(name) / "harness.js"
        part = Path(name) / "snippet.js"
        harness.write_text(JS_HARNESS, encoding="utf-8")
        part.write_text(snippet, encoding="utf-8")
        child_env = dict(os.environ)
        child_env.update(env or {})
        res = subprocess.run([node, str(harness), str(LOCAL / "ui"), str(part)],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=30, env=child_env)
        if res.returncode != 0:
            raise AssertionError("JS 行为测试崩溃：%s" % res.stderr[-800:])
        marker = res.stdout.rfind("__RESULT__")
        if marker < 0:
            raise AssertionError("JS 行为测试没有输出结果：%s" % res.stdout[-500:])
        return json.loads(res.stdout[marker + len("__RESULT__"):].strip() or "[]")


class ClockModelReliabilityTests(unittest.TestCase):
    """CB04/CB06：采样后漂移只计一次、真实校准年龄、单调 TTL。"""

    def test_sampled_uncertainty_does_not_double_count_age(self):
        res = run_js(r"""
const t0 = 1700000000000;
ingestBusinessClock({ schema: 1, owner_id: "harness-clock", generation: 10, state: "ready",
  epoch_s: t0 / 1000, tick_s: 0, sampled_tick_s: 0,
  uncertainty_s: 0.47, source: "ntp", age_s: 400, reason: "测试注入" }, { rttMs: 0 });
const first = clockReading();
const ui1 = clockUiState();
const now1 = businessNowMs();
__advanceMono(10000);
const second = clockReading();
const ui2 = clockUiState();
const now2 = businessNowMs();
__advanceMono(490001);        /* age = 900.001s：真正过期仍禁用 */
const ui3 = clockUiState();
const now3 = businessNowMs();
ingestBusinessClock({ schema: 1, owner_id: "harness-clock", generation: 11, state: "ready",
  epoch_s: t0 / 1000 + 1000, tick_s: 0, sampled_tick_s: 0,
  uncertainty_s: 0.5001, source: "ntp", age_s: 0, reason: "测试注入" }, { rttMs: 0 });
const ui4 = clockUiState();
const now4 = businessNowMs();
out.push({ unc1: first.uncertaintyMs, unc2: second.uncertaintyMs,
  age1: first.ageMs, age2: second.ageMs, deltaNow: now2 - now1,
  ui1, ui2, ui3, now3, ui4, now4, now1 });
""")
        row = res[0]
        self.assertIn(row["ui1"], ("ready", "holdover"),
                      "采样 age=400s、unc=0.47s 时后端仍可用，UI 不得提前禁用")
        self.assertIsNotNone(row["now1"])
        self.assertAlmostEqual(row["unc1"], 470.0, delta=0.5)
        self.assertAlmostEqual(row["unc2"] - row["unc1"], 1.0, delta=0.2,
                               msg="10 秒只增加约 1ms 漂移，不得再加采样前 40ms")
        self.assertAlmostEqual(row["age1"], 400000.0, delta=1.0)
        self.assertAlmostEqual(row["age2"], 410000.0, delta=1.0)
        self.assertAlmostEqual(row["deltaNow"], 10000.0, delta=1.0)
        self.assertIn(row["ui2"], ("ready", "holdover"))
        self.assertEqual(row["ui3"], "unavailable", "超过 900 秒仍必须禁用")
        self.assertIsNone(row["now3"])
        self.assertEqual(row["ui4"], "unavailable", "总误差超过 0.5 秒仍必须禁用")
        self.assertIsNone(row["now4"])

    def test_f5_old_anchor_reports_real_calibration_age(self):
        res = run_js(r"""
ingestBusinessClock({ schema: 1, owner_id: "harness-clock", generation: 10, state: "ready",
  epoch_s: Date.now() / 1000, tick_s: 0, sampled_tick_s: 600,
  uncertainty_s: 0.05, source: "ntp", age_s: 600, reason: "测试注入" });
out.push({ last: clockLastSyncText(), ui: clockUiState(),
  usable: businessNowMs() !== null });
""")
        row = res[0]
        self.assertEqual(row["last"], "10 分钟前",
                         "F5 接入 600 秒旧锚点必须显示真实年龄，不能刷新成「刚刚」")
        self.assertEqual(row["ui"], "holdover")
        self.assertTrue(row["usable"])

    def test_round_cache_ttl_uses_monotonic_elapsed(self):
        res = run_js(r"""
const realNow = Date.now;
let wall = 1700000000000;
Date.now = () => wall;
try {
  cacheRoundPayload("check", "r1", { ok: true });
  wall += 8 * 3600e3;                 /* OS 前移 8 小时 */
  const kept = takeRoundPayload("check", "r1");
  cacheRoundPayload("check", "r2", { ok: true });
  __advanceMono(130001);              /* 真正经过 130 秒才过期 */
  const expired = takeRoundPayload("check", "r2");
  cacheRoundPayload("check", "r3", { ok: true });
  wall -= 30 * 86400e3;               /* OS 回拨 30 天 */
  const keptBack = takeRoundPayload("check", "r3");
  out.push({ kept, expired, keptBack });
} finally {
  Date.now = realNow;
}
""")
        row = res[0]
        self.assertIsNotNone(row["kept"], "OS 前移不得让刚暂存的终态立刻过期")
        self.assertIsNone(row["expired"], "真正单调超过 130 秒必须过期")
        self.assertIsNotNone(row["keptBack"], "OS 回拨不得延长缓存期限")

    def test_skipped_ack_replays_cached_terminal_across_wall_jumps(self):
        for offset in (8 * 3600e3, -30 * 86400e3):
            res = run_js(r"""
const realNow = Date.now;
let wall = 1700000000000;
Date.now = () => wall;
try {
  S.round.check = "owner-new";
  S.round.gen.check = 1;
  S.login.checking = true;
  S.login.result = null;
  window.onBackend.check({ request_id: "owner-old", ok: true, result: "valid",
    session_present: true, hours_left: 3 });
  wall += %d;
  settleSynchronous({ skipped: true, request_id: "owner-old" }, "check");
  out.push({ result: S.login.result, checkId: S.round.check,
    cacheLeft: S.round.cache.length });
} finally {
  Date.now = realNow;
}
""" % offset)
            row = res[0]
            self.assertEqual(row["result"], "valid",
                             "OS 跳变 %s 后 skipped ack 仍须立即重放暂存终态" % offset)
            self.assertEqual(row["checkId"], "owner-old")
            self.assertEqual(row["cacheLeft"], 0)

    def test_true_monotonic_expiry_drops_cached_terminal(self):
        res = run_js(r"""
S.round.check = "owner-new";
S.login.checking = true;
window.onBackend.check({ request_id: "owner-old", ok: true, result: "valid",
  session_present: true, hours_left: 3 });
__advanceMono(130001);
settleSynchronous({ skipped: true, request_id: "owner-old" }, "check");
out.push({ result: S.login.result, cacheLeft: S.round.cache.length });
""")
        row = res[0]
        self.assertIsNone(row["result"], "真正超过 130 秒后不得重放暂存终态")
        self.assertLessEqual(row["cacheLeft"], 1, "过期项不得被取用重放")

    def test_wait_for_bridge_uses_monotonic_bound(self):
        res = run_js(r"""
const realNow = Date.now;
const realTimeout = setTimeout;
let wall = 1700000000000;
Date.now = () => wall;
setTimeout = (fn) => { __advanceMono(50); wall += 3600e3; fn(); return 1; };
const start = __monoValue();
try {
  const ok = await waitForBridge(15000);
  out.push({ ok: ok, elapsed: __monoValue() - start });
} finally {
  Date.now = realNow;
  setTimeout = realTimeout;
}
""")
        row = res[0]
        self.assertFalse(row["ok"])
        self.assertGreaterEqual(row["elapsed"], 14950,
                                "15 秒承诺必须按单调经过时长，不能被 OS 前移提前结束")


class UnknownClockDisplayTests(unittest.TestCase):
    """CB05：校时失败不得冒充「学校还没公布报名时间」。"""

    ITEMS = r"""
const items = [
  { id: "a", name: "已知开放时间", sign_start: "2026-10-27 09:00:00",
    sign_end: "2026-10-27 18:00:00", act_start: "2026-10-28 09:00:00",
    act_end: "2026-10-28 11:00:00", place: "A", signed: false },
  { id: "b", name: "已报名活动", sign_start: "2026-10-27 10:00:00",
    sign_end: "2026-10-27 18:00:00", act_start: "2026-10-28 09:00:00",
    act_end: "2026-10-28 11:00:00", place: "B", signed: true, state: "已报名" },
  { id: "c", name: "时间未公布", sign_start: "",
    sign_end: "", act_start: "2026-10-29 09:00:00",
    act_end: "2026-10-29 11:00:00", place: "C", signed: false }
];
applyActivities({ ok: true, items: items }, true);
"""

    def test_known_start_without_trusted_now_is_pending_calibration(self):
        res = run_js(self.ITEMS + r"""
S.selected.clear();
markClockUnavailable("校时失败");
render();
const rows = $("#rows").innerHTML;
out.push({ rows, board: $("#selected").textContent,
  boardText: $("#board-text").innerHTML,
  metric: $("#metric-free").textContent });
""")
        row = res[0]
        rows = row["rows"]
        # A 有已知开放时间：保留绝对时间并标「待校准」，不得进「报名时间待公布」
        row_a = rows.split('data-id="a"', 1)[1].split('data-id="b"', 1)[0]
        self.assertIn("待校准", row_a)
        self.assertIn("09:00", row_a, "已知绝对时间必须保留")
        # C 真正缺 sign_start：仍然是「待公布」
        row_c = rows.split('data-id="c"', 1)[1]
        self.assertIn("待公布", row_c)
        self.assertRegex(rows, r"报名时间待公布.*?count\">1 场",
                         "待公布组只能包含真正缺开放时间的活动")
        # B 已报名事实保留
        row_b = rows.split('data-id="b"', 1)[1].split('data-id="c"', 1)[0]
        self.assertIn("已报名", row_b)
        # 大屏：不得断言没有可报名场次，只给时间待校准与单一恢复入口
        self.assertEqual(row["board"], "时间待校准")
        self.assertNotIn("没有可报名的场次", row["board"] + row["boardText"])
        self.assertIn("校准北京时间", row["boardText"])
        self.assertEqual(row["boardText"].count("data-open-clock"), 1,
                         "恢复入口必须唯一")
        self.assertEqual(row["metric"], "待确认", "未知计数不得伪造 0 或 6")

    def test_recovery_recomputes_groups_counts_and_keeps_selection(self):
        res = run_js(self.ITEMS + r"""
S.selected.clear();
S.selected.add("a");
S.retryPolicy = { retry_for_s: 600, max_attempts: 2 };
markClockUnavailable("校时失败");
render();
const beforeMetric = $("#metric-free").textContent;
const beforeCountdown = $("#countdown").textContent;
ingestBusinessClock({ schema: 1, owner_id: "harness-clock", generation: 30,
  state: "ready", epoch_s: Date.now() / 1000, tick_s: 0, sampled_tick_s: 0,
  uncertainty_s: 0.05, source: "harness", age_s: 0, reason: "恢复" });
S.businessClock.attempted = true;
render();
const rows = $("#rows").innerHTML;
out.push({ beforeMetric, beforeCountdown,
  metric: $("#metric-free").textContent,
  countdown: $("#countdown").textContent,
  selected: Array.from(S.selected), policy: S.retryPolicy,
  rowA: rows.split('data-id="a"')[1].split('data-id="b"')[0] });
""")
        row = res[0]
        self.assertEqual(row["beforeMetric"], "待确认")
        self.assertIn("待校准", row["beforeCountdown"])
        self.assertRegex(row["metric"], r"^\d+$",
                         "恢复后计数必须回到真实数字")
        self.assertNotIn("待校准", row["countdown"],
                         "恢复后倒计时必须原地重算")
        self.assertEqual(row["selected"], ["a"], "恢复不得丢掉选择")
        self.assertEqual(row["policy"], {"retry_for_s": 600, "max_attempts": 2},
                         "恢复不得重置重试策略")
        self.assertNotIn("待校准", row["rowA"])
        self.assertNotIn("待公布", row["rowA"])

    def test_runtime_component_copy_points_to_redownload_and_recheck(self):
        res = run_js(r"""
out.push({ hint: RUNTIME_ERRORS.worker_missing.hint,
  title: RUNTIME_ERRORS.worker_missing.title });
""")
        row = res[0]
        self.assertIn("无法找到所需程序组件", row["hint"])
        self.assertIn("重新下载完整版本", row["hint"])
        self.assertIn("重新检查", row["hint"])
        self.assertNotIn("没跟着程序", row["hint"])


class LoginEstimateBehaviorTests(unittest.TestCase):
    """R4：登录剩余同一来源、绝对时刻计时、刷新不续期（node vm 行为，不调用学校）。"""

    def test_relogin_swaps_anchor_without_reload(self):
        """启动旧估算 → 窗口内重新登录 → check 事件带新锚点：不重载窗口也同步换新。"""
        res = run_js(r"""
const H = 3600e3;
const now = Date.now();
/* bootstrap：旧凭证还剩 1 小时，随后第一次校验回来（ok=true，与真实启动时序一致） */
setLogin(1, null, true, { session_present: true, saved_at: "s-old", estimated_expires_at: (now + 1 * H) / 1000 });
window.onBackend.check({ ok: true, result: "valid", hours_left: 1,
  session_present: true, estimated_expires_at: (now + 1 * H) / 1000, saved_at: "s-old" });
const before = S.login.expireAt;
/* 重新登录成功：旧锚点立即作废（未知，等 check 带新锚点） */
window.onBackend.done({ kind: "login", ok: true });
const cleared = S.login.expireAt;
/* check 回来：新锚点 = 新 saved_at + 17.5h */
window.onBackend.check({ ok: true, result: "valid", hours_left: 17.5,
  session_present: true, estimated_expires_at: (now + 17.5 * H) / 1000, saved_at: "s-new" });
out.push({ before, cleared, after: S.login.expireAt,
  pillText: document.querySelector("#pill-text").textContent,
  pillSub: document.querySelector("#pill-sub").textContent });
""")
        self.assertEqual(res[0]["cleared"], None, "重登成功后旧锚点必须作废")
        self.assertGreater(res[0]["after"], res[0]["before"] + 8 * 3600e3,
                           "check 事件必须换上重登后的新锚点")
        self.assertEqual(res[0]["pillText"], "已登录")

    def test_anchor_counts_down_with_clock(self):
        """时钟推进两小时：锚点不动，各处剩余同步减两小时（不重开窗口、不刷新）。"""
        res = run_js(r"""
const H = 3600e3;
const t0 = 1700000000000;
setBusinessNow(t0);
setLogin(5, null, true, { estimated_expires_at: t0 / 1000 + 5 * 3600 });
/* 校验回来：ok=true 才会离开「正在检查」分支（与真实启动时序一致） */
window.onBackend.check({ ok: true, result: "valid", hours_left: 5,
  session_present: true, estimated_expires_at: t0 / 1000 + 5 * 3600 });
const h1 = loginHoursLeft();
setBusinessNow(t0 + 2 * H);
paintLogin();
const h2 = loginHoursLeft();
const sub = document.querySelector("#pill-sub").textContent;
out.push({ h1, h2, sub });
""")
        self.assertAlmostEqual(res[0]["h1"], 5.0, delta=0.01)
        self.assertAlmostEqual(res[0]["h2"], 3.0, delta=0.01, msg="两小时后剩余必须少两小时")
        self.assertIn("3 小时", res[0]["sub"], "胶囊「约剩」要按绝对锚点走字")

    def test_repeated_check_does_not_renew_anchor(self):
        """刷新同一凭证：锚点是绝对时刻，不会按「本次刷新时刻 + hours」续期。"""
        res = run_js(r"""
const H = 3600e3;
const t0 = 1700000000000;
setBusinessNow(t0);
const anchor = t0 / 1000 + 4 * 3600;
setLogin(4, null, true, { estimated_expires_at: anchor });
window.onBackend.check({ ok: true, result: "valid", hours_left: 4,
  session_present: true, estimated_expires_at: anchor });
const a1 = S.login.expireAt;
/* 两小时后再次 check：后端 hours 按绝对时刻递减（4→2），锚点必须仍是同一绝对时刻 */
setBusinessNow(t0 + 2 * H);
window.onBackend.check({ ok: true, result: "valid", hours_left: 2,
  session_present: true, estimated_expires_at: anchor });
out.push({ a1, a2: S.login.expireAt });
""")
        self.assertEqual(res[0]["a1"], res[0]["a2"], "同一凭证的锚点不得随刷新漂移")

    def test_valid_at_zero_estimate_stays_valid(self):
        """估算到点但学校 valid：仍「登录有效」，不显示成过期（放行只看学校结论）。"""
        res = run_js(r"""
window.onBackend.check({ ok: true, result: "valid", hours_left: 0,
  session_present: true, estimated_expires_at: Date.now() / 1000 - 60 });
out.push({ state: document.querySelector("#pill").dataset.state,
  text: document.querySelector("#pill-text").textContent,
  sub: document.querySelector("#pill-sub").textContent });
""")
        self.assertEqual(res[0]["state"], "ok")
        self.assertEqual(res[0]["text"], "已登录")
        self.assertIn("以学校校验为准", res[0]["sub"])

    def test_no_anchor_shows_unknown_not_fabricated(self):
        """没有锚点：不编 17 小时；unknown 结论也不显示成过期。"""
        res = run_js(r"""
setLogin(null, true, true, { session_present: true, saved_at: null, estimated_expires_at: null });
S.login.result = "unknown"; S.login.ok = false;
paintLogin();
out.push({ text: document.querySelector("#pill-text").textContent,
  expiry: expiryText() });
""")
        self.assertEqual(res[0]["text"], "登录待确认")
        self.assertNotIn("17", res[0]["expiry"], "没有锚点不得编造 17 小时")
        self.assertIn("未知", res[0]["expiry"])


class LoginSequenceBehaviorTests(unittest.TestCase):
    """P1：取消/失败/超时的重新登录必须整份回滚，成功换锚点，且同一轮统一收口。

    用状态机序列断言（登录字段 + 胶囊 + 剩余时间 + 按钮），而不是只比一句文案：
    用户看到的一致性是「所有 surface 在同一结论下同步」，单点文案断言抓不到
    「胶囊已登录、剩余时间消失」这类混合态。
    """

    def test_cancelled_relogin_restores_full_prior_login_state(self):
        """已登录点重登 → 取消：胶囊、剩余时间、锚点、按钮全部回到登录前，只多一条失败说明。"""
        res = run_js(r"""
const H = 3600e3;
const t0 = 1700000000000;
setBusinessNow(t0);
const anchor = t0 / 1000 + 4 * 3600;
setLogin(4, null, true, { session_present: true, estimated_expires_at: anchor, saved_at: "s-old" });
window.onBackend.check({ ok: true, result: "valid", hours_left: 4, session_present: true,
  estimated_expires_at: anchor, saved_at: "s-old" });
const before = {
  hours: S.login.hours, ok: S.login.ok, present: S.login.present, expireAt: S.login.expireAt,
  text: document.querySelector("#pill-text").textContent,
  state: document.querySelector("#pill").dataset.state,
  sub: document.querySelector("#pill-sub").textContent,
  btn: document.querySelector("#btn-login").textContent,
};
let pending = null;
let loginCalls = 0;
window.pywebview = { api: {
  login: () => { loginCalls += 1; return new Promise((r) => { pending = r; }); },
  check: () => Promise.resolve({ ok: true }),
} };
doLogin();
const snapDuring = !!S.login.preLoginSnapshot;
pending({ ok: true });
await Promise.resolve(); await Promise.resolve();
window.onBackend.done({ kind: "login", ok: false, reason: "窗口被关掉" });
out.push({
  snapDuring, snapCleared: S.login.preLoginSnapshot === null, loginCalls,
  loginPending: S.login.loginPending,
  hours: S.login.hours, present: S.login.present, expireAt: S.login.expireAt,
  text: document.querySelector("#pill-text").textContent,
  state: document.querySelector("#pill").dataset.state,
  sub: document.querySelector("#pill-sub").textContent,
  btn: document.querySelector("#btn-login").textContent,
  before,
});
""")
        r = res[0]
        self.assertTrue(r["snapDuring"], "点登录时必须先存下完整登录结论快照")
        self.assertTrue(r["snapCleared"], "取消落地后快照必须清掉，不能留着下次误回滚")
        self.assertEqual(r["loginCalls"], 1)
        self.assertFalse(r["loginPending"])
        self.assertEqual(r["expireAt"], r["before"]["expireAt"], "取消不得动旧锚点")
        self.assertEqual(r["present"], r["before"]["present"])
        self.assertEqual(r["state"], "ok", "仍有效的旧会话不得画成失效")
        self.assertEqual(r["text"], "已登录")
        self.assertEqual(r["sub"], r["before"]["sub"], "剩余时间必须原样保留，不能显示成失效/未知")
        self.assertEqual(r["btn"], r["before"]["btn"], "按钮文字不得因取消而变化")

    def test_login_pending_never_paints_run_board(self):
        """点登录期间大屏必须说「登录中」，不能借 S.busy 说成「正在准备报名」。"""
        res = run_js(r"""
const t0 = 1700000000000;
setBusinessNow(t0);
const anchor = t0 / 1000 + 4 * 3600;
setLogin(4, null, true, { session_present: true, estimated_expires_at: anchor });
window.onBackend.check({ ok: true, result: "valid", hours_left: 4, session_present: true,
  estimated_expires_at: anchor });
const boardBefore = document.querySelector("#selected").textContent;
let pending = null;
window.pywebview = { api: {
  login: () => new Promise((r) => { pending = r; }),
  check: () => Promise.resolve({ ok: true }),
} };
doLogin();
const during = {
  tag: document.querySelector("#board-tag-text").textContent,
  title: document.querySelector("#selected").textContent,
  text: document.querySelector("#board-text").textContent,
  busy: S.busy,
  loginPending: S.login.loginPending,
};
pending({ ok: true });
await Promise.resolve(); await Promise.resolve();
window.onBackend.done({ kind: "login", ok: false, reason: "窗口被关掉" });
out.push({
  boardBefore, during,
  after: {
    tag: document.querySelector("#board-tag-text").textContent,
    title: document.querySelector("#selected").textContent,
  },
  loginPending: S.login.loginPending,
  busy: S.busy,
});
""")
        r = res[0]
        self.assertTrue(r["during"]["loginPending"], "点登录后必须处于登录尝试态")
        self.assertTrue(r["during"]["busy"], "登录尝试期间 S.busy 为真（等待 worker）")
        self.assertEqual(r["during"]["tag"], "登录中", "登录尝试不得露出报名计划的「准备中」状态")
        self.assertIn("登录窗口", r["during"]["title"])
        self.assertNotIn("正在准备报名", r["during"]["title"],
                         "点登录不能把大屏说成正在准备报名")
        self.assertFalse(r["loginPending"])
        self.assertFalse(r["busy"])
        self.assertNotEqual(r["after"]["tag"], "登录中", "取消落地后大屏要回到非登录态")
        self.assertEqual(r["after"]["title"], r["boardBefore"], "取消后大屏必须回到登录前的内容")

    def test_logged_in_without_anchor_says_unknown_not_blank(self):
        """有会话但锚点对不上（换过凭证/当时没可信时钟）：胶囊必须明说未知，不能空着。"""
        res = run_js(r"""
setBusinessNow(1700000000000);
setLogin(null, true, true);
out.push({
  text: document.querySelector("#pill-text").textContent,
  sub: document.querySelector("#pill-sub").textContent,
  note: document.querySelector("#status-detail").textContent,
  state: document.querySelector("#pill").dataset.state,
  hours: loginHoursLeft(),
});
""")
        r = res[0]
        self.assertIsNone(r["hours"], "没有匹配锚点时不得报出小时数")
        self.assertEqual(r["text"], "已登录")
        self.assertEqual(r["state"], "ok")
        self.assertIn("未知", r["sub"], "剩余时间未知时必须明说，不能留空白")
        self.assertIn("重新登录", r["sub"], "算不出剩余时间时要给出下一步：建议重新登录")
        self.assertNotIn("小时", r["sub"], "没有锚点不得编造剩余小时数")
        self.assertIn("登录有效", r["note"], "说明文字要告诉用户登录本身是有效的")
        self.assertIn("重新登录", r["note"], "说明文字要解释重新登录能恢复估算")

    def test_failed_relogin_keeps_countdown_instead_of_invalid(self):
        """超时/登录失败：保留旧有效期与「已登录」，只显示这次失败的说明。"""
        res = run_js(r"""
const H = 3600e3;
const t0 = 1700000000000;
setBusinessNow(t0);
const anchor = t0 / 1000 + 6 * 3600;
setLogin(6, null, true, { session_present: true, estimated_expires_at: anchor });
window.onBackend.check({ ok: true, result: "valid", hours_left: 6, session_present: true,
  estimated_expires_at: anchor });
window.pywebview = { api: { login: () => Promise.resolve({ ok: true }), check: () => Promise.resolve({ ok: true }) } };
doLogin();
window.onBackend.done({ kind: "login", ok: false, reason: "仍未检测到登录成功" });
out.push({
  expireAt: S.login.expireAt,
  state: document.querySelector("#pill").dataset.state,
  text: document.querySelector("#pill-text").textContent,
  sub: document.querySelector("#pill-sub").textContent,
  hours: loginHoursLeft(),
});
""")
        r = res[0]
        self.assertEqual(r["expireAt"], 1700000000000 + 6 * 3600e3)
        self.assertEqual(r["state"], "ok")
        self.assertEqual(r["text"], "已登录")
        self.assertIn("6 小时", r["sub"], "失败重登不能吞掉原有剩余时间")
        self.assertAlmostEqual(r["hours"], 6.0, delta=0.01)

    def test_cancelled_relogin_without_any_session_stays_logged_out(self):
        """本来就没有会话：取消重登后仍是未登录，不能因为点过登录就显示已登录。"""
        res = run_js(r"""
window.pywebview = { api: { login: () => Promise.resolve({ ok: true }), check: () => Promise.resolve({ ok: true }) } };
setLogin(null, false, false);
doLogin();
window.onBackend.done({ kind: "login", ok: false, reason: "打不开登录页" });
out.push({
  present: S.login.present, expireAt: S.login.expireAt,
  state: document.querySelector("#pill").dataset.state,
  text: document.querySelector("#pill-text").textContent,
});
""")
        r = res[0]
        self.assertFalse(r["present"])
        self.assertIsNone(r["expireAt"])
        self.assertEqual(r["state"], "none")
        self.assertEqual(r["text"], "未登录")

    def test_successful_relogin_clears_old_anchor_then_adopts_new(self):
        """成功重登：尝试前快照作废，旧锚点立刻清零，随后 check 带回新锚点。"""
        res = run_js(r"""
const H = 3600e3;
const t0 = 1700000000000;
setBusinessNow(t0);
const oldAnchor = t0 / 1000 + 1 * 3600;
setLogin(1, null, true, { session_present: true, estimated_expires_at: oldAnchor, saved_at: "s-old" });
window.onBackend.check({ ok: true, result: "valid", hours_left: 1, session_present: true,
  estimated_expires_at: oldAnchor, saved_at: "s-old" });
window.pywebview = { api: { login: () => Promise.resolve({ ok: true }), check: () => Promise.resolve({ ok: true }) } };
doLogin();
window.onBackend.done({ kind: "login", ok: true });
const cleared = { expireAt: S.login.expireAt, snap: S.login.preLoginSnapshot };
const newAnchor = t0 / 1000 + 17.5 * 3600;
window.onBackend.check({ ok: true, result: "valid", hours_left: 17.5, session_present: true,
  estimated_expires_at: newAnchor, saved_at: "s-new" });
out.push({ cleared, after: S.login.expireAt, sub: document.querySelector("#pill-sub").textContent,
  state: document.querySelector("#pill").dataset.state });
""")
        r = res[0]
        self.assertIsNone(r["cleared"]["expireAt"], "成功后旧锚点必须立即作废")
        self.assertIsNone(r["cleared"]["snap"], "成功后尝试前快照必须作废")
        self.assertAlmostEqual(r["after"], 1700000000000 + 17.5 * 3600e3, delta=1000)
        self.assertEqual(r["state"], "ok")
        self.assertIn("约剩", r["sub"], "新锚点回来后胶囊必须报剩余时间")

    def test_old_round_check_cannot_override_restored_state(self):
        """取消落地后，旧轮 check 迟到：不得改写回滚后的登录结论。"""
        res = run_js(r"""
const H = 3600e3;
const t0 = 1700000000000;
setBusinessNow(t0);
const anchor = t0 / 1000 + 4 * 3600;
setLogin(4, null, true, { session_present: true, estimated_expires_at: anchor });
window.onBackend.check({ ok: true, result: "valid", hours_left: 4, session_present: true,
  estimated_expires_at: anchor });
window.pywebview = { api: { login: () => Promise.resolve({ ok: true }), check: () => Promise.resolve({ ok: true }) } };
doLogin();
window.onBackend.done({ kind: "login", ok: false, reason: "窗口被关掉" });
const kept = { expireAt: S.login.expireAt, state: document.querySelector("#pill").dataset.state };
/* 回滚后新一轮刷新已经开始（S.round.check 是新 id）；此时上一轮的 check 才迟到 */
S.round.check = "round-new";
S.round.checkTimer = 0;
window.onBackend.check({ request_id: "round-from-before", ok: false, result: "none", hours_left: null });
out.push({ kept, after: { expireAt: S.login.expireAt, state: document.querySelector("#pill").dataset.state } });
""")
        r = res[0]
        self.assertEqual(r["after"], r["kept"], "旧轮迟到结论不得覆盖回滚后的状态")

    def test_sync_login_dependent_view_repaints_every_surface(self):
        """统一收口函数必须同时重画胶囊/时钟/空态/大屏/路线/列表，不只改胶囊。"""
        res = run_js(r"""
window.onBackend.check({ ok: true, result: "valid", hours_left: 3, session_present: true,
  estimated_expires_at: Date.now() / 1000 + 3 * 3600 });
const seen = [];
for (const name of ["paintList", "paintRoute", "paintSelected", "paintClockStatus", "refreshLoginEmptyState"]) {
  const real = window[name] || eval(name);
  const spy = function () { seen.push(name); return real.apply(null, arguments); };
  if (name in window) window[name] = spy; else eval(name + " = spy");
}
syncLoginDependentView();
out.push({ seen, state: document.querySelector("#pill").dataset.state });
""")
        r = res[0]
        self.assertEqual(r["state"], "ok")
        for name in ("paintList", "paintRoute", "paintSelected", "paintClockStatus", "refreshLoginEmptyState"):
            self.assertIn(name, r["seen"], "统一收口必须重画 %s" % name)


class TourBehaviorTests(unittest.TestCase):
    """R5/R6：引导内容版本判定、焦点循环、背景快捷键隔离、暂停放行（node vm 行为）。"""

    def test_autostart_by_content_version_not_timestamps(self):
        """新用户自动弹；同版完成/跳过不弹；计划运行中不弹。"""
        res = run_js(r"""
S.busy = false;
S.prefs = {};
out.push({ fresh: tourShouldAutoStart() });
S.prefs = { tour_version: TOUR_VERSION, tour_completed_at: "2026-01-01T00:00:00Z" };
out.push({ sameDone: tourShouldAutoStart() });
S.prefs = { tour_version: TOUR_VERSION, tour_skipped_at: "2026-01-01T00:00:00Z" };
out.push({ sameSkipped: tourShouldAutoStart() });
S.prefs = { tour_version: 0, tour_completed_at: "2026-01-01T00:00:00Z" };
out.push({ oldVersionWithOldStamp: tourShouldAutoStart() });
S.busy = true;
out.push({ busyBlocks: !tourShouldAutoStart() });
S.busy = false;
""")
        self.assertTrue(res[0]["fresh"], "新用户必须自动弹")
        self.assertFalse(res[1]["sameDone"], "同版完成不重复弹")
        self.assertFalse(res[2]["sameSkipped"], "同版跳过不重复弹")
        self.assertTrue(res[3]["oldVersionWithOldStamp"],
                        "旧内容版本的时间戳不得挡住更新的内容版本（R5）")
        self.assertTrue(res[4]["busyBlocks"], "计划运行中不自动弹")

    def test_tab_cycles_inside_card_and_background_shortcuts_blocked(self):
        """Tab 在卡片按钮间循环；背景 Ctrl+F/`/`/Enter 不移焦点、不开始计划。"""
        res = run_js(r"""
S.busy = false;
startTour(0);
const qFocused = () => focused.indexOf("#q") >= 0;
/* startTour 已聚焦 #tour-next（buttons=[skip,prev,next] 的第 3 个）：
 * Tab 从 next 前进绕回 skip，Shift+Tab 再回到 next——首尾相接 */
__dispatchKey({ key: "Tab" });
__dispatchKey({ key: "Tab", shiftKey: true });
const tabInCard = focused.slice(-2);
/* 背景快捷键：焦点不得进搜索框 */
__dispatchKey({ key: "f", ctrlKey: true });
const ctrlF = qFocused();
__dispatchKey({ key: "/" });
const slash = qFocused();
/* 背景 Enter：必须拦截（不得开始计划） */
const ev = __dispatchKey({ key: "Enter" });
out.push({ tabInCard, ctrlF, slash, enterBlocked: ev.defaultPrevented,
  tourOpen: S.tour.open });
tourSkip();
""")
        self.assertEqual(res[0]["tabInCard"], ["#tour-skip", "#tour-next"],
                         "Tab 必须在卡片按钮间首尾循环（next → skip → next）")
        self.assertFalse(res[0]["ctrlF"], "引导可见时 Ctrl+F 不得把焦点送进背景搜索框")
        self.assertFalse(res[0]["slash"], "引导可见时 `/` 不得移动焦点")
        self.assertTrue(res[0]["enterBlocked"], "背景 Enter 必须被拦截，不得开始计划")
        self.assertTrue(res[0]["tourOpen"], "以上按键都不得顺手关掉引导")

    def test_esc_skips_once_and_restores_focus(self):
        """Esc=跳过且只落一次偏好；关闭后焦点还给引导开始前的元素。"""
        res = run_js(r"""
const saved = [];
savePrefsNow = (patch) => saved.push(patch);
const target = document.querySelector("#btn-refresh");
target.focus();
startTour(2);
const enteredTour = focused.indexOf("#tour-next") >= 0;
__dispatchKey({ key: "Escape" });
__dispatchKey({ key: "Escape" });   /* 第二次 Esc：引导已关，不得再落一次跳过记录 */
out.push({ savedCount: saved.length, savedKeys: Object.keys(saved[0] || {}),
  tourOpen: S.tour.open, restored: focused.slice(-1)[0], enteredTour });
""")
        self.assertTrue(res[0]["enteredTour"], "开始引导时焦点必须进卡片")
        self.assertEqual(res[0]["savedCount"], 1, "Esc 跳过只持久化一次")
        self.assertIn("tour_version", res[0]["savedKeys"])
        self.assertFalse(res[0]["tourOpen"])
        self.assertEqual(res[0]["restored"], "#btn-refresh", "关闭后焦点还给引导前的元素")

    def test_busy_pause_releases_constraints_and_resume(self):
        """计划运行暂停引导：键盘约束放行（背景快捷键正常工作）、不挡停止；结束后回原步骤。"""
        res = run_js(r"""
startTour(3);
setBusy(true);          /* 暂停：遮罩隐藏 */
const pausedVisible = tourVisible();
/* 暂停期间背景快捷键照常工作：Ctrl+F 正常跳搜索框（约束已解除） */
__dispatchKey({ key: "f", ctrlKey: true });
const qReachable = focused.indexOf("#q") >= 0;
const stillOpen = S.tour.open;
setBusy(false);         /* 恢复 */
out.push({ pausedVisible, qReachable,
  stillOpen, resumedVisible: tourVisible(), step: S.tour.step,
  boxHidden: document.querySelector("#tour").hidden });
tourSkip();
""")
        self.assertFalse(res[0]["pausedVisible"], "暂停期间引导不可见（不挡运行界面）")
        self.assertTrue(res[0]["qReachable"], "暂停时背景快捷键放行（停止/搜索操作不受影响）")
        self.assertTrue(res[0]["stillOpen"], "暂停只是隐藏，引导状态保留")
        self.assertTrue(res[0]["resumedVisible"], "计划结束恢复引导显示")
        self.assertEqual(res[0]["step"], 3, "恢复后回到原步骤")
        self.assertFalse(res[0]["boxHidden"])

    def test_render_repositions_open_tour(self):
        """列表重画挪动目标后：重新定位现有高亮，不重开、不假点击。"""
        res = run_js(r"""
startTour(1);
const spot = document.querySelector("#tour-spot");
spot.style.top = "0px";
render();               /* 活动重画（空列表也走 render） */
out.push({ step: S.tour.step, open: S.tour.open,
  repositioned: String(spot.style.top) !== "0px" });
tourSkip();
""")
        self.assertEqual(res[0]["step"], 1, "重定位不得改变当前步骤")
        self.assertTrue(res[0]["open"], "render 不得重开或关闭引导")
        self.assertTrue(res[0]["repositioned"], "render 后必须重新定位高亮框")


class SettingsEntryContractTests(unittest.TestCase):
    """历史双入口覆盖迁移至单帮助；真实DOM不得保留第二套旧节点。"""

    def test_index_has_settings_entry_and_groups(self):
        """单帮助入口；使用说明、手动更新与本机数据都在同一模态。"""
        html = read("ui/index.html")
        self.assertIn('id="btn-help"', html, "顶栏必须有独立「更多」按钮")
        self.assertIn('aria-label="帮助：使用说明、版本更新与本机数据"', html,
                      "入口必须有可读的无障碍名（不能只靠图标）")
        self.assertIn(">帮助<", html, "顶栏按钮的文字必须是「帮助」")
        self.assertIn("<svg", html[max(0, html.index('id="btn-help"') - 300):
                                   html.index('id="btn-help"') + 400],
                      "更多按钮必须带内联 SVG 图标")
        for needle in ("btn-help-tour", "btn-help-latest", "btn-help-data",
                       "help-title", "help-version", "help-status"):
            self.assertIn('id="%s"' % needle, html, "更多弹窗缺组件 #%s" % needle)
        dialog = help_dialog(html)
        self.assertNotIn('id="btn-settings"', html)
        self.assertNotIn('id="settings"', html)
        for text in ("三步使用说明", "本机数据", "重看引导", "前往下载页",
                     "打开数据目录", "Esc 关闭"):
            self.assertIn(text, dialog, "更多弹窗缺「%s」" % text)
        # 不再用三张同形分组卡叠套；现行入口统一叫「更多」
        self.assertNotIn("set-group", dialog, "不再用三张同形分组卡")
        self.assertNotIn('id="settings"', dialog, "不保留第二个设置模态")
        for banned in ("清空凭证", "重置", "自动更新", "自动检查更新"):
            self.assertNotIn(banned, dialog,
                             "更多弹窗不得出现未确认功能「%s」" % banned)

    def test_help_dialog_keeps_quick_help_only(self):
        """合并后仍有完整三步，并能直接找到版本与更新；不用第二层页面。"""
        html = read("ui/index.html")
        help_body = help_dialog(html)
        for component in ("help-version", "btn-update-check", "update-status"):
            self.assertIn('id="%s"' % component, help_body)
        self.assertIn("关闭", help_body, "帮助弹窗保留关闭按钮")
        self.assertIn('id="btn-help-tour"', help_body,
                      "说明底部保留重播快捷入口（学习入口就近可重播）")
        self.assertIn('id="help-steps"', help_body, "说明弹窗保留三步快速帮助容器")
        js = read("ui/app.js")
        for step_title in ("登录账号", "设置报名计划", "查看报名结果"):
            self.assertIn(step_title, js, "三步快速帮助必须保留（%s）" % step_title)
        self.assertIn("再选择「开始报名计划」", js, "三步说明必须写出真正的开始动作")

    def test_app_js_wires_settings_actions(self):
        """两处重播共用 replayTourFrom；版本/目录仍走既有接口；忙时禁用有可见原因。"""
        js = read("ui/app.js")
        for anchor in ('$("#btn-help")', '$("#btn-help-tour")',
                       '$("#btn-help-tour")', '$("#btn-help-latest")',
                       '$("#btn-help-data")'):
            self.assertIn(anchor, js, "app.js 缺辅助入口绑定：%s" % anchor)
        for stale in ('$("#btn-latest")', '$("#settings-version")', '$("#btn-settings")'):
            self.assertNotIn(stale, js, "app.js 残留旧引用 %s（元素已删除会直接抛错）" % stale)
        self.assertIn('replayTourFrom("#help", "#btn-help")', js,
                      "更多里的重播必须走共用处理函数")
        self.assertIn('replayTourFrom("#help", "#btn-help")', js,
                      "说明里的重播必须走同一个处理函数")
        body = js[js.index("function replayTourFrom("):js.index("function paintReplayButtons")]
        self.assertIn("S.busy", body, "重播必须先确认非忙")
        self.assertIn("dialog.close()", body, "重播前必须关掉当前弹窗，不叠模态")
        self.assertIn("opener.focus()", body, "重播前把焦点还给对应顶栏入口")
        self.assertIn("startTour(0)", body, "重播最终从第 1 步开始")
        self.assertIn('api("open_latest_release")', js, "查看发布页仍走既有后端外链")
        self.assertIn('api("open_data")', js, "打开数据目录复用既有后端接口")
        replay = js[js.index("function paintReplayButtons("):js.index("function savePrefs(")]
        self.assertIn("btn.disabled = !!S.busy", replay, "忙时必须禁用重播")
        self.assertIn('"计划结束后可重看引导"', replay, "禁用原因必须可见，不只靠 title")

    def test_dialog_keys_do_not_penetrate(self):
        """任意 dialog 打开时 Enter/Esc 不穿透（不开始计划、不清空选择）。"""
        js = read("ui/app.js")
        key_at = js.index('document.addEventListener("keydown"')
        body = js[key_at:key_at + 2400]
        self.assertIn('"#help"', body,
                      "弹窗键盘隔离必须覆盖更多弹窗")
        self.assertIn('"#dlg-stop"', body, "停止确认弹窗保持原有 Esc 语义")


class SettingsDialogBehaviorTests(unittest.TestCase):
    """设置入口行为回归（node vm 行为，不开浏览器、不调用学校）。"""

    def test_top_entry_opens_and_replay_chain(self):
        """顶栏入口打开设置；重看引导先关设置再进引导。"""
        res = run_js(r"""
document.querySelector("#btn-help").click();
const opened = document.querySelector("#help").open;
document.querySelector("#btn-help-tour").click();
out.push({ opened,
  settingsClosed: !document.querySelector("#help").open,
  tourOpen: S.tour.open, step: S.tour.step });
tourSkip();
""")
        self.assertTrue(res[0]["opened"], "点顶栏「设置」必须打开设置弹窗")
        self.assertTrue(res[0]["settingsClosed"], "重播前必须先关闭设置弹窗")
        self.assertTrue(res[0]["tourOpen"], "重播必须进入六步引导")
        self.assertEqual(res[0]["step"], 0, "重播从第 1 步开始")

    def test_running_can_open_settings_but_not_replay(self):
        """计划运行中：设置可打开、重看引导禁用且不重播；结束后可重播。"""
        res = run_js(r"""
setBusy(true);
document.querySelector("#btn-help").click();
const openedWhileBusy = document.querySelector("#help").open;
const replayDisabled = document.querySelector("#btn-help-tour").disabled;
document.querySelector("#btn-help-tour").click();
const tourNotOpened = !S.tour.open;
setBusy(false);
document.querySelector("#btn-help-tour").click();
const tourOpenedAfterEnd = S.tour.open;
out.push({ openedWhileBusy, replayDisabled, tourNotOpened, tourOpenedAfterEnd });
tourSkip();
""")
        self.assertTrue(res[0]["openedWhileBusy"], "计划运行中也必须能打开设置")
        self.assertTrue(res[0]["replayDisabled"], "运行中「重看引导」必须禁用")
        self.assertTrue(res[0]["tourNotOpened"], "运行中不得重播引导打断计划")
        self.assertTrue(res[0]["tourOpenedAfterEnd"], "计划结束后可以重播")

    def test_dialog_open_blocks_enter_and_escape_penetration(self):
        """设置弹窗打开时：Enter 不开始计划、Esc 不清空选择、不误跳引导。"""
        res = run_js(r"""
S.selected.add("a1");
let startCalled = 0;
const realStart = startPlan;
startPlan = () => { startCalled += 1; };
const dlg = document.querySelector("#help");
dlg.open = true;
__dispatchKey({ key: "Enter" });
__dispatchKey({ key: "Escape" });
dlg.open = false;
startPlan = realStart;
out.push({ startCalled, selKept: S.selected.has("a1"), tourOpen: S.tour.open });
""")
        self.assertEqual(res[0]["startCalled"], 0, "弹窗打开时 Enter 不得穿透开始计划")
        self.assertTrue(res[0]["selKept"], "弹窗打开时 Esc 不得清空选择")
        self.assertFalse(res[0]["tourOpen"], "弹窗打开时 Esc 不得误触引导")

    def test_latest_and_data_actions_called_once_per_click(self):
        """「GitHub 最新版本」与「打开数据目录」各走一次既有后端接口。"""
        res = run_js(r"""
window.pywebview = { api: {
  open_latest_release: () => { out.latest = (out.latest || 0) + 1; return Promise.resolve({ ok: true }); },
  open_data: () => { out.data = (out.data || 0) + 1; return Promise.resolve({ ok: true }); },
} };
document.querySelector("#btn-help-latest").click();
document.querySelector("#btn-help-data").click();
out.push({ latest: out.latest, data: out.data });
""")
        self.assertEqual(res[0]["latest"], 1, "外链每次点击恰调用一次")
        self.assertEqual(res[0]["data"], 1, "数据目录每次点击恰调用一次")


class ErrorFeedbackContractTests(unittest.TestCase):
    """错误说明唯一归属的静态契约：结果框/大屏/空态不再复读同一份错误说明。"""

    def test_apply_activities_failure_has_no_result_popup(self):
        """列表失败收尾不生成同义结果框：失败原话留给列表空态做唯一完整说明。"""
        js = read("ui/app.js")
        start = js.index("function applyActivities(")
        body = js[start:js.index("function render(", start)]
        self.assertNotIn("setResult(", body,
                         "列表失败收尾不得再弹同义结果框")
        self.assertIn("S.listError = res.text", body,
                      "失败原话必须记入 S.listError 供空态归因")

    def test_refresh_list_catch_has_no_result_popup(self):
        """refresh 的列表异常回调不再额外 setResult；只保留「未连接本机脚本」一处。"""
        js = read("ui/app.js")
        start = js.index("function refresh(")
        body = js[start:js.index("function applyActivities", start)]
        self.assertEqual(body.count("setResult("), 1,
                         "refresh 内只允许「未连接本机脚本」一处结果框")
        self.assertIn("还没有连接本机程序", body)
        self.assertIn("S.listError = msg(err)", body,
                      "桥接异常与 worker 失败同口径：原话归列表空态")

    def test_board_and_empty_no_longer_repeat_full_component_hint(self):
        """组件长说明与「重新检查」唯一归顶部横幅：大屏只留短状态、空态只留占位。"""
        js = read("ui/app.js")
        empty = js[js.index("function paintEmpty("):js.index("function paintCount(")]
        # 组件分支 = paintEmpty 里第一段；到「列表失败」分支为止（登录四态已抽成
        # loginEmptyState()，不再用 if (S.login.present) 当切片锚点）
        comp = empty[empty.index("if (S.runtime.error)"):
                     empty.index('if (S.listState === "error")')]
        self.assertNotIn("copy.hint", comp, "列表空态组件分支不得复读长说明")
        self.assertNotIn("重新检查", comp, "「重新检查」只能出现在顶部横幅")
        board = js[js.index("function paintSelected("):js.index("function setBoardTag(")]
        comp_board = board[board.index('S.runtime.error && S.listState !== "ready"'):
                           board.index('if (!S.busy && S.listState === "error"')]
        self.assertNotIn(".hint", comp_board, "大屏组件分支只留短状态")

    def test_list_error_copy_uses_real_failure_text(self):
        """空态归因用真实失败原话（S.listError），不再丢原话按空串猜。"""
        js = read("ui/app.js")
        empty = js[js.index("function paintEmpty("):js.index("function paintCount(")]
        self.assertIn('listErrorCopy(S.listError || "")', empty,
                      "空态归因必须用真实失败原话")


class ErrorFeedbackBehaviorTests(unittest.TestCase):
    """错误说明唯一归属的行为回归（node vm，不开浏览器、不调用学校）。"""

    def test_component_hint_only_in_banner_with_single_recheck(self):
        """组件错误：完整说明+重新检查只在顶部横幅；大屏/列表只留短状态与占位。"""
        res = run_js(r"""
setResult(null);   /* 桩里 #result 初始可见：先把结果框归零再进场景 */
S.runtime.error = "worker_missing";
S.listState = "error";
render();
const hint = RUNTIME_ERRORS.worker_missing.hint.slice(0, 10);
const banners = document.querySelector("#banners").innerHTML;
const board = document.querySelector("#board-text").textContent +
              document.querySelector("#selected").textContent;
const empty = document.querySelector("#empty-title").textContent +
              document.querySelector("#empty-text").textContent;
const action = document.querySelector("#empty-action");
const result = document.querySelector("#result");
/* 只数恢复入口（data-act="runtime-refresh" 按钮）：hint 文案本身含「重新检查」
 * 三个字，按字面量计数会把说明文本误当第二个入口 */
const recheck = (banners.match(/data-act="runtime-refresh"/g) || []).length +
  (action.hidden || action.textContent.indexOf("重新检查") < 0 ? 0 : 1);
out.push({ hintInBanner: banners.indexOf(hint) >= 0,
  hintInBoard: board.indexOf(hint) >= 0,
  hintInEmpty: empty.indexOf(hint) >= 0,
  resultVisible: !result.hidden,
  emptyActionHidden: action.hidden,
  recheck });
""")
        self.assertTrue(res[0]["hintInBanner"], "横幅必须承载完整组件说明")
        self.assertFalse(res[0]["hintInBoard"], "大屏不得复读长说明（只留短状态）")
        self.assertFalse(res[0]["hintInEmpty"], "列表空态只留未加载占位，不得复读长说明")
        self.assertFalse(res[0]["resultVisible"], "不得生成同义结果框")
        self.assertTrue(res[0]["emptyActionHidden"], "空态不得出现第二个「重新检查」")
        self.assertEqual(res[0]["recheck"], 1, "「重新检查」全页只能有一个")

    def test_list_failure_full_reason_only_in_empty_and_result_preserved(self):
        """普通列表失败：完整原因+重试只在列表空态；先放入的真实报名结果保留。"""
        res = run_js(r"""
setResult({ ok: true, title: "报名成功：校园夜跑", text: "建议去活动页确认一眼。" });
window.onBackend.activities({ ok: false, text: "学校接口 500 了", items: [] });
const result = document.querySelector("#result");
const action = document.querySelector("#empty-action");
out.push({
  resultKept: !result.hidden && result.innerHTML.indexOf("报名成功：校园夜跑") >= 0,
  resultText: result.innerHTML,
  emptyTitle: document.querySelector("#empty-title").textContent,
  emptyText: document.querySelector("#empty-text").textContent,
  retryText: action.hidden ? "" : action.textContent,
  board: document.querySelector("#board-text").textContent,
  banners: document.querySelector("#banners").innerHTML,
});
""")
        self.assertTrue(res[0]["resultKept"], "列表失败不得清掉真实报名结果")
        self.assertEqual(res[0]["retryText"], "重试", "「重试」只在列表空态")
        self.assertNotIn("报名成功", res[0]["emptyTitle"] + res[0]["emptyText"],
                         "空态说明与报名结果互不混写")
        self.assertNotIn("活动暂时没加载", res[0]["resultText"],
                         "结果框不得复读列表失败的完整说明")
        self.assertNotIn("脚本没跑通", res[0]["resultText"] + res[0]["board"],
                         "结果框与大屏都不得复读空态的完整原因")
        self.assertNotIn("程序组件", res[0]["banners"],
                         "普通列表失败不得冒充组件横幅")

    def test_component_recovery_restores_state(self):
        """组件故障恢复：本轮两分支都完成且无组件故障才撤横幅（轮次协议）。"""
        res = run_js(r"""
let resolveCheck, resolveList;
window.pywebview = { api: {
  check: () => new Promise((resolve) => { resolveCheck = resolve; }),
  list_activities: () => new Promise((resolve) => { resolveList = resolve; }),
}};
S.runtime.error = "worker_missing";
S.listState = "error";
render();
refresh(false);          /* 开启复查轮：两分支 pending，旧横幅保持到收口 */
await Promise.resolve();
resolveCheck({ ok: true, queued: true });
window.onBackend.check({ ok: true, result: "valid", hours_left: 10, session_present: true });
await Promise.resolve();
const bannerMidRound = document.querySelector("#banners").innerHTML.indexOf("程序组件缺失") >= 0;
resolveList({ ok: true, queued: true });
window.onBackend.activities({ ok: true, items: [] });
await Promise.resolve(); await Promise.resolve();
out.push({
  bannerMidRound,
  bannerGone: document.querySelector("#banners").innerHTML.indexOf("程序组件缺失") < 0,
  loginEnabled: !document.querySelector("#btn-login").disabled,
  runtimeCleared: !S.runtime.error,
});
""")
        self.assertTrue(res[0]["bannerMidRound"], "list 分支未完成时不得提前撤横幅")
        self.assertTrue(res[0]["runtimeCleared"], "本轮两分支完成且无故障后恢复")
        self.assertTrue(res[0]["bannerGone"], "恢复后横幅必须撤下")
        self.assertTrue(res[0]["loginEnabled"], "恢复后登录按钮必须解禁")


class ErrorFeedbackRoundtripTests(unittest.TestCase):
    """FB4 四入口组件码与刷新轮次恢复的行为回归（node vm + 受控 Promise 次序，不执行）。

    场景：A/B=check/list 两种到达顺序；C/D=login/start_plan 同步组件失败；
    E=同轮不互相撤、下一轮恢复、同步拒绝不悬挂 pending；F=真实业务结果保留与
    普通登录失败既有呈现。数 data-act 属性而非 hint 中的同名字样。
    """

    def test_check_valid_then_list_component_error_keeps_banner(self):
        """A：check valid 先到、list 组件故障后到——横幅恰一、控件禁用、无同义结果框。"""
        res = run_js(r"""
setResult(null);   /* 桩里 #result 初始可见：先归零再进场景 */
let resolveCheck, resolveList;
window.pywebview = { api: {
  check: () => new Promise((resolve) => { resolveCheck = resolve; }),
  list_activities: () => new Promise((resolve) => { resolveList = resolve; }),
}};
refresh(false);
await Promise.resolve();
resolveCheck({ ok: true, queued: true });
window.onBackend.check({ ok: true, result: "valid", hours_left: 10,
  session_present: true, estimated_expires_at: Date.now() / 1000 + 15 * 3600 });
await Promise.resolve();
resolveList({ ok: true, queued: true });
window.onBackend.activities({ ok: false, text: "程序组件缺失，请重新下载完整版本",
  items: [], runtime_error: "worker_missing" });
await Promise.resolve(); await Promise.resolve();
const banners = document.querySelector("#banners").innerHTML;
out.push({
  bannerHint: (banners.match(/无法找到所需程序组件/g) || []).length,
  recheck: (banners.match(/data-act="runtime-refresh"/g) || []).length,
  loginDisabled: document.querySelector("#btn-login").disabled,
  startDisabled: document.querySelector("#btn-plan").disabled,
  resultVisible: !document.querySelector("#result").hidden,
  runtime: S.runtime.error,
});
""")
        self.assertEqual(res[0]["bannerHint"], 1, "组件完整说明全页恰一次")
        self.assertEqual(res[0]["recheck"], 1, "恢复入口恰一个")
        self.assertTrue(res[0]["loginDisabled"], "缺组件时登录必须禁用")
        self.assertTrue(res[0]["startDisabled"], "缺组件时开始必须禁用")
        self.assertFalse(res[0]["resultVisible"], "不得生成同义结果框")
        self.assertEqual(res[0]["runtime"], "worker_missing")

    def test_list_component_error_then_check_valid_keeps_banner(self):
        """B：activities 组件故障先到、check valid 后到——横幅不得被同轮另一分支撤掉。"""
        res = run_js(r"""
let resolveCheck, resolveList;
window.pywebview = { api: {
  check: () => new Promise((resolve) => { resolveCheck = resolve; }),
  list_activities: () => new Promise((resolve) => { resolveList = resolve; }),
}};
refresh(false);
await Promise.resolve();
resolveList({ ok: true, queued: true });
window.onBackend.activities({ ok: false, text: "程序组件缺失，请重新下载完整版本",
  items: [], runtime_error: "worker_missing" });
await Promise.resolve();
const bannerAfterList = (document.querySelector("#banners").innerHTML
  .match(/无法找到所需程序组件/g) || []).length;
resolveCheck({ ok: true, queued: true });
window.onBackend.check({ ok: true, result: "valid", hours_left: 10, session_present: true });
await Promise.resolve(); await Promise.resolve();
const banners = document.querySelector("#banners").innerHTML;
out.push({
  bannerAfterList,
  bannerAfterCheck: (banners.match(/无法找到所需程序组件/g) || []).length,
  recheck: (banners.match(/data-act="runtime-refresh"/g) || []).length,
  runtime: S.runtime.error,
});
""")
        self.assertEqual(res[0]["bannerAfterList"], 1)
        self.assertEqual(res[0]["bannerAfterCheck"], 1, "同轮 check valid 后到不得撤组件横幅")
        self.assertEqual(res[0]["recheck"], 1)
        self.assertEqual(res[0]["runtime"], "worker_missing")

    def test_login_component_failure_banner_only(self):
        """C：login 同步组件失败只挂横幅，不再生成「登录没能开始」同义结果框。"""
        res = run_js(r"""
window.pywebview = { api: {
  login: () => Promise.resolve({ ok: false,
    text: "程序组件无法读取，请重新下载完整版本", error: "worker_permission" }),
}};
doLogin();
await Promise.resolve(); await Promise.resolve();
const banners = document.querySelector("#banners").innerHTML;
const result = document.querySelector("#result");
out.push({
  bannerHint: (banners.match(/被系统拦住或没有权限/g) || []).length,
  recheck: (banners.match(/data-act="runtime-refresh"/g) || []).length,
  resultVisible: !result.hidden,
  resultText: result.hidden ? "" : result.innerHTML.slice(0, 40),
  loginDisabled: document.querySelector("#btn-login").disabled,
  runtime: S.runtime.error,
});
""")
        self.assertEqual(res[0]["bannerHint"], 1, "组件说明只在横幅")
        self.assertEqual(res[0]["recheck"], 1)
        self.assertFalse(res[0]["resultVisible"], "组件失败不得再弹同义结果框")
        self.assertEqual(res[0]["runtime"], "worker_permission")
        self.assertTrue(res[0]["loginDisabled"], "组件横幅挂出后登录禁用")

    def test_start_plan_component_failure_banner_only(self):
        """D：start_plan 同步组件失败只挂横幅；提交中标志释放、按钮恢复可点文案。"""
        res = run_js(r"""
const item = { id: "a1", name: "活动", sign_start: "2099-01-01 09:00:00",
  sign_end: "2099-01-02 09:00:00", act_start: "2099-01-03 09:00:00",
  act_end: "2099-01-03 11:00:00", signed: false, seconds_to_open: 1,
  state: "未开放", place: "", checkin_required: null, checkout_required: null };
S.items = [item];
S.byId = new Map([["a1", item]]);
S.listState = "ready";
S.login.result = "valid";
S.login.checking = false;
S.selected.add("a1");
S.demo = false;
window.pywebview = { api: {
  start_plan: () => Promise.resolve({ ok: false,
    text: "程序组件启动失败，请重新下载完整版本", error: "spawn_failed" }),
}};
startPlan();
await Promise.resolve(); await Promise.resolve();
const banners = document.querySelector("#banners").innerHTML;
out.push({
  bannerHint: (banners.match(/组件在但没能启动/g) || []).length,
  recheck: (banners.match(/data-act="runtime-refresh"/g) || []).length,
  resultVisible: !document.querySelector("#result").hidden,
  starting: S.starting,
  planText: document.querySelector("#btn-plan").textContent,
  runtime: S.runtime.error,
});
""")
        self.assertEqual(res[0]["bannerHint"], 1)
        self.assertEqual(res[0]["recheck"], 1)
        self.assertFalse(res[0]["resultVisible"], "组件失败不得生成同义失败结果框")
        self.assertFalse(res[0]["starting"], "提交中标志必须释放")
        self.assertEqual(res[0]["planText"], "开始报名计划", "拒绝后按钮恢复")
        self.assertEqual(res[0]["runtime"], "spawn_failed")

    def test_next_round_recovery_and_no_permanent_pending(self):
        """E：同轮不互相撤、下一轮两分支完成才恢复；同步拒绝不把 pending 卡死。"""
        res = run_js(r"""
let resolveCheck, resolveList;
window.pywebview = { api: {
  check: () => new Promise((resolve) => { resolveCheck = resolve; }),
  list_activities: () => new Promise((resolve) => { resolveList = resolve; }),
}};
refresh(false);
await Promise.resolve();
resolveList({ ok: true, queued: true });
window.onBackend.activities({ ok: false, text: "程序组件缺失，请重新下载完整版本",
  items: [], runtime_error: "worker_missing" });
await Promise.resolve();
resolveCheck({ ok: true, queued: true });
window.onBackend.check({ ok: false, result: "unknown", hours_left: null, session_present: true });
await Promise.resolve(); await Promise.resolve();
const bannerRound1 = document.querySelector("#banners").innerHTML
  .match(/无法找到所需程序组件/g).length;
/* 下一轮：check valid 先完成（list 未完成）→ 不得提前撤 */
window.pywebview = { api: {
  check: () => Promise.resolve({ ok: true, queued: true }),
  list_activities: () => Promise.resolve({ ok: true, queued: true }),
}};
refresh(false);
await Promise.resolve();
window.onBackend.check({ ok: true, result: "valid", hours_left: 10, session_present: true });
await Promise.resolve();
const bannerMidRound = document.querySelector("#banners").innerHTML
  .match(/无法找到所需程序组件/g).length;
window.onBackend.activities({ ok: true, items: [] });
await Promise.resolve(); await Promise.resolve();
const bannerGone = document.querySelector("#banners").innerHTML
  .indexOf("程序组件缺失") < 0;
/* 第三轮：后端同步拒绝（无终态事件）不得把 pending 卡死 */
window.pywebview = { api: {
  check: () => Promise.resolve({ ok: false, queued: false, text: "程序正在关闭，已取消本次检查" }),
  list_activities: () => Promise.resolve({ ok: false, queued: false }),
}};
refresh(false);
await Promise.resolve(); await Promise.resolve();
out.push({
  bannerRound1, bannerMidRound, bannerGone,
  loginEnabled: !document.querySelector("#btn-login").disabled,
  runtimeCleared: !S.runtime.error,
  pendingAfterRefusal: S.runtime.checkPending || S.runtime.listPending,
});
""")
        self.assertEqual(res[0]["bannerRound1"], 1)
        self.assertEqual(res[0]["bannerMidRound"], 1, "下一轮 list 未完成不得提前撤")
        self.assertTrue(res[0]["bannerGone"], "两分支都完成且无故障后撤横幅")
        self.assertTrue(res[0]["loginEnabled"])
        self.assertTrue(res[0]["runtimeCleared"])
        self.assertFalse(res[0]["pendingAfterRefusal"], "同步拒绝也必须有轮次终态")

    def test_business_results_survive_component_rounds(self):
        """F：真实报名结果在组件错误出现与恢复期间保留；普通登录失败仍按既有结果呈现。"""
        res = run_js(r"""
setResult({ ok: true, title: "报名成功：校园夜跑", text: "建议去活动页确认一眼。" });
window.pywebview = { api: {
  check: () => Promise.resolve({ ok: true, queued: true }),
  list_activities: () => Promise.resolve({ ok: true, queued: true }),
}};
refresh(false);
await Promise.resolve();
window.onBackend.activities({ ok: false, text: "程序组件缺失，请重新下载完整版本",
  items: [], runtime_error: "worker_missing" });
await Promise.resolve(); await Promise.resolve();
const result = document.querySelector("#result");
const keptDuring = !result.hidden && result.innerHTML.indexOf("报名成功：校园夜跑") >= 0;
window.onBackend.activities({ ok: true, items: [] });
await Promise.resolve(); await Promise.resolve();
const keptAfter = !result.hidden && result.innerHTML.indexOf("报名成功：校园夜跑") >= 0;
/* 普通登录失败（非组件码）：既有「登录没能开始」结果框语义不变 */
window.pywebview = { api: {
  login: () => Promise.resolve({ ok: false, text: "[X] 窗口被关掉了" }),
}};
doLogin();
await Promise.resolve(); await Promise.resolve();
const loginResult = document.querySelector("#result");
out.push({
  keptDuring, keptAfter,
  bannerUp: document.querySelector("#banners").innerHTML
    .indexOf("无法找到所需程序组件") >= 0,
  normalLoginResultShown: !loginResult.hidden &&
    loginResult.innerHTML.indexOf("登录没能开始") >= 0,
});
""")
        self.assertTrue(res[0]["keptDuring"], "组件错误出现不得抹掉真实报名结果")
        self.assertTrue(res[0]["keptAfter"], "组件恢复也不得抹掉真实报名结果")
        self.assertTrue(res[0]["bannerUp"], "组件横幅与业务结果并存")
        self.assertTrue(res[0]["normalLoginResultShown"], "普通登录失败保持既有结果框语义")


class _ActionInterleaveMixin:
    """FB4-R1 交错场景的共享装置：受控 check/list/动作 Promise，全走生产动作路径。"""

    _CHECK_OK_EVENT = r"""
resolveCheck({ ok: true, queued: true });
window.onBackend.check({ ok: true, result: "valid", hours_left: 10, session_present: true });
await Promise.resolve();
"""
    _LIST_OK_EVENT = r"""
resolveList({ ok: true, queued: true });
window.onBackend.activities({ ok: true, items: [] });
await Promise.resolve();
"""

    @staticmethod
    def _interleave_snippet(action_key, prepare, body):
        template = r"""
__PREPARE__
setResult(null);   /* 结果桩初始可见：场景起点归零（生产动作自身也会清） */
let resolveCheck, resolveList, resolveAction;
window.pywebview = { api: {
  check: () => new Promise((resolve) => { resolveCheck = resolve; }),
  list_activities: () => new Promise((resolve) => { resolveList = resolve; }),
  __ACTION__: () => new Promise((resolve) => { resolveAction = resolve; }),
}};
__BODY__
const kept = {
  error: S.runtime.error,
  roundError: S.runtime.roundError,
  bannerHint: (document.querySelector("#banners").innerHTML
    .match(/无法找到所需程序组件|被系统拦住或没有权限|组件在但没能启动/g) || []).length,
  recheck: (document.querySelector("#banners").innerHTML
    .match(/data-act="runtime-refresh"/g) || []).length,
  resultVisible: !document.querySelector("#result").hidden,
  loginDisabled: document.querySelector("#btn-login").disabled,
  startDisabled: document.querySelector("#btn-plan").disabled,
  planText: document.querySelector("#btn-plan").textContent,
  starting: S.starting,
};
/* 下一轮完整复查：只有一分支完成时旧横幅还在；两分支无组件故障后恢复 */
window.pywebview = { api: {
  check: () => Promise.resolve({ ok: true, queued: true }),
  list_activities: () => Promise.resolve({ ok: true, queued: true }),
}};
refresh(false);
await Promise.resolve();
window.onBackend.check({ ok: true, result: "valid", hours_left: 10, session_present: true });
await Promise.resolve(); await Promise.resolve();
const midRound = (document.querySelector("#banners").innerHTML
  .match(/data-act="runtime-refresh"/g) || []).length;
window.onBackend.activities({ ok: true, items: [] });
await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
out.push({ kept, midRound,
  bannerGone: (document.querySelector("#banners").innerHTML
    .match(/data-act="runtime-refresh"/g) || []).length === 0,
  runtimeCleared: !S.runtime.error,
  roundCleared: !S.runtime.roundError,
  loginEnabled: !document.querySelector("#btn-login").disabled,
});
"""
        return (template.replace("__PREPARE__", prepare)
                .replace("__ACTION__", action_key)
                .replace("__BODY__", body))

    @staticmethod
    def _phase_orders():
        """动作失败前无分支成功 / check 先成功 / list 先成功 三种交错。"""
        return {
            "before-none": ("", _ActionInterleaveMixin._CHECK_OK_EVENT
                            + _ActionInterleaveMixin._LIST_OK_EVENT),
            "check-first": (_ActionInterleaveMixin._CHECK_OK_EVENT,
                            _ActionInterleaveMixin._LIST_OK_EVENT),
            "list-first": (_ActionInterleaveMixin._LIST_OK_EVENT,
                           _ActionInterleaveMixin._CHECK_OK_EVENT),
        }


class ActionInterleaveBehaviorTests(_ActionInterleaveMixin, unittest.TestCase):
    """FB4-R1：刷新未收尾时的登录/开始组件失败必须进入本轮累计（迟到成功不得撤）。

    每组经生产 doLogin()/startPlan() 进入（受控 Promise），覆盖三种交错顺序与
    下一轮恢复；零学校/浏览器/进程。本任务仅定义，不运行。
    """

    _START_PREPARE = r"""
const item = { id: "a1", name: "活动", sign_start: "2099-01-01 09:00:00",
  sign_end: "2099-01-02 09:00:00", act_start: "2099-01-03 09:00:00",
  act_end: "2099-01-03 11:00:00", signed: false, seconds_to_open: 1,
  state: "未开放", place: "", checkin_required: null, checkout_required: null };
S.items = [item];
S.byId = new Map([["a1", item]]);
S.listState = "ready";
S.login.result = "valid";
S.login.checking = false;
S.selected.add("a1");
S.demo = false;
"""

    @staticmethod
    def _assert_kept_and_recovered(case, expected_code, expect_starting_released=False):
        kept = case["kept"]
        assert kept["error"] == expected_code, kept
        assert kept["roundError"] == expected_code, kept
        assert kept["bannerHint"] == 1, kept
        assert kept["recheck"] == 1, kept
        assert kept["resultVisible"] is False, kept
        assert kept["loginDisabled"] is True, kept
        assert kept["startDisabled"] is True, kept
        if expect_starting_released:
            assert kept["starting"] is False, kept
            assert kept["planText"] == "开始报名计划", kept
        assert case["midRound"] == 1, case
        assert case["bannerGone"] is True, case
        assert case["runtimeCleared"] is True, case
        assert case["roundCleared"] is True, case
        assert case["loginEnabled"] is True, case

    def test_login_component_failure_survives_late_refresh_success(self):
        """登录组：刷新未收尾时登录组件失败——迟到成功不得撤新横幅。"""
        payload = ('{ ok: false, error: "worker_permission", '
                   'text: "程序组件无法读取，请重新下载完整版本" }')
        for name, (before, after) in self._phase_orders().items():
            with self.subTest(order=name):
                body = ("refresh(false);\nawait Promise.resolve();\n" + before
                        + "\ndoLogin();\nawait Promise.resolve(); await Promise.resolve();\n"
                        + "resolveAction(" + payload + ");\n"
                        + "await Promise.resolve(); await Promise.resolve();"
                        + " await Promise.resolve();\n" + after)
                res = run_js(self._interleave_snippet("login", "", body))
                self.assertEqual(res[0]["kept"]["error"], "worker_permission")
                self._assert_kept_and_recovered(res[0], "worker_permission")

    def test_start_plan_component_failure_survives_late_refresh_success(self):
        """开始组：先点开始、刷新未收尾时动作失败——迟到成功不得撤新横幅。"""
        payload = ('{ ok: false, error: "worker_missing", '
                   'text: "程序组件缺失，请重新下载完整版本" }')
        for name, (before, after) in self._phase_orders().items():
            with self.subTest(order=name):
                body = ("startPlan();\nawait Promise.resolve();\n"
                        + "refresh(false);\nawait Promise.resolve();\n" + before
                        + "\nresolveAction(" + payload + ");\n"
                        + "await Promise.resolve(); await Promise.resolve();"
                        + " await Promise.resolve();\n" + after)
                res = run_js(self._interleave_snippet("start_plan",
                                                      self._START_PREPARE, body))
                self.assertEqual(res[0]["kept"]["error"], "worker_missing")
                self._assert_kept_and_recovered(res[0], "worker_missing",
                                                expect_starting_released=True)


class ErrorFeedbackRoundtripContractTests(unittest.TestCase):
    """FB4 接线的静态契约：四入口消费、轮次协议、提交中标志。"""

    def test_apply_activities_consumes_runtime_error(self):
        js = read("ui/app.js")
        body = js[js.index("function applyActivities("):js.index("function render(")]
        self.assertIn("noteRoundError(res.runtime_error)", body,
                      "列表失败必须先消费后端组件归类再渲染")
        self.assertIn('settleRefreshBranch("list")', body,
                      "activities 事件必须收口本轮 list 分支")

    def test_check_event_uses_round_protocol(self):
        js = read("ui/app.js")
        handler = js[js.index("window.onBackend"):]
        self.assertNotIn("S.runtime.error = data.runtime_error || null", handler,
                         "check 事件不得无条件覆盖组件结论（会抹掉同轮另一分支故障）")
        self.assertIn("S.runtime.roundError", handler, "check 终态必须进轮次故障累计")

    def test_login_and_start_plan_consume_component_codes(self):
        js = read("ui/app.js")
        login_body = js[js.index("function doLogin()"):
                        js.index("/* ---------------------------------------------------------------- 事件 */")]
        self.assertIn("RUNTIME_ERRORS[res.error]", login_body,
                      "登录同步失败必须只认可知组件码并走横幅")
        start_body = js[js.index("function startPlan()"):
                        js.index("function cancelTask(")]
        self.assertIn("RUNTIME_ERRORS[res.error]", start_body,
                      "开始计划的同步失败必须只认可知组件码并走横幅")
        self.assertIn("S.starting = true", start_body, "提交中标志必须置位")

    def test_action_failures_enter_round_accumulator(self):
        """FB4-R1：两个动作分支必须复用 noteRoundError（写 error+roundError），
        不得只写 S.runtime.error——否则刷新未收尾时迟到成功会撤掉新横幅。"""
        js = read("ui/app.js")
        login_body = js[js.index("function doLogin()"):
                        js.index("/* ---------------------------------------------------------------- 事件 */")]
        start_body = js[js.index("function startPlan()"):
                        js.index("function cancelTask(")]
        for label, body in (("doLogin", login_body), ("startPlan", start_body)):
            self.assertIn("noteRoundError(res.error)", body,
                          "%s 的组件失败必须进入本轮故障累计" % label)
            self.assertNotIn("S.runtime.error = res.error", body,
                             "%s 不得绕过累计函数只写显示态" % label)
        note = js[js.index("function noteRoundError("):
                  js.index("function settleRefreshBranch(")]
        self.assertIn("S.runtime.roundError = code", note)
        self.assertIn("S.runtime.error = code", note)
        gate = js[js.index("function updateControls("):
                  js.index("/* ---------------------------------------------------------------- 横幅 */")]
        self.assertIn("S.starting", gate, "提交中标志必须进入开始按钮门禁")


class RuntimeDisplayBehaviorTests(unittest.TestCase):
    """UX-04/05：当前目标只跟真实任务走，单场结果不抢写整屏（node vm 行为）。

    全部经生产函数 render()/handleTask()/handlePlanDone() 进入；
    零学校、零 worker、零浏览器。本任务只定义，不运行。
    """

    def test_canceled_task_leaves_batch_count(self):
        """A 取消、C 仍等待：批次由 2 变 1，已取消的场次不再当目标。"""
        res = run_js(r"""
const start = "2099-01-01 09:00:00";
S.busy = true;
S.plan = { id: "p1", status: "running", tasks: new Map() };
S.plan.tasks.set("A", { task_id: "A", activity_id: "A", name: "活动A",
  status: "canceled", sign_start: start, signup_mode: "pre_register" });
S.plan.tasks.set("C", { task_id: "C", activity_id: "C", name: "活动C",
  status: "waiting", sign_start: start, signup_mode: "pre_register" });
/* 原勾选集合没被清理：修复前倒计时读的就是它，会继续说「同时提交 2 场」 */
S.selected = new Set(["A", "C"]);
render();
const view = executionView();
out.push({ phase: view.phase, taskId: view.task.task_id, count: view.count,
  countdown: document.querySelector("#countdown").textContent,
  title: document.querySelector("#selected").textContent });
""")
        case = res[0]
        self.assertEqual(case["phase"], "waiting")
        self.assertEqual(case["taskId"], "C", "当前目标必须是还没结束的 C")
        self.assertEqual(case["count"], 1, "取消 A 后同批只剩 1 场")
        self.assertIn("同时开放 1 场", case["countdown"])
        self.assertNotIn("2 场", case["countdown"], "已取消的场次不得继续计入批次")
        self.assertEqual(case["title"], "活动C", "标题必须跟着当前执行目标")

    def test_success_leaves_target_on_future_activity(self):
        """立即场成功后目标回到仍等待的未来场；LED 标注用真实开放时刻，不重拼。"""
        res = run_js(r"""
const start = "2099-01-01 09:00:00";
S.busy = true;
S.plan = { id: "p1", status: "running", tasks: new Map() };
S.plan.tasks.set("B", { task_id: "B", activity_id: "B", name: "活动B",
  status: "success", sign_start: "2020-01-01 09:00:00", signup_mode: "instant_signup" });
S.plan.tasks.set("A", { task_id: "A", activity_id: "A", name: "活动A",
  status: "waiting", sign_start: start, signup_mode: "pre_register" });
render();
out.push({ taskId: executionView().task.task_id,
  countdown: document.querySelector("#countdown").textContent,
  label: document.querySelector("#led-label b").textContent,
  expected: fmtClock(toTs(start)) });
""")
        case = res[0]
        self.assertEqual(case["taskId"], "A", "立即场成功后目标必须落到未来场")
        self.assertIn("同时开放 1 场", case["countdown"])
        self.assertNotIn("00:00:00", case["countdown"], "未来场不得显示成全开放")
        self.assertEqual(case["label"], case["expected"],
                         "LED 标注必须直接格式化开放时刻（now+left 会差 1 秒）")

    def test_preview_open_label_uses_absolute_time(self):
        """未选择与选择预览也必须直接显示开放时刻，不能从取整倒计时反推。"""
        res = run_js(r"""
const start = "2099-01-01 09:00:00";
setBusinessNow((toTs(start) - 60) * 1000 + 250);
const item = { id: "preview", name: "预览场", sign_start: start,
  sign_end: "2099-01-01 10:00:00", act_start: "2099-01-02 10:00:00",
  act_end: "2099-01-02 11:00:00", signed: false };
S.busy = false; S.items = [item]; S.byId = new Map([[item.id, item]]);
S.selected = new Set(); S.plan = { id: null, status: "idle", tasks: new Map() };
paintLed();
const idle = document.querySelector("#led-label b").textContent;
S.selected.add(item.id);
paintLed();
const selected = document.querySelector("#led-label b").textContent;
out.push({ idle, selected, expected: fmtClock(toTs(start)) });
""")
        self.assertEqual(res[0]["idle"], res[0]["expected"])
        self.assertEqual(res[0]["selected"], res[0]["expected"])

    def test_submitting_wins_over_waiting(self):
        """真实提交优先于未来预热；批次数只数真正提交过且仍活跃的任务。

        样本必须是生产可能出现的形状：已开放（sign_start 在过去）+ 真实
        run_started_at + attempts>0。未来 2099 + attempts 缺失的样本只能
        证明「raw submitting 也算提交」，正是本轮要修掉的错口径。
        """
        res = run_js(r"""
const open = "2026-10-25 19:00:00";
const openTs = toTs(open);
S.busy = true;
S.plan = { id: "p1", status: "running", tasks: new Map() };
/* 未来预热场：raw submitting，但还没开放、一次都没提交 */
S.plan.tasks.set("W", { task_id: "W", activity_id: "W", name: "预热场",
  status: "submitting", sign_start: "2099-01-01 09:00:00", page_opened_at: openTs,
  signup_mode: "pre_register" });
/* 两场已开放、各提交过一次（并列：应保持快照顺序取 S1） */
S.plan.tasks.set("S1", { task_id: "S1", activity_id: "S1", name: "提交场1",
  status: "submitting", attempts: 1, sign_start: open, run_started_at: openTs,
  retry_for_s: 600, signup_mode: "instant_signup" });
S.plan.tasks.set("S2", { task_id: "S2", activity_id: "S2", name: "提交场2",
  status: "submitting", attempts: 2, sign_start: open, run_started_at: openTs,
  retry_for_s: 600, signup_mode: "instant_signup" });
S.plan.tasks.set("D", { task_id: "D", activity_id: "D", name: "已完成",
  status: "success", attempts: 1, sign_start: open, signup_mode: "instant_signup" });
render();
const view = executionView();
out.push({ phase: view.phase, taskId: view.task.task_id, count: view.count,
  countdown: document.querySelector("#countdown").textContent,
  label: document.querySelector("#led-label b").textContent,
  expected: fmtClock(view.retryUntil),
  warmups: Array.from(S.plan.tasks.values())
    .map((task) => task.task_id + ":" + taskDisplayPhase(task)).join(",") });
""")
        case = res[0]
        self.assertEqual(case["phase"], "submitting")
        self.assertEqual(case["taskId"], "S1", "并列时保持快照顺序")
        self.assertEqual(case["count"], 2, "只数真正提交过且仍活跃的两场，不含未来预热")
        self.assertIn("正在提交 2 场", case["countdown"])
        self.assertEqual(case["label"], case["expected"], "LED 标注必须是当前场的重试截止")
        self.assertIn("W:preopen", case["warmups"], "未来预热不是提交阶段")
        self.assertIn("S1:retrying", case["warmups"])

    def test_all_terminal_waits_for_plan_done(self):
        """全终态但 plan_done 未到：汇总中；没有任务：准备中，都不提前报成功。"""
        res = run_js(r"""
setResult(null);        /* 结果桩归零：桩默认 hidden=false，真实首屏是隐藏的 */
S.busy = true;
S.plan = { id: "p1", status: "running", tasks: new Map() };
S.plan.tasks.set("A", { task_id: "A", activity_id: "A", name: "活动A",
  status: "success", sign_start: "2099-01-01 09:00:00", signup_mode: "instant_signup" });
render();
const settling = { phase: executionView().phase,
  countdown: document.querySelector("#countdown").textContent,
  tag: document.querySelector("#board-tag-text").textContent,
  text: document.querySelector("#board-text").textContent,
  resultHidden: document.querySelector("#result").hidden,
  board: document.querySelector("#board").dataset.planState };
S.plan.tasks = new Map();
render();
const preparing = { phase: executionView().phase,
  countdown: document.querySelector("#countdown").textContent,
  tag: document.querySelector("#board-tag-text").textContent };
out.push({ settling, preparing });
""")
        case = res[0]
        self.assertEqual(case["settling"]["phase"], "settling")
        self.assertEqual(case["settling"]["countdown"], "正在汇总结果")
        self.assertEqual(case["settling"]["tag"], "汇总中")
        self.assertTrue(case["settling"]["resultHidden"],
                        "计划级结论没到，结果框不得先写成功")
        self.assertEqual(case["settling"]["board"], "running", "汇总中仍是运行色")
        self.assertEqual(case["preparing"]["phase"], "preparing")
        self.assertEqual(case["preparing"]["countdown"], "正在准备")
        self.assertEqual(case["preparing"]["tag"], "准备中")

    def test_snapshot_restore_without_selection_or_list_item(self):
        """快照重载且选择为空、列表项不在：仍从任务快照得到同一目标，不回退到别的场。"""
        res = run_js(r"""
S.selected = new Set();
S.items = [];
S.byId = new Map();
S.busy = true;
S.plan = { id: "p1", status: "running", tasks: new Map() };
S.plan.tasks.set("A", { task_id: "A", activity_id: "A", name: "活动A",
  status: "waiting", sign_start: "2099-01-01 09:00:00", signup_mode: "pre_register" });
render();
const restored = { taskId: executionView().task.task_id,
  itemName: executionView().item.name,
  title: document.querySelector("#selected").textContent,
  route: !!routeTarget(),
  countdown: document.querySelector("#countdown").textContent };
/* 连 sign_start 都没有：显示「待公布」，不拿另一场或当前时间凑 */
S.plan.tasks.set("A", { task_id: "A", activity_id: "A", name: "活动A", status: "waiting" });
render();
const unknown = { openAt: executionView().openAt,
  countdown: document.querySelector("#countdown").textContent,
  label: document.querySelector("#led-label b").textContent,
  title: document.querySelector("#selected").textContent };
out.push({ restored, unknown });
""")
        case = res[0]
        self.assertEqual(case["restored"]["taskId"], "A")
        self.assertEqual(case["restored"]["itemName"], "活动A", "列表项缺失时用任务字段现构")
        self.assertEqual(case["restored"]["title"], "活动A")
        self.assertTrue(case["restored"]["route"], "路线目标必须仍能从任务快照得到")
        self.assertIn("同时开放 1 场", case["restored"]["countdown"])
        self.assertIsNone(case["unknown"]["openAt"])
        self.assertIn("时间待公布", case["unknown"]["countdown"])
        self.assertEqual(case["unknown"]["label"], "待公布")
        self.assertEqual(case["unknown"]["title"], "活动A", "缺字段也不得换成另一场")

    def test_single_failure_does_not_paint_plan(self):
        """单场失败只更新任务反馈；整屏色彩等计划级终态，且各终态各有其色。"""
        res = run_js(r"""
setResult(null);        /* 结果桩归零：桩默认 hidden=false，真实首屏是隐藏的 */
S.busy = true;
S.plan = { id: "p1", status: "running", tasks: new Map() };
handleTask({ plan_id: "p1", task_id: "A", activity_id: "A", name: "活动A",
  status: "failed", reason: "名额已满", stop_reason: "max_attempts",
  sign_start: "2099-01-01 09:00:00", sign_end: "2099-01-02 09:00:00" });
const running = { board: document.querySelector("#board").dataset.planState,
  resultHidden: document.querySelector("#result").hidden,
  tasksHtml: document.querySelector("#plan-tasks").innerHTML };
handlePlanDone({ plan_id: "p1", status: "failed",
  summary: { counts: { failed: 1 } } });
const failed = { board: document.querySelector("#board").dataset.planState,
  resultVisible: !document.querySelector("#result").hidden,
  resultHtml: document.querySelector("#result").innerHTML };
const colors = {};
[["partial"], ["completed"], ["stopped"]].forEach((pair) => {
  S.plan.status = pair[0];
  S.plan.doneSelection = selectionSignature();
  render();
  colors[pair[0]] = document.querySelector("#board").dataset.planState;
});
S.selected.add("zzz");
render();
colors.afterChange = document.querySelector("#board").dataset.planState;
out.push({ running, failed, colors });
""")
        case = res[0]
        self.assertEqual(case["running"]["board"], "running",
                         "单场失败不得把整屏染成计划失败")
        self.assertTrue(case["running"]["resultHidden"],
                        "运行中的单场失败不得抢写结果框")
        self.assertIn("task-state is-bad", case["running"]["tasksHtml"],
                      "任务卡要照实显示这一场失败")
        self.assertIn("报名未成功", case["running"]["tasksHtml"])
        self.assertIn("达到最大提交次数", case["running"]["tasksHtml"],
                      "任务卡要写出到达的停止条件")
        self.assertEqual(case["failed"]["board"], "failed")
        self.assertIn("报名计划没有成功", case["failed"]["resultHtml"])
        self.assertEqual(case["colors"]["partial"], "partial")
        self.assertEqual(case["colors"]["completed"], "completed")
        self.assertEqual(case["colors"]["stopped"], "stopped")
        self.assertEqual(case["colors"]["afterChange"], "idle",
                         "选择集合变了就回到常态，不继续挂着上一轮终态")


class RetryDeadlineBoundaryTests(unittest.TestCase):
    """BC-01：界面回退截止的锚点不能随墙钟跨过开放时刻而切换。

    样本按生产形状写：预报名提前 100 秒开页（run_started_at = 开放 - 100），
    运行期间还没有 retry_deadline 遥测。worker 的 retry_anchor 在开页前就钉死，
    界面若在开放那一秒改用启动点，会让截止倒退 100 秒。
    """

    def test_anchor_is_stable_across_open(self):
        """open-1 秒 / 开放瞬间 / open+1 秒：推导出的截止必须是同一时刻。"""
        res = run_js(r"""
const openTs = toTs("2026-10-25 20:00:00");
const task = { task_id: "A", status: "submitting", attempts: 1,
  sign_start: "2026-10-25 20:00:00", sign_end: "", retry_for_s: 120,
  run_started_at: openTs - 100 };
out.push({ before: effectiveRetryDeadline(task, openTs - 1),
  atOpen: effectiveRetryDeadline(task, openTs),
  after: effectiveRetryDeadline(task, openTs + 1),
  expected: openTs + 120 });
""")
        case = res[0]
        self.assertEqual(case["before"], case["expected"],
                         "开放前按 max(开放, 启动) 推导")
        self.assertEqual(case["atOpen"], case["expected"], "开放瞬间不得换锚")
        self.assertEqual(case["after"], case["expected"], "开放后不得倒退回启动点 + 时长")

    def test_truncation_fallback_and_telemetry(self):
        """sign_end 截断、迟到启动、时长 0 回退、遥测优先、未知返回 null。"""
        res = run_js(r"""
const openTs = toTs("2026-10-25 20:00:00");
const base = { task_id: "A", status: "submitting", attempts: 1,
  sign_start: "2026-10-25 20:00:00", retry_for_s: 120, run_started_at: openTs - 100 };
const truncated = effectiveRetryDeadline(
  Object.assign({}, base, { sign_end: "2026-10-25 20:01:00" }), openTs);
/* 迟到启动：开放后才拉起 worker（已开放场次按启动时刻起算） */
const late = effectiveRetryDeadline(
  Object.assign({}, base, { run_started_at: openTs + 30 }), openTs + 40);
/* 时长 0 且没有 sign_end：回退默认 120 秒 */
const zeroNoEnd = effectiveRetryDeadline(
  Object.assign({}, base, { retry_for_s: 0, sign_end: "" }), openTs);
/* 时长 0 且有 sign_end：直接截到报名截止 */
const zeroWithEnd = effectiveRetryDeadline(
  Object.assign({}, base, { retry_for_s: 0, sign_end: "2026-10-25 20:30:00" }), openTs);
/* 遥测优先：即便本机推导不同，也以 worker 的结构化截止为准 */
const telemetry = effectiveRetryDeadline(
  Object.assign({}, base, { retry_deadline: openTs + 300 }), openTs);
/* 已开放、又没有任何启动参考点：未知（不得拿 now 冒充 0 秒） */
const unknown = effectiveRetryDeadline(
  { task_id: "B", status: "submitting", sign_start: "2026-10-25 20:00:00",
    sign_end: "", retry_for_s: 120 }, openTs + 5);
/* 未来场且还没启动：原定开放作计划参考 */
const planned = effectiveRetryDeadline(
  { task_id: "C", status: "waiting", sign_start: "2026-10-25 21:00:00",
    sign_end: "", retry_for_s: 120 }, openTs);
out.push({ truncated, late, zeroNoEnd, zeroWithEnd, telemetry, unknown, planned,
  openTs });
""")
        case = res[0]
        self.assertEqual(case["truncated"], case["openTs"] + 60, "sign_end 必须硬截断")
        self.assertEqual(case["late"], case["openTs"] + 30 + 120,
                         "迟到启动从启动点起算，不再借用原定开放")
        self.assertEqual(case["zeroNoEnd"], case["openTs"] + 120,
                         "时长 0 且无截止时回退 120 秒")
        self.assertEqual(case["zeroWithEnd"], case["openTs"] + 1800,
                         "时长 0 且有截止时直接到截止")
        self.assertEqual(case["telemetry"], case["openTs"] + 300, "遥测必须优先")
        self.assertIsNone(case["unknown"], "没有时间参考时必须是未知，不是当前时刻")
        self.assertEqual(case["planned"], case["openTs"] + 3600 + 120,
                         "未来场未启动时用原定开放作计划参考")


    def test_absolute_deadline_needs_no_anchor(self):
        """ED-01：「直到报名截止」+ 已知 sign_end 时，截止与起点无关。

        排队中或快照字段还没齐时，正会出现「有绝对截止、没有启动/开放参考点」；
        这时不能判未知。有限时长仍需要起点，缺 sign_end 的 0 时长仍回退 120 秒。
        """
        res = run_js(r"""
const now = toTs("2026-10-25 21:00:00");
out.push({
  /* 已开放、没有 run_started_at / submitSeen / 遥测 */
  openPast: effectiveRetryDeadline({ status: "queued",
    sign_start: "2026-10-25 20:00:00", sign_end: "2026-10-25 22:00:00",
    retry_for_s: 0 }, now),
  /* 连开放时间都还没有，但有绝对截止 */
  noOpen: effectiveRetryDeadline({ status: "queued",
    sign_start: "", sign_end: "2026-10-25 22:00:00", retry_for_s: 0 }, now),
  /* 0 时长且没有 sign_end、也没有起点：确实未知 */
  noEnd: effectiveRetryDeadline({ status: "queued",
    sign_start: "2026-10-25 20:00:00", sign_end: "", retry_for_s: 0 }, now),
  /* 有限时长 + 无起点：仍未知（不能拿 now 起算） */
  finiteNoStart: effectiveRetryDeadline({ status: "queued",
    sign_start: "2026-10-25 20:00:00", sign_end: "2026-10-25 22:00:00",
    retry_for_s: 120 }, now),
  /* 0 时长 + 绝对截止 + 遥测：遥测优先 */
  telemetry: effectiveRetryDeadline({ status: "queued",
    sign_start: "", sign_end: "2026-10-25 22:00:00", retry_for_s: 0,
    retry_deadline: toTs("2026-10-25 21:30:00") }, now),
  deadline: toTs("2026-10-25 22:00:00"),
  telemetry_expected: toTs("2026-10-25 21:30:00"),
});
""")
        case = res[0]
        self.assertEqual(case["openPast"], case["deadline"],
                         "已开放无起点时，0 时长应直接取 sign_end")
        self.assertEqual(case["noOpen"], case["deadline"],
                         "连开放时间都没有，也不影响绝对截止")
        self.assertIsNone(case["noEnd"], "没有 sign_end 又无起点，只能未知")
        self.assertIsNone(case["finiteNoStart"], "有限时长缺起点仍未知，不拿 now 起算")
        self.assertEqual(case["telemetry"], case["telemetry_expected"],
                         "有遥测时以 worker 的结构化截止为准")
        self.assertLess(case["telemetry"], case["deadline"], "遥测优先于 sign_end")


class DisplayPhaseBoundaryTests(unittest.TestCase):
    """BC-02：raw submitting 只表示 worker 已接管，预热不等于已提交。"""

    def test_activity_row_tracks_phase_and_open_time(self):
        """活动行与任务卡共用阶段，缓存的未开放文本不能阻止跨开放时刻换组。"""
        res = run_js(r"""
const open = toTs("2099-01-01 09:00:00");
const item = { id: "A", sign_start: "2099-01-01 09:00:00",
  sign_end: "2099-01-01 10:00:00", state: "未开放" };
const task = { ...item, task_id: "A", status: "submitting", attempts: 0 };
setBusinessNow((open - 10) * 1000);
const waiting = stateCellHtml(item, stateOf(item), "10 秒", task);
setBusinessNow((open + 1) * 1000);
const preparing = stateCellHtml(item, stateOf(item), "已开放", task);
const rowState = stateOf(item);
task.attempts = 1;
const retrying = stateCellHtml(item, rowState, "已开放", task);
out.push({ waiting, preparing, retrying, rowState });
""")
        self.assertIn("等待开放", res[0]["waiting"])
        self.assertIn("准备报名", res[0]["preparing"])
        self.assertIn("提交中", res[0]["retrying"])
        self.assertEqual(res[0]["rowState"], "open")

    def test_future_prewarm_is_not_reported_as_submitting(self):
        """未来场 raw submitting（提前开页）：标题/LED/任务卡都必须是等待开放。

        再把时钟推过开放一秒并跑 tick：必须变成「准备报名」且不被旧文案盖回。
        """
        res = run_js(r"""
const openStr = "2099-01-01 09:00:00";
const openTs = toTs(openStr);
S.busy = true;
S.plan = { id: "p1", status: "running", tasks: new Map() };
S.plan.tasks.set("A", { task_id: "A", activity_id: "A", name: "未来场",
  status: "submitting", sign_start: openStr, page_opened_at: openTs - 100,
  signup_mode: "pre_register" });
render();
const before = { phase: executionView().phase, display: executionView().display,
  countdown: document.querySelector("#countdown").textContent,
  tag: document.querySelector("#board-tag-text").textContent,
  led: document.querySelector("#led-label b").textContent,
  html: document.querySelector("#plan-tasks").innerHTML };
const openTs0 = openTs;
setBusinessNow((openTs0 + 1) * 1000);
tick();
const after = { phase: executionView().phase,
  countdown: document.querySelector("#countdown").textContent,
  tag: document.querySelector("#board-tag-text").textContent,
  detail: document.querySelector('.task-row[data-task-id="A"] .task-detail').textContent,
  label: document.querySelector('.task-row[data-task-id="A"] .task-state').textContent };
out.push({ before, after, openClock: fmtClock(openTs) });
""")
        case = res[0]
        self.assertEqual(case["before"]["phase"], "waiting", "未来预热不是提交阶段")
        self.assertEqual(case["before"]["display"], "preopen")
        self.assertIn("同时开放 1 场", case["before"]["countdown"])
        self.assertEqual(case["before"]["tag"], "等开放")
        self.assertEqual(case["before"]["led"], case["openClock"],
                         "LED 必须走到原定开放，不是重试窗口")
        self.assertIn("提前准备", case["before"]["html"])
        self.assertNotIn("正在提交", case["before"]["html"],
                         "任务卡不得把预热说成已提交")
        self.assertIn("等待开放", case["before"]["html"])
        self.assertEqual(case["after"]["phase"], "arming", "开放后进入准备报名")
        self.assertIn("已开放", case["after"]["countdown"])
        self.assertEqual(case["after"]["tag"], "准备报名")
        self.assertNotIn("等开放", case["after"]["tag"], "已开放就不能还说等开放")
        self.assertIn("已开放，正在准备提交", case["after"]["detail"])
        self.assertEqual(case["after"]["label"], "准备报名",
                         "标签也要跟着 tick 更新，不能退回原始 submitting 文案")

    def test_missing_attempts_is_not_a_submission(self):
        """attempts 缺失或非法一律不算已提交（NaN/负数/字符串都不算）。"""
        res = run_js(r"""
const openTs = toTs("2026-10-25 19:00:00");
const phases = [
  taskDisplayPhase({ status: "submitting", sign_start: "2026-10-25 19:00:00" }, openTs + 1),
  taskDisplayPhase({ status: "submitting", attempts: 0, sign_start: "2026-10-25 19:00:00" }, openTs + 1),
  taskDisplayPhase({ status: "submitting", attempts: "abc", sign_start: "2026-10-25 19:00:00" }, openTs + 1),
  taskDisplayPhase({ status: "submitting", attempts: -2, sign_start: "2026-10-25 19:00:00" }, openTs + 1),
  taskDisplayPhase({ status: "submitting", attempts: 1, sign_start: "2026-10-25 19:00:00" }, openTs + 1),
  taskDisplayPhase({ status: "queued", sign_start: "2026-10-25 19:00:00" }, openTs + 1),
  taskDisplayPhase({ status: "success", attempts: 3, sign_start: "2026-10-25 19:00:00" }, openTs + 1),
];
out.push({ phases });
""")
        phases = res[0]["phases"]
        self.assertEqual(phases[:4], ["preparing"] * 4, "没有真实提交就不能进 retrying")
        self.assertEqual(phases[4], "retrying")
        self.assertEqual(phases[5], "preparing", "已开放的排队任务是准备报名，不是等开放")
        self.assertEqual(phases[6], "terminal")


class EmptyPreferenceBehaviorTests(unittest.TestCase):
    """UX-01/03 + 清筛选：空态按真实原因给下一步，清筛选不重置用户策略。"""

    def test_known_zero_has_no_login_nudge(self):
        """登录有效 + 成功返回 0 场：指标写 0，空态不再诱导登录，大屏不重复原因。"""
        res = run_js(r"""
S.items = [];
S.byId = new Map();
S.listState = "ready";
S.login.present = true; S.login.result = "valid"; S.login.ok = true; S.login.checking = false;
S.selected = new Set();
render();
out.push({ title: document.querySelector("#empty-title").textContent,
  text: document.querySelector("#empty-text").textContent,
  total: document.querySelector("#metric-total").textContent,
  free: document.querySelector("#metric-free").textContent,
  action: document.querySelector("#empty-action").textContent,
  boardTitle: document.querySelector("#selected").textContent,
  boardText: document.querySelector("#board-text").textContent });
""")
        case = res[0]
        self.assertEqual(case["title"], "学校当前没有返回活动")
        self.assertNotIn("重新登录", case["text"])
        self.assertNotIn("点右上角", case["text"], "登录有效时不得再给登录下一步")
        self.assertEqual(case["total"], "0", "已知零条必须写 0，不是破折号")
        self.assertEqual(case["free"], "0")
        self.assertIn("刷新", case["action"])
        self.assertEqual(case["boardTitle"], "学校这次没有返回活动")
        self.assertNotIn("还没公布", case["boardText"], "大屏不得把已知零说成还没公布")

    def test_login_states_get_their_own_next_step(self):
        """none→登录、expired→重新登录、unknown→刷新（且不得说成过期）。"""
        res = run_js(r"""
function snapshot() {
  render();
  return { title: document.querySelector("#empty-title").textContent,
    text: document.querySelector("#empty-text").textContent,
    action: document.querySelector("#empty-action").textContent };
}
S.items = []; S.byId = new Map(); S.listState = "ready";
S.login.present = false; S.login.result = "none"; S.login.checking = false;
const none = snapshot();
S.login.present = true; S.login.result = "expired"; S.login.ok = false;
const expired = snapshot();
S.login.result = "unknown"; S.login.ok = false;
const unknown = snapshot();
out.push({ none, expired, unknown });
""")
        case = res[0]
        self.assertEqual(case["none"]["title"], "登录后查看活动")
        self.assertIn("「登录」", case["none"]["text"])
        self.assertIn("重新登录", case["expired"]["title"])
        self.assertIn("「重新登录」", case["expired"]["text"])
        self.assertEqual(case["unknown"]["title"], "暂时无法确认登录状态")
        for banned in ("过期", "重新登录"):
            self.assertNotIn(banned, case["unknown"]["text"],
                             "unknown 一律不得说成过期或要求重登")
        self.assertEqual(case["unknown"]["action"], "刷新")

    def test_error_and_idle_are_not_known_zero(self):
        """还没拉到 / 拉失败都不是「学校零活动」：指标未知，原因各归其位。"""
        res = run_js(r"""
function snapshot() {
  render();
  return { title: document.querySelector("#empty-title").textContent,
    text: document.querySelector("#empty-text").textContent,
    total: document.querySelector("#metric-total").textContent };
}
S.items = []; S.byId = new Map(); S.listState = "idle";
S.login.result = "valid"; S.login.present = true;
const idle = snapshot();
S.listState = "error"; S.listError = "连不上学校站点";
const error = snapshot();
out.push({ idle, error });
""")
        case = res[0]
        self.assertEqual(case["idle"]["title"], "还没有加载活动列表")
        self.assertEqual(case["idle"]["total"], "—", "没拉到之前是未知，不是 0")
        self.assertNotIn("学校当前没有返回活动", case["error"]["title"],
                         "网络失败不得伪装成学校零活动")
        self.assertEqual(case["error"]["total"], "—")

    def test_component_banner_keeps_single_owner(self):
        """组件问题仍只有横幅一个归属：空态不复读说明、不重复恢复按钮。"""
        res = run_js(r"""
S.items = []; S.byId = new Map(); S.listState = "error"; S.listError = "程序组件缺失";
S.runtime.error = "worker_missing";
S.login.result = "valid"; S.login.present = true;
render();
const html = document.querySelector("#banners").innerHTML;
out.push({ title: document.querySelector("#empty-title").textContent,
  text: document.querySelector("#empty-text").textContent,
  actionHidden: document.querySelector("#empty-action").hidden,
  rechecks: (html.match(/data-act="runtime-refresh"/g) || []).length,
  hints: (html.match(/无法找到所需程序组件/g) || []).length });
""")
        case = res[0]
        self.assertEqual(case["title"], "活动列表未加载")
        self.assertNotIn("登录", case["text"], "缺组件不许显示成「登录后查看活动」")
        self.assertTrue(case["actionHidden"], "空态不重复恢复按钮")
        self.assertEqual(case["rechecks"], 1, "横幅只有一个恢复入口")
        self.assertEqual(case["hints"], 1, "完整原因只出现一次")

    def test_clear_filter_keeps_retry_policy_and_other_prefs(self):
        """清筛选只重置 6 个筛选键：600/3、浏览器开关、日志、引导版本全部保留。"""
        res = run_js(r"""
const item = { id: "a1", name: "活动A", sign_start: "2099-01-01 09:00:00",
  sign_end: "2099-01-02 09:00:00", act_start: "2099-01-03 09:00:00",
  act_end: "2099-01-03 11:00:00", signed: false, state: "未开放" };
S.items = [normalize(item)];
S.byId = new Map([["a1", S.items[0]]]);
S.listState = "ready";
S.prefs = { retry_policy: { retry_for_s: 600, max_attempts: 3 }, headed: true,
  log_open: true, tour_version: 2, tour_completed_at: "2026-01-01T00:00:00Z",
  legacy_unknown: "keep" };
S.retryPolicy = { retry_for_s: 600, max_attempts: 3 };
S.logOpen = true;
S.filter = { q: "zzz", status: "free", time: "today", sort: "name",
  checkin: "yes", checkout: "no" };
/* 控件此刻显示用户设的 600 / 3：修复前 applyPrefs 会把它们打回 120 / 0 */
document.querySelector("#retry-for").value = "600";
document.querySelector("#max-attempts").value = "3";
const saved = [];
savePrefs = (patch) => { saved.push(patch); Object.assign(S.prefs, patch); };
render();
const emptyTitle = document.querySelector("#empty-title").textContent;
document.querySelector("#empty-action").click();
const payload = saved[saved.length - 1] || {};
out.push({ emptyTitle,
  filter: Object.assign({}, S.filter),
  retryFor: S.retryPolicy.retry_for_s, maxAttempts: S.retryPolicy.max_attempts,
  retrySelect: document.querySelector("#retry-for").value,
  maxInput: document.querySelector("#max-attempts").value,
  policy: S.prefs.retry_policy, headed: S.prefs.headed, logOpen: S.logOpen,
  tourVersion: S.prefs.tour_version, tourStamp: S.prefs.tour_completed_at,
  legacy: S.prefs.legacy_unknown,
  payloadKeys: Object.keys(payload), payloadFilter: payload.filter,
  q: document.querySelector("#q").value });
""")
        case = res[0]
        self.assertEqual(case["emptyTitle"], "没有符合筛选的活动")
        self.assertEqual(case["retryFor"], 600, "清筛选不得把重试时长打回默认")
        self.assertEqual(case["maxAttempts"], 3)
        self.assertEqual(case["retrySelect"], "600", "控件值必须还是用户设的 10 分钟")
        self.assertEqual(case["maxInput"], "3", "控件值必须还是用户设的 3 次")
        self.assertEqual(case["policy"]["retry_for_s"], 600, "偏好里的策略必须原样保留")
        self.assertTrue(case["headed"], "浏览器开关偏好必须保留")
        self.assertTrue(case["logOpen"], "运行记录展开状态必须保留")
        self.assertEqual(case["tourVersion"], 2, "引导内容版本必须保留")
        self.assertEqual(case["tourStamp"], "2026-01-01T00:00:00Z")
        self.assertEqual(case["legacy"], "keep", "未识别的偏好键不得被清掉")
        self.assertEqual(case["payloadKeys"], ["filter"], "save_prefs 只写过滤器差量")
        self.assertEqual(case["payloadFilter"]["q"], "")
        self.assertEqual(case["payloadFilter"]["status"], "all")
        self.assertEqual(case["filter"]["sort"], "open", "排序也要回到默认")
        self.assertEqual(case["q"], "", "搜索框跟着清空")

    def test_start_plan_payload_keeps_policy_after_clear(self):
        """清筛选后再开始：start_plan 载荷里的策略仍是 600/3。"""
        res = run_js(r"""
let sent = null;
window.pywebview = { api: {
  start_plan: (items, headed, policy) => {
    sent = { items, headed, policy };
    return Promise.resolve({ ok: true, plan_id: "p1" });
  },
} };
const item = { id: "a1", name: "活动A", sign_start: "2099-01-01 09:00:00",
  sign_end: "2099-01-02 09:00:00", act_start: "2099-01-03 09:00:00",
  act_end: "2099-01-03 11:00:00", signed: false, state: "未开放" };
S.items = [normalize(item)];
S.byId = new Map([["a1", S.items[0]]]);
S.listState = "ready";
S.prefs = { retry_policy: { retry_for_s: 600, max_attempts: 3 } };
S.retryPolicy = { retry_for_s: 600, max_attempts: 3 };
S.filter = { q: "zzz", status: "free", time: "today", sort: "name",
  checkin: "yes", checkout: "no" };
savePrefs = () => {};
render();
document.querySelector("#empty-action").click();   /* 先清筛选，不动策略 */
S.selected.add("a1");
S.login.present = true; S.login.result = "valid"; S.login.ok = true; S.login.checking = false;
S.demo = false;
render();
startPlan();
await Promise.resolve(); await Promise.resolve();
out.push({ policy: sent && sent.policy, itemCount: sent && sent.items.length,
  selected: S.selected.size });
""")
        case = res[0]
        self.assertEqual(case["itemCount"], 1)
        self.assertEqual(case["policy"], {"retry_for_s": 600, "max_attempts": 3},
                         "开始计划必须用用户设的 600/3，不能被清筛选重置")


    def test_ready_nonempty_without_target_keeps_data(self):
        """BC-03：有活动但没有可执行目标，不等于学校没返回。"""
        res = run_js(r"""
function snap() {
  render();
  return { title: document.querySelector("#selected").textContent,
    text: document.querySelector("#board-text").textContent,
    count: document.querySelector("#count").textContent };
}
S.listState = "ready"; S.login.result = "valid"; S.login.present = true;
S.selected = new Set();
const signed = normalize({ id: "s1", name: "已报名场", signed: true,
  sign_start: "2099-01-01 09:00:00", sign_end: "2099-01-02 09:00:00",
  act_start: "2099-01-03 09:00:00", act_end: "2099-01-03 11:00:00" });
const closed = normalize({ id: "c1", name: "已截止场",
  sign_start: "2020-01-01 09:00:00", sign_end: "2020-01-01 10:00:00",
  act_start: "2020-01-02 09:00:00", act_end: "2020-01-02 11:00:00" });
const unknown = normalize({ id: "u1", name: "待公布场",
  sign_start: "", sign_end: "", act_start: "2099-05-01 09:00:00",
  act_end: "2099-05-01 11:00:00" });
S.items = [signed]; S.byId = new Map([["s1", signed]]);
const onlySigned = snap();
S.items = [closed]; S.byId = new Map([["c1", closed]]);
const onlyClosed = snap();
S.items = [unknown]; S.byId = new Map([["u1", unknown]]);
const onlyUnknown = snap();
S.items = []; S.byId = new Map();
const reallyEmpty = snap();
out.push({ onlySigned, onlyClosed, onlyUnknown, reallyEmpty });
""")
        case = res[0]
        for name in ("onlySigned", "onlyClosed", "onlyUnknown"):
            self.assertEqual(case[name]["title"], "没有可报名的场次",
                             "%s：有活动但无目标，不能说学校没返回" % name)
            self.assertEqual(case[name]["count"], "1 / 1 场",
                             "%s：列表数据必须原样保留" % name)
        self.assertNotIn("学校这次没有返回活动", case["onlySigned"]["title"])
        self.assertEqual(case["reallyEmpty"]["title"], "学校这次没有返回活动",
                         "真正零条才归咎学校空列表")

    def test_check_arrival_updates_empty_state(self):
        """BC-04：检查中不说有效；后到的 check 结论自己把空态改过来。

        全程经 window.onBackend.check 进入，不在结论之后手动 render。
        """
        res = run_js(r"""
function snap() {
  return { title: document.querySelector("#empty-title").textContent,
    text: document.querySelector("#empty-text").textContent };
}
S.items = []; S.byId = new Map(); S.listState = "ready";
/* 刷新保留旧 result 并进入 checking：这正是提前写「登录是有效的」的窗口 */
S.login.present = true; S.login.result = "valid"; S.login.ok = true; S.login.checking = true;
render();
const whileChecking = snap();
window.onBackend.check({ ok: false, result: "expired", hours_left: 0, session_present: true });
const afterExpired = snap();
/* 再来一轮 unknown：只能说不确定，不说过期、不逼重登 */
S.login.checking = true;
render();
const checkingAgain = snap();
window.onBackend.check({ ok: false, result: "unknown", hours_left: null, session_present: true });
const afterUnknown = snap();
/* 相反次序：check 先到、list 后到 */
S.login.checking = false;
window.onBackend.check({ ok: true, result: "valid", hours_left: 6, session_present: true });
window.onBackend.activities({ ok: true, items: [] });
const checkThenList = snap();
out.push({ whileChecking, afterExpired, checkingAgain, afterUnknown, checkThenList });
""")
        case = res[0]
        self.assertEqual(case["whileChecking"]["title"], "正在检查登录状态",
                         "检查没结束时不得提前宣布「登录是有效的」")
        self.assertIn("重新登录", case["afterExpired"]["title"],
                      "后到的 expired 必须自己刷新空态，不靠手动 render")
        self.assertEqual(case["checkingAgain"]["title"], "正在检查登录状态")
        self.assertEqual(case["afterUnknown"]["title"], "暂时无法确认登录状态")
        self.assertNotIn("过期", case["afterUnknown"]["text"], "unknown 不说过期")
        self.assertEqual(case["checkThenList"]["title"], "学校当前没有返回活动",
                         "check 先到、list 后到也要收敛到真实状态")

    def test_empty_action_dispatches_by_intent(self):
        """BC-05：「重试」必须真的重发请求；只有筛选空态才清筛选。"""
        res = run_js(r"""
const item = { id: "a1", name: "活动A", sign_start: "2099-01-01 09:00:00",
  sign_end: "2099-01-02 09:00:00", act_start: "2099-01-03 09:00:00",
  act_end: "2099-01-03 11:00:00", signed: false, state: "未开放" };
S.items = [normalize(item)];
S.byId = new Map([["a1", S.items[0]]]);
S.filter = { q: "zzz", status: "free", time: "today", sort: "name",
  checkin: "yes", checkout: "no" };
S.prefs = { retry_policy: { retry_for_s: 600, max_attempts: 3 } };
S.retryPolicy = { retry_for_s: 600, max_attempts: 3 };
savePrefs = () => {};
let checks = 0, lists = 0;
window.pywebview = { api: {
  check: () => { checks += 1; return Promise.reject(new Error("桥接断了")); },
  list_activities: () => { lists += 1; return Promise.reject(new Error("桥接断了")); },
} };
/* 有缓存 + 列表请求失败：空态显示「重试」 */
S.listState = "error"; S.listError = "连不上学校站点";
render();
const errorState = { action: document.querySelector("#empty-action").textContent,
  intent: document.querySelector("#empty-action").dataset.action };
document.querySelector("#empty-action").click();
await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
const afterRetry = { checks, lists, q: S.filter.q };
/* 回到正常筛选空态：这里才该清筛选，且不再发请求 */
S.listState = "ready";
render();
const filterState = { action: document.querySelector("#empty-action").textContent,
  intent: document.querySelector("#empty-action").dataset.action };
document.querySelector("#empty-action").click();
out.push({ errorState, afterRetry, filterState,
  afterClear: { checks, lists, q: S.filter.q, sort: S.filter.sort },
  retry: S.retryPolicy.retry_for_s, max: S.retryPolicy.max_attempts });
""")
        case = res[0]
        self.assertEqual(case["errorState"]["action"], "重试")
        self.assertEqual(case["errorState"]["intent"], "refresh",
                         "失败空态的动作意图必须是刷新，不是清筛选")
        self.assertEqual(case["afterRetry"]["checks"], 1, "「重试」必须真的再查登录态")
        self.assertEqual(case["afterRetry"]["lists"], 1, "「重试」必须真的再拉列表")
        self.assertEqual(case["afterRetry"]["q"], "zzz",
                         "点重试不得顺手清筛选把错误藏掉")
        self.assertEqual(case["filterState"]["intent"], "clear-filters")
        self.assertEqual(case["afterClear"]["q"], "", "筛选空态才清筛选")
        self.assertEqual(case["afterClear"]["sort"], "open")
        self.assertEqual(case["afterClear"]["checks"], 1, "清筛选不得发请求")
        self.assertEqual(case["afterClear"]["lists"], 1)
        self.assertEqual(case["retry"], 600, "清筛选仍不得动策略")
        self.assertEqual(case["max"], 3)


class AssistanceBehaviorTests(unittest.TestCase):
    """说明/更多/重播：文案同源、全状态可开、焦点归还、外链与目录一次一点击。"""

    # 已确认的文案基线：两端一起漂移时，这一份仍然能挡住
    _EXPECTED_COPY = {
        "title": "使用帮助",
        "subtitle": "使用说明、版本更新与本机数据",
        "note": "适用于天津市大学软件学院（天软）专升本学生使用的活动中心。"
                "活动要求与报名结果以学校活动页为准。",
        "steps": [
            (1, "登录账号",
             "选择「登录」，在打开的浏览器中完成登录。下次启动会检查已保存的登录状态；"
             "只有提示失效时才需要重新登录。"),
            (2, "设置报名计划",
             "选择活动，设置尝试时长和最多提交次数，再选择「开始报名计划」。"
             "预报名会在开放后尝试提交，已开放活动会在计划启动后尝试提交。"),
            (3, "查看报名结果",
             "运行期间可最小化窗口，但需保持电脑唤醒与联网。结束后查看各活动的报名结果；"
             "开始计划不代表报名成功。"),
        ],
    }

    def test_backend_help_payload_matches_default_help(self):
        """Api.help() 的真实载荷与 DEFAULT_HELP 逐字段一致（结构 + 顺序 + 文案）。

        后端：ast 只编译 Api.help，注入版本/地址常量后调用 help(None)；
        前端：node 里把生产 DEFAULT_HELP 取出来。两边深比较，不用源码子串。
        """
        backend = load_backend_help()
        frontend = run_js("out.push(JSON.parse(JSON.stringify(DEFAULT_HELP)));")[0]
        expected = self._EXPECTED_COPY

        # 后端载荷结构：字段齐全、步骤编号有序、版本/外链来自注入常量
        self.assertEqual(sorted(backend),
                         ["latest_url", "note", "steps", "subtitle", "title", "version"],
                         "help 载荷字段发生变化")
        self.assertEqual(backend["version"], HELP_TEST_VERSION,
                         "版本必须来自 APP_VERSION，不能写死")
        self.assertEqual(backend["latest_url"], HELP_TEST_URL,
                         "发布页地址必须来自 RELEASES_LATEST_URL")
        self.assertEqual([step["no"] for step in backend["steps"]], [1, 2, 3],
                         "三步顺序必须是 1/2/3")
        for step in backend["steps"]:
            self.assertEqual(sorted(step), ["no", "text", "title"],
                             "步骤字段结构变化")

        # 前端对象不带第二套版本来源
        self.assertEqual(sorted(frontend), ["note", "steps", "subtitle", "title"],
                         "DEFAULT_HELP 不得自带版本/地址字段")

        # 逐字段深比较（含中文全角标点）
        self.assertEqual(frontend["title"], backend["title"])
        self.assertEqual(frontend["subtitle"], backend["subtitle"])
        self.assertEqual(frontend["note"], backend["note"])
        self.assertEqual(len(frontend["steps"]), len(backend["steps"]))
        for got, want in zip(frontend["steps"], backend["steps"]):
            self.assertEqual((got["no"], got["title"], got["text"]),
                             (want["no"], want["title"], want["text"]),
                             "第 %s 步两处必须一字不差" % want["no"])

        # 与已确认文案独立比对：防止前后端一起漂移而深比较仍然通过
        self.assertEqual(backend["title"], expected["title"])
        self.assertEqual(backend["subtitle"], expected["subtitle"])
        self.assertEqual(backend["note"], expected["note"])
        self.assertEqual([(step["no"], step["title"], step["text"])
                          for step in backend["steps"]], expected["steps"])

    def test_more_opens_in_every_state(self):
        """未登录 / 组件报错 / 计划运行中都开得出来；说明与更多不叠模态。"""
        res = run_js(r"""
function openCase(prepare) {
  prepare();
  const dlg = document.querySelector("#help");
  if (dlg.open) dlg.close();
  document.querySelector("#btn-help").click();
  const opened = dlg.open;
  dlg.close();
  return opened;
}
const fresh = openCase(() => { S.busy = false; S.runtime.error = null; });
const errored = openCase(() => { S.runtime.error = "worker_missing"; });
const busy = openCase(() => { S.busy = true; });
S.busy = false;
document.querySelector("#btn-help").click();
const helpOpen = document.querySelector("#help").open;
document.querySelector("#btn-help").click();
const stacked = { more: document.querySelector("#help").open,
  tour: S.tour.open };
out.push({ fresh, errored, busy, helpOpen, stacked });
""")
        case = res[0]
        self.assertTrue(case["fresh"], "常态必须能打开更多")
        self.assertTrue(case["errored"], "组件报错时更多仍要能打开")
        self.assertTrue(case["busy"], "计划运行中也要能打开更多")
        self.assertTrue(case["helpOpen"])
        self.assertTrue(case["stacked"]["more"])
        self.assertFalse(case["stacked"]["tour"], "重复打开帮助不能自行启动引导")

    def test_both_replay_entries_close_dialog_and_return_focus(self):
        """两处重播都先关弹窗、从第 1 步开始，退出后焦点回到各自顶栏入口。"""
        res = run_js(r"""
const more = document.querySelector("#help");
const help = document.querySelector("#help");
document.querySelector("#btn-help").click();
document.querySelector("#btn-help-tour").click();
const fromMore = { closed: !more.open, tour: S.tour.open, step: S.tour.step,
  opener: S.tour.restoreFocus && S.tour.restoreFocus.__sel };
tourSkip();
const afterMore = focused.slice(-1)[0];
document.querySelector("#btn-help").click();
document.querySelector("#btn-help-tour").click();
const fromHelp = { closed: !help.open, tour: S.tour.open, step: S.tour.step,
  opener: S.tour.restoreFocus && S.tour.restoreFocus.__sel };
tourSkip();
const afterHelp = focused.slice(-1)[0];
out.push({ fromMore, afterMore, fromHelp, afterHelp,
  otherStillOpen: more.open, helpStillOpen: help.open });
""")
        case = res[0]
        self.assertTrue(case["fromMore"]["closed"], "重播前必须先关掉更多")
        self.assertTrue(case["fromMore"]["tour"])
        self.assertEqual(case["fromMore"]["step"], 0)
        self.assertEqual(case["fromMore"]["opener"], "#btn-help",
                         "开始前的焦点必须是「更多」入口")
        self.assertEqual(case["afterMore"], "#btn-help", "退出引导把焦点还回更多入口")
        self.assertTrue(case["fromHelp"]["closed"], "重播前必须先关掉说明")
        self.assertEqual(case["fromHelp"]["opener"], "#btn-help")
        self.assertEqual(case["afterHelp"], "#btn-help")
        self.assertFalse(case["otherStillOpen"])
        self.assertFalse(case["helpStillOpen"])

    def test_busy_disables_replay_but_keeps_version_visible(self):
        """运行中两处重播都禁用并写明原因；版本仍可查，点击也不进引导。"""
        res = run_js(r"""
document.querySelector("#help-version").textContent = "9.9.9";
setBusy(true);
document.querySelector("#btn-help").click();
const opened = document.querySelector("#help").open;
const tourBtn = document.querySelector("#btn-help-tour");
const helpBtn = document.querySelector("#btn-help-tour");
const disabled = { tour: tourBtn.disabled, help: helpBtn.disabled };
const label = tourBtn.textContent;
/* 桩不按 disabled 拦截点击：这里同时验证代码自己的忙判定 */
document.querySelector("#btn-help-tour").click();
const noTour = !S.tour.open;
const version = document.querySelector("#help-version").textContent;
setBusy(false);
out.push({ opened, disabled, label, noTour, version,
  restored: { disabled: tourBtn.disabled, label: tourBtn.textContent } });
""")
        case = res[0]
        self.assertTrue(case["opened"], "运行中也要能打开更多查版本")
        self.assertTrue(case["disabled"]["tour"], "运行中「更多」里的重播必须禁用")
        self.assertTrue(case["disabled"]["help"], "运行中说明里的重播也必须禁用")
        self.assertEqual(case["label"], "计划结束后可重看引导",
                         "禁用原因必须直接写在按钮上，不只靠 title")
        self.assertTrue(case["noTour"], "禁用期间点击不得进引导")
        self.assertEqual(case["version"], "9.9.9", "版本仍必须可见")
        self.assertFalse(case["restored"]["disabled"], "计划结束后重播恢复可用")
        self.assertEqual(case["restored"]["label"], "重看引导")

    def test_external_actions_fire_once_and_not_on_open(self):
        """打开弹窗不联网、不开目录；每次点击恰调用一次既有接口。"""
        res = run_js(r"""
let latest = 0, data = 0;
window.pywebview = { api: {
  open_latest_release: () => { latest += 1; return Promise.resolve({ ok: true }); },
  open_data: () => { data += 1; return Promise.resolve({ ok: true }); },
} };
document.querySelector("#btn-help").click();
const atOpen = { latest, data };
document.querySelector("#btn-help-latest").click();
document.querySelector("#btn-help-data").click();
await Promise.resolve();
out.push({ atOpen, latest, data,
  statusHidden: document.querySelector("#help-status").hidden });
""")
        case = res[0]
        self.assertEqual(case["atOpen"], {"latest": 0, "data": 0},
                         "打开弹窗不得自动联网或打开目录")
        self.assertEqual(case["latest"], 1, "外链每次点击恰调用一次")
        self.assertEqual(case["data"], 1, "数据目录每次点击恰调用一次")
        self.assertTrue(case["statusHidden"], "成功时不显示局部错误")

    def test_failures_show_local_status_with_fixed_url(self):
        """失败只在这个弹窗里说下一步；发布页失败要给出可复制的固定地址。"""
        res = run_js(r"""
window.pywebview = { api: {
  open_latest_release: () => Promise.resolve({ ok: false }),
  open_data: () => Promise.reject(new Error("没有权限")),
} };
document.querySelector("#btn-help").click();
document.querySelector("#btn-help-latest").click();
await Promise.resolve(); await Promise.resolve();
const latest = { hidden: document.querySelector("#help-status").hidden,
  text: document.querySelector("#help-status").textContent,
  tone: document.querySelector("#help-status").dataset.tone };
document.querySelector("#btn-help-data").click();
await Promise.resolve(); await Promise.resolve();
const data = { hidden: document.querySelector("#help-status").hidden,
  text: document.querySelector("#help-status").textContent,
  tone: document.querySelector("#help-status").dataset.tone };
out.push({ latest, data, url: RELEASES_LATEST_URL });
""")
        case = res[0]
        self.assertFalse(case["latest"]["hidden"], "失败必须在弹窗里可见")
        self.assertIn("发布页没能打开", case["latest"]["text"])
        self.assertIn(case["url"], case["latest"]["text"], "要把固定地址摆出来供复制")
        self.assertEqual(case["latest"]["tone"], "warn")
        self.assertFalse(case["data"]["hidden"])
        self.assertIn("打不开数据目录", case["data"]["text"])
        self.assertIn("没有权限", case["data"]["text"])
        self.assertEqual(case["data"]["tone"], "bad")

    def test_open_data_success_writes_real_path_into_help_text(self):
        """成功打开数据目录：真实路径进正文说明行，错误位保持隐藏。"""
        res = run_js(r"""
const realPath = "C:\\Users\\demo\\AppData\\Local\\TianSoftActivityPreRegistration\\data";
window.pywebview = { api: {
  open_data: () => Promise.resolve({ ok: true, path: realPath }),
} };
document.querySelector("#btn-help").click();
document.querySelector("#btn-help-data").click();
await Promise.resolve(); await Promise.resolve();
const line = document.querySelector("#help-data-path");
const status = document.querySelector("#help-status");
out.push({ lineText: line.textContent, statusHidden: status.hidden,
  statusText: status.textContent });
""")
        case = res[0]
        self.assertIn("C:\\Users\\demo", case["lineText"],
                      "用户最常问的就是数据在哪，真实路径必须写在正文里")
        self.assertTrue(case["statusHidden"], "成功不是错误，不得点亮错误位")
        self.assertEqual(case["statusText"], "", "错误位不得混入成功路径")

    def test_open_data_failure_keeps_help_text_and_shows_reason(self):
        """失败时正文说明行不被清空或污染，错误只进错误位。"""
        res = run_js(r"""
window.pywebview = { api: {
  open_data: () => Promise.reject(new Error("目录被占用")),
} };
document.querySelector("#btn-help").click();
const line = document.querySelector("#help-data-path");
const before = line.textContent;
document.querySelector("#btn-help-data").click();
await Promise.resolve(); await Promise.resolve();
const status = document.querySelector("#help-status");
out.push({ before, after: line.textContent, statusHidden: status.hidden,
  statusText: status.textContent });
""")
        case = res[0]
        self.assertEqual(case["after"], case["before"],
                         "失败不改写正文说明，重试语义由错误位承载")
        self.assertNotIn("目录被占用", case["after"], "错误原因不得污染正文行")
        self.assertFalse(case["statusHidden"])
        self.assertIn("打不开数据目录", case["statusText"])
        self.assertIn("目录被占用", case["statusText"])


class OpenDataContractTests(unittest.TestCase):
    """后端 open_data 契约：只回 ok + 目录路径，绝不回传登录文件内容。"""

    def test_api_open_data_returns_only_ok_and_path(self):
        """用 ast 隔离求值 Api.open_data：返回字典只允许 ok/path 两个键。"""
        import ast
        import os
        import tempfile

        tree = ast.parse(read("app.py"))
        node = next(item for item in ast.walk(tree)
                    if isinstance(item, ast.FunctionDef) and item.name == "open_data"
                    and [arg.arg for arg in item.args.args][:1] == ["self"])
        module = ast.Module(body=[node], type_ignores=[])
        ast.fix_missing_locations(module)
        with tempfile.TemporaryDirectory() as name:
            # os.startfile 只在真打开目录时有副作用；用假实现替掉，断言不执行
            namespace = {"os": type("FakeOS", (), {"startfile": staticmethod(
                lambda p: launched.append(p))})(),
                         "ensure_data_dir": lambda: Path(name)}
            launched = []
            exec(compile(module, "<Api.open_data>", "exec"), namespace)  # noqa: S102 - 只编译受控片段
            result = namespace["open_data"](None)
        self.assertTrue(result.get("ok"))
        self.assertEqual(sorted(result.keys()), ["ok", "path"],
                         "回传键固定为 ok/path：多一个字段就是把不该给的塞给界面")
        self.assertEqual(result["path"], str(Path(name)))
        self.assertEqual(launched, [str(Path(name))],
                         "打开目录的副作用必须仍然发生（os.startfile 恰一次）")

    def test_success_handler_does_not_touch_login_files(self):
        """前端成功分支只引用 res.path；登录文件名不得出现在处理块里。"""
        js = read("ui/app.js")
        start = js.index('$("#btn-help-data").addEventListener')
        end = js.index("window.addEventListener(\"resize\"", start)
        block = js[start:end]
        for banned in ("session.json", "storage_state", "token"):
            self.assertNotIn(banned, block,
                             "打开数据目录的成功反馈不得引用登录文件：%s" % banned)
        self.assertIn("help-data-path", block, "成功路径必须写回正文说明行")
        then_branch = block[block.index(".then"):block.index(".catch")]
        self.assertNotIn("helpStatus", then_branch,
                         "成功反馈不得写错误位（进入 then 前的 helpStatus("") 只是清场复位）")


class ManualUpdateUiBehaviorTests(unittest.TestCase):
    """更新通道独立于报名，覆盖请求时序、失败收尾与页面恢复。"""

    def test_late_snapshot_does_not_replace_newer_check(self):
        """接管旧请求后的快照可能晚到，不能覆盖用户随后新开的查询。"""
        res = run_js(r"""
let finishSnapshot, first = true;
window.pywebview = { api: {
  check_update: id => {
    if (first) { first = false; return Promise.resolve({ok:true,queued:true,skipped:true,request_id:"old"}); }
    return Promise.resolve({ok:true,queued:true,request_id:id});
  },
  bootstrap: () => new Promise(resolve => { finishSnapshot = resolve; }),
} };
checkUpdate();
await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
window.onBackend.update({request_id:"old",state:"current"});
checkUpdate();
const fresh = S.update.requestId;
finishSnapshot({update_check:{checking:false,request_id:"old",result:{request_id:"old",state:"current"}}});
await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
const pending = {preserved:S.update.requestId===fresh,state:S.update.state};
window.onBackend.update({request_id:fresh,state:"available",latest_version:"1.2.0"});
out.push({pending,state:S.update.state});
""")
        self.assertEqual(res[0], {"pending": {"preserved": True, "state": "checking"},
                                  "state": "available"})

    def test_result_timestamp_is_not_a_live_check(self):
        res = run_js(r"""
S.update.requestId = "request";
window.onBackend.update({request_id:"request",state:"current",checked_at:"2026-10-25T15:00:00Z"});
const known = {hidden:document.querySelector("#update-checked-at").hidden,
  text:document.querySelector("#update-checked-at").textContent};
window.onBackend.update({request_id:"request",state:"error",checked_at:"invalid"});
out.push({known,unknownHidden:document.querySelector("#update-checked-at").hidden});
""")
        self.assertFalse(res[0]["known"]["hidden"])
        self.assertIn("检查时间", res[0]["known"]["text"])
        self.assertTrue(res[0]["unknownHidden"])

    def test_open_help_never_queries_update(self):
        res = run_js(r"""
let calls = 0;
window.pywebview = { api: { check_update: () => { calls += 1; } } };
document.querySelector("#btn-help").click();
document.querySelector("#btn-help").click();
out.push({ calls, open: document.querySelector("#help").open,
  state: document.querySelector("#update-status").textContent });
""")
        self.assertEqual(res[0], {"calls": 0, "open": True, "state": "尚未检查"})

    def test_double_click_is_single_flight(self):
        res = run_js(r"""
let calls = 0;
window.pywebview = { api: { check_update: id => { calls += 1; return Promise.resolve({ok:true,queued:true,request_id:id}); } } };
document.querySelector("#btn-update-check").click();
document.querySelector("#btn-update-check").click();
out.push({ calls, disabled: document.querySelector("#btn-update-check").disabled });
""")
        self.assertEqual(res[0], {"calls": 1, "disabled": True})

    def test_result_before_ack_does_not_reenter_checking(self):
        res = run_js(r"""
window.pywebview = { api: { check_update: id => {
  window.onBackend.update({request_id:id,state:"current"});
  return Promise.resolve({ok:true,queued:true,request_id:id});
} } };
document.querySelector("#btn-update-check").click();
await Promise.resolve(); await Promise.resolve();
out.push({state:S.update.state,disabled:document.querySelector("#btn-update-check").disabled});
""")
        self.assertEqual(res[0], {"state": "current", "disabled": False})

    def test_stale_event_cannot_replace_current_request(self):
        res = run_js(r"""
window.pywebview = { api: { check_update: id => Promise.resolve({ok:true,queued:true,request_id:id}) } };
checkUpdate();
const current = S.update.requestId;
window.onBackend.update({request_id:"old",state:"available",latest_version:"9.9.9"});
const stale = S.update.state;
window.onBackend.update({request_id:current,state:"preview",latest_version:"1.0.2"});
out.push({stale,state:S.update.state,text:document.querySelector("#update-status").textContent});
""")
        self.assertEqual(res[0]["stale"], "checking")
        self.assertEqual(res[0]["state"], "preview")
        self.assertIn("1.0.2", res[0]["text"])

    def test_all_terminal_states_restore_button(self):
        res = run_js(r"""
for (const state of ["available","current","preview","unavailable","error"]) {
  S.update.requestId = "request"; S.update.state = "checking";
  window.onBackend.update({request_id:"request",state,latest_version:"1.2.0",text:"连接超时"});
  out.push({state,disabled:document.querySelector("#btn-update-check").disabled,
    text:document.querySelector("#update-status").textContent});
}
""")
        self.assertTrue(all(not item["disabled"] for item in res))
        self.assertIn("发现新版本", res[0]["text"])
        self.assertIn("最新正式版", res[1]["text"])
        self.assertIn("预览", res[2]["text"])
        self.assertEqual(res[3]["text"], "暂无正式发布")
        self.assertIn("暂时无法检查", res[4]["text"])

    def test_bridge_reject_is_local_and_recoverable(self):
        res = run_js(r"""
S.runtime.error = "worker_missing"; S.busy = true;
const oldResult = document.querySelector("#result").textContent;
window.pywebview = { api: { check_update: () => Promise.reject(new Error("桥接失败")) } };
checkUpdate();
await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
out.push({state:S.update.state,runtime:S.runtime.error,busy:S.busy,
  result:document.querySelector("#result").textContent===oldResult,
  disabled:document.querySelector("#btn-update-check").disabled});
""")
        self.assertEqual(res[0], {"state": "error", "runtime": "worker_missing", "busy": True,
                                  "result": True, "disabled": False})

    def test_bootstrap_restores_without_network_query(self):
        res = run_js(r"""
let calls = 0;
window.pywebview = { api: { check_update: () => { calls += 1; } } };
restoreUpdate({checking:true,request_id:"inflight",result:null});
const pending = S.update.state;
restoreUpdate({checking:false,request_id:"inflight",result:{request_id:"inflight",state:"current"}});
out.push({calls,pending,state:S.update.state});
""")
        self.assertEqual(res[0], {"calls": 0, "pending": "checking", "state": "current"})

    def test_lost_event_has_finite_wait(self):
        res = run_js(r"""
const original = setTimeout; let timeout;
setTimeout = fn => { timeout = fn; return 1; };
window.pywebview = { api: { check_update: id => Promise.resolve({ok:true,queued:true,request_id:id}) } };
checkUpdate(); timeout(); setTimeout = original;
out.push({state:S.update.state,disabled:document.querySelector("#btn-update-check").disabled});
""")
        self.assertEqual(res[0], {"state": "error", "disabled": False})


class AttendanceDetailUiTests(unittest.TestCase):
    """真实详情字段：时间段、无需、同步中、待确认四种展示与三态筛选。"""

    def _row(self, **extra):
        row = {"id": "a1", "name": "活动", "sign_start": "2026-10-26 09:00:00",
               "sign_end": "2026-10-26 23:00:00", "act_start": "2026-10-27 19:00:00",
               "act_end": "2026-10-27 21:00:00", "signed": False}
        row.update(extra)
        return row

    def test_required_checkin_shows_real_time_range(self):
        res = run_js(r"""
window.onBackend.activities({ ok: true, items: [{
  id: "a1", name: "活动", sign_start: "2026-10-26 09:00:00",
  sign_end: "2026-10-26 23:00:00", act_start: "2026-10-27 19:00:00",
  act_end: "2026-10-27 21:00:00", signed: false,
  checkin_required: true, checkin_start: "2026-10-27 19:00:00",
  checkin_end: "2026-10-27 19:40:00", checkout_required: false,
  checkout_start: "", checkout_end: "", attendance_state: "ready" }] });
out.push({ html: rowHtml(findItem("a1")) });
""")
        html = res[0]["html"]
        self.assertIn("签到 19:00–19:40", html)
        self.assertIn("无需签退", html)

    def test_pending_detail_is_not_unknown_no_need(self):
        res = run_js(r"""
window.onBackend.activities({ ok: true, items: [{
  id: "a1", name: "活动", sign_start: "2026-10-26 09:00:00",
  sign_end: "2026-10-26 23:00:00", act_start: "2026-10-27 19:00:00",
  act_end: "2026-10-27 21:00:00", signed: false,
  checkin_required: null, checkout_required: null, attendance_state: "pending" }] });
const pending = rowHtml(findItem("a1"));
window.onBackend.activities({ ok: true, items: [{
  id: "a1", name: "活动", sign_start: "2026-10-26 09:00:00",
  sign_end: "2026-10-26 23:00:00", act_start: "2026-10-27 19:00:00",
  act_end: "2026-10-27 21:00:00", signed: false,
  checkin_required: null, checkout_required: null, attendance_state: "unknown" }] });
const unknown = rowHtml(findItem("a1"));
out.push({ pending: pending, unknown: unknown });
""")
        self.assertIn("签到信息同步中", res[0]["pending"])
        self.assertNotIn("无需", res[0]["pending"])
        self.assertIn("签到时间待确认", res[0]["unknown"])

    def test_precise_filters_exclude_unknown_and_all_keeps_it(self):
        res = run_js(r"""
const rows = [
  { id: "yes", name: "需要签到", sign_start: "2026-10-26 09:00:00",
    sign_end: "2026-10-26 23:00:00", act_start: "2026-10-27 19:00:00",
    act_end: "2026-10-27 21:00:00", signed: false, checkin_required: true,
    checkout_required: true, attendance_state: "ready" },
  { id: "no", name: "无需签到", sign_start: "2026-10-26 09:00:00",
    sign_end: "2026-10-26 23:00:00", act_start: "2026-10-27 19:00:00",
    act_end: "2026-10-27 21:00:00", signed: false, checkin_required: false,
    checkout_required: false, attendance_state: "ready" },
  { id: "unknown", name: "待确认", sign_start: "2026-10-26 09:00:00",
    sign_end: "2026-10-26 23:00:00", act_start: "2026-10-27 19:00:00",
    act_end: "2026-10-27 21:00:00", signed: false, checkin_required: null,
    checkout_required: null, attendance_state: "pending" }
];
S.items = rows.map(normalize);
S.byId = new Map(S.items.map((item) => [item.id, item]));
const ids = (f) => { S.filter.checkin = f; return filtered().map((item) => item.id); };
out.push({ yes: ids("yes"), no: ids("no"), all: ids("all") });
""")
        data = res[0]
        self.assertEqual(data["yes"], ["yes"])
        self.assertEqual(data["no"], ["no"])
        self.assertEqual(sorted(data["all"]), ["no", "unknown", "yes"])


class AriaAndPlaceTests(unittest.TestCase):
    """D-10/D-11：chips 用 aria-pressed；地点可聚焦、Enter/Space 展开且不改选择。"""

    def test_chips_use_aria_pressed_not_selected(self):
        html = (LOCAL / "ui" / "index.html").read_text(encoding="utf-8")
        app = read("ui/app.js")   # 前端脚本联合源码（chips 渲染已随分层拆分）
        self.assertIn('aria-pressed="true"', html)
        self.assertNotIn('aria-selected="false">可报名', html,
                         "button 上的 aria-selected 非法，必须用 aria-pressed")
        self.assertIn('chip.setAttribute("aria-pressed"', app)
        self.assertNotIn('chip.setAttribute("aria-selected"', app)

    def test_place_has_role_and_expanded_state(self):
        res = run_js(r"""
window.onBackend.activities({ ok: true, items: [{
  id: "a1", name: "活动", place: "实训楼 C305 三楼最里面那间",
  sign_start: "2026-10-26 09:00:00", sign_end: "2026-10-26 23:00:00",
  act_start: "2026-10-27 09:00:00", act_end: "2026-10-27 11:00:00",
  signed: false }] });
out.push({ html: rowHtml(findItem("a1")) });
""")
        html = res[0]["html"]
        self.assertIn('role="button"', html)
        self.assertIn('aria-expanded="false"', html)
        self.assertIn('tabindex="0"', html)

    def test_enter_expands_place_without_selection(self):
        res = run_js(r"""
const place = document.createElement("div");
const handler = document.querySelector("#rows").listeners.keydown[0];
const event = { target: { closest: (sel) => (sel === ".c-place" ? place : null) },
                key: "Enter", defaultPrevented: false,
                preventDefault() { this.defaultPrevented = true; } };
handler(event);
const opened = place.getAttribute("aria-expanded");
handler(event);
const closed = place.getAttribute("aria-expanded");
const keyA = { target: { closest: (sel) => (sel === ".c-place" ? place : null) },
               key: "a", preventDefault() { this.defaultPrevented = true; } };
handler(keyA);
out.push({ opened: opened, closed: closed, prevented: event.defaultPrevented,
           selected: S.selected.size, afterA: place.getAttribute("aria-expanded") });
""")
        data = res[0]
        self.assertEqual(data["opened"], "true")
        self.assertEqual(data["closed"], "false")
        self.assertTrue(data["prevented"], "Enter 必须阻止默认滚动/提交")
        self.assertEqual(data["selected"], 0, "展开地点不得勾选活动")
        self.assertEqual(data["afterA"], "false", "其他按键不改变展开状态")


class TermsConsentContractTests(unittest.TestCase):
    """决策 013 / 条款同意门：后端快照、ui.json 落盘与版本同源契约。"""

    # 版本形如 2026-10-27 或 2026-10-27-r2：修订版必须能被解析，否则
    # 「已同意版本 != 当前版本」的判定会被旧同意记录静默绕过
    DOCS_VERSION_RE = r"版本：\*\*(\d{4}-\d{2}-\d{2}(?:-r\d+)?)\*\*"

    def _docs_terms_version(self) -> str:
        text = (LOCAL.parent / "docs" / "TERMS_AND_PRIVACY.md").read_text(encoding="utf-8")
        match = re.search(self.DOCS_VERSION_RE, text)
        self.assertIsNotNone(match, "docs/TERMS_AND_PRIVACY.md 必须带 版本：**YYYY-MM-DD** 字段")
        return match.group(1)

    def _app_terms_version(self) -> str:
        import ast

        tree = ast.parse(read("app.py"))
        node = next(item for item in tree.body
                    if isinstance(item, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "TERMS_VERSION"
                            for target in item.targets))
        return ast.literal_eval(node.value)

    def test_terms_version_matches_docs_file(self):
        """程序内条款版本与 docs/TERMS_AND_PRIVACY.md 是同一份（决策 013）。"""
        self.assertEqual(self._app_terms_version(), self._docs_terms_version(),
                         "TERMS_VERSION 与条款文件版本字段漂移了；两处必须一起改")

    def test_terms_state_reads_only_declared_prefs_keys(self):
        """terms_state 只读 ui.json 的 terms_version / terms_accepted_at。"""
        import ast

        tree = ast.parse(read("app.py"))
        node = next(item for item in ast.walk(tree)
                    if isinstance(item, ast.FunctionDef) and item.name == "terms_state"
                    and [arg.arg for arg in item.args.args][:1] == ["prefs"])
        module = ast.Module(body=[node], type_ignores=[])
        ast.fix_missing_locations(module)
        namespace = {"TERMS_VERSION": "9.9.9-test"}
        exec(compile(module, "<terms_state>", "exec"), namespace)  # noqa: S102
        terms_state = namespace["terms_state"]

        self.assertEqual(terms_state({}),
                         {"version": "9.9.9-test", "accepted_version": "", "accepted_at": ""})
        self.assertEqual(
            terms_state({"terms_version": "1.0.0", "terms_accepted_at": "2026-10-27 09:00:00"}),
            {"version": "9.9.9-test", "accepted_version": "1.0.0",
             "accepted_at": "2026-10-27 09:00:00"})
        self.assertEqual(terms_state(None)["accepted_version"], "", "None 偏好必须回退为未同意")

        # 只碰声明的两个键：不读 session/storage，也不写日志
        source = ast.unparse(node)
        self.assertIn("terms_version", source)
        self.assertIn("terms_accepted_at", source)
        for banned in ("SESSION_FILE", "STORAGE_STATE", "storage_state", "grab.log", "log"):
            self.assertNotIn(banned, source, "terms_state 不得触碰登录文件或日志")

    def test_bootstrap_carries_terms_snapshot(self):
        """bootstrap 载荷必须带 terms 快照（terms_state(prefs)），界面据此判定门禁。"""
        import ast

        tree = ast.parse(read("app.py"))
        node = next(item for item in ast.walk(tree)
                    if isinstance(item, ast.FunctionDef) and item.name == "bootstrap"
                    and [arg.arg for arg in item.args.args][:1] == ["self"])
        source = ast.unparse(node)
        self.assertIn("'terms': terms_state(prefs)", source,
                      "bootstrap 必须把 terms_state(prefs) 放进载荷")

    def test_accept_terms_writes_prefs_transaction_only(self):
        """accept_terms：经 update_prefs 原子事务写两个键；不碰登录文件与日志。"""
        import ast

        tree = ast.parse(read("app.py"))
        node = next(item for item in ast.walk(tree)
                    if isinstance(item, ast.FunctionDef) and item.name == "accept_terms"
                    and [arg.arg for arg in item.args.args][:1] == ["self"])
        # accept_terms 的返回载荷复用 terms_state：一起编译，保持与真实实现同源
        state_node = next(item for item in ast.walk(tree)
                          if isinstance(item, ast.FunctionDef) and item.name == "terms_state"
                          and [arg.arg for arg in item.args.args][:1] == ["prefs"])
        module = ast.Module(body=[node, state_node], type_ignores=[])
        ast.fix_missing_locations(module)

        source = ast.unparse(node)
        self.assertIn("update_prefs", source, "必须走 ui.json 原子偏好事务")
        for banned in ("SESSION_FILE", "STORAGE_STATE", "storage_state",
                       "read_text", "_trace", "addLog"):
            self.assertNotIn(banned, source, "同意状态不得读写登录文件或写日志")

        stamp = "2026-10-27 12:00:00"

        class FakeTime:
            @staticmethod
            def strftime(fmt, value):
                return stamp

            @staticmethod
            def localtime():
                return ()

        written = []

        def fake_update(patch):
            written.append(dict(patch))
            return True

        namespace = {"time": FakeTime, "TERMS_VERSION": "9.9.9-test",
                     "update_prefs": fake_update,
                     "load_prefs": lambda: {"terms_version": "9.9.9-test",
                                            "terms_accepted_at": stamp}}
        exec(compile(module, "<accept_terms>", "exec"), namespace)  # noqa: S102
        accept_terms = namespace["accept_terms"]

        # AST 抽出的真实签名是 accept_terms(self, document_version="")：首参
        # 会绑定 self，document_version 必须用关键字（或第二实参）传入；单传
        # 一个字符串会把版本绑进 self、让 document_version 退回默认空串，
        # 在写前被版本校验拒绝——那测不到成功事务本身。
        got = accept_terms(None, document_version="9.9.9-test")
        self.assertEqual(got["ok"], True)
        self.assertEqual(got["terms"], {"version": "9.9.9-test",
                                        "accepted_version": "9.9.9-test",
                                        "accepted_at": stamp})
        self.assertEqual(written, [{"terms_version": "9.9.9-test",
                                    "terms_accepted_at": stamp}])

        # 拿不到跨进程锁：如实失败，不落盘也不谎报成功。
        # 「尝试过写入」与「真正持久化」必须用两个容器区分：busy_attempts 记录
        # 每次尝试的 patch（期望恰为正确两键一次），written 只记真正落盘的键
        # （忙锁分支必须仍为空）——不能用一个从未被写入的列表冒充"未持久化"。
        written.clear()  # 先清空成功持久化容器：本分支只观察忙锁轮次
        busy_attempts = []

        def busy_update(patch):
            busy_attempts.append(dict(patch))
            return False

        namespace["update_prefs"] = busy_update
        busy = accept_terms(None, document_version="9.9.9-test")
        self.assertEqual(busy["ok"], False)
        self.assertEqual(busy["error"], "prefs_busy")
        self.assertEqual(busy_attempts,
                         [{"terms_version": "9.9.9-test",
                           "terms_accepted_at": stamp}],
                         "忙锁分支仍必须恰好尝试一次正确的两键")
        self.assertEqual(written, [], "成功持久化容器在忙锁分支保持为空")

    def test_accept_terms_rejects_mismatched_version_before_write(self):
        """版本不匹配（缺参/旧版/非法类型）必须在写前拒绝：零 update_prefs。"""
        import ast
        import time

        tree = ast.parse(read("app.py"))
        node = next(item for item in ast.walk(tree)
                    if isinstance(item, ast.FunctionDef) and item.name == "accept_terms"
                    and [arg.arg for arg in item.args.args][:1] == ["self"])
        source = ast.unparse(node)
        # 判定必须先于任何写入与时间戳生成（顺序即证据）
        self.assertLess(source.index("document_version == TERMS_VERSION"),
                        source.index("update_prefs"),
                        "版本判定必须位于原子写之前")
        self.assertLess(source.index("document_version == TERMS_VERSION"),
                        source.index("strftime"),
                        "版本判定必须位于时间戳生成之前")

        state_node = next(item for item in ast.walk(tree)
                          if isinstance(item, ast.FunctionDef) and item.name == "terms_state"
                          and [arg.arg for arg in item.args.args][:1] == ["prefs"])
        module = ast.Module(body=[node, state_node], type_ignores=[])
        ast.fix_missing_locations(module)

        written = []
        store = {"terms_version": "9.9.9-test", "terms_accepted_at": "2026-10-27 12:00:00"}
        namespace = {"time": time, "TERMS_VERSION": "9.9.9-test",
                     "update_prefs": lambda patch: (written.append(dict(patch)), True)[1],
                     "load_prefs": lambda: dict(store)}
        exec(compile(module, "<accept_terms>", "exec"), namespace)  # noqa: S102
        accept_terms = namespace["accept_terms"]

        # 非法 document_version 全集逐项拒绝（subTest 便于定位到具体输入）；
        # self 传 None 只是给首参占位——被检对象始终是 document_version。
        for bad in ("", "9.9.8-old", "9.9.10-new", None, 999, {"v": "9.9.9-test"}):
            with self.subTest(document_version=bad):
                got = accept_terms(None, document_version=bad)
                self.assertEqual(got["ok"], False, "非法输入 %r 必须拒绝" % (bad,))
                self.assertEqual(got["error"], "terms_version_mismatch")
                self.assertIn("terms", got, "拒绝时可带只读 terms 快照供界面比对")
        # 缺 document_version 是独立场景：签名默认空串，不能拿空串冒充缺参表达
        got_missing = accept_terms(None)
        self.assertEqual(got_missing["ok"], False, "缺 document_version 必须拒绝")
        self.assertEqual(got_missing["error"], "terms_version_mismatch")
        self.assertIn("terms", got_missing)
        self.assertEqual(written, [], "任何被拒输入都不得触发 update_prefs")
        self.assertEqual(store, {"terms_version": "9.9.9-test",
                                 "terms_accepted_at": "2026-10-27 12:00:00"},
                         "被拒后内存偏好与原同意时间必须原样不变")

    def test_terms_copy_avoids_over_promises(self):
        """负向断言：不写 MIT 之外的禁令、不写「继续使用视为同意」、不夸通信范围。"""
        docs = (LOCAL.parent / "docs" / "TERMS_AND_PRIVACY.md").read_text(encoding="utf-8")
        html = (LOCAL / "ui" / "index.html").read_text(encoding="utf-8")
        for label, text in (("docs", docs), ("html", html)):
            self.assertNotIn("个人非商业使用", text,
                             "%s：不得用条款收窄 MIT 已授予的权利" % label)
            self.assertNotIn("禁止商业销售", text,
                             "%s：不得新增 LICENSE 之外的分发禁止" % label)
            self.assertNotIn("去除本条款后分发", text,
                             "%s：去除补充文档不该写成许可限制" % label)
            self.assertNotIn("继续使用视为接受新版本", text,
                             "%s：版本变化必须显式重新同意，不能默认延续" % label)
            self.assertNotIn("仅在你主动操作时向学校系统发起访问", text,
                             "%s：同意后有周期刷新，不能写成只在手动时通信" % label)
        # 责任限制仍需保留法律不可排除的部分
        self.assertIn("不适用于法律不允许排除的情形", docs)

    def test_terms_version_is_shared_by_docs_html_and_backend(self):
        """2026-10-27-r2 必须在文档、内嵌全文与后端常量里同时出现。"""
        docs = (LOCAL.parent / "docs" / "TERMS_AND_PRIVACY.md").read_text(encoding="utf-8")
        html = (LOCAL / "ui" / "index.html").read_text(encoding="utf-8")
        version = self._app_terms_version()
        self.assertIn(version, docs, "文档版本字段与后端常量漂移")
        self.assertIn(version, html,
                      "内嵌全文必须写明同一版本：冻结程序没有 docs 目录可对照")
        self.assertIn("条款变更与终止", docs)
        self.assertIn("再次点击同意", docs,
                      "版本变化必须要求显式重新同意")
        # 内嵌全文不另立版本常量：版本由后端 bootstrap 带进来
        self.assertNotIn("TERMS_VERSION", html)

    def test_disclosure_covers_tjise_data_dir_migration_and_uninstall_scope(self):
        """§6.1 披露四件事：TJISE_DATA_DIR 覆盖、迁移源副本保留、卸载只删
        默认数据目录、同意后的周期学校刷新；两处误导性旧表述必须绝迹。"""
        docs = (LOCAL.parent / "docs" / "TERMS_AND_PRIVACY.md").read_text(encoding="utf-8")
        html = (LOCAL / "ui" / "index.html").read_text(encoding="utf-8")
        readme = (LOCAL / "README.md").read_text(encoding="utf-8")
        tips = (LOCAL / "遇到问题先看我.txt").read_text(encoding="utf-8")
        for label, text in (("docs", docs), ("内嵌全文", html)):
            self.assertIn("TJISE_DATA_DIR", text,
                          "%s：必须写明 TJISE_DATA_DIR 对数据目录的覆盖" % label)
            self.assertIn("源文件保留在原处", text,
                          "%s：迁移是复制，源文件必须写明保留" % label)
            self.assertIn("只针对默认数据目录", text,
                          "%s：卸载可选删除必须限定在默认数据目录" % label)
            self.assertIn("按界面操作与周期刷新发生", text,
                          "%s：同意后的周期学校刷新必须写明" % label)
            self.assertNotIn("仅在你主动操作时以你的账号访问学校系统", text,
                             "%s：同意后有周期刷新，不能写成只在手动时访问学校" % label)
            self.assertNotIn("彻底删除所有数据", text,
                             "%s：删一个目录只删该处，不能声称一次清光所有副本" % label)
        self.assertIn("TJISE_DATA_DIR", readme,
                      "README 必须在 TJISE_DATA_DIR 说明附近补迁移与卸载范围")
        self.assertIn("原文件保留在旧位置", readme, "README：迁移必须写明保留原文件")
        self.assertIn("只针对默认的", readme, "README：卸载可选删除只针对默认数据目录")
        # 排障说明是固定宽度折行文本，先去掉全部空白再断言，避免跨行断词误报
        tips_compact = "".join(tips.split())
        self.assertIn("TJISE_DATA_DIR", tips, "排障说明必须区分自定义覆盖目录")
        self.assertIn("迁移过来的原文件也还留在原处", tips_compact,
                      "排障说明必须写明旧 EXE 旁迁移源副本仍在")
        self.assertIn("不等于所有副本都没了", tips_compact,
                      "排障说明必须写明删一个目录不等于清光全部副本")

    def test_docs_and_embedded_text_share_eleven_sections(self):
        """文档与内嵌全文的 11 节标题与顺序必须一致（冻结 EXE 无 docs/ 也可读）。"""
        docs = (LOCAL.parent / "docs" / "TERMS_AND_PRIVACY.md").read_text(encoding="utf-8")
        html = (LOCAL / "ui" / "index.html").read_text(encoding="utf-8")
        doc_titles = re.findall(r"^## (\d+)\. (.+)$", docs, flags=re.M)
        html_titles = re.findall(r"<h3>(\d+)\. (.+?)</h3>", html)
        self.assertEqual([t[0] for t in doc_titles],
                         [str(i) for i in range(1, 12)], "文档必须是 1..11 节")
        self.assertEqual([t[0] for t in html_titles],
                         [str(i) for i in range(1, 12)], "内嵌全文必须是 1..11 节")
        for (no, title), (hno, htitle) in zip(doc_titles, html_titles):
            self.assertEqual((no, title), (hno, htitle),
                             "第 %s 节标题两处必须一字不差" % no)

    def test_index_html_has_terms_dialog_contract(self):
        """条款弹窗契约：弹窗、查看完整条款、同意、暂不使用与帮助内入口。"""
        html = (LOCAL / "ui" / "index.html").read_text(encoding="utf-8")
        for required in ('id="dlg-terms"', 'id="btn-terms-full"', 'id="btn-terms-accept"',
                         'id="btn-terms-decline"', 'id="dlg-terms-version"',
                         'id="dlg-terms-full-text"', 'id="btn-help-terms"',
                         'id="dlg-terms-status"'):
            self.assertIn(required, html, "条款弹窗缺少 %s" % required)
        self.assertIn("我已阅读并同意", html)
        self.assertIn("暂不使用", html)
        self.assertIn("查看完整条款", html)
        # 完整条款必须覆盖全部 11 节（与 docs/TERMS_AND_PRIVACY.md 同构）
        for index in range(1, 12):
            self.assertIn("%d. " % index, html, "完整条款缺第 %d 节" % index)
        # 不出现任何报名成功承诺
        for banned in ("包成功", "保证报名成功", "必定成功"):
            self.assertNotIn(banned, html)

    # ---- 内嵌全文 ↔ 规范真源逐字契约（计划 20261028-terms-embedded-exact-sync）----

    MAINTENANCE_NOTE = ("本文件是随源码分发的可审阅规范来源；程序界面内嵌同一份完整条款，"
                        "即使没有这个文件，也能在「帮助 → 使用条款与隐私」里读到。")
    DISCLAIMER = "本文件是产品使用说明与隐私告知，不是法律意见；有具体法律疑问请咨询执业律师。"
    OFFLINE_NOTE = "本份条款可离线阅读：全文就在本窗口内，不依赖网络或仓库文档。"

    @staticmethod
    def _norm_canonical(text: str, *, strip_ordered: bool) -> str:
        # HTMLParser 已去标签；HTML 中的 Markdown 字符就是可见文字，必须参与比较。
        if not strip_ordered:
            return re.sub(r"\s+", "", text)
        text = text.replace("**", "").replace("`", "")
        text = re.sub(r"(?m)^\s*>\s?", "", text)
        text = re.sub(r"(?m)^\s*-\s+", "", text)
        text = re.sub(r"(?m)^\s*\d+\.\s+", "", text)
        text = re.sub(r"(?m)^\s*#{1,6}\s*", "", text)
        return re.sub(r"\s+", "", text)

    @staticmethod
    def _first_diff(a: str, b: str) -> int:
        for index, (left, right) in enumerate(zip(a, b)):
            if left != right:
                return index
        return min(len(a), len(b))

    def _docs_canonical_texts(self):
        """只读解析 docs/TERMS_AND_PRIVACY.md →（前言规范化文本, {1..11: 规范化文本}）。
        前言剔除已知排除项「规范来源维护说明句」（计划 task4 clarify 记录），剔除前先断言
        该句仍在 docs，避免排除项在规范演化后失效。"""
        docs = (LOCAL.parent / "docs" / "TERMS_AND_PRIVACY.md").read_text(encoding="utf-8")
        start = re.search(r"(?m)^## 1\. ", docs)
        self.assertIsNotNone(start, "docs 必须从 ## 1. 定义 起进入 11 节结构")
        preamble_raw = docs[:start.start()]
        title = re.search(r"(?m)^# (.+)$", preamble_raw)
        self.assertIsNotNone(title, "docs 必须带 # 一级标题")
        quote = "\n".join(re.findall(r"(?m)^\s*>\s?(.*)$", preamble_raw))
        quote_n = self._norm_canonical(quote, strip_ordered=True)
        maint_n = re.sub(r"\s+", "", self.MAINTENANCE_NOTE)
        self.assertIn(maint_n, quote_n, "维护说明句必须仍在 docs 前言，否则该排除项失去依据")
        preamble = (self._norm_canonical(title.group(1), strip_ordered=True)
                    + quote_n.replace(maint_n, ""))
        sections = {}
        for part in re.split(r"(?m)^(?=## \d+\. )", docs):
            match = re.match(r"## (\d+)\. ", part)
            if match:
                sections[int(match.group(1))] = self._norm_canonical(part, strip_ordered=True)
        self.assertEqual(sorted(sections), list(range(1, 12)), "docs 必须是 1..11 节")
        return preamble, sections

    def _embedded_canonical_texts(self, html=None):
        """标准库 HTMLParser 从 #dlg-terms-full-text 抽取用户可见规范文本 →
        （前言, {1..11}, 容器内全部文本）。html 为 None 时才读真实 index.html，
        传入字符串时只解析内存样例（task3 突变用例）。采集范围不越过该 section：
        不误收要点、版本副标题、工具栏、帮助 UI 或离线说明。"""
        from html.parser import HTMLParser

        class _TermsFullTextParser(HTMLParser):
            """按 terms-section 分桶收集可见文本：terms-preamble 归前言，
            其余按 h3 的节号归节；all_text 记录容器内全部文本供全量比较与排除断言。
            section/div 两个深度界定桶起止、识别不支持的嵌套；其他标签不计深度。"""

            def __init__(self):
                super().__init__(convert_charrefs=True)
                self.in_full = False
                self.full_roots = 0      # 全文根 section 出现次数（必须恰好 1）
                self.section_depth = 0   # 根闭合后必须归 0
                self.div_depth = 0        # 容器内 div 深度（根 section 自身不计）
                self.bucket = None       # {"preamble", "num", "parts"} | None
                self.bucket_depth = None # 建桶时的 div_depth，只在同深度闭合封桶
                self.h3_parts = None
                self.preamble = []
                self.sections = {}
                self.section_order = []  # 封桶先后（章序断言）
                self.all_text = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if not self.in_full:
                    if tag == "section" and attrs.get("id") == "dlg-terms-full-text":
                        self.full_roots += 1
                        self.in_full = True
                        self.section_depth = 1
                    return
                if tag == "section":
                    raise AssertionError("全文容器内出现嵌套 section：结构不受支持")
                if tag == "div":
                    self.div_depth += 1
                    classes = (attrs.get("class") or "").split()
                    if "terms-section" in classes:
                        if self.bucket is not None:
                            raise AssertionError("terms-section 嵌套在另一节内：结构不受支持")
                        self.bucket = {"preamble": "terms-preamble" in classes,
                                       "num": None, "parts": []}
                        self.bucket_depth = self.div_depth
                elif tag == "h3" and self.bucket is not None:
                    self.h3_parts = []

            def handle_endtag(self, tag):
                if not self.in_full:
                    return
                if tag == "section":
                    self.in_full = False
                    self.section_depth = 0
                    return
                if tag == "h3" and self.bucket is not None:
                    head = re.match(r"(\d+)\.", "".join(self.h3_parts or []).strip())
                    if head and not self.bucket["preamble"]:
                        self.bucket["num"] = int(head.group(1))
                    self.h3_parts = None
                elif tag == "div":
                    # 只在建桶时的深度封桶：桶内纯结构嵌套 div 不提前截断
                    if self.bucket is not None and self.div_depth == self.bucket_depth:
                        text = "".join(self.bucket["parts"])
                        if self.bucket["preamble"]:
                            self.preamble.append(text)
                        else:
                            if self.bucket["num"] is None:
                                raise AssertionError("非前言桶没有可解析章号：结构不受支持")
                            self.sections.setdefault(self.bucket["num"], []).append(text)
                            self.section_order.append(self.bucket["num"])
                        self.bucket = None
                        self.bucket_depth = None
                    self.div_depth -= 1

            def handle_data(self, data):
                if not self.in_full:
                    return
                if self.h3_parts is not None:
                    self.h3_parts.append(data)
                if self.bucket is not None:
                    self.bucket["parts"].append(data)
                self.all_text.append(data)

        if html is None:
            html = (LOCAL / "ui" / "index.html").read_text(encoding="utf-8")
        parser = _TermsFullTextParser()
        parser.feed(html)
        parser.close()
        self.assertFalse(parser.in_full, "#dlg-terms-full-text 必须闭合")
        self.assertEqual(parser.full_roots, 1, "全文根 section 必须恰好出现一次")
        self.assertEqual(parser.section_depth, 0, "根闭合后 section 深度必须归 0")
        self.assertIsNone(parser.bucket, "根闭合后不得残留未封的桶")
        self.assertEqual(parser.div_depth, 0, "容器内 div 必须全部闭合")
        self.assertEqual(sorted(parser.sections), list(range(1, 12)), "内嵌必须是 1..11 节")
        self.assertTrue(all(len(texts) == 1 for texts in parser.sections.values()),
                        "每章必须恰好一个桶")
        self.assertEqual(parser.section_order, list(range(1, 12)), "章序必须是 1..11")
        self.assertEqual(len(parser.preamble), 1, "内嵌前言块必须恰好一个")
        return parser

    def _assert_embedded_matches_docs(self, html=None):
        """两道精确比较：前言 + 逐节（保留原首差诊断），最后加全文容器全量相等。
        桶外额外正文只能进 all_text，会被第三道断言抓住；两个既定排除项
        （离线提示、规范来源维护说明句）仍只按各自原位置排除，不设白名单。"""
        docs_preamble, docs_sections = self._docs_canonical_texts()
        parser = self._embedded_canonical_texts(html)
        html_preamble = self._norm_canonical("".join(parser.preamble), strip_ordered=False)
        gap = self._first_diff(html_preamble, docs_preamble)
        self.assertEqual(
            html_preamble, docs_preamble,
            "前言与规范真源不逐字一致（首差 @%d）：\n  docs: …%s…\n  html: …%s…"
            % (gap, docs_preamble[max(0, gap - 30): gap + 60],
               html_preamble[max(0, gap - 30): gap + 60]))
        self.assertEqual(list(parser.sections), list(range(1, 12)), "章节顺序必须是 1..11")
        for number in range(1, 12):
            docs_text = docs_sections[number]
            html_text = self._norm_canonical("".join(parser.sections[number]),
                                             strip_ordered=False)
            gap = self._first_diff(html_text, docs_text)
            self.assertEqual(
                html_text, docs_text,
                "第 %d 节与 docs/TERMS_AND_PRIVACY.md 不逐字一致（首差 @%d）：\n"
                "  docs: …%s…\n  html: …%s…"
                % (number, gap, docs_text[max(0, gap - 30): gap + 60],
                   html_text[max(0, gap - 30): gap + 60]))
        expected_full = docs_preamble + "".join(docs_sections[n] for n in range(1, 12))
        actual_full = self._norm_canonical("".join(parser.all_text), strip_ordered=False)
        gap = self._first_diff(actual_full, expected_full)
        self.assertEqual(actual_full, expected_full,
                         "全文容器有未纳入章节的正文或文本次序漂移（首差 @%d）" % gap)

    def test_embedded_full_text_is_verbatim_identical_to_docs(self):
        """内嵌全文与只读规范真源逐字一致：唯一允许差异为结构标签与折叠空白。"""
        self._assert_embedded_matches_docs()

    def test_disclaimer_sits_in_preamble_not_section_10(self):
        """免责声明只在前言；第 10 节以规范原文「人民法院提起诉讼。」结束。"""
        parser = self._embedded_canonical_texts()
        preamble = self._norm_canonical("".join(parser.preamble), strip_ordered=False)
        section10 = self._norm_canonical("".join(parser.sections[10]), strip_ordered=False)
        self.assertIn(self.DISCLAIMER, preamble, "免责声明必须出现在前言")
        self.assertNotIn("不是法律意见", section10, "第 10 节正文不得出现免责声明")
        self.assertTrue(section10.endswith("人民法院提起诉讼。"),
                        "第 10 节必须以规范原文「人民法院提起诉讼。」结束")

    def test_offline_note_lives_outside_canonical_comparison_flow(self):
        """「本份条款可离线阅读」是独立 UI 包装：比较容器外、真源文件里没有它。"""
        parser = self._embedded_canonical_texts()
        html = (LOCAL / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn(self.OFFLINE_NOTE, "".join(parser.all_text),
                         "离线提示不得进入规范正文比较流")
        self.assertIn(self.OFFLINE_NOTE, html, "离线提示必须仍存在于界面包装")
        self.assertLess(html.find(self.OFFLINE_NOTE), html.find('id="dlg-terms-full-text"'),
                        "离线提示应位于全文比较容器之前的要点区")
        docs = (LOCAL.parent / "docs" / "TERMS_AND_PRIVACY.md").read_text(encoding="utf-8")
        self.assertNotIn(self.OFFLINE_NOTE, docs, "界面提示不得写入规范真源")

    def test_preamble_maintenance_sentence_is_known_exclusion(self):
        """已知排除项：规范来源维护说明句只在 docs 前言，内嵌前言按计划不呈现。"""
        maint = re.sub(r"\s+", "", self.MAINTENANCE_NOTE)
        docs = (LOCAL.parent / "docs" / "TERMS_AND_PRIVACY.md").read_text(encoding="utf-8")
        # docs 侧必须做结构规范化再比较（剥 Markdown 标记、行首有序标记、标题符
        # 并折叠空白）：该句在 docs 里位于 blockquote 中，裸折叠空白会留下 `>`
        # 块引用符导致误报不一致。前言取 `^## 1\.` 之前，先断言分界存在；不能
        # 复用 _docs_canonical_texts 的前言——它已剔除本排除项。
        boundary = re.search(r"^## 1\.", docs, flags=re.M)
        self.assertIsNotNone(boundary, "docs 必须存在 `## 1.` 章节分界")
        preamble_raw = docs[:boundary.start()]
        self.assertIn(maint, self._norm_canonical(preamble_raw, strip_ordered=True),
                      "docs 前言必须保留规范来源说明")
        parser = self._embedded_canonical_texts()
        self.assertNotIn(maint, self._norm_canonical("".join(parser.preamble),
                                                     strip_ordered=False),
                         "内嵌前言按计划排除规范来源说明句")

    def test_parity_rejects_text_outside_section_buckets(self):
        """桶外额外正文必须被全文容器比较抓住：只比章节桶不算一致。"""
        html = (LOCAL / "ui" / "index.html").read_text(encoding="utf-8")
        anchor = '<div class="terms-section terms-preamble">'
        self.assertEqual(html.count(anchor), 1)
        changed = html.replace(anchor, '<p>额外条款句。</p>' + anchor, 1)
        with self.assertRaises(AssertionError):
            self._assert_embedded_matches_docs(changed)

    def test_parity_keeps_literal_markdown_characters_in_html(self):
        """HTML 里的 **、`、# 是可见文字，不得被规范化当 Markdown 抹掉。"""
        html = (LOCAL / "ui" / "index.html").read_text(encoding="utf-8")
        anchor = "本条款适用中华人民共和国法律。"
        self.assertEqual(html.count(anchor), 1)
        for marker in ("**", "`", "# "):
            with self.subTest(marker=marker):
                changed = html.replace(anchor, "\n" + marker + anchor, 1)
                with self.assertRaises(AssertionError):
                    self._assert_embedded_matches_docs(changed)

    def test_parity_accepts_text_preserving_html_wrapper(self):
        """仅标签包装不改变可见文字：只折叠空白仍判相等。"""
        html = (LOCAL / "ui" / "index.html").read_text(encoding="utf-8")
        anchor = "本条款适用中华人民共和国法律。"
        self.assertEqual(html.count(anchor), 1)
        changed = html.replace(anchor, "<span>" + anchor + "</span>", 1)
        self._assert_embedded_matches_docs(changed)

    def test_parity_accepts_pure_structural_nested_div(self):
        """纯结构嵌套 div 不提前封桶：字符顺序不变时仍相等（深度边界正向样例）。"""
        html = (LOCAL / "ui" / "index.html").read_text(encoding="utf-8")
        num10 = html.index("<h3>10. ")
        start = html.rfind('<div class="terms-section">', 0, num10)
        self.assertNotEqual(start, -1, "第 10 节开标签必须存在")
        end = html.index('<div class="terms-section">', start + 1)
        open_tag = '<div class="terms-section">'
        block = html[start:end]
        self.assertEqual(len(re.findall(re.escape(block), html)), 1, "第 10 节块必须唯一")
        content = block[len(open_tag):block.rfind("</div>")]
        wrapped_block = open_tag + "<div>" + content + "</div></div>"
        changed = html[:start] + wrapped_block + html[end:]
        self._assert_embedded_matches_docs(changed)

    def test_terms_dialog_structure_contract(self):
        """三层布局结构契约（HTMLParser 只查父子归属与 ID 唯一性）：
        head/body/foot 都是同一 dialog 的直接子级、正文控件全部在唯一
        terms-body 血统内、底栏三动作相对顺序不变、全文默认 hidden 且
        aria-controls 关联正确。不声称能证明 CSS 滚动或按钮可见——
        那些必须由后续授权的浏览器测量负责。"""
        from html.parser import HTMLParser

        class _LayoutParser(HTMLParser):
            VOID = {"br", "img", "input", "meta", "link", "hr",
                    "area", "base", "col", "embed", "source", "track", "wbr"}
            # 结构类名按优先级取首个作为规范标记，避免 dlg-foot/terms-foot
            # 这种双类名容器被记成 .dlg-foot 导致归属断言落空
            STRUCT = ("terms-head", "terms-body", "terms-feedback", "terms-foot",
                      "terms-toolbar", "terms-summary", "help-esc")

            def __init__(self):
                super().__init__(convert_charrefs=True)
                self.stack = []   # [(tag, attrs_dict)]
                self.ids = {}     # id -> 出现次数
                self.info = {}    # 关键标记 -> {parent, ancestors, attrs, order}
                self._seq = 0

            @classmethod
            def _marker(cls, tag, attrs):
                if attrs.get("id"):
                    return "#" + attrs["id"]
                tokens = (attrs.get("class") or "").split()
                for struct in cls.STRUCT:
                    if struct in tokens:
                        return "." + struct
                if tokens:
                    return "." + tokens[0]
                return "<" + tag + ">"

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                parent = (self._marker(*self.stack[-1]) if self.stack else None)
                ancestors = [self._marker(t, a) for t, a in self.stack]
                interest = []
                if attrs.get("id"):
                    self.ids[attrs["id"]] = self.ids.get(attrs["id"], 0) + 1
                    interest.append("#" + attrs["id"])
                tokens = set((attrs.get("class") or "").split())
                for struct in self.STRUCT:
                    if struct in tokens:
                        interest.append("." + struct)
                if tag == "form" and attrs.get("method") == "dialog":
                    interest.append("<form#dialog>")
                # 只记录条款 dialog 内部的元素（帮助弹窗也有自己的 help-esc）
                if interest and ("#dlg-terms" in ancestors or "#dlg-terms" in interest):
                    self._seq += 1
                    for key in interest:
                        self.info.setdefault(key, {
                            "parent": parent,
                            "ancestors": ancestors,
                            "attrs": attrs,
                            "order": self._seq,
                        })
                if tag not in self.VOID:
                    self.stack.append((tag, attrs))

            def handle_endtag(self, tag):
                for i in range(len(self.stack) - 1, -1, -1):
                    if self.stack[i][0] == tag:
                        del self.stack[i:]
                        break

        html = (LOCAL / "ui" / "index.html").read_text(encoding="utf-8")
        parser = _LayoutParser()
        parser.feed(html)
        parser.close()

        self.assertIsNotNone(parser.info.get("#dlg-terms"), "条款 dialog 必须存在")
        # head/body/foot 均为 dialog 直接子级：放错层级会改变滚动归属
        for key in (".terms-head", ".terms-body", ".terms-foot"):
            el = parser.info.get(key)
            self.assertIsNotNone(el, "缺失结构区：" + key)
            self.assertEqual(el["parent"], "#dlg-terms",
                             key + " 必须是 dialog 的直接子级，不许塞进 body")
        # 阅读区是唯一滚动区：以下控件必须位于 terms-body 血统内
        for key in ("#dlg-terms-sub", ".terms-toolbar", "#dlg-terms-summary",
                    "#dlg-terms-full-text", "#dlg-terms-status",
                    "#btn-terms-recheck", ".terms-feedback", ".help-esc"):
            el = parser.info.get(key)
            self.assertIsNotNone(el, "阅读区内缺失：" + key)
            self.assertIn(".terms-body", el["ancestors"],
                          key + " 必须位于唯一阅读区 terms-body 内")
        # 标题与 X 关闭都在 head
        self.assertIn(".terms-head",
                      parser.info["#dlg-terms-title"]["ancestors"],
                      "标题必须在 terms-head 内")
        x_form = parser.info.get("<form#dialog>")
        self.assertIsNotNone(x_form, "右上角 X 必须走 method=dialog 原生关闭")
        self.assertIn(".terms-head", x_form["ancestors"],
                      "X 关闭表单必须在 terms-head 内")
        # 底栏三动作都在 foot，且相对顺序不变
        for key in ("#btn-terms-close", "#btn-terms-decline", "#btn-terms-accept"):
            el = parser.info.get(key)
            self.assertIsNotNone(el, "底栏缺失：" + key)
            self.assertEqual(el["parent"], ".terms-foot",
                             key + " 必须是 terms-foot 的直接子级")
        orders = [parser.info[k]["order"] for k in
                  ("#btn-terms-close", "#btn-terms-decline", "#btn-terms-accept")]
        self.assertEqual(orders, sorted(orders),
                         "底栏动作相对顺序必须保持 close→decline→accept")
        # 全文默认隐藏，切换按钮的 aria-controls 正确关联
        full = parser.info["#dlg-terms-full-text"]
        self.assertIn("hidden", full["attrs"], "全文区默认必须 hidden")
        toggle = parser.info["#btn-terms-full"]
        self.assertEqual(toggle["attrs"].get("aria-controls"),
                         "dlg-terms-full-text",
                         "要点/全文切换必须 aria-controls 指向全文区")
        # 关键 ID 全文唯一（结构契约的前提）
        for dom_id in ("dlg-terms", "dlg-terms-title", "dlg-terms-sub",
                       "dlg-terms-view-title", "btn-terms-full",
                       "dlg-terms-summary", "dlg-terms-full-text",
                       "dlg-terms-status", "btn-terms-recheck",
                       "btn-terms-close", "btn-terms-decline",
                       "btn-terms-accept", "dlg-terms-document-version",
                       "dlg-terms-version"):
            self.assertEqual(parser.ids.get(dom_id), 1, "ID 必须唯一：" + dom_id)


class TermsConsentBehaviorTests(unittest.TestCase):
    """决策 013 / 条款同意门：首弹、拒绝不重弹、同意持久化解锁、版本变更再提示。"""

    TERMS = {"version": "2026-10-27", "accepted_version": "", "accepted_at": ""}

    def test_first_boot_opens_terms_and_blocks_network_actions(self):
        """首次未同意：弹一次条款；登录/列表/报名计划都不发起学校网络动作。"""
        res = run_js(r"""
let calls = [];
window.pywebview = { api: {
  login: () => { calls.push("login"); return new Promise(() => {}); },
  check: (id) => { calls.push("check"); return new Promise(() => {}); },
  list_activities: (id) => { calls.push("list"); return new Promise(() => {}); },
  start_plan: () => { calls.push("start"); return new Promise(() => {}); },
} };
S.demo = false;
S.prefs = {};
ingestTerms({ version: "2026-10-27", accepted_version: "", accepted_at: "" });
const opened = document.querySelector("#dlg-terms").open;
refresh(false);
doLogin();
startPlan();
await Promise.resolve(); await Promise.resolve();
out.push({ opened, calls,
  loginDisabled: document.querySelector("#btn-login").disabled,
  refreshDisabled: document.querySelector("#btn-refresh").disabled,
  planDisabled: document.querySelector("#btn-plan").disabled,
  banner: document.querySelector("#banners").innerHTML });
""")
        case = res[0]
        self.assertTrue(case["opened"], "首次未同意必须弹条款")
        self.assertEqual(case["calls"], [], "未同意时不得发起任何学校网络动作")
        self.assertTrue(case["loginDisabled"], "登录按钮必须禁用")
        self.assertTrue(case["refreshDisabled"], "刷新按钮必须禁用")
        self.assertTrue(case["planDisabled"], "开始按钮必须禁用")
        self.assertIn("使用条款", case["banner"], "必须挂条款横幅提供再同意入口")

    def test_accept_persists_unlocks_and_reloads(self):
        """同意：走后端 accept_terms 落盘，解锁网络动作并补上首轮加载。"""
        res = run_js(r"""
let calls = [], saved = [], accepted = 0, sentDocVersion = [];
window.pywebview = { api: {
  accept_terms: (documentVersion) => {
    accepted += 1;
    sentDocVersion.push(documentVersion);
    return Promise.resolve({ ok: true, terms: { version: "2026-10-27",
      accepted_version: "2026-10-27", accepted_at: "2026-10-27 12:00:00" } });
  },
  save_prefs: (patch) => { saved.push(JSON.parse(JSON.stringify(patch))); return Promise.resolve({ ok: true }); },
  check: (id) => { calls.push("check"); return new Promise(() => {}); },
  list_activities: (id) => { calls.push("list"); return new Promise(() => {}); },
} };
S.demo = false;
S.prefs = {};
ingestTerms({ version: "2026-10-27", accepted_version: "", accepted_at: "" });
/* 内嵌全文版本字段（生产取自 index.html）；harness 的 DOM 桩不解析 HTML，
 * 这里按真实页面写入同版，才能验证「回执版本 == 用户读到的版本」的校验 */
document.querySelector("#dlg-terms-document-version").textContent = "2026-10-27";
document.querySelector("#btn-terms-accept").click();
await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
out.push({ accepted, calls, saved, sentDocVersion,
  docNode: document.querySelector("#dlg-terms-document-version").textContent,
  dialogOpen: document.querySelector("#dlg-terms").open,
  loginDisabled: document.querySelector("#btn-login").disabled,
  refreshDisabled: document.querySelector("#btn-refresh").disabled,
  banner: document.querySelector("#banners").innerHTML });
""")
        case = res[0]
        self.assertEqual(case["accepted"], 1, "同意必须恰好调用一次后端接口")
        self.assertEqual(case["sentDocVersion"], [case["docNode"]],
                         "提交的 documentVersion 必须等于可见内嵌版本节点")
        self.assertEqual(case["calls"], ["check", "list"],
                         "同意后要补上被拦下的首轮加载")
        self.assertIn({"terms_declined_version": ""}, case["saved"],
                      "同意后要清掉拒绝版本标记")
        self.assertFalse(case["dialogOpen"], "同意成功必须关掉条款弹窗")
        self.assertFalse(case["loginDisabled"], "同意后登录必须解锁")
        self.assertNotIn("使用条款", case["banner"], "同意后必须撤掉条款横幅")

    def test_accept_failure_keeps_dialog_open_with_reason(self):
        """保存失败（prefs_busy）：留在弹窗里给重试说明，门禁保持。"""
        res = run_js(r"""
window.pywebview = { api: {
  accept_terms: () => Promise.resolve({ ok: false, text: "条款同意状态暂未保存，另一个窗口正在保存数据；请再点一次。" }),
} };
S.demo = false;
S.prefs = {};
ingestTerms({ version: "2026-10-27", accepted_version: "", accepted_at: "" });
document.querySelector("#dlg-terms-document-version").textContent = "2026-10-27";
document.querySelector("#btn-terms-accept").click();
await Promise.resolve(); await Promise.resolve();
out.push({ open: document.querySelector("#dlg-terms").open,
  statusHidden: document.querySelector("#dlg-terms-status").hidden,
  statusText: document.querySelector("#dlg-terms-status").textContent,
  locked: termsLocked() });
""")
        case = res[0]
        self.assertTrue(case["open"], "保存失败必须留在条款弹窗里")
        self.assertFalse(case["statusHidden"])
        self.assertIn("再点一次", case["statusText"], "失败说明要给出重试指引")
        self.assertTrue(case["locked"], "失败时门禁不得放行")

    def test_unknown_terms_accept_with_matching_receipt_unlocks_once(self):
        """未知态：只有回执字段齐全且版本与内嵌全文同版，才清 unknown、
        关弹窗，并且只补一次被拦下的首轮加载。"""
        res = run_js(r"""
let calls = [], saved = [], accepted = 0;
window.pywebview = { api: {
  accept_terms: () => {
    accepted += 1;
    return Promise.resolve({ ok: true, terms: { version: "2026-10-27-r2",
      accepted_version: "2026-10-27-r2", accepted_at: "2026-10-27 12:00:00" } });
  },
  save_prefs: (patch) => { saved.push(JSON.parse(JSON.stringify(patch))); return Promise.resolve({ ok: true }); },
  check: (id) => { calls.push("check"); return new Promise(() => {}); },
  list_activities: (id) => { calls.push("list"); return new Promise(() => {}); },
  login: () => { calls.push("login"); return new Promise(() => {}); },
  start_plan: () => { calls.push("start"); return new Promise(() => {}); },
} };
S.demo = false;
S.prefs = {};
markTermsUnknown();
const lockedBefore = termsLocked();
const autoOpenBefore = document.querySelector("#dlg-terms").open;
const bannerBefore = document.querySelector("#banners").innerHTML;
openTerms();
/* 内嵌全文版本（生产取自 index.html）：harness 的 DOM 桩不解析 HTML */
document.querySelector("#dlg-terms-document-version").textContent = "2026-10-27-r2";
document.querySelector("#btn-terms-accept").click();
await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
const after = { unknown: S.terms.unknown, locked: termsLocked(),
  version: S.terms.version, acceptedVersion: S.terms.acceptedVersion,
  acceptedAt: S.terms.acceptedAt, prompted: S.terms.prompted,
  dialogOpen: document.querySelector("#dlg-terms").open,
  loginDisabled: document.querySelector("#btn-login").disabled,
  banner: document.querySelector("#banners").innerHTML,
  calls: calls.slice(), saved: saved.slice(), accepted };
out.push({ lockedBefore, autoOpenBefore, bannerBefore, after });
""")
        case = res[0]
        self.assertTrue(case["lockedBefore"], "未知态本身必须是锁住的")
        self.assertFalse(case["autoOpenBefore"], "未知态不自动弹窗，用户点开的才可确认")
        self.assertIn("条款状态没能确认", case["bannerBefore"])
        after = case["after"]
        self.assertFalse(after["unknown"], "回执校验通过后才允许清 unknown")
        self.assertFalse(after["locked"], "确认成功必须解锁门禁")
        self.assertEqual(after["version"], "2026-10-27-r2", "要写入回执里的条款版本")
        self.assertEqual(after["acceptedVersion"], "2026-10-27-r2")
        self.assertEqual(after["acceptedAt"], "2026-10-27 12:00:00")
        self.assertTrue(after["prompted"])
        self.assertFalse(after["dialogOpen"], "确认成功必须关掉条款弹窗")
        self.assertFalse(after["loginDisabled"])
        self.assertNotIn("条款状态没能确认", after["banner"], "确认成功后撤掉未知横幅")
        self.assertEqual(after["accepted"], 1, "同意必须恰好调用一次后端接口")
        self.assertEqual(after["calls"], ["check", "list"],
                         "只补一次首轮加载（check+list 各一次），不得重复触发")
        self.assertIn({"terms_declined_version": ""}, after["saved"],
                      "确认成功要清掉拒绝版本标记")

    def test_unknown_terms_accept_rejects_bad_receipts(self):
        """未知态的残缺/矛盾/错版回执与桥接异常：一律留在弹窗、不清 unknown、
        不落盘，并继续阻止任何学校动作（fail closed，不做本地乐观更新）。"""
        res = run_js(r"""
let calls = [], saved = [], mode = "missing_at";
window.pywebview = { api: {
  accept_terms: () => {
    if (mode === "reject") return Promise.reject(new Error("桥接断了"));
    if (mode === "prefs_busy") return Promise.resolve({ ok: false, text: "条款同意状态暂未保存，另一个窗口正在保存数据；请再点一次。" });
    if (mode === "missing_at") return Promise.resolve({ ok: true, terms: { version: "2026-10-27-r2", accepted_version: "2026-10-27-r2" } });
    if (mode === "version_mismatch") return Promise.resolve({ ok: true, terms: { version: "2026-10-27-r2", accepted_version: "2026-10-27", accepted_at: "2026-10-27 12:00:00" } });
    if (mode === "doc_mismatch") return Promise.resolve({ ok: true, terms: { version: "2026-11-05-r1", accepted_version: "2026-11-05-r1", accepted_at: "2026-11-05 09:00:00" } });
  },
  save_prefs: (patch) => { saved.push(JSON.parse(JSON.stringify(patch))); return Promise.resolve({ ok: true }); },
  login: () => { calls.push("login"); return new Promise(() => {}); },
  check: (id) => { calls.push("check"); return new Promise(() => {}); },
  list_activities: (id) => { calls.push("list"); return new Promise(() => {}); },
  start_plan: () => { calls.push("start"); return new Promise(() => {}); },
} };
S.demo = false;
S.prefs = {};
async function probe(m, docVersion) {
  mode = m;
  markTermsUnknown();
  openTerms();
  /* 内嵌全文版本（生产取自 index.html）：doc_mismatch 用一份过期副本 */
  document.querySelector("#dlg-terms-document-version").textContent = docVersion;
  document.querySelector("#btn-terms-accept").click();
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  const state = { open: document.querySelector("#dlg-terms").open,
    unknown: S.terms.unknown, locked: termsLocked(),
    version: S.terms.version, acceptedVersion: S.terms.acceptedVersion,
    status: document.querySelector("#dlg-terms-status").textContent };
  refresh(false);
  doLogin();
  startPlan();
  return state;
}
const missingAt = await probe("missing_at", "2026-10-27-r2");
const versionMismatch = await probe("version_mismatch", "2026-10-27-r2");
const docMismatch = await probe("doc_mismatch", "2026-10-27-r2");
const prefsBusy = await probe("prefs_busy", "2026-10-27-r2");
const bridgeReject = await probe("reject", "2026-10-27-r2");
out.push({ missingAt, versionMismatch, docMismatch, prefsBusy, bridgeReject,
  calls, saved });
""")
        data = res[0]
        expect = {
            "missingAt": "缺字段",
            "versionMismatch": "自相矛盾",
            "docMismatch": "当前条款副本不匹配",
            "prefsBusy": "再点一次",
            "bridgeReject": "再点一次",
        }
        for name, hint in expect.items():
            case = data[name]
            self.assertTrue(case["open"], "%s：失败必须留在条款弹窗里" % name)
            self.assertTrue(case["unknown"], "%s：失败不得清 unknown" % name)
            self.assertTrue(case["locked"], "%s：失败时门禁不得放行" % name)
            self.assertEqual(case["version"], "", "%s：不得写入任何条款版本" % name)
            self.assertEqual(case["acceptedVersion"], "",
                             "%s：不得本地乐观标记为已同意" % name)
            self.assertIn(hint, case["status"], "%s：失败说明要讲清原因" % name)
        self.assertEqual(data["calls"], [],
                         "所有失败路径都不得发起任何学校动作")
        self.assertEqual(data["saved"], [], "失败时不得落盘任何同意标记")

    def test_decline_does_not_reprompt_and_keeps_help_available(self):
        """拒绝：不退出、不再强弹；帮助与条款入口仍可用，网络动作保持禁用。"""
        res = run_js(r"""
let calls = [], saved = [];
window.pywebview = { api: {
  login: () => { calls.push("login"); return new Promise(() => {}); },
  check: (id) => { calls.push("check"); return new Promise(() => {}); },
  list_activities: (id) => { calls.push("list"); return new Promise(() => {}); },
  start_plan: () => { calls.push("start"); return new Promise(() => {}); },
  save_prefs: (patch) => { saved.push(JSON.parse(JSON.stringify(patch))); return Promise.resolve({ ok: true }); },
} };
S.demo = false;
S.prefs = {};
ingestTerms({ version: "2026-10-27", accepted_version: "", accepted_at: "" });
const firstOpen = document.querySelector("#dlg-terms").open;
document.querySelector("#btn-terms-decline").click();
await Promise.resolve(); await Promise.resolve();
const afterDecline = { open: document.querySelector("#dlg-terms").open,
  declined: S.terms.declined };
refresh(false);
doLogin();
startPlan();
const reopened = document.querySelector("#dlg-terms").open;
document.querySelector("#btn-help").click();
const helpOpen = document.querySelector("#help").open;
document.querySelector("#btn-help-terms").click();
const termsFromHelp = document.querySelector("#dlg-terms").open;
/* 重复拒绝回归：从帮助重开同版条款后再点暂不使用，必须仍关闭弹窗 */
document.querySelector("#btn-terms-decline").click();
await Promise.resolve(); await Promise.resolve();
const afterRepeat = { open: document.querySelector("#dlg-terms").open,
  declined: S.terms.declined,
  declinedWrites: saved.filter((p) => p.terms_declined_version !== undefined).length };
out.push({ firstOpen, afterDecline, reopened, helpOpen, termsFromHelp, afterRepeat,
  calls, saved,
  loginDisabled: document.querySelector("#btn-login").disabled,
  refreshDisabled: document.querySelector("#btn-refresh").disabled,
  planDisabled: document.querySelector("#btn-plan").disabled });
""")
        case = res[0]
        self.assertTrue(case["firstOpen"], "拒绝前条款弹窗在场")
        self.assertFalse(case["afterDecline"]["open"], "拒绝后必须关掉弹窗")
        self.assertTrue(case["afterDecline"]["declined"])
        self.assertFalse(case["reopened"], "拒绝后不得再强弹条款")
        self.assertEqual(case["calls"], [], "拒绝后网络动作仍全部被拦")
        self.assertTrue(case["helpOpen"], "帮助必须继续可用")
        self.assertTrue(case["termsFromHelp"], "帮助里的条款入口必须能重新打开条款")
        self.assertIn({"terms_declined_version": "2026-10-27"}, case["saved"],
                      "拒绝版本必须落盘（跨重启不再强弹）")
        self.assertTrue(case["loginDisabled"])
        self.assertTrue(case["refreshDisabled"])
        self.assertTrue(case["planDisabled"])
        self.assertFalse(case["afterRepeat"]["open"], "重复拒绝必须仍关闭条款弹窗")
        self.assertTrue(case["afterRepeat"]["declined"], "重复拒绝后 declined 仍为 true")
        self.assertEqual(case["afterRepeat"]["declinedWrites"], 1, "拒绝版本只写一次")
        self.assertEqual(case["calls"], [], "重复拒绝后网络动作仍全部被拦")

    def test_declined_version_persists_and_version_change_reprompts(self):
        """重启后同一版本不重弹；条款版本变化时重新提示。"""
        res = run_js(r"""
let saved = [];
window.pywebview = { api: {
  save_prefs: (patch) => { saved.push(JSON.parse(JSON.stringify(patch))); return Promise.resolve({ ok: true }); },
} };
S.demo = false;
S.prefs = {};
ingestTerms({ version: "2026-10-27", accepted_version: "", accepted_at: "" });
document.querySelector("#btn-terms-decline").click();
await Promise.resolve(); await Promise.resolve();
/* 模拟重启：全新会话，prefs 带回上次拒绝过的版本 */
S.terms = { version: "", acceptedVersion: "", acceptedAt: "", declined: false, prompted: false };
ingestTerms({ version: "2026-10-27", accepted_version: "" });
const sameVersion = document.querySelector("#dlg-terms").open;
document.querySelector("#dlg-terms").close();
/* 条款版本变化：重新提示 */
S.terms = { version: "", acceptedVersion: "", acceptedAt: "", declined: false, prompted: false };
ingestTerms({ version: "2026-11-01", accepted_version: "2026-10-27" });
const newVersion = document.querySelector("#dlg-terms").open;
out.push({ saved, sameVersion, newVersion });
""")
        case = res[0]
        self.assertIn({"terms_declined_version": "2026-10-27"}, case["saved"])
        self.assertFalse(case["sameVersion"], "重启后同一版本不得再强弹")
        self.assertTrue(case["newVersion"], "条款版本变化必须重新提示")

    def test_restart_restores_declined_and_blocks_autoload_chain(self):
        """重启后刷新、F5 和可见性恢复都不再强弹，也不发学校动作（返修缺陷 1）。"""
        res = run_js(r"""
let calls = [];
window.pywebview = { api: {
  login: () => { calls.push("login"); return new Promise(() => {}); },
  check: (id) => { calls.push("check"); return new Promise(() => {}); },
  list_activities: (id) => { calls.push("list"); return new Promise(() => {}); },
  start_plan: () => { calls.push("start"); return new Promise(() => {}); },
  save_prefs: () => Promise.resolve({ ok: true }),
} };
S.demo = false;
S.prefs = {};
ingestTerms({ version: "2026-10-27-r2", accepted_version: "", accepted_at: "" });
document.querySelector("#btn-terms-decline").click();
await Promise.resolve(); await Promise.resolve();
/* 模拟重启：全新会话状态 + prefs 带回上次拒绝过的版本 */
S.terms = { version: "", acceptedVersion: "", acceptedAt: "", declined: false,
            prompted: false, unknown: false };
ingestTerms({ version: "2026-10-27-r2", accepted_version: "" });
const afterRestart = { open: document.querySelector("#dlg-terms").open,
  declined: S.terms.declined };
refresh(false);
__dispatchKey({ key: "F5" });
doLogin();
startPlan();
out.push({ afterRestart, calls, open: document.querySelector("#dlg-terms").open });
""")
        case = res[0]
        self.assertFalse(case["afterRestart"]["open"], "重启后同一版本不得再强弹")
        self.assertTrue(case["afterRestart"]["declined"],
                        "重启后必须把拒绝态恢复成会话状态，否则刷新会重复弹窗")
        self.assertEqual(case["calls"], [], "F5/刷新/可见性恢复都不得发学校动作")
        self.assertFalse(case["open"], "整条自动链路都不许再弹条款")

    def test_unknown_terms_state_fails_closed_and_keeps_help_open(self):
        """bootstrap 失败、返回 null、缺 terms、空版本或结构非法：一律拦住。"""
        res = run_js(r"""
let calls = [];
window.pywebview = { api: {
  login: () => { calls.push("login"); return new Promise(() => {}); },
  check: (id) => { calls.push("check"); return new Promise(() => {}); },
  list_activities: (id) => { calls.push("list"); return new Promise(() => {}); },
  start_plan: () => { calls.push("start"); return new Promise(() => {}); },
} };
S.demo = false;
S.prefs = {};
function probe(payload) {
  S.terms = { version: "", acceptedVersion: "", acceptedAt: "", declined: false,
              prompted: false, unknown: false };
  document.querySelector("#dlg-terms").close();
  ingestTerms(payload);
  const locked = termsLocked();
  refresh(false);
  doLogin();
  startPlan();
  const autoOpen = document.querySelector("#dlg-terms").open;
  const banner = document.querySelector("#banners").innerHTML;
  document.querySelector("#btn-help").click();
  const helpOpen = document.querySelector("#help").open;
  document.querySelector("#btn-help-terms").click();
  const manualOpen = document.querySelector("#dlg-terms").open;
  document.querySelector("#dlg-terms").close();
  return { locked, unknown: S.terms.unknown, autoOpen, manualOpen, helpOpen, banner };
}
const nullPayload = probe(null);
const missing = probe({ version: "", accepted_version: "" });
const emptyObject = probe({});
const badType = probe("不是对象");
out.push({ nullPayload, missing, emptyObject, badType, calls });
""")
        data = res[0]
        for name in ("nullPayload", "missing", "emptyObject", "badType"):
            case = data[name]
            self.assertTrue(case["locked"], "%s：条款状态未知必须拦住学校动作" % name)
            self.assertTrue(case["unknown"], "%s：必须标记为未知态" % name)
            self.assertFalse(case["autoOpen"], "%s：未知态不自动弹窗" % name)
            self.assertTrue(case["manualOpen"], "%s：帮助里的条款入口仍可显式打开" % name)
            self.assertTrue(case["helpOpen"], "%s：帮助必须始终可用" % name)
            self.assertIn("条款状态没能确认", case["banner"], "%s：必须挂可重试横幅" % name)
        self.assertEqual(data["calls"], [], "未知态不得发起任何学校动作")

    def test_version_change_prompts_only_once(self):
        """条款版本变化只强弹一次：同版本重复 ingest 不再弹。"""
        res = run_js(r"""
S.demo = false;
S.prefs = { terms_declined_version: "2026-10-27" };
ingestTerms({ version: "2026-10-27-r2", accepted_version: "" });
const firstOpen = document.querySelector("#dlg-terms").open;
document.querySelector("#dlg-terms").close();
ingestTerms({ version: "2026-10-27-r2", accepted_version: "" });
const againOpen = document.querySelector("#dlg-terms").open;
document.querySelector("#dlg-terms").close();
/* 已拒绝过当前版本：不再弹 */
S.prefs.terms_declined_version = "2026-10-27-r2";
S.terms = { version: "", acceptedVersion: "", acceptedAt: "", declined: false,
            prompted: false, unknown: false };
ingestTerms({ version: "2026-10-27-r2", accepted_version: "" });
const declinedOpen = document.querySelector("#dlg-terms").open;
out.push({ firstOpen, againOpen, declinedOpen });
""")
        case = res[0]
        self.assertTrue(case["firstOpen"], "版本变化必须重新提示一次")
        self.assertFalse(case["declinedOpen"], "本版本已拒绝过就不该再弹")

    def test_demo_mode_is_not_gated(self):
        """离线演示（demo）不做学校网络动作：条款门禁不拦演示形态。"""
        res = run_js(r"""
S.demo = true;
S.prefs = {};
ingestTerms({ version: "2026-10-27", accepted_version: "", accepted_at: "" });
out.push({ opened: document.querySelector("#dlg-terms").open,
  locked: termsLocked(),
  loginDisabled: document.querySelector("#btn-login").disabled,
  banner: document.querySelector("#banners").innerHTML });
""")
        case = res[0]
        self.assertFalse(case["opened"], "演示模式不弹条款")
        self.assertFalse(case["locked"])
        self.assertFalse(case["loginDisabled"], "演示模式登录入口不受条款门禁")
        self.assertNotIn("使用条款", case["banner"])


class TermsInteractionLoopTests(unittest.TestCase):
    """close-terms-interaction-loop：在途保存、10 秒超时未确认、重新确认、
    只读查看、关闭收口、来源焦点、引导延后与条款弹窗键盘隔离的行为契约。
    DOM 桩只复用既有能力（focus/activeElement、listeners、计时器替换），
    不把 showModal/hidden 桩当排版证据。"""

    def test_double_click_saves_once_and_locks_footer(self):
        """连续点击只登记一次在途：一次请求、保存态三控件禁用、文案「正在保存…」。"""
        res = run_js(r"""
let accepted = 0;
window.pywebview = { api: {
  accept_terms: () => { accepted += 1; return new Promise(() => {}); },
} };
S.demo = false;
S.prefs = {};
ingestTerms({ version: "2026-10-27", accepted_version: "", accepted_at: "" });
document.querySelector("#dlg-terms-document-version").textContent = "2026-10-27";
document.querySelector("#btn-terms-accept").click();
document.querySelector("#btn-terms-accept").click();
const accept = document.querySelector("#btn-terms-accept");
const decline = document.querySelector("#btn-terms-decline");
const dlg = document.querySelector("#dlg-terms");
out.push({ accepted,
  acceptDisabled: accept.disabled, acceptText: accept.textContent,
  declineDisabled: decline.disabled, xDisabled: dlg.querySelector(".dlg-x").disabled,
  escText: dlg.querySelector(".help-esc").textContent, locked: termsLocked() });
""")
        case = res[0]
        self.assertEqual(case["accepted"], 1, "保存中重复点击只能发出一次 accept_terms")
        self.assertTrue(case["acceptDisabled"])
        self.assertEqual(case["acceptText"], "正在保存…")
        self.assertTrue(case["declineDisabled"])
        self.assertTrue(case["xDisabled"])
        self.assertEqual(case["escText"], "正在保存…")
        self.assertTrue(case["locked"], "在途保存期间门禁保持")

    def test_accept_failure_restores_buttons_and_keeps_dialog(self):
        """明确失败：留在弹窗、给出原因、三控件恢复可点，不假称未确认。"""
        res = run_js(r"""
window.pywebview = { api: {
  accept_terms: () => Promise.resolve({ ok: false,
    text: "设置保存失败（设置可能正在被占用），请稍后再试。" }),
} };
S.demo = false;
S.prefs = {};
ingestTerms({ version: "2026-10-27", accepted_version: "", accepted_at: "" });
document.querySelector("#dlg-terms-document-version").textContent = "2026-10-27";
document.querySelector("#btn-terms-accept").click();
await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
const accept = document.querySelector("#btn-terms-accept");
const dlg = document.querySelector("#dlg-terms");
out.push({ open: dlg.open,
  acceptDisabled: accept.disabled, acceptText: accept.textContent,
  declineDisabled: document.querySelector("#btn-terms-decline").disabled,
  xDisabled: dlg.querySelector(".dlg-x").disabled,
  escText: dlg.querySelector(".help-esc").textContent, locked: termsLocked(),
  status: document.querySelector("#dlg-terms-status").textContent });
""")
        case = res[0]
        self.assertTrue(case["open"], "失败必须留在弹窗里")
        self.assertFalse(case["acceptDisabled"])
        self.assertEqual(case["acceptText"], "我已阅读并同意")
        self.assertFalse(case["declineDisabled"])
        self.assertFalse(case["xDisabled"])
        self.assertEqual(case["escText"], "Esc 暂不使用")
        self.assertTrue(case["locked"])
        self.assertIn("设置保存失败", case["status"])
        self.assertNotIn("未确认", case["status"], "明确失败不能说成未确认")

    def test_accept_success_refreshes_exactly_once_and_closes(self):
        """完整成功：关窗解锁、首轮学校加载只补一次、延后的引导只消费一次。"""
        res = run_js(r"""
let calls = [], saved = [];
window.pywebview = { api: {
  accept_terms: () => Promise.resolve({ ok: true, terms: { version: "2026-10-27",
      accepted_version: "2026-10-27", accepted_at: "2026-10-27 12:00:00" } }),
  save_prefs: (patch) => { saved.push(JSON.parse(JSON.stringify(patch))); return Promise.resolve({ ok: true }); },
  check: () => { calls.push("check"); return new Promise(() => {}); },
  list_activities: () => { calls.push("list"); return new Promise(() => {}); },
} };
S.demo = false;
S.prefs = {};
ingestTerms({ version: "2026-10-27", accepted_version: "", accepted_at: "" });
document.querySelector("#dlg-terms-document-version").textContent = "2026-10-27";
S.terms.deferredIntro = { tour: false, welcome: true };
document.querySelector("#btn-terms-accept").click();
await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
await Promise.resolve(); await Promise.resolve();
out.push({ open: document.querySelector("#dlg-terms").open,
  locked: termsLocked(), calls, deferred: S.terms.deferredIntro,
  declinedCleared: saved.some((p) => "terms_declined_version" in p),
  focus: document.activeElement && document.activeElement.__sel });
""")
        case = res[0]
        self.assertFalse(case["open"], "成功才关弹窗")
        self.assertFalse(case["locked"])
        self.assertEqual(case["calls"], ["check", "list"], "完整成功最多补加载一次")
        self.assertIsNone(case["deferred"], "延后的引导只继续一次（消费即清空）")
        self.assertTrue(case["declinedCleared"])
        self.assertEqual(case["focus"], "#btn-help", "同意成功焦点回 #btn-help")

    def test_save_timeout_keeps_gate_reopens_close_and_drops_late_receipt(self):
        """10 秒未获可确认结果：不假称失败/取消，门禁保持、关闭权限恢复、
        禁止直接再次写同意；迟到回执不关窗、不覆盖、不重复刷新。"""
        res = run_js(r"""
let timers = [], accepted = 0, calls = [], resolveAccept = null;
window.pywebview = { api: {
  accept_terms: () => { accepted += 1;
    return new Promise((resolve) => { resolveAccept = resolve; }); },
  check: () => { calls.push("check"); return new Promise(() => {}); },
  list_activities: () => { calls.push("list"); return new Promise(() => {}); },
} };
setTimeout = (fn) => { timers.push(fn); return timers.length; };
clearTimeout = () => {};
S.demo = false;
S.prefs = {};
ingestTerms({ version: "2026-10-27", accepted_version: "", accepted_at: "" });
document.querySelector("#dlg-terms-document-version").textContent = "2026-10-27";
document.querySelector("#btn-terms-accept").click();
const dlg = document.querySelector("#dlg-terms");
const cancelEv = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
(dlg.listeners.cancel || []).forEach((fn) => fn(cancelEv));
const savingState = { prevented: cancelEv.defaultPrevented,
  acceptDisabled: document.querySelector("#btn-terms-accept").disabled };
timers[0](); /* 10 秒超时 → 未确认 */
const statusAfterTimeout = document.querySelector("#dlg-terms-status").textContent;
const accept = document.querySelector("#btn-terms-accept");
document.querySelector("#btn-terms-accept").click(); /* 未确认期间再点：不得再发请求 */
const afterRetryClick = accepted;
resolveAccept({ ok: true, terms: { version: "2026-10-27",
  accepted_version: "2026-10-27", accepted_at: "2026-10-27 12:00:00" } });
await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
const recheck = document.querySelector("#btn-terms-recheck");
out.push({ savingState, statusAfterTimeout, accepted, afterRetryClick,
  open: dlg.open, locked: termsLocked(),
  acceptDisabled: accept.disabled,
  declineDisabled: document.querySelector("#btn-terms-decline").disabled,
  xDisabled: dlg.querySelector(".dlg-x").disabled,
  recheckHidden: recheck.hidden, recheckDisabled: recheck.disabled,
  escText: dlg.querySelector(".help-esc").textContent,
  statusFinal: document.querySelector("#dlg-terms-status").textContent,
  calls });
""")
        case = res[0]
        self.assertTrue(case["savingState"]["prevented"], "保存中的 Esc 必须被 cancel 拦下")
        self.assertTrue(case["savingState"]["acceptDisabled"])
        self.assertIn("未确认", case["statusAfterTimeout"])
        self.assertNotIn("没有保存成功", case["statusAfterTimeout"], "超时不能假称失败")
        self.assertNotIn("已拒绝", case["statusAfterTimeout"], "超时不能假称取消")
        self.assertEqual(case["afterRetryClick"], 1, "未确认期间禁止直接再次写同意")
        self.assertTrue(case["open"], "迟到回执不许关窗")
        self.assertTrue(case["locked"], "迟到回执不许解锁")
        self.assertTrue(case["acceptDisabled"], "未确认期间同意按钮保持禁用")
        self.assertTrue(case["declineDisabled"])
        self.assertFalse(case["xDisabled"], "关闭权限恢复")
        self.assertFalse(case["recheckHidden"], "亮出重新确认状态")
        self.assertFalse(case["recheckDisabled"])
        self.assertEqual(case["escText"], "Esc 关闭")
        self.assertEqual(case["calls"], [], "迟到回执不许重复刷新")
        self.assertEqual(case["statusFinal"], case["statusAfterTimeout"],
                         "迟到回执不许覆盖未确认状态")
        self.assertEqual(case["accepted"], 1)

    def test_recheck_confirmed_acceptance_closes_without_new_write(self):
        """重新确认读到「已同意同版+有时间+匹配本份」：按成功收口，零新写请求。"""
        res = run_js(r"""
let timers = [], accepted = 0, calls = [], saved = [];
window.pywebview = { api: {
  accept_terms: () => { accepted += 1; return new Promise(() => {}); },
  bootstrap: () => { calls.push("bootstrap");
    return Promise.resolve({ terms: { version: "2026-10-27",
      accepted_version: "2026-10-27", accepted_at: "2026-10-27 12:00:00" } }); },
  save_prefs: (patch) => { saved.push(JSON.parse(JSON.stringify(patch))); return Promise.resolve({ ok: true }); },
  check: () => { calls.push("check"); return new Promise(() => {}); },
  list_activities: () => { calls.push("list"); return new Promise(() => {}); },
} };
setTimeout = (fn) => { timers.push(fn); return timers.length; };
clearTimeout = () => {};
S.demo = false;
S.prefs = {};
ingestTerms({ version: "2026-10-27", accepted_version: "", accepted_at: "" });
document.querySelector("#dlg-terms-document-version").textContent = "2026-10-27";
S.terms.deferredIntro = { tour: false, welcome: false };
document.querySelector("#btn-terms-accept").click();
timers[0](); /* 超时 → 未确认 */
document.querySelector("#btn-terms-recheck").click();
await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
await Promise.resolve(); await Promise.resolve();
out.push({ open: document.querySelector("#dlg-terms").open,
  locked: termsLocked(), accepted, calls, saved,
  deferred: S.terms.deferredIntro,
  focus: document.activeElement && document.activeElement.__sel });
""")
        case = res[0]
        self.assertFalse(case["open"])
        self.assertFalse(case["locked"])
        self.assertEqual(case["accepted"], 1, "重新确认不得再发同意写请求")
        self.assertEqual(case["calls"], ["bootstrap", "check", "list"],
                         "只读一次 bootstrap，成功收口补一次加载")
        self.assertTrue(any("terms_declined_version" in p for p in case["saved"]))
        self.assertIsNone(case["deferred"], "成功收口把延后引导消费掉")
        self.assertEqual(case["focus"], "#btn-help")

    def test_recheck_explicitly_not_accepted_restores_accept_button(self):
        """重新确认读到明确未同意：恢复同意按钮与关闭语义，可再点一次。"""
        res = run_js(r"""
let timers = [], accepted = 0;
window.pywebview = { api: {
  accept_terms: () => { accepted += 1; return new Promise(() => {}); },
  bootstrap: () => Promise.resolve({ terms: { version: "2026-10-27",
    accepted_version: "", accepted_at: "" } }),
} };
setTimeout = (fn) => { timers.push(fn); return timers.length; };
clearTimeout = () => {};
S.demo = false;
S.prefs = {};
ingestTerms({ version: "2026-10-27", accepted_version: "", accepted_at: "" });
document.querySelector("#dlg-terms-document-version").textContent = "2026-10-27";
document.querySelector("#btn-terms-accept").click();
timers[0]();
document.querySelector("#btn-terms-recheck").click();
await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
const recheck = document.querySelector("#btn-terms-recheck");
out.push({ open: document.querySelector("#dlg-terms").open,
  locked: termsLocked(), accepted,
  acceptDisabled: document.querySelector("#btn-terms-accept").disabled,
  recheckHidden: recheck.hidden,
  escText: document.querySelector("#dlg-terms .help-esc").textContent,
  status: document.querySelector("#dlg-terms-status").textContent });
""")
        case = res[0]
        self.assertTrue(case["open"])
        self.assertTrue(case["locked"], "明确未同意门禁保持")
        self.assertFalse(case["acceptDisabled"], "恢复同意按钮供重试")
        self.assertTrue(case["recheckHidden"], "未确认态收口后收起重新确认")
        self.assertEqual(case["escText"], "Esc 暂不使用")
        self.assertIn("尚未记录同意", case["status"])
        self.assertEqual(case["accepted"], 1)

    def test_recheck_read_failure_keeps_unconfirmed(self):
        """重新确认读取失败：仍按未确认处理，按钮恢复可点，门禁保持。"""
        res = run_js(r"""
let timers = [], accepted = 0;
window.pywebview = { api: {
  accept_terms: () => { accepted += 1; return new Promise(() => {}); },
  bootstrap: () => Promise.reject(new Error("桥接断了")),
} };
setTimeout = (fn) => { timers.push(fn); return timers.length; };
clearTimeout = () => {};
S.demo = false;
S.prefs = {};
ingestTerms({ version: "2026-10-27", accepted_version: "", accepted_at: "" });
document.querySelector("#dlg-terms-document-version").textContent = "2026-10-27";
document.querySelector("#btn-terms-accept").click();
timers[0]();
document.querySelector("#btn-terms-recheck").click();
await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
const recheck = document.querySelector("#btn-terms-recheck");
out.push({ open: document.querySelector("#dlg-terms").open,
  locked: termsLocked(), accepted,
  acceptDisabled: document.querySelector("#btn-terms-accept").disabled,
  recheckHidden: recheck.hidden, recheckDisabled: recheck.disabled,
  recheckText: recheck.textContent,
  status: document.querySelector("#dlg-terms-status").textContent });
""")
        case = res[0]
        self.assertTrue(case["open"])
        self.assertTrue(case["locked"])
        self.assertTrue(case["acceptDisabled"], "读取失败不恢复同意按钮")
        self.assertFalse(case["recheckHidden"])
        self.assertFalse(case["recheckDisabled"], "重新确认恢复可点")
        self.assertEqual(case["recheckText"], "重新确认状态")
        self.assertIn("没能读到", case["status"])
        self.assertIn("未确认", case["status"])
        self.assertEqual(case["accepted"], 1)

    def test_readonly_view_makes_no_writes_and_restores_help_focus(self):
        """已同意查看：只读（只显示关闭），关闭零 accept/save/学校调用；
        来源是帮助时打开期间先关帮助、关闭后重开并聚焦「查看条款」，不叠弹窗。"""
        res = run_js(r"""
let calls = [];
window.pywebview = { api: {
  accept_terms: () => { calls.push("accept"); return Promise.resolve({ ok: true }); },
  save_prefs: () => { calls.push("save"); return Promise.resolve({ ok: true }); },
  check: () => { calls.push("check"); return new Promise(() => {}); },
  list_activities: () => { calls.push("list"); return new Promise(() => {}); },
} };
S.demo = false;
S.prefs = {};
ingestTerms({ version: "2026-10-27", accepted_version: "2026-10-27",
  accepted_at: "2026-10-27 12:00:00" });
const help = document.querySelector("#help");
help.showModal();
document.querySelector("#btn-help-terms").click();
const helpClosedByTerms = !help.open;
const dlg = document.querySelector("#dlg-terms");
const accept = document.querySelector("#btn-terms-accept");
const decline = document.querySelector("#btn-terms-decline");
const escBefore = dlg.querySelector(".help-esc").textContent;
dlg.close();
(dlg.listeners.close || []).forEach((fn) => fn());
out.push({ helpClosedByTerms, calls, declined: S.terms.declined,
  acceptHidden: accept.hidden, declineHidden: decline.hidden,
  closeHidden: document.querySelector("#btn-terms-close").hidden,
  recheckHidden: document.querySelector("#btn-terms-recheck").hidden,
  xDisabled: dlg.querySelector(".dlg-x").disabled, escBefore,
  helpOpen: help.open,
  focus: document.activeElement && document.activeElement.__sel,
  lockedAfter: termsLocked() });
""")
        case = res[0]
        self.assertTrue(case["helpClosedByTerms"], "打开条款时帮助先关掉，不叠两个弹窗")
        self.assertEqual(case["calls"], [], "只读查看零 accept/save/学校调用")
        self.assertFalse(case["declined"], "只读关闭不记拒绝")
        self.assertTrue(case["acceptHidden"])
        self.assertTrue(case["declineHidden"])
        self.assertFalse(case["closeHidden"], "只读只显示关闭")
        self.assertTrue(case["recheckHidden"])
        self.assertFalse(case["xDisabled"])
        self.assertEqual(case["escBefore"], "Esc 关闭")
        self.assertTrue(case["helpOpen"], "关闭后帮助重开")
        self.assertEqual(case["focus"], "#btn-help-terms", "焦点回「查看条款」入口")
        self.assertFalse(case["lockedAfter"])

    def test_view_toggle_never_saves(self):
        """要点⇄全文切换零保存、零学校调用，按钮文案/aria/阅读区标题三处同步。"""
        res = run_js(r"""
let calls = [];
window.pywebview = { api: {
  save_prefs: () => { calls.push("save"); return Promise.resolve({ ok: true }); },
  accept_terms: () => { calls.push("accept"); return Promise.resolve({ ok: true }); },
} };
S.demo = false;
S.prefs = {};
ingestTerms({ version: "2026-10-27", accepted_version: "", accepted_at: "" });
const btn = document.querySelector("#btn-terms-full");
const full = document.querySelector("#dlg-terms-full-text");
const body = document.querySelector(".terms-body");
body.scrollTop = 40;
btn.click();
const mid = { hidden: full.hidden, text: btn.textContent,
  title: document.querySelector("#dlg-terms-view-title").textContent,
  aria: btn.getAttribute("aria-expanded"), scrollTop: body.scrollTop };
btn.click();
out.push({ mid, back: { hidden: full.hidden, text: btn.textContent,
    title: document.querySelector("#dlg-terms-view-title").textContent,
    aria: btn.getAttribute("aria-expanded") },
  calls, open: document.querySelector("#dlg-terms").open });
""")
        case = res[0]
        self.assertFalse(case["mid"]["hidden"])
        self.assertEqual(case["mid"]["text"], "返回条款要点")
        self.assertEqual(case["mid"]["title"], "完整条款")
        self.assertEqual(case["mid"]["aria"], "true")
        self.assertEqual(case["mid"]["scrollTop"], 0, "切到全文滚回顶部")
        self.assertTrue(case["back"]["hidden"])
        self.assertEqual(case["back"]["text"], "查看完整条款")
        self.assertEqual(case["back"]["title"], "条款要点")
        self.assertEqual(case["back"]["aria"], "false")
        self.assertEqual(case["calls"], [], "切换视图绝不保存")
        self.assertTrue(case["open"])

    def test_close_without_acceptance_keeps_decline_semantics(self):
        """未同意关闭（Esc/X 同一条 close 收口）仍记「暂不使用」：
        版本标记落盘、门禁保持、焦点回 #btn-help。"""
        res = run_js(r"""
let saved = [];
window.pywebview = { api: {
  save_prefs: (patch) => { saved.push(JSON.parse(JSON.stringify(patch))); return Promise.resolve({ ok: true }); },
} };
S.demo = false;
S.prefs = {};
ingestTerms({ version: "2026-10-27", accepted_version: "", accepted_at: "" });
const dlg = document.querySelector("#dlg-terms");
const cancelEv = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
(dlg.listeners.cancel || []).forEach((fn) => fn(cancelEv));
dlg.close();
(dlg.listeners.close || []).forEach((fn) => fn());
out.push({ prevented: cancelEv.defaultPrevented, declined: S.terms.declined,
  saved, locked: termsLocked(),
  banner: document.querySelector("#banners").innerHTML,
  focus: document.activeElement && document.activeElement.__sel });
""")
        case = res[0]
        self.assertFalse(case["prevented"], "未保存时 Esc 放行原生关闭")
        self.assertTrue(case["declined"])
        self.assertTrue(any(p.get("terms_declined_version") == "2026-10-27"
                            for p in case["saved"]), "拒绝版本必须落盘")
        self.assertTrue(case["locked"], "拒绝后门禁保持")
        self.assertIn("使用条款", case["banner"])
        self.assertEqual(case["focus"], "#btn-help", "自动来源焦点回 #btn-help")

    def test_close_restores_banner_focus_without_stacking_dialogs(self):
        """来源是横幅：关闭回原按钮；随后自动打开再关闭回 #btn-help。"""
        res = run_js(r"""
window.pywebview = { api: {
  save_prefs: () => Promise.resolve({ ok: true }),
} };
S.demo = false;
S.prefs = {};
S.terms.prompted = true; // 同会话已提示过：防止 ingestTerms 自动首弹抢先
                         // 登记来源，让横幅打开成为真实来源
ingestTerms({ version: "2026-10-27", accepted_version: "", accepted_at: "" });
const dlg = document.querySelector("#dlg-terms");
const back = document.querySelector("#btn-plan");
const beforeBannerOpen = dlg.open;
openTerms("banner", back);
const bannerOpen = dlg.open;
const bannerSource = S.terms.source;
const bannerBackIsPlan = S.terms.focusBack === back;
const seqAfterBanner = S.terms.requestSeq;
openTerms("auto"); // 守卫：已打开时不重置任何登记（保护首来源）
const guardSource = S.terms.source;
const guardBackIsPlan = S.terms.focusBack === back;
const guardSeq = S.terms.requestSeq;
dlg.close();
(dlg.listeners.close || []).forEach((fn) => fn());
const afterBanner = document.activeElement && document.activeElement.__sel;
openTerms();
dlg.close();
(dlg.listeners.close || []).forEach((fn) => fn());
const afterAuto = document.activeElement && document.activeElement.__sel;
out.push({ beforeBannerOpen, bannerOpen, bannerSource, bannerBackIsPlan,
  seqAfterBanner, guardSource, guardBackIsPlan, guardSeq,
  afterBanner, afterAuto, declined: S.terms.declined,
  helpOpen: document.querySelector("#help").open });
""")
        case = res[0]
        self.assertFalse(case["beforeBannerOpen"], "prompted 已置：ingestTerms 不得自动首弹")
        self.assertTrue(case["bannerOpen"], "横幅打开必须真实打开弹窗")
        self.assertEqual(case["bannerSource"], "banner", "打开时必须登记横幅来源")
        self.assertTrue(case["bannerBackIsPlan"], "打开时必须登记被点按钮为焦点返回点")
        self.assertEqual(case["guardSource"], "banner", "守卫不得覆盖首来源登记")
        self.assertTrue(case["guardBackIsPlan"], "守卫不得清空焦点返回点")
        self.assertEqual(case["guardSeq"], case["seqAfterBanner"],
                         "守卫空操作：不得再次作废请求代号")
        self.assertEqual(case["afterBanner"], "#btn-plan", "横幅来源回被点的按钮")
        self.assertEqual(case["afterAuto"], "#btn-help", "自动来源回 #btn-help")
        self.assertTrue(case["declined"])
        self.assertFalse(case["helpOpen"], "帮助没被误开（不叠弹窗）")

    def test_boot_defers_intro_until_terms_accepted(self):
        """真实 boot 首次 prefs 空且未同意：只弹条款、首轮学校加载被拦、
        欢迎与引导都延后；同意后引导只继续一次，绝不伪造 tour_version。"""
        res = run_js(r"""
let calls = [], saved = [];
window.pywebview = { api: {
  bootstrap: () => { calls.push("bootstrap");
    return Promise.resolve({ demo: false, prefs: {},
      terms: { version: "2026-10-27", accepted_version: "", accepted_at: "" } }); },
  save_prefs: (patch) => { saved.push(JSON.parse(JSON.stringify(patch))); return Promise.resolve({ ok: true }); },
  front_ready: () => { calls.push("front_ready"); return Promise.resolve({ ok: true }); },
  clock_snapshot: () => new Promise(() => {}),
  check: () => { calls.push("check"); return new Promise(() => {}); },
  list_activities: () => { calls.push("list"); return new Promise(() => {}); },
  accept_terms: (v) => { calls.push("accept:" + v);
    return Promise.resolve({ ok: true, terms: { version: "2026-10-27",
      accepted_version: "2026-10-27", accepted_at: "2026-10-27 12:00:00" } }); },
} };
await boot();
const schoolCalls = () => calls.filter((c) => c === "check" || c === "list");
const atBoot = { termsOpen: document.querySelector("#dlg-terms").open,
  helpOpen: document.querySelector("#help").open, tourOpen: S.tour.open,
  deferred: JSON.parse(JSON.stringify(S.terms.deferredIntro || null)),
  schoolCalls: schoolCalls() };
document.querySelector("#dlg-terms-document-version").textContent = "2026-10-27";
document.querySelector("#btn-terms-accept").click();
await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
const afterAccept = { tourOpen: S.tour.open,
  helpOpen: document.querySelector("#help").open,
  deferred: S.terms.deferredIntro, schoolCalls: schoolCalls(),
  tourVersionWritten: saved.some((p) => "tour_version" in p),
  welcomeWritten: saved.some((p) => "welcome_seen" in p),
  locked: termsLocked() };
out.push({ atBoot, afterAccept });
""")
        at_boot, after = res[0]["atBoot"], res[0]["afterAccept"]
        self.assertTrue(at_boot["termsOpen"], "首次未同意只弹条款")
        self.assertFalse(at_boot["helpOpen"], "欢迎延后")
        self.assertFalse(at_boot["tourOpen"], "引导延后")
        self.assertEqual(at_boot["deferred"], {"tour": True, "welcome": False})
        self.assertEqual(at_boot["schoolCalls"], [], "首轮学校加载被条款拦下")
        self.assertTrue(after["tourOpen"], "同意后引导继续一次")
        self.assertFalse(after["helpOpen"], "本例引导适用，不再叠欢迎")
        self.assertIsNone(after["deferred"], "延后项消费后清空（只继续一次）")
        self.assertEqual(after["schoolCalls"], ["check", "list"], "同意后补一次加载")
        self.assertFalse(after["tourVersionWritten"], "绝不伪造 tour_version")
        self.assertTrue(after["welcomeWritten"], "首次欢迎标记仍按原语义落盘")
        self.assertFalse(after["locked"])

    def test_decline_after_deferred_boot_does_not_fake_tour_completion(self):
        """延后状态下关闭（暂不使用）：本会话不自动开引导、不写 tour_version，
        引导完成状态保持未写（下次仍可正常判定）。"""
        res = run_js(r"""
let saved = [];
window.pywebview = { api: {
  bootstrap: () => Promise.resolve({ demo: false, prefs: {},
    terms: { version: "2026-10-27", accepted_version: "", accepted_at: "" } }),
  save_prefs: (patch) => { saved.push(JSON.parse(JSON.stringify(patch))); return Promise.resolve({ ok: true }); },
  front_ready: () => Promise.resolve({ ok: true }),
  clock_snapshot: () => new Promise(() => {}),
} };
await boot();
const dlg = document.querySelector("#dlg-terms");
dlg.close();
(dlg.listeners.close || []).forEach((fn) => fn());
out.push({ declined: S.terms.declined, tourOpen: S.tour.open,
  helpOpen: document.querySelector("#help").open,
  tourVersionWritten: saved.some((p) => "tour_version" in p),
  tourStillEligible: tourShouldAutoStart(),
  declinedSaved: saved.some((p) => p.terms_declined_version === "2026-10-27"),
  focus: document.activeElement && document.activeElement.__sel });
""")
        case = res[0]
        self.assertTrue(case["declined"])
        self.assertFalse(case["tourOpen"], "拒绝后本会话不自动开引导")
        self.assertFalse(case["helpOpen"])
        self.assertFalse(case["tourVersionWritten"], "拒绝不伪造引导完成")
        self.assertTrue(case["tourStillEligible"], "引导完成状态保持未写")
        self.assertTrue(case["declinedSaved"])
        self.assertEqual(case["focus"], "#btn-help")

    def test_terms_dialog_keyboard_does_not_penetrate_background(self):
        """打开的条款弹窗独占键盘：Enter/斜杠/Ctrl+F/F5 不穿透（不 preventDefault、
        不抢搜索、不刷新），引导可见时 Esc 也不跳过引导。"""
        res = run_js(r"""
S.demo = false;
S.prefs = {};
ingestTerms({ version: "2026-10-27", accepted_version: "", accepted_at: "" });
const eEnter = __dispatchKey({ key: "Enter" });
const eSlash = __dispatchKey({ key: "/" });
const eCtrlF = __dispatchKey({ key: "f", ctrlKey: true });
const eF5 = __dispatchKey({ key: "F5" });
const focusAfterSearch = document.activeElement && document.activeElement.__sel;
/* 引导强行可见：Esc 不许跳过引导、不许写 tour_version */
S.tour.open = true;
S.tour.paused = false;
S.terms.tourYield = false;
const eEsc = __dispatchKey({ key: "Escape" });
out.push({ enterPrevented: eEnter.defaultPrevented,
  slashPrevented: eSlash.defaultPrevented,
  ctrlFPrevented: eCtrlF.defaultPrevented,
  f5Prevented: eF5.defaultPrevented, escPrevented: eEsc.defaultPrevented,
  focusAfterSearch, tourOpen: S.tour.open,
  tourVersionWritten: "tour_version" in S.prefs,
  termsOpen: document.querySelector("#dlg-terms").open });
""")
        case = res[0]
        self.assertFalse(case["enterPrevented"], "Enter 保留原生语义，不触发开始计划")
        self.assertFalse(case["slashPrevented"], "斜杠不抢搜索框")
        self.assertFalse(case["ctrlFPrevented"], "Ctrl+F 不抢搜索框")
        self.assertFalse(case["f5Prevented"], "保存外的重载保留原生（不强拦）")
        self.assertFalse(case["escPrevented"], "条款打开时 Esc 不穿透到引导")
        self.assertEqual(case["focusAfterSearch"], "#dlg-terms-title",
                         "焦点停在条款标题，没被搜索抢走")
        self.assertTrue(case["tourOpen"], "引导没被条款内的 Esc 跳过")
        self.assertFalse(case["tourVersionWritten"])
        self.assertTrue(case["termsOpen"])

    def test_terms_dialog_static_contract_native_close_and_anchors(self):
        """静态守护：X 走原生 method=dialog 关闭（由 close 监听统一收口）、
        阅读区/反馈/底栏结构与版本节点齐备。"""
        html = (LOCAL / "ui" / "index.html").read_text(encoding="utf-8")
        start = html.index('id="dlg-terms"')
        block = html[start:html.index("</dialog>", start)]
        self.assertIn('method="dialog"', block, "右上角 X 必须走原生关闭")
        self.assertEqual(block.count('id="dlg-terms-document-version"'), 1)
        self.assertEqual(block.count('id="dlg-terms-status"'), 1)
        for anchor in ("terms-head", "terms-toolbar", "terms-body",
                       "terms-feedback", "terms-foot", "terms-section"):
            self.assertIn(anchor, block, "设计稿结构区缺失：" + anchor)
        self.assertIn('id="dlg-terms-view-title"', block)
        self.assertIn('id="btn-terms-recheck"', block)
        self.assertIn('id="btn-terms-close"', block)
        self.assertIn('id="btn-terms-full"', block)

    def test_terms_status_feedback_resets_reading_scroll_zero_side_effects(self):
        """termsStatus 反馈与阅读区滚动（只定义、待授权运行）：
        非空消息把唯一阅读区 scrollTop 归零且文本/tone/hidden 正确；
        清空分支提前 return，只隐藏状态、不动滚动；
        焦点、全文展开态、条款请求代号全部不变，且反馈路径零桥调用。"""
        res = run_js(r"""
          // 本用例只验证 termsStatus 反馈，不走真实桥接：harness 不预置 pywebview，
          // 先给五个键本地 no-op Promise 桩让包装器可挂接；任何意外调用都会被记录。
          window.pywebview = { api: {
            accept_terms: () => Promise.resolve({ ok: true }),
            save_prefs: () => Promise.resolve({ ok: true }),
            bootstrap: () => Promise.resolve({}),
            check: () => Promise.resolve({ ok: true }),
            list_activities: () => Promise.resolve({ ok: true, items: [] }),
          } };
          const body = $(".terms-body");
          const status = $("#dlg-terms-status");
          const full = $("#dlg-terms-full-text");
          const bridge = window.pywebview.api;
          const bridgeCalls = [];
          ["accept_terms", "save_prefs", "bootstrap", "check", "list_activities"].forEach((key) => {
            const fn = bridge[key];
            bridge[key] = (...args) => { bridgeCalls.push(key); return fn.apply(bridge, args); };
          });
          body.scrollTop = 200;
          const focusBefore = document.activeElement;
          const seqBefore = S.terms.requestSeq;
          const fullHiddenBefore = full.hidden;
          termsStatus("保存结果未确认：请点「重新确认状态」核对本机记录。", "warn");
          out.push({
            scrollTop: body.scrollTop,
            text: status.textContent,
            tone: status.dataset.tone,
            hidden: status.hidden,
            focusUnchanged: document.activeElement === focusBefore,
            seqUnchanged: S.terms.requestSeq === seqBefore,
            fullHiddenUnchanged: full.hidden === fullHiddenBefore
          });
          body.scrollTop = 100;
          termsStatus("", "warn");
          out.push({
            scrollTop: body.scrollTop,
            hidden: status.hidden,
            text: status.textContent
          });
          out.push({ bridgeCalls: bridgeCalls });
        """)
        shown, cleared, calls = res[0], res[1], res[2]
        self.assertEqual(shown["scrollTop"], 0, "非空反馈必须把阅读区滚回顶部")
        self.assertIn("保存结果未确认", shown["text"], "状态文本必须如实展示反馈消息")
        self.assertEqual(shown["tone"], "warn", "tone 必须按传入值展示")
        self.assertFalse(shown["hidden"], "非空反馈必须可见")
        self.assertTrue(shown["focusUnchanged"], "反馈不许碰焦点")
        self.assertTrue(shown["seqUnchanged"], "反馈不许改条款请求代号")
        self.assertTrue(shown["fullHiddenUnchanged"], "反馈不许动全文展开态")
        self.assertEqual(cleared["scrollTop"], 100,
                         "清空分支必须提前 return，不能归零滚动")
        self.assertTrue(cleared["hidden"], "清空后状态必须隐藏")
        self.assertEqual(cleared["text"], "", "清空后状态文本必须置空")
        self.assertEqual(calls["bridgeCalls"], [], "反馈路径必须零桥调用")


if __name__ == "__main__":
    unittest.main()
