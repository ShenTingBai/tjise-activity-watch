#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行时组件与资源解析的契约测试（临时目录 + mock 进程，零学校调用、零真实登录态）。

覆盖：worker 路径解析（源码 grab.py / 冻结资源根，绝不借 EXE 旁旧文件）、
组件失败（缺失/权限/启动异常）时 check/list/login 的恰一次终态收尾、
正常关闭只终止本 Api 登记的进程、--runtime-check 离线探针的 JSON/退出码契约、
构建脚本把 worker 作为内置资源打进主程序（mock 定义）。

边界：这里的 mock 用例**不能**替代真实冻结验收——「仅含单 EXE 的空目录实际由
主程序启动内置 worker --help 且退出 0」必须在授权后用真实冻结包执行（见决策 010）。

运行方式（需用户授权后）：PYTHONPATH=. python -m unittest local.tests.test_runtime_bundle

@author LiXinwen
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

LOCAL = Path(__file__).resolve().parents[1]
ROOT = LOCAL.parent


def _app():
    """导入 local/app.py（webview/pyinstaller 缺失时跳过，不装依赖）。"""
    sys.path.insert(0, str(LOCAL))
    try:
        import app as local_app
    except Exception as exc:
        raise unittest.SkipTest("无法导入 local/app.py：%s" % exc)
    finally:
        sys.path.pop(0)
    return local_app


def read(rel: str) -> str:
    path = ROOT / rel
    assert path.exists(), "契约文件缺失：%s" % path
    return path.read_text(encoding="utf-8")


class WorkerResolutionTests(unittest.TestCase):
    """worker 唯一来源：源码态 grab.py，冻结态资源根 grab-worker.exe。"""

    def setUp(self):
        self.app = _app()

    def test_source_mode_resolves_grab_py(self):
        """开发/源码形态：worker 就是 local/grab.py，随仓库存在。"""
        self.assertEqual(self.app.GRAB, LOCAL / "grab.py")
        self.assertTrue(self.app.GRAB.is_file(), "源码形态必须能找到 grab.py")

    def test_frozen_mode_uses_resource_root_never_exe_sidecar(self):
        """冻结解析只看资源根：EXE 旁即使躺着一个旧 worker 也不借，也不看 cwd。"""
        here = Path("D:/only-exe-dir")
        resource_root = Path("D:/res-root")
        frozen = self.app.resolve_worker_path(True, here, resource_root)
        self.assertEqual(frozen, resource_root / "grab-worker.exe",
                         "冻结态 worker 必须来自打包资源根")
        self.assertNotEqual(frozen, here / "grab-worker.exe",
                            "不得静默借用 EXE 旁旧 worker（旧包混用会掩盖组件缺失）")
        self.assertEqual(self.app.resolve_worker_path(False, here, resource_root),
                         here / "grab.py", "源码态与资源根无关")

    def test_worker_problem_codes(self):
        """组件自检三码：worker_missing / worker_permission / 空串=齐备。"""
        app = self.app
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            with mock.patch.object(app, "GRAB", tmp / "nope.py"):
                self.assertEqual(app.worker_problem(), "worker_missing")
            readable = tmp / "grab.py"
            readable.write_text("# source", encoding="utf-8")
            with mock.patch.object(app, "GRAB", readable):
                self.assertEqual(app.worker_problem(), "", "可读即齐备（源码态）")
                with mock.patch("os.access", lambda p, m: False):
                    self.assertEqual(app.worker_problem(), "worker_permission")
            exe = tmp / "grab-worker.exe"
            exe.write_bytes(b"MZ")
            with mock.patch.object(sys, "frozen", True, create=True), \
                    mock.patch.object(app, "GRAB", exe), \
                    mock.patch("os.access", lambda p, m: True):
                self.assertEqual(app.worker_problem(), "", "冻结态存在且可访问即齐备")
            with mock.patch.object(sys, "frozen", True, create=True), \
                    mock.patch.object(app, "GRAB", tmp / "gone.exe"):
                self.assertEqual(app.worker_problem(), "worker_missing")

    def test_component_error_mapping(self):
        """Popen/run 异常 → 结构化错误码：FileNotFound→missing / Permission→permission / 其余→spawn。"""
        app = self.app
        self.assertEqual(app._component_error(FileNotFoundError()), "worker_missing")
        self.assertEqual(app._component_error(PermissionError()), "worker_permission")
        self.assertEqual(app._component_error(OSError("boom")), "spawn_failed")


