"""安装器流式下载与启动桥接的安全边界；全部替身，不访问网络、不启动正式安装器。

@author LiXinwen
"""
from hashlib import sha256
from pathlib import Path
import shutil
import sys
import unittest
import uuid
from unittest.mock import patch

LOCAL = Path(__file__).resolve().parents[1]
if str(LOCAL) not in sys.path:
    sys.path.insert(0, str(LOCAL))
import updater  # noqa: E402

NAME = "tjise-activity-watch-installer-v1.1.1-win-x64.exe"
URL = "https://github.com/ShenTingBai/tjise-activity-watch/releases/download/v1.1.1/" + NAME
BODY = b"installer-bytes-0123456789"
DIGEST = sha256(BODY).hexdigest()
TEMP_BASE = Path("D:/@Temp")
OLD_FILES = ("data/session.json", "program/tjise-activity-watch.exe")


class FakeStream:
    """替身 HTTP 响应：按脚本吐块，可模拟断流与超量。"""

    def __init__(self, chunks, break_after=None):
        self.chunks = list(chunks)
        self.break_after = break_after
        self.served = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, amt):
        if self.break_after is not None and self.served >= self.break_after:
            raise ConnectionResetError("stream reset")
        if not self.chunks:
            return b""
        self.served += 1
        return self.chunks.pop(0)


class UpdaterDirTests(unittest.TestCase):
    """隔离目录基类：只在 D:/@Temp 下开目录，不写程序目录和数据目录。"""

    def setUp(self):
        self.root = TEMP_BASE / ("tjise-updater-test-" + uuid.uuid4().hex[:12])
        self.root.mkdir(parents=True, exist_ok=True)
        program = self.root / "program"
        data = self.root / "data"
        program.mkdir()
        data.mkdir()
        (program / "tjise-activity-watch.exe").write_bytes(b"old-program")
        (data / "session.json").write_bytes(b"old-session")
        self.before = {rel: (self.root / rel).read_bytes() for rel in OLD_FILES}

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def assertOldFilesIntact(self):
        for rel, raw in self.before.items():
            self.assertEqual((self.root / rel).read_bytes(), raw,
                             "旧程序/数据被改动：" + rel)

    def leftovers(self):
        return sorted(p.relative_to(self.root).as_posix()
                      for p in self.root.rglob("*") if p.is_file())

    def assertCleanedUp(self):
        self.assertEqual(self.leftovers(), sorted(OLD_FILES))
        self.assertOldFilesIntact()


