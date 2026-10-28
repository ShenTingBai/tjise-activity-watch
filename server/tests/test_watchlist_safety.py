#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""预报名清单的并发与命名安全（离线，不碰 server/data）。

覆盖审阅 P2-3 / P2-4：
- CLI 的 add/remove/mark 与 runner 并发时不能整读整写互相覆盖；
- 清单 JSON 损坏时如实失败，不用空清单覆盖真人点出来的预报名；
- unit 名与状态文件用完整 id 的稳定编码 + 哈希，同前缀 id 不再串号。

所有文件都在临时目录里，`WATCHLIST_FILE` / `WATCHLIST_LOCK` 显式覆盖。

@author LiXinwen
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import watchlist as wl  # noqa: E402
import watchlist_runner as runner  # noqa: E402


def _test_root() -> Path:
    root = Path(os.environ.get("TJISE_TEST_ROOT")
                or "D:/@Temp/tjise-watchlist-safety-20261027")
    if os.name != "nt" and not os.environ.get("TJISE_TEST_ROOT"):
        root = Path(tempfile.gettempdir()) / "tjise-watchlist-safety-20261027"
    root.mkdir(parents=True, exist_ok=True)
    return root


class WatchlistSafetyTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="watchlist-", dir=str(_test_root())))
        self._old = {key: os.environ.get(key)
                     for key in ("WATCHLIST_FILE", "WATCHLIST_LOCK", "GRAB_STATUS_DIR")}
        os.environ["WATCHLIST_FILE"] = str(self.dir / "watchlist.json")
        os.environ["WATCHLIST_LOCK"] = str(self.dir / "watchlist.lock")
        os.environ["GRAB_STATUS_DIR"] = str(self.dir)
        importlib.reload(wl)
        importlib.reload(runner)

    def tearDown(self):
        for key, value in self._old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(wl)
        importlib.reload(runner)

    def _write(self, entries):
        (self.dir / "watchlist.json").write_text(
            json.dumps({"entries": entries}, ensure_ascii=False), encoding="utf-8")

    def _read_ids(self):
        return sorted(entry["id"] for entry in wl.load())

    def test_concurrent_add_and_mark_do_not_lose_entries(self):
        self._write([{"id": "base", "name": "底座", "open_at": "2099-01-01 09:00:00",
                      "status": "queued"}])
        errors = []

        def add(index):
            try:
                wl.add("act-%02d" % index, "活动%d" % index, "2099-01-01 09:00:00")
            except Exception as exc:      # noqa: BLE001 - 测试收集所有失败
                errors.append(exc)

        def mark(index):
            try:
                wl.mark("base", attempts=index)
            except Exception as exc:      # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=add, args=(i,)) for i in range(8)]
        threads += [threading.Thread(target=mark, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(errors, errors)
        ids = self._read_ids()
        self.assertEqual(len(ids), 9, "并发 add 不得丢用户预报名：%s" % ids)
        self.assertIn("base", ids)

    def test_concurrent_remove_and_add_keep_other_entries(self):
        self._write([{"id": "a", "name": "A", "open_at": "2099-01-01 09:00:00",
                      "status": "queued"},
                     {"id": "b", "name": "B", "open_at": "2099-01-01 09:00:00",
                      "status": "queued"}])
        wl.remove("a")
        wl.add("c", "C", "2099-01-01 09:00:00")
        self.assertEqual(self._read_ids(), ["b", "c"])
        self.assertTrue((self.dir / "watchlist.lock").exists(), "必须真的用到跨进程锁文件")

    def test_corrupt_json_fails_without_overwriting(self):
        path = self.dir / "watchlist.json"
        broken = '{"entries": [{"id": "user-pick"'
        path.write_text(broken, encoding="utf-8")
        with self.assertRaises(RuntimeError):
            wl.add("x", "X", "2099-01-01 09:00:00")
        self.assertEqual(path.read_text(encoding="utf-8"), broken,
                         "损坏的清单必须原样保留，不能清空")

    def test_same_prefix_ids_get_distinct_units_and_status_files(self):
        left = {"id": "activity-abcdefgh-001", "name": "左"}
        right = {"id": "activity-abcdefgh-002", "name": "右"}
        self.assertNotEqual(runner.unit_name(left), runner.unit_name(right))
        self.assertNotEqual(runner.status_file(left), runner.status_file(right))
        self.assertIn(runner.unit_token(left["id"]), runner.unit_name(left))
        self.assertNotEqual(runner.unit_token(left["id"]).split("-")[-1],
                            runner.unit_token(right["id"]).split("-")[-1],
                            "哈希必须覆盖完整 id，同前缀不再同名")

    def test_unit_name_is_systemd_safe(self):
        entry = {"id": "活动 id/带 空格&符号"}
        name = runner.unit_name(entry)
        self.assertLessEqual(len(name), 255)
        self.assertRegex(name, r"^[A-Za-z0-9_.-]+$")

    def test_saved_file_is_owner_only_and_atomic(self):
        wl.add("a", "A", "2099-01-01 09:00:00")
        path = self.dir / "watchlist.json"
        self.assertTrue(path.is_file())
        self.assertEqual([p.name for p in self.dir.glob("*.tmp")], [],
                         "原子写不得留下临时文件")
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_add_rejects_sign_end_before_open(self):
        # 截止早于开放等于一开跑就过期：拒绝写入坏策略
        with self.assertRaises(RuntimeError):
            wl.add("a", "A", "2099-01-01 09:00:00",
                   sign_end="2099-01-01 08:00:00")

    def test_add_accepts_and_freezes_sign_end(self):
        entry = wl.add("a", "A", "2099-01-01 09:00:00",
                       sign_end="2099-01-01 23:59:59")
        self.assertEqual(entry["sign_end"], "2099-01-01 23:59:59")
        self.assertEqual(entry["open_at"], "2099-01-01 09:00:00")
        self.assertEqual(entry["window"], wl.DEFAULT_WINDOW)


def _soon(seconds=30) -> str:
    return (datetime.now() + timedelta(seconds=seconds)).strftime(wl.FMT)


class WatchlistRunnerLaunchTests(unittest.TestCase):
    """runner 单飞、取消、窗口过期与冻结参数传递（替身，不碰 systemd/网络）。

    覆盖 2026-10-28 服务器契约：同一 activity-id 禁止重复拉起；取消后不启动；
    过窗口标 missed；启动参数原样传冻结的 open_at/lead/window/sign_end；
    重复唤醒（cron 每分钟）不重拉 running 中的场次。
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="watchlist-runner-", dir=str(_test_root())))
        self._old = {key: os.environ.get(key)
                     for key in ("WATCHLIST_FILE", "WATCHLIST_LOCK", "GRAB_STATUS_DIR")}
        os.environ["WATCHLIST_FILE"] = str(self.dir / "watchlist.json")
        os.environ["WATCHLIST_LOCK"] = str(self.dir / "watchlist.lock")
        os.environ["GRAB_STATUS_DIR"] = str(self.dir)
        importlib.reload(wl)
        importlib.reload(runner)
        self.refresh_calls = []
        # 卡片刷新会走 grab_card → 飞书：替身接管，绝不外发
        self._refresh_orig = wl._refresh_card
        wl._refresh_card = lambda: self.refresh_calls.append(1)

    def tearDown(self):
        wl._refresh_card = self._refresh_orig
        for key, value in self._old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(wl)
        importlib.reload(runner)

    def _write(self, entries):
        (self.dir / "watchlist.json").write_text(
            json.dumps({"entries": entries}, ensure_ascii=False), encoding="utf-8")

    def _entry(self, **kw):
        entry = {"id": "act-a", "name": "活动甲", "open_at": _soon(),
                 "lead": 20.0, "window": 120.0, "sign_end": "",
                 "status": "queued", "attempts": 0}
        entry.update(kw)
        return entry

    def _run_once(self, unit_active, launches):
        with mock.patch.object(runner, "unit_active", lambda unit: unit_active), \
                mock.patch.object(runner, "launch",
                                  lambda entry, dry: launches.append(entry["id"]) or True):
            return runner.main([])

    def test_same_activity_id_single_flight_when_unit_active(self):
        # runner 重启后清单还停在 queued、但同名 unit 活着：收编，不重复拉起
        self._write([self._entry()])
        launches = []
        self._run_once(unit_active=True, launches=launches)
        self.assertEqual(launches, [], "unit 存活时禁止重复启动同一 activity-id")
        entries = wl.load()
        self.assertEqual(entries[0]["status"], "running", "收编为 running 等退出码")

    def test_duplicate_activity_id_entries_launch_once(self):
        # 清单损坏才会出现重复 id：一轮只拉起一次
        self._write([self._entry(), self._entry(name="活动甲副本")])
        launches = []
        self._run_once(unit_active=False, launches=launches)
        self.assertEqual(launches, ["act-a"], "同一 activity-id 只允许一个 runner")

    def test_cancelled_entry_never_launches(self):
        self._write([self._entry()])
        wl.remove("act-a")
        launches = []
        self._run_once(unit_active=False, launches=launches)
        self.assertEqual(launches, [], "取消后不启动")
        self.assertEqual(wl.load(), [])

    def test_expired_window_marks_missed_without_launch(self):
        self._write([self._entry(open_at=_soon(-700))])
        launches = []
        self._run_once(unit_active=False, launches=launches)
        self.assertEqual(launches, [], "过窗口不得再拉起")
        self.assertEqual(wl.load()[0]["status"], "missed")

    def test_repeat_wake_does_not_relaunch_running_entry(self):
        self._write([self._entry(status="running", attempts=1)])
        for _ in range(3):        # cron 每分钟唤醒都不得重拉
            launches = []
            self._run_once(unit_active=True, launches=launches)
            self.assertEqual(launches, [])
        self.assertEqual(wl.load()[0]["status"], "running")

    def test_launch_passes_frozen_policy_params(self):
        entry = self._entry(open_at="2099-01-01 09:00:00", lead=100.0,
                            window=120.0, sign_end="2099-01-01 23:59:59")
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            result = mock.Mock()
            result.returncode = 0
            result.stdout = ""
            return result

        with mock.patch.object(runner.subprocess, "run", side_effect=fake_run):
            self.assertTrue(runner.launch(entry, dry=False))
        inner = captured["cmd"][captured["cmd"].index("-lc") + 1]
        # 冻结参数原样传递：runner 不改写 add 时定下的策略起点
        self.assertIn("--activity-id act-a", inner)
        self.assertIn("--at '2099-01-01 09:00:00'", inner)
        self.assertIn("--lead 100.0", inner)
        self.assertIn("--window 120.0", inner)
        self.assertIn("--sign-end '2099-01-01 23:59:59'", inner)

    def test_launch_omits_sign_end_when_absent(self):
        entry = self._entry(open_at="2099-01-01 09:00:00")
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            result = mock.Mock()
            result.returncode = 0
            result.stdout = ""
            return result

        with mock.patch.object(runner.subprocess, "run", side_effect=fake_run):
            self.assertTrue(runner.launch(entry, dry=False))
        inner = captured["cmd"][captured["cmd"].index("-lc") + 1]
        self.assertNotIn("--sign-end", inner)
        self.assertIn("--window 120.0", inner)


if __name__ == "__main__":
    unittest.main()