class ComponentFailureCloseoutTests(unittest.TestCase):
    """check/list/login 的组件失败收尾：恰一次终态、busy 清位、零学校调用、会话不变。"""

    def setUp(self):
        self.app = _app()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.session = self.tmp / "session.json"
        for patch in (mock.patch.object(self.app, "SESSION_FILE", self.session),
                      mock.patch.object(self.app, "DEMO", False)):
            patch.start()
            self.addCleanup(patch.stop)

    def _api_with_events(self):
        api = self.app.Api()
        events = []
        api._emit = lambda kind, payload=None: events.append((kind, payload))
        return api, events

    def _missing_worker(self) -> Path:
        return self.tmp / "no-such-worker.exe"

    def test_check_worker_missing_emits_unknown_once(self):
        """check 遇缺组件：恰一次 check 终态（unknown + runtime_error），检查位清零。"""
        api, events = self._api_with_events()
        cmd = mock.Mock(side_effect=lambda *a: [str(self._missing_worker())])
        with mock.patch.object(self.app, "worker_command", cmd):
            api._do_check()
        checks = [payload for kind, payload in events if kind == "check"]
        self.assertEqual(len(checks), 1, "check 必须恰发一次终态事件")
        payload = checks[0]
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["result"], "unknown",
                         "组件失败不得冒充登录四态里的 expired/none")
        self.assertEqual(payload["runtime_error"], "worker_missing")
        self.assertEqual(api._last_check["result"], "unknown")
        self.assertEqual(api._last_check["runtime_error"], "worker_missing")
        self.assertFalse(api._checking, "finally 必须清「检查中」标记")
        self.assertEqual(cmd.call_args_list, [mock.call("check")],
                         "只允许分发 check，不得顺势调 list/login（零学校调用）")
        self.assertFalse(self.session.exists(), "组件失败路径不得创建/改写会话文件")

    def test_check_worker_missing_logs_technical_detail(self):
        """技术细节只进运行记录（log 事件），主文案保持人话。"""
        api, events = self._api_with_events()
        with mock.patch.object(self.app, "worker_command",
                               lambda *a: [str(self._missing_worker())]):
            api._do_check()
        logs = [payload for kind, payload in events if kind == "log"]
        self.assertTrue(logs, "组件诊断的技术细节必须落日志")
        self.assertIn("worker_missing", logs[0])
        check = next(p for kind, p in events if kind == "check")
        self.assertNotIn("FileNotFoundError", check["text"], "主文案不得含技术词")

    def test_list_worker_missing_emits_failure_once(self):
        """list 遇缺组件：恰一次 activities 失败终态——骨架必须能结束（WinError 2 缺口）。"""
        api, events = self._api_with_events()
        with mock.patch.object(self.app, "worker_command",
                               lambda *a: [str(self._missing_worker())]):
            api._do_list()
        acts = [payload for kind, payload in events if kind == "activities"]
        self.assertEqual(len(acts), 1, "list 必须恰发一次终态事件")
        payload = acts[0]
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["runtime_error"], "worker_missing")
        self.assertIn("程序组件缺失", payload["text"])
        self.assertFalse(api._listing, "finally 必须清「拉列表」标记")

    def test_list_spawn_exception_maps_to_spawn_failed(self):
        """非文件缺失类异常（权限/占用）同样给终态并归 spawn_failed。"""
        api, events = self._api_with_events()
        with mock.patch.object(self.app.subprocess, "Popen",
                               mock.Mock(side_effect=OSError("boom"))):
            api._do_list()
        acts = [payload for kind, payload in events if kind == "activities"]
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts[0]["runtime_error"], "spawn_failed")

    def test_list_permission_error_maps_to_worker_permission(self):
        """权限类进程创建异常归 worker_permission（组件码只来自进程创建 OSError）。"""
        api, events = self._api_with_events()
        with mock.patch.object(self.app.subprocess, "Popen",
                               mock.Mock(side_effect=PermissionError("denied"))):
            api._do_list()
        acts = [payload for kind, payload in events if kind == "activities"]
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts[0]["runtime_error"], "worker_permission")

    def test_list_data_exception_is_plain_failure_not_component(self):
        """数据异常（TypeError 等）：恰一次失败终态、无 runtime_error、
        不冒充组件损坏（不诱导重新下载）；检查位解除、凭证零接触。"""
        api, events = self._api_with_events()
        fake = mock.Mock()
        fake.returncode = 0
        fake.communicate.return_value = ('[{"id": "a1", "name": "活动"}]', "")
        fake.poll.return_value = 0
        with mock.patch.object(self.app.subprocess, "Popen", mock.Mock(return_value=fake)), \
                mock.patch.object(self.app, "_seconds_until",
                                  mock.Mock(side_effect=TypeError("bad data shape"))):
            api._do_list()
        acts = [payload for kind, payload in events if kind == "activities"]
        self.assertEqual(len(acts), 1, "数据异常也必须恰一次终态")
        payload = acts[0]
        self.assertFalse(payload["ok"])
        self.assertNotIn("runtime_error", payload, "数据异常不得带组件码")
        self.assertNotIn("程序组件", payload["text"], "数据异常不得冒充组件损坏")
        self.assertEqual(payload["text"], "活动列表没能加载")
        self.assertFalse(api._listing, "finally 必须清「拉列表」标记")
        self.assertFalse(self.session.exists(), "数据异常路径不得创建/改写会话文件")

    def test_check_data_exception_is_plain_failure_not_component(self):
        """check 数据异常：恰一次检查失败终态、无 runtime_error、技术细节只进日志。"""
        api, events = self._api_with_events()
        with mock.patch.object(self.app.Api, "_run",
                               mock.Mock(side_effect=AttributeError("bad check data"))):
            api._do_check()
        checks = [payload for kind, payload in events if kind == "check"]
        self.assertEqual(len(checks), 1)
        payload = checks[0]
        self.assertFalse(payload["ok"])
        self.assertNotIn("runtime_error", payload, "数据异常不得带组件码")
        self.assertIn("检查登录态没跑完", payload["text"])
        self.assertEqual(api._last_check["runtime_error"], "")
        self.assertFalse(api._checking, "finally 必须清「检查中」标记")
        logs = [payload for kind, payload in events if kind == "log"]
        self.assertTrue(any("AttributeError" in str(item) for item in logs),
                        "技术细节必须落日志")

    def test_list_unknown_exception_closes_out_once(self):
        """未知类型异常：恰一次终态收尾，不因空错误码取字典二次抛错。"""
        api, events = self._api_with_events()
        with mock.patch.object(self.app.subprocess, "Popen",
                               mock.Mock(side_effect=RuntimeError("weird"))):
            api._do_list()   # 不向上抛即通过：兜底分支不得让异常冲出收尾
        acts = [payload for kind, payload in events if kind == "activities"]
        self.assertEqual(len(acts), 1)
        self.assertNotIn("runtime_error", acts[0])
        self.assertEqual(acts[0]["text"], "活动列表没能加载")

    def test_check_outer_fallback_revokes_stale_valid(self):
        """FB3：结论构造阶段异常命中外层兜底——后端旧 valid 同步撤销为 unknown。"""
        app = self.app
        api, events = self._api_with_events()
        api._last_check = {"result": "valid", "checked_at": 0.0, "runtime_error": ""}
        api._checking = True
        made = []

        def fake_coord(*args, **kwargs):
            made.append(args)
            return mock.Mock()

        ok_run = mock.Mock(return_value={"code": 0, "out": "[OK] 登录态还有 15 小时"})
        item = {"id": "a1", "name": "活动", "sign_start": "2026-01-01 09:00:00",
                "sign_end": "2099-01-02 09:00:00",
                "act_start": "2099-01-03 09:00:00", "act_end": "2099-01-03 11:00:00",
                "signed": False}
        with mock.patch.object(app.Api, "_run", ok_run), \
                mock.patch.object(app, "_check_reason_line",
                                  mock.Mock(side_effect=TypeError("结论构造炸了"))), \
                mock.patch.object(app, "PlanCoordinator", fake_coord):
            api._do_check()
            res = api.start_plan([dict(item)])
        checks = [payload for kind, payload in events if kind == "check"]
        self.assertEqual(len(checks), 1, "外层兜底仍恰一次终态")
        self.assertEqual(checks[0]["result"], "unknown")
        self.assertNotIn("runtime_error", checks[0], "外层兜底不是组件错误")
        self.assertEqual(api._last_check["result"], "unknown", "后端判定必须同步撤销")
        self.assertEqual(api._last_check["runtime_error"], "")
        self.assertFalse(api._checking)
        self.assertFalse(res.get("ok"), "旧 valid 不得继续放行 start_plan")
        self.assertEqual(made, [], "拒绝路径不得构造协调器")
        # 随后正常校验恢复 valid：按既有规则重新接纳
        with mock.patch.object(app.Api, "_run", ok_run), \
                mock.patch.object(app, "PlanCoordinator", fake_coord):
            api._do_check()
            res2 = api.start_plan([dict(item)])
        self.assertEqual(api._last_check["result"], "valid")
        self.assertTrue(res2.get("ok"), "正常校验恢复 valid 后按既有规则接纳")

    def test_login_worker_missing_returns_structured_no_busy(self):
        """login 同步组件失败：ok=false + error 码，不留 busy/子进程占位。"""
        api, events = self._api_with_events()
        with mock.patch.object(self.app, "worker_command",
                               lambda *a: [str(self._missing_worker())]):
            res = api.login()
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "worker_missing")
        self.assertIn("程序组件缺失", res["text"])
        self.assertIn("重新下载完整版本", res["text"])
        self.assertIsNone(api._proc, "失败路径不得留下子进程占位")
        self.assertFalse(api._is_busy_locked())
        self.assertNotIn("done", [kind for kind, _ in events], "没启动就没有 done 事件")

    def test_start_plan_rejects_missing_component_but_demo_skips(self):
        """start_plan 保留组件门禁（真实模式）；demo 不起 worker，跳过组件检查。"""
        item = {"id": "a", "name": "活动", "sign_start": "2099-01-01 09:00:00",
                "sign_end": "2099-01-02 09:00:00",
                "act_start": "2099-01-03 09:00:00", "act_end": "2099-01-03 11:00:00",
                "signed": False}
        api = self.app.Api()
        api._last_check = {"result": "valid", "checked_at": 0.0}
        with mock.patch.object(self.app, "worker_problem", lambda: "worker_missing"):
            res = api.start_plan([dict(item)])
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "worker_missing")
        self.assertIn("程序组件缺失", res["text"])
        self.assertIsNone(api._plan, "拒绝路径不得创建计划")
        # demo：不起 worker，组件门禁放行（落到计划校验层；缺举办时间的样本直接被拒）
        with mock.patch.object(self.app, "DEMO", True):
            api2 = self.app.Api()
            res2 = api2.start_plan([{"id": "b", "name": "缺时间",
                                     "sign_start": "2099-01-01 09:00:00",
                                     "signed": False}])
            self.assertFalse(res2["ok"])
            self.assertNotEqual(res2.get("error"), "worker_missing",
                                "demo 模式不得被组件门禁拦截")