class DownloadTests(UpdaterDirTests):
    def download(self, chunks, expect_bytes=len(BODY), expect_sha256=DIGEST,
                 max_bytes=updater.DEFAULT_MAX_BYTES, break_after=None,
                 cancel=None, progress=None):
        stream = FakeStream(chunks, break_after=break_after)
        with patch.object(updater, "urlopen", return_value=stream) as fetch:
            result = updater.download_installer(
                URL, name=NAME, expect_bytes=expect_bytes, expect_sha256=expect_sha256,
                root=self.root, max_bytes=max_bytes, cancel=cancel, progress=progress)
        return result, fetch

    def test_success_keeps_atomically_named_file(self):
        result, fetch = self.download([BODY[i:i + 8] for i in range(0, len(BODY), 8)])
        self.assertTrue(result["ok"])
        self.assertEqual(result["bytes"], len(BODY))
        self.assertEqual(result["sha256"], DIGEST)
        path = Path(result["path"])
        self.assertTrue(path.is_file())
        self.assertEqual(path.name, NAME)
        self.assertEqual(path.read_bytes(), BODY)
        self.assertNotIn(NAME + ".part", self.leftovers())
        self.assertOldFilesIntact()
        self.assertEqual(fetch.call_count, 1)

    def test_https_only(self):
        for bad in ("http://github.com/x/" + NAME, "ftp://github.com/x/" + NAME, "", None, 5):
            with self.subTest(url=bad), self.assertRaises(ValueError):
                updater.download_installer(bad, name=NAME, expect_bytes=len(BODY),
                                           expect_sha256=DIGEST, root=self.root)
        self.assertOldFilesIntact()

    def test_bad_arguments_raise_not_swallow(self):
        base = {"name": NAME, "expect_bytes": len(BODY), "expect_sha256": DIGEST}
        for bad in ({"name": "a/b.exe"}, {"name": ""},
                    {"expect_sha256": "zz"}, {"expect_bytes": 0},
                    {"expect_bytes": "9"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                updater.download_installer(URL, root=self.root, **dict(base, **bad))

    def test_stream_break_cleans_up(self):
        result, _ = self.download([BODY[:8], BODY[8:16], BODY[16:]], break_after=2)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "network_error")
        self.assertCleanedUp()

    def test_declared_oversize_rejected_without_fetch(self):
        result, fetch = self.download([], expect_bytes=updater.DEFAULT_MAX_BYTES + 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "size_limit")
        fetch.assert_not_called()

    def test_stream_oversize_rejected(self):
        # 声明大小在上限内，但服务端发来更多字节：必须中途放弃并清理
        result, _ = self.download([BODY[:8], BODY[8:16], BODY[16:]],
                                  expect_bytes=8, max_bytes=12)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "size_limit")
        self.assertCleanedUp()

    def test_short_stream_rejected(self):
        result, _ = self.download([BODY[:4]])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "size_mismatch")
        self.assertCleanedUp()

    def test_hash_mismatch_cleans_up(self):
        result, _ = self.download([b"evil" + BODY[4:]])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "hash_mismatch")
        self.assertCleanedUp()

    def test_user_cancel_cleans_up(self):
        calls = []
        result, _ = self.download([BODY[:8], BODY[8:16], BODY[16:]],
                                  cancel=lambda: calls.append(1) or len(calls) > 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "cancelled")
        self.assertCleanedUp()

    def test_progress_reports_each_chunk(self):
        steps = []
        result, _ = self.download([BODY[:8], BODY[8:16], BODY[16:]],
                                  progress=lambda done, total: steps.append((done, total)))
        self.assertTrue(result["ok"])
        self.assertEqual(steps, [(8, len(BODY)), (16, len(BODY)), (len(BODY), len(BODY))])


class LaunchTests(UpdaterDirTests):
    def test_launch_success_returns_path(self):
        target = self.root / NAME
        target.write_bytes(BODY)
        launched = []
        got = updater.launch_installer(str(target), root=self.root,
                                       launcher=lambda p: launched.append(p))
        self.assertTrue(got["ok"])
        self.assertEqual(launched, [str(target)])

    def test_launcher_failure_is_reported(self):
        def boom(path):
            raise RuntimeError("uac denied")
        target = self.root / NAME
        target.write_bytes(BODY)
        got = updater.launch_installer(str(target), root=self.root, launcher=boom)
        self.assertFalse(got["ok"])
        self.assertEqual(got["error_code"], "launch_failed")
        self.assertIn("RuntimeError", got["text"])
        self.assertOldFilesIntact()

    def test_launch_refuses_paths_outside_temp_root(self):
        outside = self.root.parent / ("outside-" + uuid.uuid4().hex[:8] + ".exe")
        outside.write_bytes(BODY)
        try:
            got = updater.launch_installer(str(outside), root=self.root,
                                           launcher=lambda p: self.fail("不该启动"))
            self.assertFalse(got["ok"])
            self.assertEqual(got["error_code"], "launch_failed")
        finally:
            outside.unlink(missing_ok=True)

    def test_launch_refuses_missing_file(self):
        got = updater.launch_installer(str(self.root / "ghost.exe"), root=self.root,
                                       launcher=lambda p: self.fail("不该启动"))
        self.assertFalse(got["ok"])
        self.assertEqual(got["error_code"], "launch_failed")


if __name__ == "__main__":
    unittest.main()
