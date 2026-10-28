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
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
