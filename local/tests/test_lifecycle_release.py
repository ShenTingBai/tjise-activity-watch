#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""版本单一来源与 Release 资产的契约测试（纯静态/临时目录，零外部副作用）。

覆盖：VERSION 格式、app/build_exe/release 三处版本读取、资产命名（正式产品名）、
完整 ZIP 白名单成员、publish 默认关闭与发布门禁、GitHub latest 固定外链、
绿色版清单包含 runtime_paths.py 与 VERSION。
不访问真实 GitHub、不创建 git tag、不上传 Release、不构建 EXE。

运行方式（需用户授权后）：PYTHONPATH=. python -m unittest local.tests.test_lifecycle_release

@author LiXinwen
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

LOCAL = Path(__file__).resolve().parents[1]
ROOT = LOCAL.parent
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def read(rel: str) -> str:
    path = ROOT / rel
    assert path.exists(), "契约文件缺失：%s" % path
    return path.read_text(encoding="utf-8")


def load_release():
    """把 tools/release.py 当模块导入（不执行 main）。"""
    spec = importlib.util.spec_from_file_location("tjise_release_for_test",
                                                  ROOT / "tools" / "release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VersionSourceTests(unittest.TestCase):
    """VERSION 文件格式与三处读取口径。"""

    def test_repo_version_file_format(self):
        """格式契约只认 major.minor.patch；当前候选号不硬编码进测试
        （是否为 1.1.0 由最终静态门禁单独核对，升版不再改一组测试镜像）。"""
        text = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertRegex(text, r"^\d+\.\d+\.\d+$",
                         "VERSION 只允许 major.minor.patch：%r" % text)

    def test_app_reads_version_from_repo_root(self):
        """源码形态：app.read_version 从仓库根读到同一版本，不写死。"""
        sys.path.insert(0, str(LOCAL))
        try:
            import app as local_app
        except Exception as exc:
            self.skipTest("无法导入 local/app.py：%s" % exc)
        finally:
            sys.path.pop(0)
        expected = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(local_app.APP_VERSION, expected,
                         "APP_VERSION 必须等于 VERSION 文件内容")
        src = read("local/app.py")
        self.assertIn("APP_VERSION = read_version()", src, "版本必须来自 read_version()")
        self.assertIn('"version": APP_VERSION', src, "bootstrap 必须用 APP_VERSION")
        self.assertNotIn('"version": "3.', src, "不得再写死版本字符串")

    def test_release_rejects_bad_version(self):
        """release.read_version：格式非法/缺失直接失败，不产出版本对不上号的资产名。"""
        import tempfile
        rel = load_release()
        with tempfile.TemporaryDirectory() as name:
            bad = Path(name) / "VERSION"
            for body in ("3.1", "v3.1.0", "3.1.0.1", "abc", ""):
                bad.write_text(body, encoding="utf-8")
                with mock.patch.object(rel, "VERSION_FILE", bad):
                    with self.assertRaises(SystemExit, msg="VERSION=%r 必须拒绝" % body):
                        rel.read_version()
            with mock.patch.object(rel, "VERSION_FILE", Path(name) / "nope"):
                with self.assertRaises(SystemExit):
                    rel.read_version()
            bad.write_text("1.2.3\n", encoding="utf-8")
            with mock.patch.object(rel, "VERSION_FILE", bad):
                self.assertEqual(rel.read_version(), "1.2.3")

    def test_build_exe_validates_version_before_build(self):
        """build_exe：构建前读 VERSION 并校验；同一份 VERSION 进冻结资源根。"""
        src = read("local/build_exe.py")
        self.assertIn("def read_version", src, "build_exe 必须读 VERSION")
        self.assertIn("--add-data", src, "必须把 VERSION 打进冻结资源")
        self.assertIn('extra_data=[(worker, "."), (VERSION_FILE, ".")]', src,
                      "worker 与根 VERSION 一同以 dest=\".\" 放到资源根（app 按 RESOURCE_ROOT 读）")
        self.assertIn("read_version()", src[src.index("def main("):],
                      "main 流程必须先过版本校验")


class AssetNamingTests(unittest.TestCase):
    """Release 资产命名：英文下载基名 + 版本 + win-x64（R16）。

    v1.1.0 的上传把中文前缀交给 GitHub 规范化过，只能发布后人工改名。
    所以下载名固定 ASCII；中文产品名只用于 Release 标题与说明。
    """

    def setUp(self):
        self.rel = load_release()

    def test_asset_names_are_stable_ascii(self):
        version = "1.2.3"   # 独立合法样本：命名函数契约与当前发行号解耦
        self.assertEqual(self.rel.exe_asset_name(version),
                         "tjise-activity-watch-v1.2.3-win-x64.exe")
        self.assertEqual(self.rel.zip_asset_name(version),
                         "tjise-activity-watch-v1.2.3-win-x64.zip")
        self.assertEqual(self.rel.tag_of(version), "v1.2.3")
        for name in (self.rel.exe_asset_name(version), self.rel.zip_asset_name(version)):
            self.assertTrue(name.isascii(), "下载名必须是 ASCII：%r" % name)
            self.assertNotIn("天软", name, "中文名交给 Release 标题，不进文件名")

    def test_release_label_keeps_chinese_product_name(self):
        """中文产品名仍用于 Release 标题/说明，不能因为改文件名就丢掉品牌。"""
        self.assertEqual(self.rel.PRODUCT_NAME, "天软活动预报名工具")
        notes = self.rel.release_notes("v1.2.3")
        self.assertIn("tjise-activity-watch-v1.2.3-win-x64.exe", notes,
                      "说明里的下载名必须与资产名同源")
        self.assertIn("tjise-activity-watch-v1.2.3-win-x64.zip", notes)

    def test_zip_whitelist_members(self):
        """完整 ZIP 白名单：主 EXE、README、启动说明、LICENSE——worker 已内置进 EXE，
        外置 grab-worker.exe 不再是成员；任何 data/config/log/缓存都不在清单里。"""
        members = set(self.rel.ZIP_MEMBERS)
        self.assertIn("EXE", members)
        for needed in ("README.md", "启动说明.txt", "LICENSE"):
            self.assertIn(needed, members, "ZIP 白名单缺 %s" % needed)
        self.assertNotIn("grab-worker.exe", members,
                         "worker 已内置进主 EXE，ZIP 不再携带外置 worker")
        joined = "、".join(members)
        for banned in ("data", "config", "log", "cache", "__pycache__", "storage_state"):
            self.assertNotIn(banned, joined, "白名单不得包含 %s" % banned)

    def test_launch_note_explains_standalone_exe(self):
        """启动说明必须写清：EXE 独立可用（内置组件）、ZIP 是含说明的分发形式；
        数据位置与系统要求口径不变，不再要求手动配 worker。"""
        note = self.rel.LAUNCH_NOTE
        self.assertIn("独立", note, "启动说明要写明 EXE 独立可用")
        self.assertIn("TianSoftActivityPreRegistration", note, "数据位置说明保留")
        self.assertIn("WebView2", note, "系统要求说明保留")
        self.assertNotIn("同目录", note, "不再要求 worker 与 EXE 同目录使用")


class PublishGateTests(unittest.TestCase):
    """publish 默认关闭；外部发布有 main+干净+人工确认三道门。"""

    def setUp(self):
        self.rel = load_release()

    def test_default_stage_is_read_only_check(self):
        """无参数默认走 check_only：不构建、不打 tag、不上传。"""
        src = read("tools/release.py")
        main_src = src[src.index("def main("):]
        self.assertIn("check_only(version)   # 默认与 --check：零副作用", src,
                      "默认阶段必须是只读 check")
        self.assertIn('"--prepare"', main_src, "prepare 必须显式选择")
        self.assertIn('"--publish"', main_src, "publish 必须显式选择")

    def test_publish_requires_clean_main(self):
        """外部发布门禁：分支不是 main / 工作区脏都直接退出。"""
        src = read("tools/release.py")
        gate = src[src.index("def source_commit"):src.index("def sha256_of")]
        self.assertIn('!= "main"', gate, "非 main 分支必须拒绝外部发布")
        self.assertIn("dirty", gate, "脏工作区必须拒绝外部发布")

    def test_publish_needs_human_confirmation(self):
        """user-gate：不 --yes 就必须人工输入 yes，其他输入中止且零副作用。"""
        src = read("tools/release.py")
        gate = src[src.index("def confirm_publish"):src.index("def do_publish")]
        self.assertIn("input(", gate, "publish 必须有人工确认输入")
        self.assertIn('!= "yes"', gate, "未确认必须中止")
        # 确认在 tag/上传之前
        do = src[src.index("def do_publish"):src.index("def main(")]
        self.assertLess(do.index("confirm_publish"), do.index("ensure_tag"),
                        "确认必须发生在打 tag 之前")

    def test_no_network_in_check_and_prepare(self):
        """check/prepare 阶段不得调用 gh、不得 push tag。"""
        src = read("tools/release.py")
        check = src[src.index("def check_only"):src.index("def build_portable")]
        self.assertNotIn("GH", check, "check 阶段不得触碰 gh")
        stage_src = src[src.index("def stage_local_assets"):src.index("def check_only")]
        self.assertNotIn("ensure_tag", stage_src, "prepare 不得打 tag")
        self.assertNotIn('"release", "create"', stage_src, "prepare 不得上传")
        self.assertIn("write_manifest(", stage_src, "prepare 必须写 SHA-256 资产清单（manifest）")


class LatestLinkTests(unittest.TestCase):
    """GitHub latest 固定外链：用户点击触发，启动不自动联网。"""

    def test_constant_url_consistent_everywhere(self):
        url = "https://github.com/ShenTingBai/tjise-activity-watch/releases/latest"
        app_src = read("local/app.py")
        self.assertIn('RELEASES_LATEST_URL = "%s"' % url, app_src,
                      "app.py 的外链必须是固定常量")
        self.assertIn("releases/latest", read("README.md"), "根 README 要有 latest 入口")

    def test_open_latest_release_opens_system_browser_only(self):
        """open_latest_release：只打开常量地址；不收任意 URL、不自动调用。"""
        src = read("local/app.py")
        block = src[src.index("def open_latest_release"):src.index("def save_prefs")]
        self.assertIn("os.startfile(RELEASES_LATEST_URL)", block,
                      "必须用系统默认浏览器打开常量地址")
        self.assertNotIn("urllib", block)
        self.assertNotIn("requests", block)


class PortableBundleTests(unittest.TestCase):
    """绿色版清单：runtime_paths.py 与 VERSION 必须带上（历史缺口修复）。"""

    def test_app_files_include_runtime_paths(self):
        src = read("local/make_portable.py")
        self.assertIn('"runtime_paths.py"', src,
                      "APP_FILES 必须包含 runtime_paths.py（缺了 app.py 直接 ImportError）")
        self.assertIn('"gui-preview.png"', src, "当前预览图要随绿色版分发")
        self.assertIn('out / "VERSION"', src, "根 VERSION 必须复制到应用目录")


    def test_manifest_records_version_commit_and_sha256(self):
        """资产证据：manifest 记录版本、来源分支/commit 与每个文件的 SHA-256/大小。"""
        import hashlib
        import json
        import tempfile
        rel = load_release()
        with tempfile.TemporaryDirectory() as name:
            stage = Path(name)
            f1 = stage / "a.zip"
            f2 = stage / "a.exe"
            f1.write_bytes(b"zip-body")
            f2.write_bytes(b"exe-body")
            path = rel.write_manifest(stage, "1.2.3", "v1.2.3", [f1, f2])
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["version"], "1.2.3")
            self.assertEqual(data["tag"], "v1.2.3")
            self.assertIn("branch", data)
            self.assertIn("commit", data)
            self.assertEqual(data["files"]["a.zip"]["sha256"],
                             hashlib.sha256(b"zip-body").hexdigest())
            self.assertEqual(data["files"]["a.exe"]["bytes"], len(b"exe-body"))

    def test_manifest_records_full_head_and_dirty_flag(self):
        """R1：manifest 记完整 HEAD 与来源是否干净——脏构建只能预览，发布侧据此拒绝。"""
        import json
        import tempfile
        rel = load_release()
        full = "f" * 40
        state = {"branch": "codex/x", "dirty": " M local/app.py", "commit": full[:7], "full": full}
        with tempfile.TemporaryDirectory() as name:
            stage = Path(name)
            f1 = stage / "a.zip"
            f1.write_bytes(b"x")
            data = json.loads(rel.write_manifest(stage, "1.2.3", "v1.2.3", [f1],
                                                 build_state=state).read_text(encoding="utf-8"))
            self.assertEqual(data["commit"], full, "commit 必须是完整 HEAD")
            self.assertTrue(data["source_dirty"], "脏工作区构建必须如实标记")
            clean = dict(state, dirty="")
            data2 = json.loads(rel.write_manifest(stage, "1.2.3", "v1.2.3", [f1],
                                                  build_state=clean).read_text(encoding="utf-8"))
            self.assertFalse(data2["source_dirty"])