class ShutdownLifecycleTests(unittest.TestCase):
    """正常关闭收尾：先停协调器，再按树终止**仅本 Api 登记**的进程。"""

    def setUp(self):
        self.app = _app()

    def _sleeper(self, seconds: int = 30) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(%d)" % seconds],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @staticmethod
    def _force_kill(proc: subprocess.Popen) -> None:
        try:
            if proc.poll() is None:
                proc.kill()
                proc.wait(5)
        except Exception:
            pass

    def test_shutdown_kills_only_registered_procs(self):
        """登记过的登录/计划/辅助进程全部终止；未登记的外部进程原样存活。"""
        p_login = self._sleeper()
        p_plan = self._sleeper()
        p_aux = self._sleeper()
        p_foreign = self._sleeper()
        self.addCleanup(self._force_kill, p_foreign)
        api = self.app.Api()
        api._proc = p_login
        api._plan_procs["task-01"] = p_plan
        api._aux_procs.add(p_aux)
        api.shutdown()
        deadline = time.time() + 5
        for proc in (p_login, p_plan, p_aux):
            while proc.poll() is None and time.time() < deadline:
                time.sleep(0.05)
            self.assertIsNotNone(proc.poll(), "登记过的进程必须在关闭收尾中被终止")
        self.assertIsNone(p_foreign.poll(), "未登记的外部进程不得被扫杀")
        self._force_kill(p_foreign)

    def test_shutdown_stops_coordinator_once(self):
        """有计划时先置停协调器（等待/排队任务标 canceled），再走进程收尾。"""
        api = self.app.Api()
        plan = mock.Mock()
        plan.plan_id = "p1"
        api._plan = plan
        api.shutdown()
        plan.stop.assert_called_once_with()

    def test_spawn_registers_proc_inside_lock(self):
        """Popen 与 _proc 登记在同一把锁内：关闭与异步启动竞态不留孤儿（结构锁死）。"""
        src = read("local/app.py")
        spawn = src[src.index("def _spawn"):src.index("def _pump")]
        self.assertIn("with self._lock:", spawn)
        self.assertLess(spawn.index("with self._lock:"),
                        spawn.index("subprocess.Popen"),
                        "Popen 必须在锁内构造（登记与创建原子，关闭快照才能看到它）")
        self.assertIn("self._proc = subprocess.Popen", spawn)

    def test_kill_tree_is_pid_only(self):
        """树终止只按 PID：不得出现按映像名扫杀（用户可能开着别的实例）。"""
        src = read("local/app.py")
        self.assertIn('["taskkill", "/F", "/T", "/PID", str(proc.pid)]', src,
                      "树终止命令必须按 PID")
        self.assertNotIn('"/IM"', src, "不得按映像名扫杀")


