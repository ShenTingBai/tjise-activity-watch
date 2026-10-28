"""后端条款门禁的隔离测试（plan-20261027-distribution-contract-repair）。

不 `import app.py`：它的顶层会 import webview 并经 runtime_paths 触碰数据根，
与「不读真实登录文件、不起真实 worker」的边界冲突。这里用 ast 抽出 Api 的
相关方法，注入 DEMO / TERMS_VERSION / load_prefs / threading 等假件后单独
求值，拿到的是**真实方法的返回值**，不是源码子串的推测。

覆盖：授权 helper、terms_required 返回、学校动作（check / list_activities /
login / grab / start_plan）在未授权与已授权两种状态下的行为、start_plan 的
幂等重放优先于门禁、demo grab 不启动真实 worker，以及非学校动作不被门禁误伤；
另有 accept_terms 的版本绑定——不匹配输入在写前拒绝、零副作用。
"""
from __future__ import annotations

import ast
import re
import threading
import time
import types
import unittest
from pathlib import Path

LOCAL = Path(__file__).resolve().parents[1]

# 与 local/app.py 的 TERMS_VERSION 同源的测试常量：任何一处漂移都会让
# 「已同意版本 == 当前版本」的判定失效，测试就是要盯住这一点。
# r2：条款修订（数据目录与迁移披露）后必须重新同意，r1 的同意记录不再放行。
TEST_TERMS = "2026-10-27-r2"

SCHOOL_METHODS = ("check", "list_activities", "login", "grab", "start_plan")
HELPER_METHODS = ("_terms_gate", "_remember_start", "_start_replay")
# 明确不在门禁范围内的公开桥接：停止/取消、帮助、数据目录、更新检查、偏好与同意
NON_SCHOOL_METHODS = ("stop_plan", "cancel_task", "cancel_start_request",
                      "help", "open_data", "check_update", "save_prefs",
                      "accept_terms", "start_request_state")


def _read(rel: str) -> str:
    return (LOCAL / rel).read_text(encoding="utf-8")


def _function_node(name: str):
    tree = ast.parse(_read("app.py"))
    node = next(item for item in ast.walk(tree)
                if isinstance(item, ast.FunctionDef) and item.name == name)
    return node


def _module_source(*names: str) -> str:
    """把若干方法拼成一个可执行的模块源码（保持真实实现，不改写）。"""
    parts = []
    for name in names:
        parts.append(ast.unparse(_function_node(name)))
    return "\n\n".join(parts)


class _FakeThread:
    """只登记、不起真实线程：测试要断言的是「有没有被排上」。"""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self.target = target
        self.args = args or ()

    def start(self):
        STARTED_TARGETS.append(self.target)


class _FakeThreading:
    Thread = _FakeThread


STARTED_TARGETS: list = []


class _FakeApi:
    """Api 的最小替身：只提供被抽出方法真正会用到的属性。"""

    # 与生产 Api.START_KEEP 同值：真实 _remember_start 会按它裁剪 start_plan
    # 登记队列，替身缺这个类属性会让每次登记都抛 AttributeError（假报错）
    START_KEEP = 32

    def __init__(self):
        self._lock = threading.RLock()
        self._closing = False
        self._checking = False
        self._listing = False
        self._check_request_id = ""
        self._list_request_id = ""
        self._check_last = {}
        self._list_last = {}
        self._start_requests = {}
        self._start_order = []
        self._plan = None
        self.spawned = []
        self.inner_calls = 0
        self.inner_result = {"ok": True, "plan_id": "p-test"}

    def _do_check(self):
        self._checking = False

    def _do_list(self):
        self._listing = False

    def _spawn(self, args, label, kind="grab"):
        self.spawned.append({"args": list(args), "label": label, "kind": kind})
        return {"ok": True}

    def _start_plan_inner(self, items, headed, retry_policy, request_id=""):
        self.inner_calls += 1
        return dict(self.inner_result)