class AssetVerificationTests(unittest.TestCase):
    """R1/R2：publish 前逐项核验候选资产；拒绝路径全部在 tag/push/upload 之前。

    全部用临时目录 + 假 ZIP + mock git/gh：不连 GitHub、不跑构建、不读真实登录态。
    """

    VERSION = "1.2.3"   # 独立合法样本，与当前发行号解耦

    def setUp(self):
        self.rel = load_release()

    def _state(self, dirty="", full=None, branch="main"):
        return {"branch": branch, "dirty": dirty,
                "commit": (full or "f" * 40)[:7], "full": full or "f" * 40}

    def _candidate(self, tmp, version=VERSION, dirty=""):
        """在临时 DIST 下搭一份候选：EXE + 四成员白名单 ZIP（内外 EXE 字节一致）+ 真实 write_manifest。"""
        import zipfile as zf_mod
        stage = Path(tmp) / ("release-" + self.rel.tag_of(version))
        stage.mkdir(parents=True, exist_ok=True)
        exe = stage / self.rel.exe_asset_name(version)
        zp = stage / self.rel.zip_asset_name(version)
        exe.write_bytes(b"exe-body")
        with zf_mod.ZipFile(zp, "w") as zf:
            zf.writestr(self.rel.exe_asset_name(version), b"exe-body")   # 内外 EXE 一致
            zf.writestr("README.md", "readme")
            zf.writestr("启动说明.txt", "note")
            zf.writestr("LICENSE", "lic")
        manifest = self.rel.write_manifest(stage, version, self.rel.tag_of(version),
                                           [zp, exe], build_state=self._state(dirty))
        return stage, exe, zp, manifest

    def _patched(self, tmp):
        return [mock.patch.object(self.rel, "DIST", Path(tmp)),
                mock.patch.object(self.rel, "git_state", lambda: self._state())]

    def test_verify_accepts_good_candidate(self):
        import tempfile
        with tempfile.TemporaryDirectory() as name:
            for p in self._patched(name):
                p.start()
            try:
                self._candidate(name)
                got = self.rel.verify_release_assets(self.VERSION)
                self.assertEqual(got["exe"].name, self.rel.exe_asset_name(self.VERSION))
                self.assertEqual(got["manifest"].name, "manifest.json")
            finally:
                mock.patch.stopall()

    def test_verify_rejects_each_bad_candidate(self):
        """缺 manifest / 脏来源 / 旧 HEAD / 版本不符 / 哈希错 / 大小错 / 多余成员 / 缺 worker。"""
        import json
        import tempfile
        import zipfile as zf_mod

        def set_dirty(stage, exe, zp, manifest):
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["source_dirty"] = True
            manifest.write_text(json.dumps(data), encoding="utf-8")

        def set_stale_head(stage, exe, zp, manifest):
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["source_dirty"] = False
            data["commit"] = "0" * 40
            manifest.write_text(json.dumps(data), encoding="utf-8")

        def set_wrong_version(stage, exe, zp, manifest):
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["version"] = "9.9.9"
            manifest.write_text(json.dumps(data), encoding="utf-8")

        def tamper_exe(stage, exe, zp, manifest):
            exe.write_bytes(b"tampered")

        def set_wrong_bytes(stage, exe, zp, manifest):
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["files"][zp.name]["bytes"] = 1
            manifest.write_text(json.dumps(data), encoding="utf-8")

        def rewrite_manifest_for_zip(stage, exe, zp, manifest):
            """改 ZIP 后把 manifest 的大小/哈希同步成新值：让外围校验通过，
            才能真正打到白名单/成员层（复审 FG2 附带发现：旧负例只触发哈希层）。"""
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["files"][zp.name] = {"sha256": self.rel.sha256_of(zp),
                                      "bytes": zp.stat().st_size}
            manifest.write_text(json.dumps(data), encoding="utf-8")

        def add_data_member(stage, exe, zp, manifest):
            with zf_mod.ZipFile(zp, "a") as zf:
                zf.writestr("data/session.json", "{}")
            rewrite_manifest_for_zip(stage, exe, zp, manifest)

        def swap_inner_exe(stage, exe, zp, manifest):
            """ZIP 内主 EXE 与单独 EXE 字节不一致：外围校验同步放行，必须由
            内外一致性检查层拒绝（取代旧「缺 worker」负例——worker 已内置）。"""
            rebuilt = zp.with_name("rebuilt.zip")
            with zf_mod.ZipFile(rebuilt, "w") as zf:
                for member in (self.rel.exe_asset_name(self.VERSION), "README.md",
                               "启动说明.txt", "LICENSE"):
                    zf.writestr(member, b"other-exe-bytes"
                                if member == self.rel.exe_asset_name(self.VERSION) else "x")
            zp.unlink()
            rebuilt.rename(zp)
            rewrite_manifest_for_zip(stage, exe, zp, manifest)

        cases = [("dirty", set_dirty), ("stale-head", set_stale_head),
                 ("wrong-version", set_wrong_version), ("hash", tamper_exe),
                 ("size", set_wrong_bytes), ("extra-member", add_data_member),
                 ("inner-exe-mismatch", swap_inner_exe)]
        messages = {"extra-member": "白名单", "inner-exe-mismatch": "ZIP 内主 EXE"}
        for label, mutate in cases:
            with self.subTest(case=label):
                with tempfile.TemporaryDirectory() as name:
                    for p in self._patched(name):
                        p.start()
                    try:
                        stage, exe, zp, manifest = self._candidate(name)
                        mutate(stage, exe, zp, manifest)
                        with self.assertRaises(SystemExit) as caught:
                            self.rel.verify_release_assets(self.VERSION)
                        if label in messages:
                            self.assertIn(messages[label], str(caught.exception),
                                          "%s 负例必须由白名单层拒绝（外围已放行）" % label)
                    finally:
                        mock.patch.stopall()
        # 缺 manifest：连候选目录都没有，同样拒绝（提示显式 --prepare）
        with tempfile.TemporaryDirectory() as name:
            for p in self._patched(name):
                p.start()
            try:
                with self.assertRaises(SystemExit):
                    self.rel.verify_release_assets(self.VERSION)
            finally:
                mock.patch.stopall()

    def test_source_dirty_must_be_explicit_false(self):
        """source_dirty 只认显式布尔 false：缺失/null/0/"false" 都是来源未知，拒绝。"""
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as name:
            for p in self._patched(name):
                p.start()
            try:
                stage, exe, zp, manifest = self._candidate(name)
                for bad in (None, 0, "false", "no"):
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                    if bad is None:
                        data.pop("source_dirty", None)   # 缺失 ≠ false
                    else:
                        data["source_dirty"] = bad
                    manifest.write_text(json.dumps(data), encoding="utf-8")
                    with self.subTest(source_dirty=bad):
                        with self.assertRaises(SystemExit):
                            self.rel.verify_release_assets(self.VERSION)
                # 显式布尔 false：通过
                data = json.loads(manifest.read_text(encoding="utf-8"))
                data["source_dirty"] = False
                manifest.write_text(json.dumps(data), encoding="utf-8")
                got = self.rel.verify_release_assets(self.VERSION)
                self.assertEqual(got["manifest"].name, "manifest.json")
                # manifest 根不是对象同样拒绝
                manifest.write_text("[]", encoding="utf-8")
                with self.assertRaises(SystemExit):
                    self.rel.verify_release_assets(self.VERSION)
            finally:
                mock.patch.stopall()

    def test_do_publish_refuses_before_any_side_effect(self):
        """publish 缺候选/错 tag：拒绝且零副作用——不 tag、不上传、不确认、不自动构建。"""
        import tempfile
        with tempfile.TemporaryDirectory() as name:
            for p in self._patched(name):
                p.start()
            calls = []
            patches = [
                mock.patch.object(self.rel, "source_commit", lambda: "f" * 40),
                mock.patch.object(self.rel, "confirm_publish",
                                  lambda *a, **k: calls.append("confirm")),
                mock.patch.object(self.rel, "ensure_tag",
                                  lambda *a, **k: calls.append("tag")),
                mock.patch.object(self.rel, "publish",
                                  lambda *a, **k: calls.append("upload")),
                mock.patch.object(self.rel, "stage_local_assets",
                                  lambda *a, **k: calls.append("build")),
                mock.patch.object(self.rel, "build_portable",
                                  lambda: calls.append("portable")),
                mock.patch.object(self.rel, "zip_server",
                                  lambda *a, **k: calls.append("server")),
            ]
            for p in patches:
                p.start()
            try:
                # 缺候选：拒绝，且不得自动 --prepare/build
                with self.assertRaises(SystemExit):
                    self.rel.do_publish(self.rel.tag_of(self.VERSION), False, self.VERSION)
                self.assertEqual(calls, [], "缺候选必须零副作用（尤其不得自动构建）")
                # 错 tag（与 VERSION 推导不一致）：在一切副作用之前拒绝
                with self.assertRaises(SystemExit):
                    self.rel.do_publish("v9.9.9", False, self.VERSION)
                self.assertEqual(calls, [])
            finally:
                mock.patch.stopall()

    def test_do_publish_happy_path_uploads_assets_and_manifest(self):
        """核验通过 + 确认后才 tag/上传；上传清单= ZIP + EXE + manifest。"""
        import tempfile
        with tempfile.TemporaryDirectory() as name:
            for p in self._patched(name):
                p.start()
            recorded = {}
            try:
                stage, exe, zp, manifest = self._candidate(name)
                patches = [
                    mock.patch.object(self.rel, "source_commit", lambda: "f" * 40),
                    mock.patch.object(self.rel, "confirm_publish",
                                      lambda tag, yes: recorded.setdefault("confirm", tag)),
                    mock.patch.object(self.rel, "ensure_tag",
                                      lambda tag, commit: recorded.setdefault("tag", (tag, commit))),
                    mock.patch.object(self.rel, "publish",
                                      lambda tag, assets, clobber=False: recorded.setdefault(
                                          "upload", (tag, [a.name for a in assets], clobber))),
                ]
                for p in patches:
                    p.start()
                tag = self.rel.tag_of(self.VERSION)
                self.rel.do_publish(tag, True, self.VERSION)
                self.assertEqual(recorded["confirm"], tag)
                self.assertEqual(recorded["tag"][0], tag)
                self.assertEqual(recorded["tag"][1], "f" * 40,
                                 "tag 必须绑定完整 HEAD")
                self.assertEqual(recorded["upload"][1],
                                 [zp.name, exe.name, "manifest.json"],
                                 "上传清单=完整 ZIP + 单文件 EXE + manifest")
                self.assertFalse(recorded["upload"][2], "默认不覆盖已有附件")
            finally:
                mock.patch.stopall()

    def test_positional_tag_never_publishes_alone(self):
        """位置 tag 只是校验输入：与 VERSION 不一致拒绝；一致但没有阶段参数也不发布。"""
        import sys
        import tempfile
        calls = []
        with tempfile.TemporaryDirectory() as name:
            vf = Path(name) / "VERSION"
            vf.write_text(self.VERSION + "\n", encoding="utf-8")
            patches = [
                mock.patch.object(self.rel, "VERSION_FILE", vf),
                mock.patch.object(self.rel, "check_only", lambda v: calls.append("check")),
                mock.patch.object(self.rel, "stage_local_assets", lambda v: calls.append("prepare")),
                mock.patch.object(self.rel, "do_publish", lambda *a, **k: calls.append("publish")),
            ]
            for argv in ([sys.argv[0], "v9.9.9"], [sys.argv[0], self.VERSION]):
                for p in patches:
                    p.start()
                try:
                    with mock.patch.object(sys, "argv", argv):
                        with self.assertRaises(SystemExit):
                            self.rel.main()
                finally:
                    mock.patch.stopall()
                self.assertEqual(calls, [], "位置 tag 不得单独触发任何阶段：%r" % argv)

    def test_stages_are_mutually_exclusive(self):
        """--check/--prepare/--publish 一次只能选一个。"""
        import sys
        calls = []
        patches = [
            mock.patch.object(self.rel, "check_only", lambda v: calls.append("check")),
            mock.patch.object(self.rel, "stage_local_assets", lambda v: calls.append("prepare")),
            mock.patch.object(self.rel, "do_publish", lambda *a, **k: calls.append("publish")),
        ]
        for p in patches:
            p.start()
        try:
            with mock.patch.object(sys, "argv", [sys.argv[0], "--check", "--prepare"]):
                with self.assertRaises(SystemExit):
                    self.rel.main()
            self.assertEqual(calls, [])
        finally:
            mock.patch.stopall()

    def test_existing_release_not_clobbered_by_default(self):
        """已有 Release：默认拒绝覆盖附件；显式 --clobber 才允许替换。"""
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as name:
            asset = Path(name) / "a.zip"
            asset.write_bytes(b"x")
            calls = []
            exists = mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch.object(self.rel, "GH", "gh-fake"), \
                 mock.patch.object(subprocess, "run", mock.Mock(return_value=exists)), \
                 mock.patch.object(self.rel, "run",
                                   lambda cmd, **k: calls.append(cmd) or ""):
                with self.assertRaises(SystemExit):
                    self.rel.publish("v1", [asset])            # 默认拒绝
                self.assertEqual(calls, [], "拒绝时不得有任何上传命令")
                self.rel.publish("v1", [asset], clobber=True)  # 显式覆盖
                self.assertTrue(any("--clobber" in cmd for cmd in calls),
                                "覆盖必须显式带 --clobber")

    def test_release_create_carries_verify_tag(self):
        """gh release create 必须带 --verify-tag：远端缺 tag 时中止，禁止 gh 自动补 tag。"""
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as name:
            asset = Path(name) / "a.zip"
            asset.write_bytes(b"x")
            notes = Path(name) / ".notes-v1.md"
            calls = []
            absent = mock.Mock(returncode=1, stdout="", stderr="not found")
            with mock.patch.object(self.rel, "GH", "gh-fake"), \
                 mock.patch.object(self.rel, "DIST", Path(name)), \
                 mock.patch.object(subprocess, "run", mock.Mock(return_value=absent)), \
                 mock.patch.object(self.rel, "release_notes", lambda tag: "notes\n"), \
                 mock.patch.object(self.rel, "run",
                                   lambda cmd, **k: calls.append(cmd) or ""):
                self.rel.publish("v1", [asset])   # Release 不存在 → create 分支
            create = next(cmd for cmd in calls if "release" in cmd and "create" in cmd)
            self.assertIn("--verify-tag", create, "create 必须带 --verify-tag")