class _FakeProc:
    """内存假进程：poll 恒为 None（活得好好的一段内存），kill 只翻标记。"""

    def __init__(self, pid: int = 1000):
        self.pid = pid
        self.killed = False

    def poll(self):
        return None

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


class _GatePopen:
    """可控 Popen 替身：构造（=Popen 调用）内等 release 才返回，communicate 内等 finish 才返回。

    创建与登记在同一临界区里：构造内阻塞让创建线程停在「已进锁、尚未登记」
    的中段（shutdown 必须等在锁外）；communicate 内阻塞让进程停在
    「已登记、还活着」——此时 shutdown 取快照必须收走它。行为全部内存化
    （零真实子进程）。
    """

    def __init__(self, entered: threading.Event, release: threading.Event,
                 finish: threading.Event, args, **kwargs):
        self.args = args
        self.pid = 4711
        self.returncode = 0
        self.killed = False
        self._finish = finish
        entered.set()
        release.wait(5)

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return self.returncode

    def communicate(self, timeout=None):
        self._finish.wait(5)
        return ("[]", "")


class ShutdownBarrierTests(unittest.TestCase):
    """关闭屏障的行为回归（事件/屏障驱动，不靠 sleep 碰竞态、不只断言源码含锁）。

    覆盖四类交错：关闭先置位时四类公开入口均不 Popen；创建已开始时 shutdown
    等待登记完成且收走不漏；排队 check/list 与计划任务在关闭后到达创建点被拒；
    并发两次 shutdown 只收尾一次且无死锁。除既有真实进程用例外，这里全部用
    内存假进程 + 打点替身（零真实子进程、零学校调用），外部未登记 fixture
    必须存活仍由 ShutdownLifecycleTests.test_shutdown_kills_only_registered_procs
    以真实无害进程守护。本轮只定义，不运行（待授权轮统一执行）。
    """

    def setUp(self):
        self.app = _app()

    def _plan_item(self) -> dict:
        return {"id": "a1", "name": "活动", "sign_start": "2099-01-01 09:00:00",
                "sign_end": "2099-01-02 09:00:00",
                "act_start": "2099-01-03 09:00:00", "act_end": "2099-01-03 11:00:00",
                "signed": False}

    def test_closing_refuses_all_public_starters_without_popen(self):
        """关闭先置位：check/list/login/start_plan 四类入口都拒绝且零 Popen。"""
        app = self.app
        api = app.Api()
        popen_calls = []

        def counting_popen(args, **kwargs):
            popen_calls.append(args)
            raise AssertionError("关闭后不得创建任何进程")

        with mock.patch.object(app, "worker_command", lambda *a: ["fake-worker"]), \
                mock.patch.object(app, "worker_problem", lambda: ""), \
                mock.patch.object(app.subprocess, "Popen", counting_popen):
            api.shutdown()
            r_check = api.check()
            r_list = api.list_activities()
            r_login = api.login()
            r_plan = api.start_plan([self._plan_item()])
        for name, res in (("check", r_check), ("list", r_list),
                          ("login", r_login), ("start_plan", r_plan)):
            self.assertFalse(res.get("ok"), "%s 关闭后必须拒绝" % name)
            self.assertIn("关闭", res.get("text", ""), "%s 拒绝话术要说明正在关闭" % name)
            self.assertNotIn("error", res, "%s 拒绝不是组件码，不得带 error 字段" % name)
        self.assertEqual(popen_calls, [], "四类入口都不得走到 Popen")
        self.assertFalse(api._checking)
        self.assertFalse(api._listing)
        self.assertIsNone(api._plan, "关闭后不得创建计划")

    def test_shutdown_waits_for_inflight_creation_and_reaps_it(self):
        """创建先开始：shutdown 等在锁外；登记完成、进程仍活着时必被收走（不漏登记）。"""
        app = self.app
        api = app.Api()
        entered, release, finish = (threading.Event(), threading.Event(),
                                    threading.Event())
        made = []

        def gate_popen(args, **kwargs):
            proc = _GatePopen(entered, release, finish, args, **kwargs)
            made.append(proc)
            return proc

        kills = []

        def recorder(proc):
            proc.killed = True
            kills.append(proc)

        done = threading.Event()

        def closer():
            api.shutdown()
            done.set()

        with mock.patch.object(app.subprocess, "Popen", gate_popen), \
                mock.patch.object(app, "_kill_tree", recorder), \
                mock.patch.object(app, "worker_command", lambda *a: ["fake-worker"]):
            worker = threading.Thread(target=api._do_list, daemon=True)
            worker.start()
            self.assertTrue(entered.wait(5), "创建线程必须到达 Popen 边界（锁内）")
            closer_t = threading.Thread(target=closer, daemon=True)
            closer_t.start()
            self.assertFalse(done.wait(0.2),
                             "创建未登记完成前 shutdown 必须等在锁外，不得提前收尾")
            release.set()
            self.assertTrue(done.wait(5), "登记完成后 shutdown 必须完成收尾")
            finish.set()            # 收尾完成后再放进程走完 communicate
            worker.join(5)
            closer_t.join(5)
        self.assertEqual(kills, made, "shutdown 快照必须接住已登记的活进程并收走")

    def _queued_refusal(self, do_request, event_kind):
        """排队请求在关闭后到达创建点：拒绝创建、恰一次取消终态、无组件码。"""
        app = self.app
        api = app.Api()
        entered, proceed = threading.Event(), threading.Event()

        def gate_cmd(*a):
            entered.set()
            proceed.wait(5)
            return ["fake-worker"]

        popen_calls = []

        def counting_popen(args, **kwargs):
            popen_calls.append(args)
            raise AssertionError("关闭后不得创建进程")

        events = []
        api._emit = lambda kind, payload=None: events.append((kind, payload))
        with mock.patch.object(app, "worker_command", gate_cmd), \
                mock.patch.object(app.subprocess, "Popen", counting_popen):
            worker = threading.Thread(target=do_request, args=(api,), daemon=True)
            worker.start()
            self.assertTrue(entered.wait(5), "排队线程必须先到达创建点之前")
            api.shutdown()          # 创建者还没进锁：shutdown 零等待完成
            proceed.set()
            worker.join(5)
        self.assertEqual(popen_calls, [], "关闭后排队请求不得创建任何进程")
        finals = [payload for kind, payload in events if kind == event_kind]
        self.assertEqual(len(finals), 1, "%s 请求必须恰一次终态" % event_kind)
        self.assertNotIn("runtime_error", finals[0], "关闭取消不是组件错误")
        return api

    def test_queued_check_refuses_creation_after_closing(self):
        """已排队的 check 在关闭后到达创建点：拒绝创建、恰一次取消终态。"""
        api = self._queued_refusal(lambda api: api._do_check(), "check")
        self.assertFalse(api._checking, "finally 必须清「检查中」标记")

    def test_queued_list_refuses_creation_after_closing(self):
        """已排队的 list 在关闭后到达创建点：拒绝创建、恰一次取消终态。"""
        api = self._queued_refusal(lambda api: api._do_list(), "activities")
        self.assertFalse(api._listing, "finally 必须清「拉列表」标记")

    def test_plan_task_refuses_creation_after_closing(self):
        """计划任务在关闭后到达创建点：_run_plan_task 抛关闭拒绝（取消语义）。"""
        app = self.app
        api = app.Api()
        spec = SimpleNamespace(task_id="task-01-a1", activity_id="a1", name="活动",
                               sign_start="2099-01-01 09:00:00",
                               sign_end="2099-01-02 09:00:00",
                               lead_s=100, retry_for_s=120, max_attempts=0,
                               headed=False)
        entered, proceed = threading.Event(), threading.Event()

        def gate_cmd(*a):
            entered.set()
            proceed.wait(5)
            return ["fake-worker"]

        popen_calls = []

        def counting_popen(args, **kwargs):
            popen_calls.append(args)
            raise AssertionError("关闭后不得创建进程")

        errors = []

        def work():
            try:
                api._run_plan_task(spec, threading.Event(), None)
            except Exception as exc:  # noqa: BLE001 - 断言的就是这个异常
                errors.append(exc)

        with mock.patch.object(app, "worker_command", gate_cmd), \
                mock.patch.object(app.subprocess, "Popen", counting_popen):
            worker = threading.Thread(target=work, daemon=True)
            worker.start()
            self.assertTrue(entered.wait(5))
            api.shutdown()
            proceed.set()
            worker.join(5)
        self.assertEqual(popen_calls, [], "关闭后计划任务不得创建进程")
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], app._ClosingError,
                              "计划任务的关闭拒绝必须是取消语义异常，不是组件错误")

    def test_runner_converts_closing_refusal_to_canceled(self):
        """计划运行中关闭：runner 把关闭拒绝转成 canceled，协调器照常收出唯一 plan_done。"""
        app = self.app
        api = app.Api()
        api._last_check = {"result": "valid", "checked_at": 0.0}
        # 已开放场次（instant_signup）：协调器立即启动任务线程——
        # 未来场次是 pre_register，真实调度会等到准备时刻，测试里永远到不了创建点
        item = {"id": "a1", "name": "活动", "sign_start": "2026-01-01 09:00:00",
                "sign_end": "2099-01-02 09:00:00",
                "act_start": "2099-01-03 09:00:00", "act_end": "2099-01-03 11:00:00",
                "signed": False}
        entered, proceed = threading.Event(), threading.Event()
        events = []
        plan_done = threading.Event()

        def fake_task(api_self, spec, cancel_event, status):
            # 挂在类上的普通函数经实例访问会绑定 self：首参是 Api 实例
            entered.set()
            proceed.wait(5)
            raise app._ClosingError("程序正在关闭，本场次未启动")

        def capture(kind, payload=None):
            events.append((kind, payload))
            if kind == "plan_done":
                plan_done.set()

        api._emit = capture
        with mock.patch.object(app.Api, "_run_plan_task", fake_task):
            # 整段编排必须在补丁存活期内：start_plan 返回≠任务线程已到创建点，
            # 补丁提前撤销会让线程拿回真方法、真实拉起 worker
            res = api.start_plan([dict(item)])
            self.assertTrue(res.get("ok"), "计划本身应能启动（拒绝发生在任务创建点）")
            self.assertTrue(entered.wait(5), "任务线程必须到达关闭拒绝边界")
            api.shutdown()          # 先停协调器（排队任务标 canceled），再放行拒绝
            proceed.set()
            self.assertTrue(plan_done.wait(5), "计划必须收出唯一一次 plan_done")
        plan_dones = [payload for kind, payload in events if kind == "plan_done"]
        self.assertEqual(len(plan_dones), 1, "plan_done 恰一次")
        self.assertEqual(api._plan, None, "计划收尾后 Api 不得再持有协调器")
        tasks = [payload for kind, payload in events if kind == "task"]
        self.assertTrue(tasks, "任务终态事件必须存在")
        self.assertNotIn("failed", [payload.get("status") for payload in tasks],
                         "关闭拒绝不得冒充报名失败")
        self.assertEqual(tasks[-1].get("status"), "canceled",
                         "任务最后一个事件必须是 canceled 终态")
        self.assertEqual(plan_dones[0]["summary"]["counts"].get("canceled"), 1,
                         "计划汇总必须计 1 场 canceled")

    def test_concurrent_shutdowns_close_out_once_without_deadlock(self):
        """并发两次 shutdown：只真正收尾一次（stop 恰一次、每进程恰收一次）、无死锁。"""
        app = self.app
        api = app.Api()
        plan = mock.Mock()
        api._plan = plan
        p_login, p_plan = _FakeProc(pid=1001), _FakeProc(pid=1002)
        api._proc = p_login
        api._plan_procs["task-01"] = p_plan
        kills = []
        barrier = threading.Barrier(2)

        def closer():
            barrier.wait(5)
            api.shutdown()

        with mock.patch.object(app, "_kill_tree", lambda proc: kills.append(proc)):
            threads = [threading.Thread(target=closer, daemon=True) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
            self.assertFalse(any(thread.is_alive() for thread in threads),
                             "并发 shutdown 必须都能完成（无死锁）")
        plan.stop.assert_called_once_with()
        self.assertEqual(len(kills), 2, "每个登记进程恰被收走一次（重复 shutdown 不重复收尾）")
        self.assertEqual({id(proc) for proc in kills}, {id(p_login), id(p_plan)})


class ShutdownAcceptTests(unittest.TestCase):
    """FB1 计划接纳原子化与 FB2 关闭完成信号的行为回归（事件/屏障驱动，全内存替身）。

    协调器一律 mock（不真实 start）、树终止 patch 为打点替身：补丁作用域覆盖
    线程创建至 join，零真实 Popen、零学校访问、无 sleep 猜次序。本轮只定义不运行。
    """

    def setUp(self):
        self.app = _app()

    @staticmethod
    def _instant_item():
        # 已开放场次（instant_signup）：请求合法且协调器无需等待即可调度
        return {"id": "a1", "name": "活动", "sign_start": "2026-01-01 09:00:00",
                "sign_end": "2099-01-02 09:00:00",
                "act_start": "2099-01-03 09:00:00", "act_end": "2099-01-03 11:00:00",
                "signed": False}

    def test_concurrent_start_plans_accept_exactly_one(self):
        """两请求都越过开头 busy 检查后在最终接纳点串行：恰一个接纳、一个拒绝、一个协调器启动。"""
        app = self.app
        api = app.Api()
        api._last_check = {"result": "valid", "checked_at": 0.0}
        item = self._instant_item()
        barrier = threading.Barrier(2)
        real_validate = app.validate_plan

        def gate_validate(normalized):
            barrier.wait(5)          # 两请求都越过开头 busy 检查后才放行
            return real_validate(normalized)

        instances = []

        def fake_coord(plan_id, specs, runner, emit):
            inst = mock.Mock()
            inst.plan_id = plan_id
            # 真实协调器登记后任务全 queued → snapshot 为 running（busy 判据）；
            # 替身必须还原这一语义，否则最终接纳点的 busy 重检判不出
            inst.snapshot = mock.Mock(return_value={"status": "running"})
            instances.append(inst)
            return inst

        with mock.patch.object(app, "validate_plan", gate_validate), \
                mock.patch.object(app, "PlanCoordinator", fake_coord):
            results = [None, None]

            def starter(index):
                results[index] = api.start_plan([dict(item)])

            threads = [threading.Thread(target=starter, args=(i,), daemon=True)
                       for i in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
            self.assertFalse(any(thread.is_alive() for thread in threads),
                             "并发接纳不得死锁")
        self.assertEqual(len(instances), 2, "两请求各自构建协调器（接纳点前无法共享）")
        started = [inst for inst in instances if inst.start.call_count == 1]
        self.assertEqual(len(started), 1, "恰一个协调器被启动")
        self.assertIs(api._plan, started[0], "_plan 不得被后到请求覆盖")
        oks = [r for r in results if r and r.get("ok")]
        refused = [r for r in results if r and not r.get("ok")]
        self.assertEqual(len(oks), 1, "恰一个请求接纳")
        self.assertEqual(len(refused), 1, "另一请求被拒")
        self.assertIn("先停止", refused[0]["text"], "拒绝话术沿用既有 busy 口径")

    def test_request_paused_before_reservation_refused_after_shutdown(self):
        """请求停在校验后、最终预留前，shutdown 先完成：放行后被拒、零启动、无新计划。"""
        app = self.app
        api = app.Api()
        api._last_check = {"result": "valid", "checked_at": 0.0}
        item = self._instant_item()
        entered, release = threading.Event(), threading.Event()
        made = []

        def gate_coord(plan_id, specs, runner, emit):
            entered.set()            # 已过开头检查，停在校验后、最终预留前
            release.wait(5)
            inst = mock.Mock()
            inst.plan_id = plan_id
            made.append(inst)
            return inst

        holder = {}

        def starter():
            holder["res"] = api.start_plan([dict(item)])

        with mock.patch.object(app, "PlanCoordinator", gate_coord):
            worker = threading.Thread(target=starter, daemon=True)
            worker.start()
            self.assertTrue(entered.wait(5), "请求必须先到达预留边界之前")
            api.shutdown()           # 关闭先完成：快照看不到未预留的请求
            release.set()
            worker.join(5)
        self.assertFalse(holder["res"].get("ok"), "最终接纳点必须核到关闭已开始")
        self.assertIn("关闭", holder["res"]["text"])
        self.assertEqual(made and made[0].start.call_count, 0, "未接纳请求零 start")
        self.assertIsNone(api._plan, "未接纳的请求不得留下计划")

    def test_shutdown_stops_reserved_coordinator(self):
        """已预留的协调器必须被 shutdown 快照看见并置停（预留与快照同锁）。"""
        app = self.app
        api = app.Api()
        api._last_check = {"result": "valid", "checked_at": 0.0}
        item = self._instant_item()
        inst = mock.Mock()
        inst.plan_id = "p1"
        with mock.patch.object(app, "PlanCoordinator", lambda *a, **k: inst):
            res = api.start_plan([dict(item)])
        self.assertTrue(res.get("ok"))
        self.assertIs(api._plan, inst)
        api.shutdown()
        inst.stop.assert_called_once_with()   # 预留后关闭必须置停协调器

    def test_start_failure_releases_own_reservation_only(self):
        """coordinator.start 抛错：释放本人预留、下一次正常请求可开始、不清别的计划。"""
        app = self.app
        api = app.Api()
        api._last_check = {"result": "valid", "checked_at": 0.0}
        item = self._instant_item()
        boom = mock.Mock()
        boom.start.side_effect = RuntimeError("计划已经在运行")
        boom.snapshot = mock.Mock(return_value={"status": "running"})
        ok_inst = mock.Mock()
        ok_inst.plan_id = "p2"
        ok_inst.snapshot = mock.Mock(return_value={"status": "running"})
        made = []

        def factory(plan_id, specs, runner, emit):
            inst = ok_inst if made else boom
            made.append(inst)
            return inst

        with mock.patch.object(app, "PlanCoordinator", factory):
            res1 = api.start_plan([dict(item)])
            self.assertFalse(res1.get("ok"))
            self.assertIn("计划没能开始", res1["text"], "启动失败返回可读结论")
            self.assertIsNone(api._plan, "失败必须撤销本请求的预留")
            res2 = api.start_plan([dict(item)])
            self.assertTrue(res2.get("ok"), "释放后下一次正常请求可开始")
            res3 = api.start_plan([dict(item)])
            self.assertFalse(res3.get("ok"), "已有计划时新请求被拒")
            self.assertIs(api._plan, ok_inst, "拒绝路径不得清掉已接纳的计划")

    def test_second_shutdown_waits_for_closeout_completion(self):
        """后到 shutdown 等首次收尾的完成信号：释放前都不返回、锁外等待、每 PID 恰收一次。"""
        app = self.app
        api = app.Api()
        plan = mock.Mock()
        api._plan = plan
        p_login, p_plan = _FakeProc(pid=2001), _FakeProc(pid=2002)
        api._proc = p_login
        api._plan_procs["task-01"] = p_plan
        kills = []
        entered, release = threading.Event(), threading.Event()

        def gate_kill(proc):
            kills.append(proc)
            entered.set()            # 首次收尾停在树终止中段
            release.wait(5)

        d1, d2 = threading.Event(), threading.Event()

        def closer(flag):
            api.shutdown()
            flag.set()

        with mock.patch.object(app, "_kill_tree", gate_kill):
            s1 = threading.Thread(target=closer, args=(d1,), daemon=True)
            s1.start()
            self.assertTrue(entered.wait(5), "首次收尾必须到达树终止边界")
            s2 = threading.Thread(target=closer, args=(d2,), daemon=True)
            s2.start()
            self.assertFalse(d1.wait(0.1), "收尾未完成时首次调用不得返回")
            self.assertFalse(d2.wait(0.1), "后到调用必须等待完成信号，不得提前返回")
            self.assertTrue(api._lock.acquire(timeout=1),
                            "等待完成信号不得持有 Api 锁（锁内等待会死锁）")
            api._lock.release()
            release.set()
            s1.join(5)
            s2.join(5)
            self.assertTrue(d1.is_set() and d2.is_set(), "释放后两个调用都必须退出")
        plan.stop.assert_called_once_with()
        self.assertEqual(kills, [p_plan, p_login], "每个登记 PID 恰收一次")
        self.assertTrue(api._shutdown_done_event.is_set(), "完成信号由收尾 finally 发出")


class RuntimeCheckTests(unittest.TestCase):
    """--runtime-check 离线探针：前置早返回、临时数据根必填、JSON/退出码契约。

    worker 一律 mock 成无害本地命令（零学校调用）；mock 通过≠真实冻结验收——
    「仅含单 EXE 的空目录实际启动内置 worker --help」仍须授权后实包执行。
    """

    def setUp(self):
        self.app = _app()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.session = self.tmp / "session.json"
        for patch in (mock.patch.object(self.app, "SESSION_FILE", self.session),
                      mock.patch.object(self.app, "PREFS", self.tmp / "ui.json")):
            patch.start()
            self.addCleanup(patch.stop)

    def _report(self) -> Path:
        return self.tmp / "report.json"

    def _argv(self, report: Path) -> list:
        return ["app.py", "--runtime-check", "--report", str(report)]

    def test_missing_report_arg_returns_2_without_json(self):
        """前置早返回：缺 --report 直接退出码 2，什么都不写。"""
        rc = self.app._runtime_check_main(["app.py", "--runtime-check"])
        self.assertEqual(rc, 2)
        self.assertFalse(self.tmp.joinpath("report.json").exists())

    def test_missing_data_dir_refused_and_reported(self):
        """临时数据根必填：缺 TJISE_DATA_DIR 拒绝运行，结论落 JSON、退出码 1。"""
        report = self._report()
        env = {k: v for k, v in os.environ.items() if k != "TJISE_DATA_DIR"}
        with mock.patch.dict(os.environ, env, clear=True):
            rc = self.app._runtime_check_main(self._argv(report))
        self.assertEqual(rc, 1)
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertFalse(data["ok"])
        self.assertIn("TJISE_DATA_DIR", data["error"], "拒绝原因要指明缺什么")

    def test_worker_help_success_writes_clean_json(self):
        """worker --help 成功：JSON ok=true + 退出码 0，进程退出码 0；载荷脱敏。"""
        report = self._report()
        with mock.patch.dict(os.environ, {"TJISE_DATA_DIR": str(self.tmp / "data")}), \
                mock.patch.object(self.app, "worker_command",
                                  lambda *a: [sys.executable, "-c", "import sys; sys.exit(0)"]):
            rc = self.app._runtime_check_main(self._argv(report))
        self.assertEqual(rc, 0)
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertTrue(data["ok"])
        self.assertEqual(data["exit_code"], 0)
        for key in ("version", "frozen", "worker"):
            self.assertIn(key, data, "报告必须带 %s（版本/frozen/组件来源）" % key)
        self.assertNotIn("token", report.read_text(encoding="utf-8").lower(),
                         "报告必须脱敏")
        self.assertFalse(self.session.exists(), "探针不得触碰真实登录态文件")

    def test_worker_help_timeout_kills_and_reports(self):
        """worker --help 超时：树终止挂着的进程，JSON ok=false + 超时说明、退出码 1。"""
        report = self._report()
        with mock.patch.dict(os.environ, {"TJISE_DATA_DIR": str(self.tmp / "data")}), \
                mock.patch.object(self.app, "worker_command",
                                  lambda *a: [sys.executable, "-c", "import time; time.sleep(30)"]), \
                mock.patch.object(self.app, "RUNTIME_CHECK_TIMEOUT_S", 0.3):
            rc = self.app._runtime_check_main(self._argv(report))
        self.assertEqual(rc, 1)
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertFalse(data["ok"])
        self.assertIn("超时", data["error"])

    def test_worker_missing_reported(self):
        """组件缺失：JSON component=worker_missing + 人话错误、退出码 1，零子进程。"""
        report = self._report()
        with mock.patch.dict(os.environ, {"TJISE_DATA_DIR": str(self.tmp / "data")}), \
                mock.patch.object(self.app, "GRAB", self.tmp / "gone.exe"):
            rc = self.app._runtime_check_main(self._argv(report))
        self.assertEqual(rc, 1)
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertFalse(data["ok"])
        self.assertEqual(data["component"], "worker_missing")
        self.assertIn("程序组件缺失", data["error"])

    def test_dispatch_precedes_gui_and_data_dir(self):
        """分发顺序锁死：runtime-check 先于数据根初始化、生命周期与窗口创建。"""
        src = read("local/app.py")
        main_src = src[src.index("def main("):]
        probe_at = main_src.index("--runtime-check")
        self.assertLess(probe_at, main_src.index("ensure_data_dir()"),
                        "runtime-check 必须先于数据根初始化分发")
        self.assertLess(probe_at, main_src.index("webview.create_window"),
                        "runtime-check 必须先于窗口创建分发")
        # 正常关闭：先收 worker（shutdown），再写关闭时刻（finalize）
        self.assertLess(main_src.index("api.shutdown()"),
                        main_src.index("lifecycle.finalize()"),
                        "关闭必须先收本实例 worker，再 finalize 生命周期")


class BuildEmbedsWorkerTests(unittest.TestCase):
    """构建脚本把 worker 作为数据资源打进主程序（mock 定义，不运行 PyInstaller）。"""

    def setUp(self):
        sys.path.insert(0, str(LOCAL))
        try:
            import build_exe
        except Exception as exc:
            raise unittest.SkipTest("无法导入 build_exe：%s" % exc)
        finally:
            sys.path.pop(0)
        self.be = build_exe

    def test_worker_built_before_main_and_embedded_as_data(self):
        """先构建 worker 再构建主程序；worker 以数据资源进资源根，不用 --add-binary。"""
        src = read("local/build_exe.py")
        assemble = src[src.index("def assemble"):src.index("def main(")]
        self.assertLess(assemble.index('run_pyinstaller("grab-worker"'),
                        assemble.index('run_pyinstaller("tjise-activity-watch"'),
                        "必须先构建 worker，再作为资源构建主程序")
        self.assertIn('extra_data=[(worker, "."), (VERSION_FILE, ".")]',
                      assemble,
                      "实际构建出的 worker 与 VERSION 以 dest=\".\" 进资源根"
                      "（--add-data 对文件源按 dest/源文件名归档，写 dest=文件名会变成同名目录）")
        self.assertNotIn('"--add-binary"', src,
                         "不得把 --add-binary 参数传给 PyInstaller 重新分析 worker 依赖")
        self.assertNotIn('OUT / "grab-worker.exe"', assemble,
                         "OUT 根目录不再放置外置 worker（独立 EXE 定位）")
        self.assertIn("prune_output(OUT)", assemble, "重建清理通路保留")
        self.assertIn('if child.name == "data":', src, "prune_output 仍跳过 data")

    def test_assemble_embeds_worker_and_skips_sidecar(self):
        """mock 构建行为：记录 run_pyinstaller 调用序与 extra_data 指向实际 worker/VERSION。"""
        import tempfile
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            out = tmp / "out"
            fake_worker = tmp / "stage-worker" / "dist" / "grab-worker.exe"
            fake_worker.parent.mkdir(parents=True)
            fake_worker.write_bytes(b"worker")
            fake_app = tmp / "stage-app" / "dist" / "tjise-activity-watch.exe"
            fake_app.parent.mkdir(parents=True)
            fake_app.write_bytes(b"app")
            calls = []

            def fake_run(name_, source, mode, add_ui, extra_data=None):
                calls.append((name_, list(extra_data or [])))
                return fake_worker if name_ == "grab-worker" else fake_app

            with mock.patch.object(self.be, "OUT", out), \
                    mock.patch.object(self.be, "run_pyinstaller", fake_run):
                self.be.assemble("onefile")
            self.assertEqual([c[0] for c in calls],
                             ["grab-worker", "tjise-activity-watch"],
                             "构建顺序：worker 先于主程序")
            embed = {Path(src_).name: dest for src_, dest in calls[1][1]}
            # --add-data 对文件源按 dest/basename 归档：dest="." 时 basename
            # （恰为 grab-worker.exe）就是资源根的最终落点
            self.assertEqual(embed.get("grab-worker.exe"), ".",
                             "extra_data 必须指向实际构建出的 worker，dest=\".\" 落资源根")
            self.assertEqual(embed.get("VERSION"), ".", "VERSION 同批进资源根")
            self.assertTrue((out / "tjise-activity-watch.exe").is_file())
            self.assertFalse((out / "grab-worker.exe").exists(),
                             "OUT 根目录不再有外置 worker")
            self.assertTrue((out / "data").is_dir(), "data 占位目录维持旧布局")

    def test_resource_locations_match_app_resolution(self):
        """两模式资源位置与 app.py 解析同源：onedir 在 _internal，onefile 在运行期 _MEIPASS。"""
        app_src = read("local/app.py")
        self.assertIn('resource_root / "grab-worker.exe"', app_src,
                      "app.py 冻结解析 = 资源根/grab-worker.exe")
        self.assertIn("RESOURCE_ROOT = Path(getattr(sys, \"_MEIPASS\", HERE))", app_src,
                      "资源根两种模式都经 _MEIPASS（onedir 6.x 即 _internal）")
        be_src = read("local/build_exe.py")
        self.assertIn('extra_data=[(worker, "."), (VERSION_FILE, ".")]', be_src,
                      "--add-data 落点与 app.py 解析是同一个资源根（dest=\".\" + basename）")


if __name__ == "__main__":
    unittest.main()