def build_api(prefs: dict, demo: bool = False) -> _FakeApi:
    """按给定 ui.json 偏好构造一个装好真实方法的 Api 替身。"""
    namespace = {
        "DEMO": demo,
        "TERMS_VERSION": TEST_TERMS,
        "load_prefs": lambda: dict(prefs or {}),
        "re": re,
        "time": time,
        "threading": _FakeThreading,
        "worker_command": lambda *args: ["worker"] + list(args),
    }
    # terms_state / terms_unknown_payload 是模块级纯函数：直接取真实实现
    helpers = ast.Module(body=[_function_node("terms_state"),
                               _function_node("terms_unknown_payload")],
                         type_ignores=[])
    ast.fix_missing_locations(helpers)
    exec(compile(helpers, "<terms-helpers>", "exec"), namespace)  # noqa: S102

    methods = ast.Module(body=[_function_node(name)
                               for name in HELPER_METHODS + SCHOOL_METHODS],
                         type_ignores=[])
    ast.fix_missing_locations(methods)
    exec(compile(methods, "<terms-gate>", "exec"), namespace)  # noqa: S102

    api = _FakeApi()
    for name in HELPER_METHODS + SCHOOL_METHODS:
        setattr(api, name, types.MethodType(namespace[name], api))
    return api


class TermsGateHelperTests(unittest.TestCase):
    """授权 helper：只有「已同意当前版本」才放行。"""

    def test_gate_passes_only_for_current_version(self):
        accepted = build_api({"terms_version": TEST_TERMS,
                              "terms_accepted_at": "2026-10-27 19:00:00"})
        self.assertIsNone(accepted._terms_gate(), "已同意当前版本必须放行")

        # 旧 r1 的同意记录在 r2 下必须被拒：版本变化要求重新点击同意
        old = build_api({"terms_version": "2026-10-27-r1"})
        gate = old._terms_gate()
        self.assertIsNotNone(gate)
        self.assertEqual(gate["error"], "terms_required")
        self.assertEqual(gate["terms"]["accepted_version"], "2026-10-27-r1")
        self.assertEqual(gate["terms"]["version"], TEST_TERMS)
        self.assertIn("查看条款", gate["text"])

        empty = build_api({})
        self.assertIsNotNone(empty._terms_gate(), "没同意过必须拦住")

    def test_gate_carries_request_id_for_reconciliation(self):
        api = build_api({})
        gate = api._terms_gate("start-1")
        self.assertEqual(gate["request_id"], "start-1",
                         "拒绝也要带原 request_id，界面按 ID 对账而不是当新一轮")
        self.assertNotIn("request_id", api._terms_gate(), "没有 ID 时不伪造")

    def test_demo_bypasses_gate(self):
        self.assertIsNone(build_api({}, demo=True)._terms_gate(),
                          "演示模式完全离线，不受条款门禁限制")

    def test_gate_never_touches_login_files_or_logs(self):
        source = ast.unparse(_function_node("_terms_gate"))
        self.assertIn("load_prefs", source)
        for banned in ("SESSION_FILE", "STORAGE_STATE", "storage_state",
                       "session_path", "addLog", "_trace", "grab.log"):
            self.assertNotIn(banned, source, "门禁不得读写登录文件或写运行日志")


class SchoolActionGateTests(unittest.TestCase):
    """五个学校动作：未授权一律不起线程、worker、学校请求或计划。"""

    def setUp(self):
        STARTED_TARGETS.clear()

    def test_unauthorized_check_and_list_do_not_start_threads(self):
        api = build_api({})
        for rid in ("check-1", "list-1"):
            got = api.check(rid)
            self.assertEqual(got["error"], "terms_required")
            self.assertEqual(got["queued"], False)
            self.assertEqual(got["request_id"], rid)
        got = api.list_activities("list-9")
        self.assertEqual(got["error"], "terms_required")
        self.assertEqual(STARTED_TARGETS, [], "未获授权不得排上任何后台线程")
        self.assertFalse(api._checking)
        self.assertFalse(api._listing)

    def test_unauthorized_login_and_grab_do_not_spawn_worker(self):
        api = build_api({})
        self.assertEqual(api.login()["error"], "terms_required")
        self.assertEqual(api.grab("a1", "活动")["error"], "terms_required")
        self.assertEqual(api.spawned, [], "未获授权不得起真实 worker")

    def test_unauthorized_start_plan_leaves_no_pending_and_no_plan(self):
        api = build_api({})
        got = api.start_plan([{"id": "a1"}], False, None, "start-7")
        self.assertEqual(got["error"], "terms_required")
        self.assertEqual(got["request_id"], "start-7")
        self.assertEqual(api.inner_calls, 0, "未获授权不得进入计划构建")
        self.assertEqual(api._start_requests["start-7"]["state"], "rejected",
                         "本次登记必须收成 rejected，不能留下悬空 pending")

    def test_accepted_version_releases_school_actions(self):
        api = build_api({"terms_version": TEST_TERMS})
        self.assertTrue(api.check("c1")["queued"])
        self.assertTrue(api.list_activities("l1")["queued"])
        self.assertTrue(api._checking, "放行后要真的进入检查态")
        self.assertTrue(api._listing)
        self.assertEqual(len(STARTED_TARGETS), 2, "放行后两个后台线程都要排上")
        self.assertEqual(api.login(), {"ok": True})
        self.assertEqual(len(api.spawned), 1, "放行后登录才起 worker")
        got = api.start_plan([{"id": "a1"}], False, None, "start-8")
        self.assertTrue(got["ok"])
        self.assertEqual(api.inner_calls, 1)

    def test_start_plan_replay_precedes_gate(self):
        """同一 request_id 的既有结论先重放：门禁不得改写已接纳/已拒绝的结论。"""
        api = build_api({})
        api._remember_start("rid-accepted", "accepted",
                            plan_id="p-old", result={"ok": True, "plan_id": "p-old"})
        api._remember_start("rid-rejected", "rejected",
                            result={"ok": False, "text": "上次被拒绝"})
        accepted = api.start_plan([{"id": "a1"}], False, None, "rid-accepted")
        self.assertTrue(accepted.get("duplicate"), "已接纳请求必须重放原结论")
        self.assertEqual(accepted["plan_id"], "p-old")
        rejected = api.start_plan([{"id": "a1"}], False, None, "rid-rejected")
        self.assertTrue(rejected.get("duplicate"))
        self.assertEqual(api.inner_calls, 0, "重放不得重复建计划")

    def test_start_plan_without_request_id_still_gated(self):
        api = build_api({})
        got = api.start_plan([{"id": "a1"}])
        self.assertEqual(got["error"], "terms_required")
        self.assertEqual(api.inner_calls, 0)