class RemoteTagBindingTests(unittest.TestCase):
    """FG1/FG2：tag 显式绑定已核验提交；origin 事实独立核验；任何不一致写入前拒绝。
    EXTRA-1：ls-remote 必须同时请求精确 tag 与 peeled 两个 pattern。

    全部 mock git/gh，不发起真实远端查询（真实过滤回归见 RealGitFixtureTests，需单独授权）。
    """

    def setUp(self):
        self.rel = load_release()
        self.commit = "f" * 40
        self.other = "0" * 40

    def _tag_cmd_log(self, remote_values, local_listing="", local_revparse=None):
        """mock rel.run：按 git 子命令分发；remote_tag_commit 用 remote_values 序列。"""
        log = []

        def fake_run(cmd, **kwargs):
            log.append(list(cmd))
            if cmd[:2] == ["git", "tag"] and cmd[2] == "-l":
                return local_listing + "\n" if local_listing else ""
            if cmd[:2] == ["git", "rev-parse"] and cmd[2].endswith("^{}"):
                return (local_revparse if local_revparse is not None else self.commit) + "\n"
            return ""
        return log, fake_run

    def test_remote_tag_commit_queries_exact_and_peeled_patterns(self):
        """EXTRA-1：ls-remote 命令必须同时带 <tag> 与 <tag>^{} 两个 pattern——
        mock 不许无视参数直接吐 peeled 行，先核参数再谈解析。"""
        log, fake_run = self._tag_cmd_log([])
        with mock.patch.object(self.rel, "run", fake_run):
            self.rel.remote_tag_commit("v1.1.0")
        ls = next(cmd for cmd in log if cmd[:2] == ["git", "ls-remote"])
        self.assertIn("--tags", ls)
        self.assertIn("v1.1.0", ls, "精确 pattern 必须在")
        self.assertIn("v1.1.0^{}", ls, "peeled pattern 必须在（EXTRA-1 修复点）")
        self.assertLess(ls.index("v1.1.0"), ls.index("v1.1.0^{}"))

    def test_remote_tag_commit_prefers_peeled(self):
        """附注 tag：ls-remote 有 tag 对象行与 ^{} 行，取 peeled 的最终提交。"""
        out = ("%s\trefs/tags/v1.1.0\n%s\trefs/tags/v1.1.0^{}\n"
               % (self.other, self.commit))
        with mock.patch.object(self.rel, "run", lambda cmd, **k: out):
            self.assertEqual(self.rel.remote_tag_commit("v1.1.0"), self.commit)

    def test_remote_tag_commit_lightweight_value(self):
        """轻量 tag：只有一行，值就是提交。"""
        out = "%s\trefs/tags/v1.1.0\n" % self.commit
        with mock.patch.object(self.rel, "run", lambda cmd, **k: out):
            self.assertEqual(self.rel.remote_tag_commit("v1.1.0"), self.commit)

    def test_remote_tag_commit_object_row_only_returns_object_sha(self):
        """旧缺陷场景留证：只有对象行（旧单参数下附注 tag 的真实输出）时解析器取
        轻量回退=对象 SHA，与提交比对会失败——保守拒绝。修复靠参数补全，
        解析器不放宽，本用例锁住该行为不被顺手改坏。"""
        out = "%s\trefs/tags/v1.1.0\n" % self.other
        with mock.patch.object(self.rel, "run", lambda cmd, **k: out):
            self.assertEqual(self.rel.remote_tag_commit("v1.1.0"), self.other)

    def test_remote_tag_commit_missing_is_empty(self):
        """查询成功且 origin 无此 tag：返回空串（不是错误）。"""
        with mock.patch.object(self.rel, "run", lambda cmd, **k: ""):
            self.assertEqual(self.rel.remote_tag_commit("v1.1.0"), "")

    def test_remote_tag_commit_query_failure_rejected(self):
        """ls-remote 命令失败：直接拒绝，不当成「不存在」。"""
        def boom(cmd, **kwargs):
            raise SystemExit("git ls-remote 失败")
        with mock.patch.object(self.rel, "run", boom):
            with self.assertRaises(SystemExit):
                self.rel.remote_tag_commit("v1.1.0")

    def _ensure(self, local_listing="", remote_values=(), local_revparse=None):
        log, fake_run = self._tag_cmd_log([], local_listing, local_revparse)
        with mock.patch.object(self.rel, "run", fake_run), \
             mock.patch.object(self.rel, "remote_tag_commit",
                               mock.Mock(side_effect=list(remote_values))):
            try:
                self.rel.ensure_tag("v1.1.0", self.commit)
                error = None
            except SystemExit as exc:
                error = str(exc)
        return log, error

    def test_ensure_tag_local_right_remote_wrong_rejected(self):
        """本地指向正确、origin 指向别的提交：拒绝，无任何 tag/push 写入。"""
        log, error = self._ensure(local_listing="v1.1.0", remote_values=[self.other])
        self.assertTrue(error and "origin" in error)
        self.assertFalse(any(cmd[1:3] == ["tag", "-a"] for cmd in log), "不得创建 tag")
        self.assertFalse(any(cmd[1:2] == ["push"] for cmd in log), "不得 push")

    def test_ensure_tag_local_wrong_rejected_even_if_remote_right(self):
        """本地 tag 指向别的提交：本地层先拒绝（不重写历史）。"""
        log, error = self._ensure(local_listing="v1.1.0",
                                  local_revparse=self.other, remote_values=[self.commit])
        self.assertTrue(error and "本地" in error)
        self.assertFalse(any(cmd[1:2] == ["push"] for cmd in log))

    def test_ensure_tag_remote_right_reuses_without_write(self):
        """远端已指向本次提交：复用，不创建、不 push。"""
        log, error = self._ensure(local_listing="", remote_values=[self.commit])
        self.assertIsNone(error)
        self.assertFalse(any(cmd[1:3] == ["tag", "-a"] for cmd in log))
        self.assertFalse(any(cmd[1:2] == ["push"] for cmd in log))

    def test_ensure_tag_remote_missing_local_right_pushes_only(self):
        """远端缺失、本地正确：显式 push 已核验 tag，不重建；push 后核验通过。"""
        log, error = self._ensure(local_listing="v1.1.0",
                                  remote_values=["", self.commit])
        self.assertIsNone(error)
        self.assertFalse(any(cmd[1:3] == ["tag", "-a"] for cmd in log),
                         "本地已正确，不得重建 tag")
        self.assertTrue(any(cmd[1:2] == ["push"] and "origin" in cmd for cmd in log))

    def test_ensure_tag_both_missing_creates_bound_and_pushes(self):
        """两边都缺：新附注 tag 显式绑定 expect_commit 后 push（FG1 核心）。"""
        log, error = self._ensure(local_listing="", remote_values=["", self.commit])
        self.assertIsNone(error)
        created = [cmd for cmd in log if cmd[1:3] == ["tag", "-a"]]
        self.assertEqual(len(created), 1, "两边都缺才创建一次")
        self.assertIn(self.commit, created[0],
                      "git tag -a 必须显式传入已核验提交，不靠当前 HEAD 默认值")
        self.assertTrue(any(cmd[1:2] == ["push"] and "origin" in cmd for cmd in log))

    def test_ensure_tag_post_push_verification_failure_rejected(self):
        """push 后再查远端指向不一致：拒绝并提示人工检查。"""
        log, error = self._ensure(local_listing="", remote_values=["", self.other])
        self.assertTrue(error and "push 后" in error)


