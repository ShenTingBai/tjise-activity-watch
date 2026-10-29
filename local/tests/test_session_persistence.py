#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""登录态跨重启持久化的契约测试（无真实凭证、无真实网络）。

覆盖：开发/冻结数据根、TJISE_DATA_DIR 覆盖、LOCALAPPDATA 缺失回退、
旧 EXE 旁 data 一次性迁移（只补缺失/不覆盖/原子）、login_state 三态、
check 四态映射与来源行归因（网络异常不误判 expired）、start_plan 登录门禁
（checking/unknown/none/expired 拒绝、demo 放行）、grab.main 迁移入口、
build_exe 清理保留 OUT/data。
全部使用 tmp_path + mock，不读取 local/data，不发真实网络请求。

运行方式（需用户授权后）：PYTHONPATH=. python -m unittest local.tests.test_session_persistence

@author LiXinwen
"""
from __future__ import annotations

import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

LOCAL = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
from fixtures.business_clock import TestWallClock  # noqa: E402
from time_contract import configure_time_source  # noqa: E402
configure_time_source(time.time)   # 旧用例显式注入本机时钟；时钟用例自行注入 Fake


class DataRootResolutionTests(unittest.TestCase):
    """五种运行形态的数据根口径。"""

    def test_dev_mode_uses_script_local_data(self):
        """开发模式：数据根仍是 local/data，语义与历史版本一致。"""
        # unittest 无内建 monkeypatch：用 mock 上下文模拟
        with mock.patch.dict("os.environ", {}, clear=False), \
                mock.patch.object(sys, "frozen", False, create=True):
            import os
            os.environ.pop("TJISE_DATA_DIR", None)
            import runtime_paths
            module = importlib.reload(runtime_paths)
            self.assertEqual(module.DATA_DIR, LOCAL / "data")

    def test_frozen_mode_uses_localappdata(self):
        """冻结主程序/worker：默认 %LOCALAPPDATA%/TianSoftActivityPreRegistration/data。"""
        with mock.patch.dict("os.environ", {"LOCALAPPDATA": "C:/fake-la"}, clear=False), \
                mock.patch.object(sys, "frozen", True, create=True):
            os.environ.pop("TJISE_DATA_DIR", None) if False else None
            import os
            os.environ.pop("TJISE_DATA_DIR", None)
            import runtime_paths
            module = importlib.reload(runtime_paths)
            self.assertEqual(
                module.DATA_DIR,
                Path("C:/fake-la") / "TianSoftActivityPreRegistration" / "data")

    def test_env_override_wins_and_relative_resolves(self):
        """TJISE_DATA_DIR 覆盖一切；相对路径锚定当前工作目录。"""
        import os
        with mock.patch.dict("os.environ", {"TJISE_DATA_DIR": "rel-data"}, clear=False):
            import runtime_paths
            module = importlib.reload(runtime_paths)
            self.assertEqual(module.DATA_DIR, Path("rel-data").resolve())
        with mock.patch.dict("os.environ", {"TJISE_DATA_DIR": "D:/somewhere/abs"}, clear=False):
            import runtime_paths
            module = importlib.reload(runtime_paths)
            self.assertEqual(module.DATA_DIR, Path("D:/somewhere/abs"))

    def test_localappdata_missing_falls_back_home(self):
        """精简系统没有 LOCALAPPDATA：回退 home/AppData/Local，不崩。"""
        env = dict("os.environ" and __import__("os").environ)
        env.pop("LOCALAPPDATA", None)
        env.pop("TJISE_DATA_DIR", None)
        with mock.patch.dict("os.environ", env, clear=True), \
                mock.patch.object(sys, "frozen", True, create=True):
            import runtime_paths
            module = importlib.reload(runtime_paths)
            self.assertEqual(
                module.DATA_DIR,
                Path.home() / "AppData" / "Local" / "TianSoftActivityPreRegistration" / "data")


class LegacyMigrationTests(unittest.TestCase):
    """旧 EXE 旁 data 的一次性原子迁移。"""

    def _run_migration(self, tmp: Path, preset_target: dict | None = None) -> Path:
        legacy = tmp / "old-exe"
        (legacy / "data").mkdir(parents=True)
        (legacy / "tjise-activity-watch.exe").write_bytes(b"fake")
        for name in ("session.json", "storage_state.json", "ui.json"):
            (legacy / "data" / name).write_text('{"legacy": "%s"}' % name, encoding="utf-8")
        target = tmp / "target-data"
        target.mkdir()
        for name, body in (preset_target or {}).items():
            (target / name).write_text(body, encoding="utf-8")
        with mock.patch.dict("os.environ", {"TJISE_DATA_DIR": str(target)}, clear=False), \
                mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", str(legacy / "tjise-activity-watch.exe")):
            import runtime_paths
            module = importlib.reload(runtime_paths)
            module.ensure_data_dir()
        return target

    def test_migration_copies_missing_files_atomically(self):
        import tempfile
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            target = self._run_migration(tmp)
            for fname in ("session.json", "storage_state.json", "ui.json"):
                self.assertTrue((target / fname).is_file(), "缺失文件应被迁移：%s" % fname)
                self.assertEqual(json.loads((target / fname).read_text(encoding="utf-8"))["legacy"], fname)
            # 不留临时残片
            leftovers = [p.name for p in target.iterdir() if p.name.endswith(".tmp")]
            self.assertEqual(leftovers, [], "原子迁移不得留下 .tmp 残片")

    def test_migration_never_overwrites_existing_new_files(self):
        import tempfile
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            target = self._run_migration(tmp, preset_target={"session.json": '{"new": 1}'})
            self.assertEqual(json.loads((target / "session.json").read_text(encoding="utf-8")),
                             {"new": 1}, "目标已有新文件不得被旧数据覆盖")
            # 其余缺失文件照常补齐
            self.assertTrue((target / "ui.json").is_file())

    def test_migration_failure_does_not_block_startup(self):
        import tempfile
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            target = tmp / "target"
            target.mkdir()
            legacy = tmp / "old"
            (legacy / "data").mkdir(parents=True)
            (legacy / "data" / "session.json").write_text("{}", encoding="utf-8")
            with mock.patch.dict("os.environ", {"TJISE_DATA_DIR": str(target)}, clear=False), \
                    mock.patch.object(sys, "frozen", True, create=True), \
                    mock.patch.object(sys, "executable", str(legacy / "x.exe")), \
                    mock.patch("shutil.copy2", side_effect=PermissionError("locked")):
                import runtime_paths
                module = importlib.reload(runtime_paths)
                try:
                    module.ensure_data_dir()  # 不得抛异常
                except Exception as exc:  # pragma: no cover - 防御性断言
                    self.fail("迁移失败不得阻断启动：%s" % exc)
            self.assertTrue(target.is_dir(), "数据根本身必须已创建")


class LoginStateContractTests(unittest.TestCase):
    """统一 SESSION_FILE 下的登录三态（缺文件/空 token/有效 token）。"""

    def _app(self):
        sys.path.insert(0, str(LOCAL))
        try:
            import app as local_app
        except Exception as exc:
            self.skipTest("无法导入 local/app.py：%s" % exc)
        finally:
            sys.path.pop(0)
        return local_app

    def test_missing_empty_and_valid_sessions(self):
        import tempfile
        app = self._app()
        with tempfile.TemporaryDirectory() as name:
            session = Path(name) / "session.json"
            prefs = Path(name) / "ui.json"
            with mock.patch.object(app, "SESSION_FILE", session), \
                 mock.patch.object(app, "PREFS", prefs):
                self.assertFalse(app.login_state()["session_present"], "缺文件=未登录")
                session.write_text("{坏 json", encoding="utf-8")
                self.assertFalse(app.login_state()["session_present"], "解析失败=未登录")
                session.write_text(json.dumps(
                    {"saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}),
                    encoding="utf-8")
                empty = app.login_state()
                self.assertFalse(empty["session_present"], "空 token=未登录")
                self.assertIsNone(empty["hours_left"])
                session.write_text(json.dumps(
                    {"token": "t", "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}),
                    encoding="utf-8")
                valid = app.login_state()
                self.assertTrue(valid["session_present"])
                self.assertFalse(valid["session_expired"])
                # 旧 saved_at 是本机墙钟文本：没有 v2 锚点就不报数、不判失效（决策 012）
                self.assertIsNone(valid["hours_left"],
                                  "旧 saved_at 不可校准：宁可显示「暂无法估算」")
                stale = (datetime.now() - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S")
                session.write_text(json.dumps({"token": "t", "saved_at": stale}), encoding="utf-8")
                expired = app.login_state()
                self.assertTrue(expired["session_present"])
                self.assertFalse(expired["session_expired"],
                                 "旧墙钟文本不得改判登录失效；有效性只由学校校验决定")
                self.assertIsNone(expired["hours_left"])

    def test_check_result_four_states(self):
        """check 四态：valid/none/expired/unknown；网络异常不算失效。"""
        app = self._app()
        self.assertEqual(app._check_result({"session_present": True}, 0, "[OK] 登录态有效"), "valid")
        self.assertEqual(app._check_result({"session_present": False}, 3, "[X] 本机还没有登录过"), "none")
        self.assertEqual(app._check_result({"session_present": True}, 3, "[X] 登录态已失效 code:4001"), "expired")
        self.assertEqual(app._check_result({"session_present": True}, -1, "[X] 执行超时（90 秒）"), "unknown")
        self.assertEqual(app._check_result({"session_present": True}, 1, "[X] URLError timed out"), "unknown")


class CheckGateContractTests(unittest.TestCase):
    """check 归因与开始门禁：网络异常不得误判 expired；未确认登录不能开始真实计划。"""

    def setUp(self):
        """本类只验证登录态之后的 start_plan 业务校验：先把偏好钉到临时 ui.json，
        再写入当前版本的条款同意，让 start_plan 先过 _terms_gate，否则断言
        拿到的是 terms_required 而不是登录/截止/计划校验的结论。

        同意状态只落在这轮临时文件的 terms_version / terms_accepted_at 两个键上，
        不读写开发者真实数据目录，也不改本类任何一条原断言；未同意状态、
        旧版本与 demo 豁免的专测在 test_terms_consent.py 里照旧构造。
        """
        app = self._app()
        prefs_tmp = tempfile.TemporaryDirectory(prefix="tjise-terms-prefs-")
        self.addCleanup(prefs_tmp.cleanup)
        patcher = mock.patch.object(app, "PREFS",
                                    Path(prefs_tmp.name) / "ui.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        if not app.update_prefs({"terms_version": app.TERMS_VERSION,
                                 "terms_accepted_at": "2026-10-27 12:00:00"}):
            self.fail("条款同意偏好夹具写入失败")

    def _app(self):
        sys.path.insert(0, str(LOCAL))
        try:
            import app as local_app
        except Exception as exc:
            self.skipTest("无法导入 local/app.py：%s" % exc)
        finally:
            sys.path.pop(0)
        return local_app

    def test_reason_source_line_beats_trailing_login_guidance(self):
        """归因只信 [X] 来源行：末尾「请重新登录」指引不得把网络错误拖成 expired。"""
        app = self._app()
        output = ("[X] 接口异常：HTTPError\n"
                  "    请运行：python grab.py login   （或双击 1-登录.bat）")
        reason, text = app._check_reason_line(output)
        self.assertEqual(reason, "[X] 接口异常：HTTPError", "来源行必须是第一条 [X] 行")
        self.assertTrue(text.endswith("1-登录.bat）"), "末行仍返回，供时钟偏差展示")
        state = {"session_present": True, "session_expired": False, "hours_left": 5.0}
        self.assertEqual(app._check_result(state, 2, reason), "unknown",
                         "网络/接口异常必须落入 unknown，不得因末尾登录指引误判 expired")
        self.assertEqual(app._check_hint(reason, 2), "network")

    def test_four_states_by_reason_line(self):
        """四态口径：code=0 → valid；4001/明确身份验证失败 → expired；超时 → unknown。"""
        app = self._app()
        state = {"session_present": True, "session_expired": False, "hours_left": 5.0}
        self.assertEqual(app._check_result(state, 0, "[OK] 登录态有效。"), "valid")
        self.assertEqual(app._check_result(state, 2, "[X] 登录态已过期（code:4001）"), "expired")
        self.assertEqual(app._check_result(state, 2, "[X] 登录态已过期（身份验证失败）"), "expired")
        self.assertEqual(app._check_result(state, -1, "[X] 执行超时（90 秒）"), "unknown")

    def test_auth_service_network_failures_stay_unknown(self):
        """身份验证服务的超时/连接失败是网络问题：网络词优先，不得误归 expired。"""
        app = self._app()
        state = {"session_present": True, "session_expired": False, "hours_left": 5.0}
        for reason in ("[X] 接口异常：身份验证服务超时",
                       "[X] 接口异常：身份验证接口连接失败"):
            self.assertEqual(app._check_hint(reason, 2), "network",
                             "%s 必须先按网络词归因" % reason)
            self.assertEqual(app._check_result(state, 2, reason), "unknown",
                             "%s 不得被误导成重新登录" % reason)
        # 4001 与网络词互不覆盖：明确失效仍是 expired
        self.assertEqual(app._check_result(state, 2, "[X] 登录态已过期（code:4001）"), "expired")

    def test_start_plan_requires_valid_login_unless_demo(self):
        """后端最终防线：checking/unknown/none/expired 拒绝并给下一步；demo 放行登录门禁。

        样本活动缺少举办时间：越过登录门禁后必然落在计划校验层返回，
        不会拉起真实 worker 子进程，也不访问网络。
        """
        app = self._app()
        items = [{"id": "a1", "name": "活动", "sign_start": "2026-01-01 09:00:00",
                  "act_start": "", "act_end": "", "signed": False}]
        for result, needle in (("unknown", "暂时无法确认登录态"),
                               ("none", "还没有登录"),
                               ("expired", "重新登录")):
            api = app.Api(clock=TestWallClock())
            api._last_check = {"result": result, "checked_at": 0.0}
            res = api.start_plan([dict(item) for item in items])
            self.assertFalse(res["ok"], "%s 必须拒绝开始真实计划" % result)
            self.assertIn(needle, res["text"], "%s 的拒绝理由要给出下一步" % result)
        # 检查进行中：同样拒绝，避免拿到半截结论就开始
        api = app.Api(clock=TestWallClock())
        api._checking = True
        res = api.start_plan([dict(item) for item in items])
        self.assertFalse(res["ok"])
        self.assertIn("正在检查登录态", res["text"])
        # 上一轮 valid + 新一轮检查进行中：仍拒绝（refresh 不清旧 valid，门禁必须显式排除 checking）
        api = app.Api(clock=TestWallClock())
        api._last_check = {"result": "valid", "checked_at": 0.0}
        api._checking = True
        res = api.start_plan([dict(item) for item in items])
        self.assertFalse(res["ok"], "新一轮检查完成前不得放行真实计划")
        self.assertIn("正在检查登录态", res["text"])
        # demo 只绕过登录门禁：其余计划校验照走（缺活动时间的样本落到校验层）
        with mock.patch.object(app, "DEMO", True):
            api = app.Api(clock=TestWallClock())
            res = api.start_plan([dict(item) for item in items])
            self.assertFalse(res["ok"])
            self.assertNotIn("登录", res.get("text", ""),
                             "demo 放行的只是登录门禁，不是所有校验")
        # valid：越过登录门禁，走到计划校验层
        api = app.Api(clock=TestWallClock())
        api._last_check = {"result": "valid", "checked_at": 0.0}
        res = api.start_plan([dict(item) for item in items])
        self.assertFalse(res["ok"])
        self.assertNotIn("登录", res.get("text", ""))

    def test_start_plan_rejects_closed_activity(self):
        """报名截止是硬门禁：已截止活动即使通过登录门禁也不能进入 worker。"""
        app = self._app()
        api = app.Api(clock=TestWallClock())
        api._last_check = {"result": "valid", "checked_at": 0.0}
        past_end = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        closed = {"id": "c1", "name": "已截止活动", "sign_start": "2000-01-01 09:00:00",
                  "sign_end": past_end, "act_start": "2099-01-03 09:00:00",
                  "act_end": "2099-01-03 11:00:00", "signed": False}
        # 计划接纳以本进程缓存为权威（R05）：先登记同一份快照，
        # 这样「已截止」是按缓存判定，而不是靠前端自报
        api._activities = {closed["id"]: dict(closed)}
        res = api.start_plan([closed])
        self.assertFalse(res["ok"], "已截止活动必须被 start_plan 拒绝")
        self.assertIn("报名已截止", res["text"])
        self.assertIn("已截止活动", res["text"], "拒绝信息必须带具体活动名")
        self.assertIn(past_end, res["text"], "拒绝信息必须带报名截止时间")
        kinds = [e.get("kind") for e in res.get("errors", [])]
        self.assertIn("sign_end_passed", kinds)
        self.assertIsNone(api._plan, "拒绝路径不得创建计划")

    def test_grab_main_ensures_data_dir_before_dispatch(self):
        """冻结 worker 直接跑 check/list/grab 也要先初始化统一数据根（迁移入口）。"""
        sys.path.insert(0, str(LOCAL))
        try:
            import grab
        finally:
            sys.path.pop(0)
        import inspect
        src = inspect.getsource(grab.main)
        self.assertIn("ensure_data_dir()", src,
                      "grab.main 分发前必须调用 ensure_data_dir()")
        self.assertLess(src.index("ensure_data_dir()"), src.index("parse_args"),
                        "ensure_data_dir 必须在子命令分发之前调用")


class BuildCleanupContractTests(unittest.TestCase):
    """build_exe 清理只删构建产物，OUT/data 原样保留。"""

    def test_prune_output_keeps_data_dir(self):
        import tempfile
        sys.path.insert(0, str(LOCAL))
        try:
            import build_exe
        except Exception as exc:
            self.skipTest("无法导入 build_exe：%s" % exc)
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory() as name:
            out = Path(name)
            (out / "data").mkdir()
            (out / "data" / "session.json").write_text('{"keep": 1}', encoding="utf-8")
            (out / "data" / "storage_state.json").write_text("{}", encoding="utf-8")
            (out / "_internal").mkdir()
            (out / "_internal" / "ui").mkdir()
            (out / "tjise-activity-watch.exe").write_bytes(b"exe")
            (out / "grab-worker.exe").write_bytes(b"exe")
            (out / "README.md").write_text("r", encoding="utf-8")
            build_exe.prune_output(out)
            self.assertTrue((out / "data" / "session.json").is_file(), "重建不得删除旧登录态")
            self.assertTrue((out / "data" / "storage_state.json").is_file())
            self.assertFalse((out / "_internal").exists(), "旧构建产物应清理")
            self.assertFalse((out / "tjise-activity-watch.exe").exists())
            self.assertFalse((out / "grab-worker.exe").exists())
            self.assertFalse((out / "README.md").exists())

    def test_prune_output_missing_dir_is_noop(self):
        sys.path.insert(0, str(LOCAL))
        try:
            import build_exe
        except Exception as exc:
            self.skipTest("无法导入 build_exe：%s" % exc)
        finally:
            sys.path.pop(0)
        import tempfile
        with tempfile.TemporaryDirectory() as name:
            build_exe.prune_output(Path(name) / "not-exist")  # 目录不存在=与旧行为一致


class PlanRestoreContractTests(unittest.TestCase):
    """页面重载恢复：bootstrap 的 plan 快照带真实 checkpoint、模式与报名截止。

    恢复载荷若丢运行时 detail，时间线会退回估算时间、把历史开放当成执行
    参考；这里用内存协调器 + tmp 数据根验证 bootstrap 链路，不碰真实目录。
    """

    def _app(self):
        sys.path.insert(0, str(LOCAL))
        try:
            import app as local_app
        except Exception as exc:
            self.skipTest("无法导入 local/app.py：%s" % exc)
        finally:
            sys.path.pop(0)
        return local_app

    def test_bootstrap_plan_snapshot_keeps_runtime_detail(self):
        import tempfile

        sys.path.insert(0, str(LOCAL))
        try:
            from signup_plan import PlanCoordinator, TaskSpec
        finally:
            sys.path.pop(0)
        app = self._app()
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            with mock.patch.object(app, "PREFS", tmp / "ui.json"), \
                    mock.patch.object(app, "SESSION_FILE", tmp / "session.json"):
                at = (datetime.now() - timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S")
                spec = TaskSpec.from_activity(
                    {"id": "o", "name": "开放场", "sign_start": at,
                     "sign_end": "2099-01-02 09:00:00",
                     "act_start": "2099-01-03 09:00:00",
                     "act_end": "2099-01-03 11:00:00"},
                    "task-o", retry_for_s=600, max_attempts=3)

                def runner(spec, cancel_event, status):
                    status("submitting", {"run_started_at": 1000.0,
                                          "page_opened_at": 1002.5})
                    return {"status": "canceled", "reason": "测试收尾"}

                coordinator = PlanCoordinator("plan-restore", [spec], runner,
                                              lambda *a: None)
                coordinator.start()
                thread = coordinator._thread
                thread.join(2)
                self.assertFalse(thread.is_alive())
                api = app.Api(clock=TestWallClock())
                api._plan = coordinator
                payload = api.bootstrap()
        plan = payload.get("plan") or {}
        task = (plan.get("tasks") or [{}])[0]
        self.assertEqual(plan.get("plan_id"), "plan-restore")
        self.assertEqual(task.get("run_started_at"), 1000.0,
                         "重载恢复必须拿到真实的「本次启动」checkpoint")
        self.assertEqual(task.get("page_opened_at"), 1002.5,
                         "重载恢复必须拿到真实的「活动页已打开」checkpoint")
        self.assertEqual(task.get("signup_mode"), "instant_signup",
                         "恢复的模式字段不得丢（否则立即报名被显示成等待开放）")
        self.assertEqual(task.get("sign_end"), "2099-01-02 09:00:00",
                         "恢复的报名截止不得丢")
        self.assertEqual(task.get("retry_for_s"), 600, "恢复的重试策略不得丢")


class LifecycleContractTests(unittest.TestCase):
    """应用生命周期元数据：关闭时长、异常退出、登录估算（ui.json 独占，不碰登录文件）。

    覆盖：compute_lifecycle 纯函数三态（clean/abnormal/unknown）与时钟回拨；
    AppLifecycle 临时目录闭环（心跳/正常关闭/偏好合并不丢键）；登录估算按
    saved_at + TOKEN_LIFETIME_H 绝对墙钟计算且不改动 session 文件内容与 mtime。
    """

    def _app(self):
        sys.path.insert(0, str(LOCAL))
        try:
            import app as local_app
        except Exception as exc:
            self.skipTest("无法导入 local/app.py：%s" % exc)
        finally:
            sys.path.pop(0)
        return local_app

    @staticmethod
    def _stamp(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def test_clean_close_gives_exact_duration(self):
        """正常关闭（有 last_closed_at 且心跳不晚于它）：精确关闭时长。"""
        app = self._app()
        closed = datetime.now() - timedelta(hours=1)
        prev = {"last_closed_at": self._stamp(closed),
                "last_heartbeat_at": self._stamp(closed - timedelta(seconds=5))}
        life = app.compute_lifecycle(prev, datetime.now())
        self.assertEqual(life["close_quality"], "clean")
        self.assertAlmostEqual(life["closed_for_s"], 3600.0, delta=5.0)

    def test_abnormal_exit_only_heartbeat_approx(self):
        """异常退出（只有心跳）：只能给约值，质量标 abnormal。"""
        app = self._app()
        beat = datetime.now() - timedelta(hours=2)
        life = app.compute_lifecycle({"last_heartbeat_at": self._stamp(beat)},
                                     datetime.now())
        self.assertEqual(life["close_quality"], "abnormal")
        self.assertAlmostEqual(life["closed_for_s"], 7200.0, delta=5.0)
        # 心跳晚于关闭时间（关闭后又跑过片刻）同样按 abnormal 的约值处理
        closed = datetime.now() - timedelta(hours=3)
        life2 = app.compute_lifecycle(
            {"last_closed_at": self._stamp(closed),
             "last_heartbeat_at": self._stamp(closed + timedelta(minutes=10))},
            datetime.now())
        self.assertEqual(life2["close_quality"], "abnormal")

    def test_first_run_and_bad_values_are_unknown(self):
        """首次运行 / 字段不可解析：未知，不给编造的时长。"""
        app = self._app()
        life = app.compute_lifecycle({}, datetime.now())
        self.assertEqual(life["close_quality"], "unknown")
        self.assertIsNone(life["closed_for_s"])
        bad = app.compute_lifecycle({"last_closed_at": "not-a-time"}, datetime.now())
        self.assertEqual(bad["close_quality"], "unknown")

    def test_clock_rollback_is_unknown(self):
        """系统时钟回拨（参考点晚于本次启动）：精确/约值都不可信。"""
        app = self._app()
        future = datetime.now() + timedelta(hours=2)
        life = app.compute_lifecycle({"last_closed_at": self._stamp(future)},
                                     datetime.now())
        self.assertEqual(life["close_quality"], "unknown")
        self.assertIsNone(life["closed_for_s"])

    def test_lifecycle_writes_only_ui_json(self):
        """生命周期记录只写 ui.json；session/storage_state 内容与 mtime 均不变。"""
        import tempfile
        app = self._app()
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            session = root / "session.json"
            storage = root / "storage_state.json"
            session.write_text('{"token": "t", "saved_at": "2026-10-25 08:00:00"}',
                               encoding="utf-8")
            storage.write_text('{"cookies": []}', encoding="utf-8")
            # 旧 ui.json 只有界面偏好（缺生命周期字段）：必须平滑启动且偏好不丢
            (root / "ui.json").write_text(
                '{"filter": {"q": "讲座"}, "welcome_seen": true}', encoding="utf-8")
            before = (session.read_bytes(), storage.read_bytes(),
                      session.stat().st_mtime_ns, storage.stat().st_mtime_ns)
            life = app.AppLifecycle(prefs_file=root / "ui.json")
            try:
                after = (session.read_bytes(), storage.read_bytes(),
                         session.stat().st_mtime_ns, storage.stat().st_mtime_ns)
                self.assertEqual(before, after,
                                 "生命周期记录不得改动登录凭证文件（内容与 mtime）")
                prefs = json.loads((root / "ui.json").read_text(encoding="utf-8"))
                self.assertEqual(prefs["filter"]["q"], "讲座", "已有偏好不得被覆盖")
                self.assertEqual(prefs["welcome_seen"], True)
                self.assertIn("last_started_at", prefs, "启动必须写入本次开始时间")
                # F5 语义：重复 snapshot 拿到同一份进程级生命周期（started 不变）
                snap1 = life.snapshot()
                snap2 = life.snapshot()
                self.assertEqual(snap1["last_started_at"], snap2["last_started_at"])
                self.assertEqual(snap1["close_quality"], "unknown", "旧 ui.json 缺字段=未知")
            finally:
                life.finalize()
            closed = json.loads((root / "ui.json").read_text(encoding="utf-8"))["last_closed_at"]
            self.assertTrue(closed, "正常关闭必须写入 last_closed_at")
            life._thread.join(2)
            self.assertFalse(life._thread.is_alive(), "关闭后心跳线程必须退出")

    def test_heartbeat_merge_keeps_prefs_keys(self):
        """心跳是读-合并-写：不能把用户偏好（筛选/引导状态）整份覆盖掉。"""
        import tempfile
        app = self._app()
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            life = app.AppLifecycle(prefs_file=root / "ui.json")
            try:
                # 模拟界面在心跳之后保存了一个筛选偏好，再看心跳会不会把它覆盖掉
                life._write_merged({"filter": {"q": "志愿", "status": "all"}})
                life._write_merged({"last_heartbeat_at": self._stamp(datetime.now())})
                prefs = json.loads((root / "ui.json").read_text(encoding="utf-8"))
                self.assertEqual(prefs["filter"]["q"], "志愿",
                                 "心跳不得覆盖界面偏好（必须共用读-合并-写）")
                self.assertIn("last_started_at", prefs)
                self.assertIn("last_heartbeat_at", prefs)
            finally:
                life.finalize()

    def test_run_open_false_with_contradictory_times_unknown(self):
        """FG3：正常关闭标记下关闭早于本周期启动（如系统回拨后关机）→ 退未知，不伪造精确值。"""
        app = self._app()
        # 上一周期 20:00 启动、19:01 心跳、19:02 关闭；本次 19:03 启动：
        # run_open=false 但时间顺序矛盾，closed<started_prev → unknown 且无时长
        prev = {"run_open": False,
                "last_started_at": "2026-10-24 20:00:00",
                "last_heartbeat_at": "2026-10-24 19:01:00",
                "last_closed_at": "2026-10-24 19:02:00"}
        life = app.compute_lifecycle(prev, datetime(2026, 10, 24, 19, 3, 0))
        self.assertEqual(life["close_quality"], "unknown",
                         "时间矛盾时正常关闭标记不能覆盖顺序校验")
        self.assertIsNone(life["closed_for_s"], "矛盾时间不给精确关闭时长")

    def test_run_open_false_same_second_or_later_stays_clean(self):
        """关闭不早于本周期启动（含同一秒等号）且心跳不晚于关闭：维持精确值主流程。"""
        app = self._app()
        prev = {"run_open": False,
                "last_started_at": "2026-10-24 20:00:00",
                "last_heartbeat_at": "2026-10-24 20:30:00",
                "last_closed_at": "2026-10-24 21:00:00"}
        life = app.compute_lifecycle(prev, datetime(2026, 10, 24, 23, 0, 0))
        self.assertEqual(life["close_quality"], "clean")
        self.assertAlmostEqual(life["closed_for_s"], 7200.0, delta=1.0)
        same_second = app.compute_lifecycle(
            dict(prev, last_heartbeat_at="2026-10-24 20:00:00",
                 last_closed_at="2026-10-24 20:00:00"),
            datetime(2026, 10, 24, 23, 0, 0))
        self.assertEqual(same_second["close_quality"], "clean",
                         "同一秒关闭与启动允许等号，仍按正常关闭")

    def test_two_run_cycle_abnormal_then_clean(self):
        """跨运行周期闭环：崩溃退出（不 finalize）→ 下次报「约」；正常关闭 → 再下次报精确。"""
        import tempfile
        app = self._app()
        with tempfile.TemporaryDirectory() as name:
            prefs_file = Path(name) / "ui.json"
            # 第一次运行：2 小时前启动并留有心跳，然后像崩溃一样消失（不写关闭、run_open 残留 true）。
            # 直接改文件模拟：_write_merged 在置停后会被停止标记拦下，不能用它伪造迟到心跳。
            past = datetime.now() - timedelta(hours=2)
            prefs_file.write_text(json.dumps({
                "last_started_at": self._stamp(past),
                "last_heartbeat_at": self._stamp(past),
                "run_open": True,
            }, ensure_ascii=False), encoding="utf-8")
            # 第二次运行：run_open=true 只能给「约」，参考点是心跳（2 小时前）
            life2 = app.AppLifecycle(prefs_file=prefs_file)
            try:
                snap2 = life2.snapshot()
                self.assertEqual(snap2["close_quality"], "abnormal",
                                 "run_open 残留 true 的上一周期必须按异常退出处理")
                self.assertAlmostEqual(snap2["closed_for_s"], 7200.0, delta=5.0)
            finally:
                life2.finalize()   # 这次是正常关闭
            # 第三次运行：run_open=false + 关闭时间不早于该周期启动 → 精确（很短的）时长
            life3 = app.AppLifecycle(prefs_file=prefs_file)
            try:
                snap3 = life3.snapshot()
                self.assertEqual(snap3["close_quality"], "clean",
                                 "正常关闭后的重开必须拿到精确关闭时长")
                self.assertIsNotNone(snap3["closed_for_s"])
                self.assertLess(snap3["closed_for_s"], 60.0,
                                "紧邻重开的关闭时长应接近 0，而不是心跳粒度的约值")
            finally:
                life3.finalize()

    def test_run_open_marker_prevents_stale_clean(self):
        """R3 主场景：A 正常关闭后 B 重开即崩，C 不得把 A 的关闭时间当 B 的正常退出。"""
        app = self._app()
        closed_a = datetime.now() - timedelta(hours=3)
        started_b = datetime.now() - timedelta(minutes=10)
        prev = {"last_closed_at": self._stamp(closed_a),
                "last_started_at": self._stamp(started_b),
                "last_heartbeat_at": self._stamp(started_b),
                "run_open": True}       # B 崩溃：没人写 false
        life = app.compute_lifecycle(prev, datetime.now())
        self.assertEqual(life["close_quality"], "abnormal",
                         "上一周期 run_open=true 只能是异常退出")
        self.assertAlmostEqual(life["closed_for_s"], 600.0, delta=5.0,
                               msg="参考点必须是 B 的存活时刻（约 10 分钟），不是 A 的 3 小时前关闭")

    def test_run_open_with_bad_heartbeat_falls_back_to_start(self):
        """run_open=true 且心跳不可解析：退该周期启动时刻，仍给「约」。"""
        app = self._app()
        started = datetime.now() - timedelta(minutes=5)
        life = app.compute_lifecycle({"run_open": True,
                                      "last_started_at": self._stamp(started),
                                      "last_heartbeat_at": "garbage"},
                                     datetime.now())
        self.assertEqual(life["close_quality"], "abnormal")
        self.assertAlmostEqual(life["closed_for_s"], 300.0, delta=5.0)

    def test_same_second_close_reopen_marker_decides(self):
        """同一秒关闭→重开：关闭记录属于谁由 run_open 标记区分，不再靠秒级比大小。"""
        app = self._app()
        at = datetime.now() - timedelta(minutes=1)
        base = {"last_closed_at": self._stamp(at),
                "last_started_at": self._stamp(at),
                "last_heartbeat_at": self._stamp(at)}
        clean = app.compute_lifecycle(dict(base, run_open=False), datetime.now())
        self.assertEqual(clean["close_quality"], "clean", "run_open=false = 正常关闭，同秒也是精确")
        crash = app.compute_lifecycle(dict(base, run_open=True), datetime.now())
        self.assertEqual(crash["close_quality"], "abnormal", "run_open=true = 没走正常关闭，同秒也只是约值")

    def test_legacy_stale_closed_degrades_to_abnormal(self):
        """旧格式（无 run_open）：关闭记录早于上一周期启动 → 降级 abnormal，参考最近存活时刻。"""
        app = self._app()
        prev = {"last_closed_at": self._stamp(datetime.now() - timedelta(hours=3)),
                "last_started_at": self._stamp(datetime.now() - timedelta(minutes=10)),
                "last_heartbeat_at": self._stamp(datetime.now() - timedelta(minutes=11))}
        life = app.compute_lifecycle(prev, datetime.now())
        self.assertEqual(life["close_quality"], "abnormal",
                         "旧关闭记录属于更早周期，不得冒充最近周期的正常关闭")
        self.assertAlmostEqual(life["closed_for_s"], 600.0, delta=5.0,
                               msg="参考点取最近存活时刻（启动后 10 分钟），而不是 3 小时前的旧关闭")

    def test_finalize_ignores_straggler_heartbeat(self):
        """finalize 之后迟到的心跳必须被停止标记拦下：关闭记录不被覆写。"""
        import tempfile
        app = self._app()
        with tempfile.TemporaryDirectory() as name:
            prefs_file = Path(name) / "ui.json"
            life = app.AppLifecycle(prefs_file=prefs_file)
            life.finalize()
            closed_prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
            self.assertIs(closed_prefs.get("run_open"), False, "正常关闭必须写 run_open=false")
            self.assertTrue(closed_prefs.get("last_closed_at"))
            # 已置停后的心跳写入：锁内核验停止标记，必须直接放弃
            life._write_merged({"last_heartbeat_at":
                                self._stamp(datetime.now() + timedelta(hours=1))})
            after = json.loads(prefs_file.read_text(encoding="utf-8"))
            self.assertEqual(after, closed_prefs,
                             "置停后的心跳不得写入（last_heartbeat_at 不得晚于关闭时刻）")
            life._thread.join(2)
            self.assertFalse(life._thread.is_alive())

    def test_finalize_concurrent_with_heartbeat_no_deadlock(self):
        """心跳写入与 finalize 并发：不死锁，最终文件里心跳不晚于关闭。"""
        import tempfile
        app = self._app()
        with tempfile.TemporaryDirectory() as name:
            prefs_file = Path(name) / "ui.json"
            life = app.AppLifecycle(prefs_file=prefs_file)
            # 缩短心跳间隔制造真实并发，再立刻收尾
            life.INTERVAL_S = 0.01
            try:
                life.finalize()   # 不持锁 join：若实现持锁 join，这里会卡死超时
            finally:
                life._thread.join(5)
            self.assertFalse(life._thread.is_alive(), "关闭后心跳线程必须能退出（无死锁）")
            prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
            self.assertIs(prefs.get("run_open"), False)
            beat = app._parse_local_time(prefs.get("last_heartbeat_at"))
            closed = app._parse_local_time(prefs.get("last_closed_at"))
            self.assertIsNotNone(closed)
            if beat is not None:
                self.assertLessEqual(beat, closed,
                                     "最后一次心跳不得晚于关闭时刻（锁内核验停止标记）")

    def test_check_anchor_switches_on_relogin_and_never_writes(self):
        """决策 012：v2 锚点跟凭证走（新登录才建立）；复用/刷新不续期、读会话不写文件。"""
        import tempfile
        app = self._app()
        with tempfile.TemporaryDirectory() as name:
            session = Path(name) / "session.json"
            prefs = Path(name) / "ui.json"
            old_saved = datetime.now() - timedelta(hours=12)
            session.write_text(json.dumps(
                {"token": "t-old", "saved_at": self._stamp(old_saved)}), encoding="utf-8")
            with mock.patch.object(app, "SESSION_FILE", session), \
                 mock.patch.object(app, "PREFS", prefs):
                # 旧凭证没有 v2 锚点：不报数，但不要求重登
                first = app.login_state(TestWallClock())
                self.assertIsNone(first["hours_left"])
                self.assertTrue(first["session_present"])
                # 新登录成功 + 可信北京时间 → 建 v2 锚点（grab 登录路径调用）
                anchor_epoch = old_saved.timestamp()
                app.save_session_timing_anchor("t-old", anchor_epoch)
                with_anchor = app.login_state(TestWallClock())
                self.assertGreater(with_anchor["hours_left"], 0)
                self.assertAlmostEqual(
                    with_anchor["estimated_expires_at"],
                    anchor_epoch + app.TOKEN_LIFETIME_H * 3600.0, delta=5.0)
                # 复用/刷新不续期：锚点 saved_epoch 不变
                app.login_state(TestWallClock())
                self.assertEqual(
                    (app.load_prefs() or {}).get("session_timing_v2", {}).get("saved_epoch"),
                    anchor_epoch, "复查不得刷新锚点延长有效期")
                # 读取登录估算不得改动会话文件
                before = (session.read_bytes(), session.stat().st_mtime_ns)
                app.login_state(TestWallClock())
                self.assertEqual(before, (session.read_bytes(), session.stat().st_mtime_ns),
                                 "读取登录估算不得改动会话文件")
                # 重新登录换凭证：旧锚点立即作废（不匹配 → 不报数）
                new_saved = datetime.now() - timedelta(minutes=1)
                session.write_text(json.dumps(
                    {"token": "t-new", "saved_at": self._stamp(new_saved)}), encoding="utf-8")
                second = app.login_state(TestWallClock())
                self.assertIsNone(second["hours_left"],
                                  "换凭证后旧锚点不匹配：宁可不报数，不沿用旧估算")
            # 生命周期快照不再携带登录估算（唯一来源是 login_state 载荷）
            # check 事件载荷整理已下沉 application.login_service
            src = (LOCAL / "application" / "login_service.py").read_text(encoding="utf-8")
            self.assertIn('"estimated_expires_at": state.get("estimated_expires_at")', src,
                          "check 事件必须带当前会话的估算锚点（与 bootstrap 同源）")
            # 生命周期实现已下沉 infrastructure.lifecycle_store，快照契约随实现迁移
            life_src = (LOCAL / "infrastructure" / "lifecycle_store.py").read_text(encoding="utf-8")
            self.assertNotIn('"login": {',
                             life_src[life_src.index("def snapshot"):life_src.index("def finalize")],
                             "lifecycle 快照不得再内嵌登录估算（避免冻结旧值第二来源）")

    def test_saved_at_estimate_survives_overnight_gap(self):
        """saved_at 跨关闭间隔继续计算：估算只看绝对墙钟，不看本进程跑了多久。"""
        app = self._app()
        # 12 小时前保存的会话 + 中间关机 10 小时：剩余仍按 saved_at 起算 ≈ 5.5 小时
        saved = datetime.now() - timedelta(hours=12)
        life = app.compute_lifecycle({"last_closed_at": self._stamp(datetime.now() - timedelta(hours=10))},
                                     datetime.now())
        self.assertEqual(life["close_quality"], "clean")
        self.assertAlmostEqual(life["closed_for_s"], 36000.0, delta=5.0,
                               msg="关闭时长覆盖整段关机间隔（这里 10 小时）")
        est = saved.timestamp() + app.TOKEN_LIFETIME_H * 3600.0
        self.assertAlmostEqual((est - datetime.now().timestamp()) / 3600.0, 5.5, delta=0.01,
                               msg="登录剩余与进程是否运行无关，按 saved_at 绝对墙钟计算")

    def test_login_estimate_absolute_clock(self):
        """决策 012：登录估算 = v2 锚点 + 业务时钟（绝对业务时刻，跨关闭继续计算）。"""
        import tempfile
        app = self._app()
        with tempfile.TemporaryDirectory() as name:
            session = Path(name) / "session.json"
            prefs = Path(name) / "ui.json"
            saved = datetime.now() - timedelta(hours=10)
            session.write_text(json.dumps(
                {"token": "t", "saved_at": self._stamp(saved)}), encoding="utf-8")
            with mock.patch.object(app, "SESSION_FILE", session), \
                 mock.patch.object(app, "PREFS", prefs):
                # 无锚点：不报数（旧 saved_at 是本机墙钟文本）
                state = app.login_state(TestWallClock())
                self.assertIsNone(state["hours_left"])
                self.assertEqual(state["saved_at"], self._stamp(saved))
                # 建立 v2 锚点：按业务时钟算剩余
                app.save_session_timing_anchor("t", saved.timestamp())
                state = app.login_state(TestWallClock())
                expect = saved.timestamp() + app.TOKEN_LIFETIME_H * 3600.0
                self.assertAlmostEqual(state["estimated_expires_at"], expect, delta=1.0)
                self.assertAlmostEqual(state["hours_left"], 7.5, delta=0.01)
                # 估算到期（25 小时前登录）也不清理会话：有效性只由学校校验决定
                stale = datetime.now() - timedelta(hours=25)
                session.write_text(json.dumps(
                    {"token": "t", "saved_at": self._stamp(stale)}), encoding="utf-8")
                app.save_session_timing_anchor("t", stale.timestamp())
                body = session.read_text(encoding="utf-8")
                with mock.patch.object(app, "SESSION_FILE", session):
                    expired = app.login_state(TestWallClock())
                self.assertEqual(session.read_text(encoding="utf-8"), body,
                                 "login_state 不得改写 session 文件")
                self.assertTrue(expired["session_present"])
                self.assertEqual(expired["hours_left"], 0.0)
                self.assertLessEqual(expired["estimated_expires_at"],
                                     datetime.now().timestamp() + 0.001)


class LoginAnchorProductionPathTests(unittest.TestCase):
    """C07：新登录成功锚点必须由真实完成路径建立，而不是直接调用辅助保存函数。"""

    def _app(self):
        sys.path.insert(0, str(LOCAL))
        try:
            import app as local_app
        except Exception as exc:
            raise unittest.SkipTest("无法导入 local/app.py：%s" % exc)
        finally:
            try:
                sys.path.pop(0)
            except IndexError:
                pass
        return local_app

    def test_login_completion_creates_anchor(self):
        from fixtures.business_clock import FakeBusinessClock
        app = self._app()
        with tempfile.TemporaryDirectory() as name:
            session = Path(name) / "session.json"
            prefs = Path(name) / "ui.json"
            session.write_text(json.dumps({"token": "tok-new", "saved_at": "2026-10-26 10:00:00"}),
                               encoding="utf-8")
            clock = FakeBusinessClock(epoch=1793100000.0, uncertainty=0.1)

            class Proc:
                returncode = 0

                def __init__(self):
                    self.stdout = io.StringIO("[OK] 登录成功\n")

                def wait(self):
                    return 0

            api = app.Api(clock=clock)
            with mock.patch.object(app, "SESSION_FILE", session), \
                    mock.patch.object(app, "PREFS", prefs):
                api._pump(Proc(), "登录", kind="login")
                timing = (app.load_prefs() or {}).get("session_timing_v2") or {}
            self.assertEqual(timing.get("credential_id"),
                             app._credential_id("tok-new"))
            self.assertAlmostEqual(timing.get("saved_epoch"), 1793100000.0, delta=1.0)
            self.assertEqual(timing.get("time_source"), "ntp")

    def test_login_without_trusted_clock_writes_no_anchor(self):
        app = self._app()
        with tempfile.TemporaryDirectory() as name:
            session = Path(name) / "session.json"
            prefs = Path(name) / "ui.json"
            session.write_text(json.dumps({"token": "tok", "saved_at": "2026-10-26 10:00:00"}),
                               encoding="utf-8")

            class Proc:
                returncode = 0

                def __init__(self):
                    self.stdout = io.StringIO("[OK] 登录成功\n")

                def wait(self):
                    return 0

            api = app.Api()          # 未注入时钟：显式不可用
            with mock.patch.object(app, "SESSION_FILE", session), \
                    mock.patch.object(app, "PREFS", prefs):
                api._pump(Proc(), "登录", kind="login")
                self.assertFalse(prefs.exists(), "没有可信时钟不得写 v2 锚点")

    def test_failed_or_cancelled_login_keeps_existing_anchor(self):
        """失败/取消的重新登录：旧凭证与旧锚点一律不动，只上报失败结论。"""
        from fixtures.business_clock import FakeBusinessClock
        app = self._app()
        with tempfile.TemporaryDirectory() as name:
            session = Path(name) / "session.json"
            prefs = Path(name) / "ui.json"
            session.write_text(json.dumps({"token": "tok-old", "saved_at": "2026-10-26 10:00:00"}),
                               encoding="utf-8")
            prefs.write_text(json.dumps({"session_timing_v2": {
                "credential_id": app._credential_id("tok-old"),
                "saved_epoch": 1793000000.0, "time_source": "ntp", "uncertainty_s": 0.1}}),
                encoding="utf-8")

            class Proc:
                returncode = 1

                def __init__(self):
                    self.stdout = io.StringIO("[X] 窗口被关掉，登录没有完成\n")

                def wait(self):
                    return 1

            events = []
            api = app.Api(clock=FakeBusinessClock(epoch=1793100000.0, uncertainty=0.1))
            api._emit = lambda kind, payload: events.append((kind, payload))
            with mock.patch.object(app, "SESSION_FILE", session), \
                    mock.patch.object(app, "PREFS", prefs):
                api._pump(Proc(), "登录", kind="login")
                timing = (app.load_prefs() or {}).get("session_timing_v2") or {}
            self.assertEqual(timing.get("credential_id"), app._credential_id("tok-old"),
                             "失败/取消不得改写旧锚点")
            self.assertAlmostEqual(float(timing.get("saved_epoch") or 0), 1793000000.0, delta=0.001)
            done = [p for k, p in events if k == "done"]
            self.assertEqual(len(done), 1)
            self.assertFalse(done[0]["ok"])
            self.assertEqual(done[0]["reason"], "窗口被关掉，登录没有完成")

    def test_failed_login_creates_no_anchor_for_fresh_session(self):
        """本来没有锚点、登录又失败：不得凭空冒出 session_timing_v2。"""
        from fixtures.business_clock import FakeBusinessClock
        app = self._app()
        with tempfile.TemporaryDirectory() as name:
            session = Path(name) / "session.json"
            prefs = Path(name) / "ui.json"
            session.write_text(json.dumps({"token": "tok", "saved_at": "2026-10-26 10:00:00"}),
                               encoding="utf-8")

            class Proc:
                returncode = 1

                def __init__(self):
                    self.stdout = io.StringIO("[X] 打不开登录页\n")

                def wait(self):
                    return 1

            api = app.Api(clock=FakeBusinessClock(epoch=1793100000.0, uncertainty=0.1))
            api._emit = lambda kind, payload: None
            with mock.patch.object(app, "SESSION_FILE", session), \
                    mock.patch.object(app, "PREFS", prefs):
                api._pump(Proc(), "登录", kind="login")
                timing = (app.load_prefs() or {}).get("session_timing_v2")
            self.assertIsNone(timing, "失败路径不得建立锚点")

    def test_successful_relogin_replaces_old_anchor(self):
        """成功重登：锚点换成新凭证，旧凭证的锚点不能继承下来。"""
        from fixtures.business_clock import FakeBusinessClock
        app = self._app()
        with tempfile.TemporaryDirectory() as name:
            session = Path(name) / "session.json"
            prefs = Path(name) / "ui.json"
            session.write_text(json.dumps({"token": "tok-new", "saved_at": "2026-10-27 10:00:00"}),
                               encoding="utf-8")
            prefs.write_text(json.dumps({"session_timing_v2": {
                "credential_id": app._credential_id("tok-old"),
                "saved_epoch": 1793000000.0, "time_source": "ntp", "uncertainty_s": 0.1}}),
                encoding="utf-8")

            class Proc:
                returncode = 0

                def __init__(self):
                    self.stdout = io.StringIO("[OK] 登录成功\n")

                def wait(self):
                    return 0

            api = app.Api(clock=FakeBusinessClock(epoch=1793100000.0, uncertainty=0.1))
            with mock.patch.object(app, "SESSION_FILE", session), \
                    mock.patch.object(app, "PREFS", prefs):
                api._pump(Proc(), "登录", kind="login")
                timing = (app.load_prefs() or {}).get("session_timing_v2") or {}
            self.assertEqual(timing.get("credential_id"), app._credential_id("tok-new"))
            self.assertAlmostEqual(float(timing.get("saved_epoch") or 0), 1793100000.0, delta=1.0)

    def test_anchor_write_failure_never_swallows_login_done(self):
        """锚点写入失败也必须照常上报登录结论：否则界面永远停在「登录中」。"""
        from fixtures.business_clock import FakeBusinessClock
        app = self._app()
        with tempfile.TemporaryDirectory() as name:
            session = Path(name) / "session.json"
            prefs = Path(name) / "ui.json"
            session.write_text(json.dumps({"token": "tok-new", "saved_at": "2026-10-27 10:00:00"}),
                               encoding="utf-8")

            class Proc:
                returncode = 0

                def __init__(self):
                    self.stdout = io.StringIO("[OK] 登录成功\n")

                def wait(self):
                    return 0

            events = []
            api = app.Api(clock=FakeBusinessClock(epoch=1793100000.0, uncertainty=0.1))
            api._emit = lambda kind, payload: events.append((kind, payload))
            with mock.patch.object(app, "SESSION_FILE", session), \
                    mock.patch.object(app, "PREFS", prefs), \
                    mock.patch.object(app, "save_session_timing_anchor",
                                      side_effect=RuntimeError("prefs lock timeout")):
                api._pump(Proc(), "登录", kind="login")
            done = [p for k, p in events if k == "done"]
            self.assertEqual(len(done), 1, "锚点失败不得中断 done 上报")
            self.assertTrue(done[0]["ok"])
            logs = [p for k, p in events if k == "log"]
            self.assertTrue(any("锚点没有写成" in str(line) for line in logs),
                            "锚点失败必须留下可诊断的日志")


class LifecycleV2ClockTests(unittest.TestCase):
    """C08：晚校时恢复、未校准关闭、v1/v2 混存与异常退出事实。"""

    def _app(self):
        sys.path.insert(0, str(LOCAL))
        try:
            import app as local_app
        except Exception as exc:
            raise unittest.SkipTest("无法导入 local/app.py：%s" % exc)
        finally:
            try:
                sys.path.pop(0)
            except IndexError:
                pass
        return local_app

    def test_previous_clean_v2_then_crash_is_abnormal(self):
        app = self._app()
        prev = {"run_id": "old", "run_open": True, "quality": "ready",
                "started_epoch": 1000.0, "heartbeat_epoch": 1060.0,
                "closed_epoch": None}
        result = app.compute_lifecycle_v2(prev, 2000.0)
        self.assertEqual(result["close_quality"], "abnormal")
        self.assertAlmostEqual(result["closed_for_s"], 940.0)

    def test_previous_unknown_quality_never_becomes_precise(self):
        app = self._app()
        prev = {"run_id": "old", "run_open": False, "quality": "unknown",
                "started_epoch": None, "closed_epoch": None}
        result = app.compute_lifecycle_v2(prev, 2000.0)
        self.assertEqual(result["close_quality"], "unknown")
        self.assertIsNone(result["closed_for_s"])

    def test_v1_only_falls_back_to_text_path(self):
        app = self._app()
        self.assertIsNone(app.compute_lifecycle_v2({"last_closed_at": "x"}, 2000.0))

    def test_late_calibration_recovers_start_epoch(self):
        app = self._app()
        from fixtures.business_clock import ClockUnavailable

        class FlakyClock:
            def __init__(self):
                self.fail = True

            def now(self):
                if self.fail:
                    raise ClockUnavailable("首校时未完成")
                return 1793100000.0

        clock = FlakyClock()
        with tempfile.TemporaryDirectory() as name:
            prefs = Path(name) / "ui.json"
            prefs.write_text(json.dumps({
                "lifecycle_v2": {"run_id": "old", "run_open": False,
                                 "quality": "ready", "started_epoch": 1793090000.0,
                                 "heartbeat_epoch": 1793090000.0,
                                 "closed_epoch": 1793091000.0}}), encoding="utf-8")
            with mock.patch.object(app.AppLifecycle, "INTERVAL_S", 0.01):
                life = app.AppLifecycle(prefs_file=prefs, now_fn=clock.now)
                try:
                    self.assertIsNone(life.snapshot().get("closed_for_s"),
                                      "首校时未完成不得冒充精确关闭时长")
                    clock.fail = False
                    # 心跳是后台线程：轮询到状态真正恢复，不靠固定 sleep 碰运气
                    deadline = time.monotonic() + 5.0
                    snap = life.snapshot()
                    while time.monotonic() < deadline and snap["close_quality"] != "clean":
                        time.sleep(0.02)
                        snap = life.snapshot()
                finally:
                    life.finalize()      # 走生产关闭路径，保证最后一次写入落盘
            self.assertEqual(snap["close_quality"], "clean",
                             "迟到校准后应恢复本周期起点并算出关闭时长")
            self.assertAlmostEqual(snap["closed_for_s"], 9000.0, delta=5.0)
            stored = json.loads(prefs.read_text(encoding="utf-8"))["lifecycle_v2"]
            self.assertEqual(stored.get("run_id"), life._run_id)
            self.assertIsNotNone(stored.get("started_epoch"))

    def test_uncalibrated_normal_close_keeps_unknown_quality(self):
        app = self._app()
        from fixtures.business_clock import ClockUnavailable

        class DeadClock:
            def now(self):
                raise ClockUnavailable("始终未校准")

        with tempfile.TemporaryDirectory() as name:
            prefs = Path(name) / "ui.json"
            life = app.AppLifecycle(prefs_file=prefs, now_fn=DeadClock().now)
            life._stop.set()
            life.finalize()
            stored = json.loads(prefs.read_text(encoding="utf-8"))
            self.assertFalse(stored.get("run_open"), "正常关闭事实必须落盘")
            self.assertTrue(stored.get("last_closed_at"))
            self.assertEqual(stored["lifecycle_v2"].get("quality"), "unknown")
            self.assertFalse(stored["lifecycle_v2"].get("run_open"))


_LIFECYCLE_CHILD = r'''
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import app

prefs = Path(sys.argv[2])
base = float(sys.argv[3])
app.PREFS = prefs          # 生产 update_prefs 也钉在本轮临时文件上


class Clock:
    def __init__(self):
        self.epoch = base

    def now(self):
        return self.epoch


clock = Clock()
life = app.AppLifecycle(prefs_file=prefs, now_fn=clock.now)


def out(**kw):
    print(json.dumps(kw, ensure_ascii=False), flush=True)


out(run_id=life._run_id, base=base)
for line in sys.stdin:
    cmd = line.strip()
    if not cmd:
        continue
    if cmd == "beat":
        clock.epoch = base + 1.0
        ok = life._beat_once()
        out(ok=ok, run_id=life._run_id, foreign=life._foreign_owner)
    elif cmd == "finalize":
        clock.epoch = base + 2.0
        life.finalize()
        out(ok=True, run_id=life._run_id)
    elif cmd == "save_pref":
        clock.epoch = base + 4.0
        started = time.monotonic()
        app.update_prefs({"probe_saved": True})
        out(ok=True, elapsed=time.monotonic() - started)
    elif cmd == "hold_slow":
        clock.epoch = base + 3.0
        # 真实跨进程事务：写入后仍持锁 0.4 秒，制造读-写临界区交错窗口
        with app._prefs_transaction(prefs):
            data = app.load_prefs_from(prefs)
            data["last_heartbeat_at"] = "9999-01-01 00:00:00"
            data["lifecycle_v2"] = {
                "run_id": life._run_id, "run_open": True,
                "started_epoch": base, "heartbeat_epoch": base + 3.0,
                "quality": "ready"}
            app._atomic_write_prefs(prefs, data)
            out(holding=True)
            time.sleep(0.4)
        out(holding=False)
    elif cmd == "crash":
        os._exit(0)
    elif cmd == "exit":
        break
'''


class LifecycleTransactionTests(unittest.TestCase):
    """CB03：未知质量、旧文本不迁移、归属事务与真实双进程排他。

    每个用例使用独立临时 prefs 和业务时钟，走真实 AppLifecycle 入口；
    跨进程用例由真实子进程执行生产类，不在测试进程里直接改字典。
    """

    EPOCH = 1793100000.0

    def _app(self):
        sys.path.insert(0, str(LOCAL))
        try:
            import app as local_app
        except Exception as exc:
            raise unittest.SkipTest("无法导入 local/app.py：%s" % exc)
        finally:
            try:
                sys.path.pop(0)
            except IndexError:
                pass
        return local_app

    @staticmethod
    def _read(prefs):
        return json.loads(Path(prefs).read_text(encoding="utf-8"))

    @staticmethod
    def _legacy_clean_prefs(prefs: Path) -> None:
        """旧格式（无 v2）但墙钟文本能推出精确关闭时长的一组数据。"""
        now = datetime.now()
        prefs.write_text(json.dumps({
            "run_open": False,
            "last_started_at": (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
            "last_heartbeat_at": (now - timedelta(hours=1, minutes=59)).strftime("%Y-%m-%d %H:%M:%S"),
            "last_closed_at": (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "filter": {"q": "讲座"},
        }, ensure_ascii=False), encoding="utf-8")

    def test_heartbeat_failure_is_unknown_and_keeps_normal_close_fact(self):
        app = self._app()
        from fixtures.business_clock import ClockUnavailable

        class ToggleClock:
            def __init__(self):
                self.epoch = LifecycleTransactionTests.EPOCH
                self.ok = True

            def now(self):
                if not self.ok:
                    raise ClockUnavailable("取时失败")
                return self.epoch

        clock = ToggleClock()
        with tempfile.TemporaryDirectory() as name:
            prefs = Path(name) / "ui.json"
            life = app.AppLifecycle(prefs_file=prefs, now_fn=clock.now)
            try:
                clock.epoch = self.EPOCH + 30.0
                self.assertTrue(life._beat_once())
                stored = self._read(prefs)["lifecycle_v2"]
                self.assertEqual(stored["quality"], "ready")
                beat = stored["heartbeat_epoch"]
                clock.ok = False
                # 这次心跳仍要落盘（质量降级）；返回值表示「写入了」，不是「时刻可信」
                self.assertTrue(life._beat_once())
                stored = self._read(prefs)["lifecycle_v2"]
                self.assertEqual(stored["quality"], "unknown",
                                 "本次心跳取不到可信时刻必须降为 unknown")
                self.assertEqual(stored["heartbeat_epoch"], beat,
                                 "不得把旧心跳写成新的可信事实")
                self.assertTrue(stored["run_open"])
            finally:
                life.finalize()
            stored = self._read(prefs)["lifecycle_v2"]
            self.assertFalse(stored["run_open"], "正常关闭文本事实仍须保留")
            self.assertEqual(stored["quality"], "unknown")
            self.assertNotIn("closed_epoch", stored,
                             "关闭时刻不可信时不得写 closed_epoch")
            # 下一次启动：上一周期没有可信关闭时刻 → unknown，而不是拿旧心跳给「约」
            life2 = app.AppLifecycle(prefs_file=prefs,
                                     now_fn=lambda: self.EPOCH + 300.0)
            try:
                snap = life2.snapshot()
                self.assertEqual(snap["close_quality"], "unknown")
                self.assertIsNone(snap["closed_for_s"])
            finally:
                life2.finalize()

    def test_trusted_clock_without_v2_never_uses_wall_clock_text(self):
        app = self._app()
        with tempfile.TemporaryDirectory() as name:
            prefs = Path(name) / "ui.json"
            self._legacy_clean_prefs(prefs)
            life = app.AppLifecycle(prefs_file=prefs, now_fn=lambda: self.EPOCH)
            try:
                snap = life.snapshot()
                self.assertEqual(snap["close_quality"], "unknown",
                                 "生产时钟路径缺 v2 时不得退回旧墙钟精确值")
                self.assertIsNone(snap["closed_for_s"])
                self.assertEqual(self._read(prefs)["filter"]["q"], "讲座",
                                 "启动写入不得覆盖已有偏好")
            finally:
                life.finalize()

    def test_late_calibration_without_v2_stays_unknown(self):
        app = self._app()
        from fixtures.business_clock import ClockUnavailable

        class LateClock:
            def __init__(self):
                self.ok = False

            def now(self):
                if not self.ok:
                    raise ClockUnavailable("首校时未完成")
                return LifecycleTransactionTests.EPOCH

        clock = LateClock()
        with tempfile.TemporaryDirectory() as name:
            prefs = Path(name) / "ui.json"
            self._legacy_clean_prefs(prefs)
            life = app.AppLifecycle(prefs_file=prefs, now_fn=clock.now)
            try:
                self.assertEqual(life.snapshot()["close_quality"], "unknown")
                clock.ok = True
                self.assertTrue(life._beat_once())
                snap = life.snapshot()
                self.assertEqual(snap["close_quality"], "unknown",
                                 "迟到校准只恢复本周期起点，不把旧墙钟文本变成可信关闭")
                self.assertIsNone(snap["closed_for_s"])
                stored = self._read(prefs)["lifecycle_v2"]
                self.assertIsNotNone(stored.get("started_epoch"))
                self.assertEqual(stored.get("quality"), "ready")
            finally:
                life.finalize()

    def test_ready_normal_close_is_exact_for_next_run(self):
        app = self._app()
        clock = {"epoch": self.EPOCH}
        with tempfile.TemporaryDirectory() as name:
            prefs = Path(name) / "ui.json"
            life1 = app.AppLifecycle(prefs_file=prefs,
                                     now_fn=lambda: clock["epoch"])
            try:
                clock["epoch"] = self.EPOCH + 30.0
                life1._beat_once()
                clock["epoch"] = self.EPOCH + 600.0
            finally:
                life1.finalize()
            clock["epoch"] = self.EPOCH + 660.0
            life2 = app.AppLifecycle(prefs_file=prefs,
                                     now_fn=lambda: clock["epoch"])
            try:
                snap = life2.snapshot()
                self.assertEqual(snap["close_quality"], "clean")
                self.assertAlmostEqual(snap["closed_for_s"], 60.0, delta=0.1)
            finally:
                life2.finalize()

    def test_crash_without_finalize_is_only_approximate(self):
        app = self._app()
        clock = {"epoch": self.EPOCH}
        with tempfile.TemporaryDirectory() as name:
            prefs = Path(name) / "ui.json"
            life1 = app.AppLifecycle(prefs_file=prefs,
                                     now_fn=lambda: clock["epoch"])
            clock["epoch"] = self.EPOCH + 120.0
            life1._beat_once()
            # 模拟崩溃：只停心跳线程，不调用 finalize（run_open 残留 true）
            life1._stop.set()
            life1._thread.join(2.0)
            self.assertTrue(self._read(prefs)["lifecycle_v2"]["run_open"])
            clock["epoch"] = self.EPOCH + 300.0
            life2 = app.AppLifecycle(prefs_file=prefs,
                                     now_fn=lambda: clock["epoch"])
            try:
                snap = life2.snapshot()
                self.assertEqual(snap["close_quality"], "abnormal")
                self.assertAlmostEqual(snap["closed_for_s"], 180.0, delta=0.1)
            finally:
                life2.finalize()

    # ---- 真实双进程：归属接管与跨进程排他 -------------------------------
    def _spawn_child(self, prefs: Path, base: float):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(LOCAL) + os.pathsep + str(TESTS_DIR)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["TJISE_DATA_DIR"] = str(Path(prefs).parent / "data-root")
        env.pop("PYTHONPYCACHEPREFIX", None)
        proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-c", _LIFECYCLE_CHILD,
             str(LOCAL), str(prefs), str(base)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", env=env,
            cwd=tempfile.gettempdir())
        return proc, self._await_child(proc, timeout=20.0)

    def _await_child(self, proc, timeout=10.0):
        box = {}

        def reader():
            box["line"] = proc.stdout.readline()

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            raise AssertionError("生命周期子进程未在 %.1f 秒内响应" % timeout)
        line = box.get("line") or ""
        if not line:
            err = proc.stderr.read() if proc.stderr else ""
            raise AssertionError("生命周期子进程提前退出：%s" % str(err)[-800:])
        return json.loads(line)

    def _send_child(self, proc, command: str, timeout=10.0):
        proc.stdin.write(command + "\n")
        proc.stdin.flush()
        return self._await_child(proc, timeout)

    def _close_child(self, proc) -> None:
        if proc is None:
            return
        try:
            if proc.poll() is None:
                try:
                    proc.stdin.write("exit\n")
                    proc.stdin.flush()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(5.0)
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass

    def test_takeover_and_cross_process_lock(self):
        self._app()       # 子进程可导入时才运行
        with tempfile.TemporaryDirectory() as name:
            prefs = Path(name) / "ui.json"
            proc_a = proc_b = None
            try:
                proc_a, info_a = self._spawn_child(prefs, self.EPOCH)
                proc_b, info_b = self._spawn_child(prefs, self.EPOCH + 100.0)
                self.assertNotEqual(info_a["run_id"], info_b["run_id"])

                # B 写入自己的记录后继续持锁 0.4 秒
                proc_b.stdin.write("hold_slow\n")
                proc_b.stdin.flush()
                self.assertTrue(self._await_child(proc_b)["holding"])
                before = prefs.read_bytes()
                # A 的迟到心跳：在事务内看到新 owner，整份放弃，文件字节不变
                result = self._send_child(proc_a, "beat")
                self.assertFalse(result["ok"])
                self.assertTrue(result["foreign"])
                self.assertEqual(prefs.read_bytes(), before,
                                 "旧实例不得改写新 owner 的周期")
                self.assertFalse(self._await_child(proc_b)["holding"])
                # A 的迟到正常关闭同样让位
                self._send_child(proc_a, "finalize")
                stored = self._read(prefs)["lifecycle_v2"]
                self.assertEqual(stored["run_id"], info_b["run_id"])
                self.assertTrue(stored["run_open"])
                self.assertEqual(stored["heartbeat_epoch"], self.EPOCH + 103.0)

                # 真实跨进程排他：B 持锁时 A 的偏好保存必须等锁，随后合并而非覆盖
                proc_b.stdin.write("hold_slow\n")
                proc_b.stdin.flush()
                self.assertTrue(self._await_child(proc_b)["holding"])
                proc_a.stdin.write("save_pref\n")
                proc_a.stdin.flush()
                time.sleep(0.15)
                self.assertIsNone(proc_a.poll(), "A 应在等待跨进程锁而不是退出")
                saved = self._await_child(proc_a)
                self.assertGreaterEqual(saved["elapsed"], 0.3,
                                        "A 的保存必须被 B 的事务阻塞")
                self.assertFalse(self._await_child(proc_b)["holding"])
                data = self._read(prefs)
                self.assertTrue(data.get("probe_saved"))
                self.assertEqual(data["lifecycle_v2"]["run_id"], info_b["run_id"])
                self.assertEqual(data["lifecycle_v2"]["heartbeat_epoch"],
                                 self.EPOCH + 103.0,
                                 "偏好保存不得覆盖较新的生命周期")

                # B 正常关闭：写入 B 自己的 closed_epoch
                self._send_child(proc_b, "finalize")
                stored = self._read(prefs)["lifecycle_v2"]
                self.assertEqual(stored["run_id"], info_b["run_id"])
                self.assertFalse(stored["run_open"])
                self.assertEqual(stored["closed_epoch"], self.EPOCH + 102.0)
            finally:
                self._close_child(proc_a)
                self._close_child(proc_b)


if __name__ == "__main__":
    unittest.main()