class DemoOfflineTests(unittest.TestCase):
    """demo 语义：不联网、不起真实 worker。"""

    def test_demo_grab_returns_offline_result_without_worker(self):
        api = build_api({}, demo=True)
        got = api.grab("a1", "活动")
        self.assertFalse(got["ok"])
        self.assertTrue(got["demo"])
        self.assertIn("演示模式", got["text"])
        self.assertEqual(api.spawned, [], "demo 的 grab 绝不能起真实 worker")

    def test_demo_login_keeps_offline_answer(self):
        api = build_api({}, demo=True)
        got = api.login()
        self.assertTrue(got.get("demo"))
        self.assertEqual(api.spawned, [])


class NonSchoolActionContractTests(unittest.TestCase):
    """门禁范围之外：停止、取消、帮助、数据目录、更新检查、偏好与同意本身。"""

    def test_non_school_methods_have_no_terms_gate(self):
        for name in NON_SCHOOL_METHODS:
            source = ast.unparse(_function_node(name))
            self.assertNotIn("_terms_gate", source,
                             "%s 不是学校网络动作，不得被条款门禁误伤" % name)

    def test_school_methods_all_call_the_gate(self):
        for name in SCHOOL_METHODS:
            source = ast.unparse(_function_node(name))
            self.assertIn("_terms_gate", source,
                          "%s 是学校网络动作，必须有后端授权兜底" % name)


def build_accept_api(prefs: dict, *, update_ok: bool = True):
    """抽出真实 accept_terms：注入内存偏好与 update_prefs spy。

    返回 (api, store, writes, terms_state_fn)：store 是内存偏好（可乱改来模拟
    旧 HTML/混合资源），writes 记录每次真实落盘的补丁，terms_state_fn 用于在
    拒绝后重读快照、确认没有伪造出当前版本的同意。
    """
    store = dict(prefs or {})
    writes: list = []

    def update_prefs(patch):
        # 与生产同语义：拿不到锁返回 False 且一个键都不写
        if not update_ok:
            return False
        writes.append(dict(patch))
        store.update(patch)
        return True

    namespace = {
        "TERMS_VERSION": TEST_TERMS,
        "load_prefs": lambda: dict(store),
        "update_prefs": update_prefs,
        "time": time,
    }
    helpers = ast.Module(body=[_function_node("terms_state")], type_ignores=[])
    ast.fix_missing_locations(helpers)
    exec(compile(helpers, "<terms-helpers>", "exec"), namespace)  # noqa: S102

    method = ast.Module(body=[_function_node("accept_terms")], type_ignores=[])
    ast.fix_missing_locations(method)
    exec(compile(method, "<accept-terms>", "exec"), namespace)  # noqa: S102

    api = _FakeApi()
    api.accept_terms = types.MethodType(namespace["accept_terms"], api)
    return api, store, writes, namespace["terms_state"]