class ConfirmedSourceRecheckTests(unittest.TestCase):
    """FG1：人工确认等待期间来源/资产变化，必须在 tag/push/upload 之前拒绝。"""

    VERSION = "1.2.3"

    def setUp(self):
        self.rel = load_release()

    def _state(self, dirty="", full=None, branch="main"):
        return {"branch": branch, "dirty": dirty,
                "commit": (full or "f" * 40)[:7], "full": full or "f" * 40}

    def _candidate(self, tmp, version=VERSION):
        import zipfile as zf_mod
        stage = Path(tmp) / ("release-" + self.rel.tag_of(version))
        stage.mkdir(parents=True, exist_ok=True)
        exe = stage / self.rel.exe_asset_name(version)
        zp = stage / self.rel.zip_asset_name(version)
        exe.write_bytes(b"exe-body")
        with zf_mod.ZipFile(zp, "w") as zf:
            zf.writestr(self.rel.exe_asset_name(version), b"exe-body")   # 内外 EXE 一致
            zf.writestr("README.md", "readme")
            zf.writestr("启动说明.txt", "note")
            zf.writestr("LICENSE", "lic")
        manifest = self.rel.write_manifest(stage, version, self.rel.tag_of(version),
                                           [zp, exe], build_state=self._state())
        return stage, exe, zp, manifest

    def _run_do_publish(self, tmp, source_commits, mutate_after_confirm=None):
        """跑 do_publish（确认+复核 mock 化），返回 (side_effect_calls, SystemExit|None)。

        source_commits：source_commit 的返回序列；mutate_after_confirm 在确认
        返回后、复核前执行（模拟另一执行器在等待期动了工作区/资产）。
        """
        import itertools
        calls = []
        stage, exe, zp, manifest = self._candidate(tmp)
        commits = itertools.cycle(source_commits) if len(source_commits) == 1 else iter(source_commits)

        def fake_source_commit():
            value = next(commits)
            return value

        def fake_confirm(tag, yes):
            if mutate_after_confirm:
                mutate_after_confirm(stage, exe, zp, manifest)

        patches = [
            mock.patch.object(self.rel, "DIST", Path(tmp)),
            mock.patch.object(self.rel, "git_state", lambda: self._state()),
            mock.patch.object(self.rel, "source_commit", fake_source_commit),
            mock.patch.object(self.rel, "confirm_publish", fake_confirm),
            mock.patch.object(self.rel, "ensure_tag",
                              lambda *a, **k: calls.append("tag")),
            mock.patch.object(self.rel, "publish",
                              lambda *a, **k: calls.append("upload")),
        ]
        for p in patches:
            p.start()
        try:
            try:
                self.rel.do_publish(self.rel.tag_of(self.VERSION), True, self.VERSION)
                error = None
            except SystemExit as exc:
                error = str(exc)
        finally:
            mock.patch.stopall()
        return calls, error

    def test_stable_source_publishes(self):
        """确认前后来源/资产一致：正常走完 tag/upload。"""
        import tempfile
        with tempfile.TemporaryDirectory() as name:
            calls, error = self._run_do_publish(name, ["f" * 40])
            self.assertIsNone(error)
            self.assertEqual(calls, ["tag", "upload"])

    def test_head_changed_during_confirmation_rejected(self):
        """确认期间 HEAD 变化（切分支/新提交）：复核拒绝，零 Git 写入。"""
        import tempfile
        with tempfile.TemporaryDirectory() as name:
            calls, error = self._run_do_publish(name, ["f" * 40, "0" * 40])
            self.assertTrue(error and "确认期间 HEAD" in error)
            self.assertEqual(calls, [], "来源变化必须在 tag/upload 前中止")

    def test_source_commit_raises_during_recheck_rejected(self):
        """复核时不再满足干净 main（分支/脏变化）：source_commit 抛错即中止，零 Git 写入。"""
        import tempfile
        with tempfile.TemporaryDirectory() as name:
            state = {"n": 0}

            def flip():
                state["n"] += 1
                if state["n"] == 1:
                    return "f" * 40      # 确认前：干净 main
                raise SystemExit("外部发布必须从 main 运行，当前分支是 other")
            calls = []
            patches = [
                mock.patch.object(self.rel, "DIST", Path(name)),
                mock.patch.object(self.rel, "git_state", lambda: self._state()),
                mock.patch.object(self.rel, "source_commit", flip),
                mock.patch.object(self.rel, "confirm_publish", lambda *a: None),
                mock.patch.object(self.rel, "ensure_tag", lambda *a, **k: calls.append("tag")),
                mock.patch.object(self.rel, "publish", lambda *a, **k: calls.append("upload")),
            ]
            for p in patches:
                p.start()
            try:
                self._candidate(name)
                try:
                    self.rel.do_publish(self.rel.tag_of(self.VERSION), True, self.VERSION)
                    error = None
                except SystemExit as exc:
                    error = str(exc)
            finally:
                mock.patch.stopall()
            self.assertTrue(error and "main" in error, "复核时不再满足干净 main 必须中止")
            self.assertEqual(calls, [], "复核失败必须在 tag/upload 前中止")

    def test_asset_replaced_during_confirmation_rejected(self):
        """确认期间候选资产被替换（EXE 字节变化）：复核拦下，零副作用。

        替换会被复核 verify 的哈希层先拦截（SHA-256 与 manifest 不符），
        到不了指纹比对层——两层殊途同归，断言只锁「拒绝 + 零副作用」。
        """
        import tempfile
        def swap_exe(stage, exe, zp, manifest):
            exe.write_bytes(b"replaced-by-someone-else")
        with tempfile.TemporaryDirectory() as name:
            calls, error = self._run_do_publish(name, ["f" * 40], mutate_after_confirm=swap_exe)
            self.assertTrue(error, "确认期间资产被替换必须拒绝")
            self.assertEqual(calls, [])

    def test_upload_digest_recheck_rejects_replacement(self):
        """tag 之后、上传之前的最后核验：资产被换立即中止上传（tag 已打但不上传）。"""
        import tempfile
        with tempfile.TemporaryDirectory() as name:
            stage, exe, zp, manifest = self._candidate(name)
            calls = []

            def swap_exe_after_tag(*a, **k):
                calls.append("tag")
                exe.write_bytes(b"swapped-after-tag")   # tag 与上传之间被替换
            patches = [
                mock.patch.object(self.rel, "DIST", Path(name)),
                mock.patch.object(self.rel, "git_state", lambda: self._state()),
                mock.patch.object(self.rel, "source_commit", lambda: "f" * 40),
                mock.patch.object(self.rel, "confirm_publish", lambda *a: None),
                mock.patch.object(self.rel, "ensure_tag", swap_exe_after_tag),
                mock.patch.object(self.rel, "publish", lambda *a, **k: calls.append("upload")),
            ]
            for p in patches:
                p.start()
            try:
                try:
                    self.rel.do_publish(self.rel.tag_of(self.VERSION), True, self.VERSION)
                    error = None
                except SystemExit as exc:
                    error = str(exc)
            finally:
                mock.patch.stopall()
            # 替换在哈希层即被拦截（verify 的 SHA-256 比对先于上传指纹比对），殊途同归：
            # 无论命中哪一层，上传都必须中止
            self.assertTrue(error, "上传前必须拦下被替换的资产")
            self.assertEqual(calls, ["tag"], "tag 已打（Git 副作用已发生），但上传必须中止")


