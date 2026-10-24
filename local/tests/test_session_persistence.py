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
import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

LOCAL = Path(__file__).resolve().parents[1]


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
            with mock.patch.object(app, "SESSION_FILE", session):
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
                stale = (datetime.now() - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S")
                session.write_text(json.dumps({"token": "t", "saved_at": stale}), encoding="utf-8")
                expired = app.login_state()
                self.assertTrue(expired["session_present"])
                self.assertTrue(expired["session_expired"])

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
            api = app.Api()
            api._last_check = {"result": result, "checked_at": 0.0}
            res = api.start_plan([dict(item) for item in items])
            self.assertFalse(res["ok"], "%s 必须拒绝开始真实计划" % result)
            self.assertIn(needle, res["text"], "%s 的拒绝理由要给出下一步" % result)
        # 检查进行中：同样拒绝，避免拿到半截结论就开始
        api = app.Api()
        api._checking = True
        res = api.start_plan([dict(item) for item in items])
        self.assertFalse(res["ok"])
        self.assertIn("正在检查登录态", res["text"])
        # 上一轮 valid + 新一轮检查进行中：仍拒绝（refresh 不清旧 valid，门禁必须显式排除 checking）
        api = app.Api()
        api._last_check = {"result": "valid", "checked_at": 0.0}
        api._checking = True
        res = api.start_plan([dict(item) for item in items])
        self.assertFalse(res["ok"], "新一轮检查完成前不得放行真实计划")
        self.assertIn("正在检查登录态", res["text"])
        # demo 只绕过登录门禁：其余计划校验照走（缺活动时间的样本落到校验层）
        with mock.patch.object(app, "DEMO", True):
            api = app.Api()
            res = api.start_plan([dict(item) for item in items])
            self.assertFalse(res["ok"])
            self.assertNotIn("登录", res.get("text", ""),
                             "demo 放行的只是登录门禁，不是所有校验")
        # valid：越过登录门禁，走到计划校验层
        api = app.Api()
        api._last_check = {"result": "valid", "checked_at": 0.0}
        res = api.start_plan([dict(item) for item in items])
        self.assertFalse(res["ok"])
        self.assertNotIn("登录", res.get("text", ""))

    def test_start_plan_rejects_closed_activity(self):
        """报名截止是硬门禁：已截止活动即使通过登录门禁也不能进入 worker。"""
        app = self._app()
        api = app.Api()
        api._last_check = {"result": "valid", "checked_at": 0.0}
        past_end = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        closed = {"id": "c1", "name": "已截止活动", "sign_start": "2000-01-01 09:00:00",
                  "sign_end": past_end, "act_start": "2099-01-03 09:00:00",
                  "act_end": "2099-01-03 11:00:00", "signed": False}
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
                api = app.Api()
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


if __name__ == "__main__":
    unittest.main()