class AcceptTermsWriteTests(unittest.TestCase):
    """同意提交必须绑定实际阅读版本：一切不匹配输入在写前被拒、零副作用。"""

    OLD = {"terms_version": "2026-10-27-r1", "terms_accepted_at": "2026-10-27 10:00:00"}

    def setUp(self):
        STARTED_TARGETS.clear()

    def test_current_version_writes_only_two_keys(self):
        api, store, writes, _ = build_accept_api(dict(self.OLD))
        got = api.accept_terms(TEST_TERMS)
        self.assertTrue(got["ok"], "提交当前版必须成功")
        self.assertEqual(len(writes), 1, "成功只发一次原子写")
        self.assertEqual(set(writes[0]), {"terms_version", "terms_accepted_at"},
                         "只允许写这两个键：不碰登录文件、不写日志")
        self.assertEqual(store["terms_version"], TEST_TERMS)
        self.assertTrue(store["terms_accepted_at"], "同意时间必须落盘")
        self.assertEqual(got["terms"]["accepted_version"], TEST_TERMS,
                         "成功回执与 terms_state 同源")
        self.assertEqual(got["terms"]["version"], TEST_TERMS)

    def test_mismatched_inputs_write_nothing_and_gate_stays_closed(self):
        # 缺参（默认空串）、空串、旧版、未知新版、None、数字、对象、列表
        for bad in ("", "2026-10-27-r1", "2026-10-28-r3", None, 2026,
                    ["2026-10-27-r2"], {"version": TEST_TERMS}):
            api, store, writes, terms_state = build_accept_api(dict(self.OLD))
            got = api.accept_terms(bad)
            self.assertEqual(got["ok"], False, "输入 %r 必须拒绝" % (bad,))
            self.assertEqual(got["error"], "terms_version_mismatch")
            self.assertEqual(writes, [], "输入 %r 不得触发 update_prefs" % (bad,))
            self.assertEqual(store, self.OLD, "内存偏好与原同意时间必须原样")
            self.assertEqual(terms_state(store)["accepted_version"], "2026-10-27-r1",
                             "快照如实反映旧状态，不得伪造当前版同意")
            gate = build_api(dict(store))._terms_gate()
            self.assertIsNotNone(gate, "拒绝后 _terms_gate 仍须拒绝")
            self.assertEqual(gate["error"], "terms_required")
            self.assertEqual(STARTED_TARGETS, [], "拒绝路径不得排任何线程")

    def test_old_html_cannot_forge_current_acceptance(self):
        """旧 HTML（只认识 r1）向当前后端提交：重读 terms_state 不出现 r2。"""
        api, store, writes, terms_state = build_accept_api(dict(self.OLD))
        got = api.accept_terms("2026-10-27-r1")   # 旧副本提交旧版
        self.assertEqual(got["ok"], False)
        self.assertEqual(got["error"], "terms_version_mismatch")
        self.assertEqual(writes, [], "旧副本提交不得留下任何写入")
        snapshot = terms_state(store)
        self.assertNotEqual(snapshot["accepted_version"], TEST_TERMS,
                            "拒绝后不得出现伪造的 r2 同意")
        self.assertEqual(snapshot["accepted_at"], "2026-10-27 10:00:00",
                         "原同意时间不得被改写")

    def test_prefs_busy_keeps_failure_semantics(self):
        api, store, writes, _ = build_accept_api(dict(self.OLD), update_ok=False)
        got = api.accept_terms(TEST_TERMS)
        self.assertEqual(got["ok"], False)
        self.assertEqual(got["error"], "prefs_busy")
        self.assertEqual(writes, [], "拿不到锁时一个键都不写")
        self.assertEqual(store, self.OLD, "偏好保持原样，界面留在弹窗重试")

    def test_rejection_path_has_no_side_effect_calls(self):
        source = ast.unparse(_function_node("accept_terms"))
        # 版本判定先于时间戳与原子写（顺序即证据）
        self.assertLess(source.index("document_version == TERMS_VERSION"),
                        source.index("strftime"), "判定必须先于时间戳生成")
        self.assertLess(source.index("document_version == TERMS_VERSION"),
                        source.index("update_prefs"), "判定必须先于原子写")
        for banned in ("SESSION_FILE", "STORAGE_STATE", "storage_state",
                       "read_text", "_trace", "addLog", "threading", "log"):
            self.assertNotIn(banned, source,
                             "同意不得读写登录文件、写日志或起线程")


if __name__ == "__main__":
    unittest.main()