@unittest.skipUnless(
    os.environ.get("TJISE_GIT_FIXTURE_AUTH") == "1",
    "真实 Git fixture（临时仓库 init/commit/tag/push/ls-remote）需单独授权："
    "确认后设 TJISE_GIT_FIXTURE_AUTH=1 再运行；本轮只定义不执行")
class RealGitFixtureTests(unittest.TestCase):
    """EXTRA-1 的真实过滤回归：本地临时仓库当 origin，验证真实 ls-remote 参数行为。

    绝不写本项目 Git、绝不连接真实 origin：仓库建在 tempfile 里用完即删，
    ls-remote 只指向临时 work 仓库的 file 远端。
    """

    def setUp(self):
        self.rel = load_release()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name) / "work"
        self.bare = Path(self.tmp.name) / "origin.git"
        env = dict(os.environ, GIT_AUTHOR_NAME="fixture",
                   GIT_AUTHOR_EMAIL="fixture@example.com",
                   GIT_COMMITTER_NAME="fixture",
                   GIT_COMMITTER_EMAIL="fixture@example.com")

        def git(*args, cwd=None):
            subprocess.run(["git", *args], cwd=str(cwd or self.work), check=True,
                           capture_output=True, text=True, env=env)

        # 裸仓库必须显式传目标目录：不带参数的 init 会把 cwd 本身变成裸仓库
        git("init", "--bare", "-b", "main", self.bare.as_posix(), cwd=self.tmp.name)
        self.work.mkdir(parents=True)   # cwd 必须已存在，CreateProcess 才能启动 git
        git("init", "-b", "main", cwd=self.work)
        (self.work / "f.txt").write_text("fixture", encoding="utf-8")
        git("add", "f.txt")
        git("commit", "-m", "fixture commit")
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(self.work),
                              check=True, capture_output=True, text=True).stdout.strip()
        git("tag", "-a", "v9.9.9", head, "-m", "annotated fixture tag")
        git("tag", "v9.9.8", head)
        git("remote", "add", "origin", self.bare.as_posix())   # git 远端只认正斜杠路径
        git("push", "--quiet", "origin", "main", "v9.9.9", "v9.9.8")
        self.head = head

    def _patched_run(self):
        """只把 remote_tag_commit 构造出的 ls-remote 命令重定向到临时仓库执行；
        其余命令仍走真实 run（release.py 的 cwd 锚在项目根，不能动）。"""
        real_run = self.rel.run

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["git", "ls-remote"]:
                res = subprocess.run(cmd, cwd=str(self.work), capture_output=True,
                                     text=True, encoding="utf-8", errors="replace")
                if res.returncode != 0:
                    raise SystemExit("fixture ls-remote 失败：%s" % res.stderr[-300:])
                return res.stdout
            return real_run(cmd, **kwargs)
        return fake_run

    def test_annotated_tag_peeled_resolved_from_real_git(self):
        """真实附注 tag：双 pattern 下 peeled 行确实返回，最终提交解析正确。"""
        with mock.patch.object(self.rel, "run", self._patched_run()):
            self.assertEqual(self.rel.remote_tag_commit("v9.9.9"), self.head)

    def test_lightweight_tag_resolved_from_real_git(self):
        """真实轻量 tag：单行值即提交。"""
        with mock.patch.object(self.rel, "run", self._patched_run()):
            self.assertEqual(self.rel.remote_tag_commit("v9.9.8"), self.head)

    def test_missing_tag_is_empty_from_real_git(self):
        """真实查询成功且无此 tag：空串（不是错误）。"""
        with mock.patch.object(self.rel, "run", self._patched_run()):
            self.assertEqual(self.rel.remote_tag_commit("v0.0.0-fixture-nope"), "")


if __name__ == "__main__":
    unittest.main()
