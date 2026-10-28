#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安装包脚本（local/build_installer.py）的契约测试。

这些用例只碰纯函数与渲染出来的文本，**不调用 ISCC、不装软件、不写注册表**，
所以任何机器上都能跑。

守的是三条已经踩过的坑：

1. AppId 末尾只能有一个右花括号。Inno 只对**左**花括号转义，写成
   `{{GUID}}` 渲染出来是 `{GUID}}`，卸载注册表键名变成 `{GUID}}_is1`
   （2026-10-27 实测）。这类错不报错、不影响安装，只在注册表里留个畸形键名，
   靠人眼是发现不了的，所以必须用测试钉住。
2. 卸载不能删掉登录态，除非有人明确选了「是」：交互卸载用
   SuppressibleMsgBox + MB_DEFBUTTON2 + IDNO 让「否」成为默认按钮，静默卸载
   （/VERYSILENT、/SUPPRESSMSGBOXES）先判 UninstallSilent 直接跳过删除分支
   ——被抑制的返回值不是用户的选择，不能拿来删数据（2026-10-27 实测并返修）。
3. [Setup] 段的值不加引号、模板不许留 @@占位符——留一个就是半成品包。

运行方式：PYTHONPATH=. python -m unittest local.tests.test_installer_script

@author LiXinwen
"""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

LOCAL = Path(__file__).resolve().parents[1]
if str(LOCAL) not in sys.path:
    sys.path.insert(0, str(LOCAL))

import build_installer as bi  # noqa: E402


class AppIdContractTest(unittest.TestCase):
    """AppId 的花括号数量：少一个括号就是另一条卸载记录。"""

    def test_left_brace_doubled_for_inno_escape(self):
        self.assertTrue(bi.APP_ID.startswith("{{"),
                        "Inno 要求左花括号写成 {{，否则会当成常量去展开")

    def test_exactly_one_trailing_brace(self):
        self.assertTrue(bi.APP_ID.endswith("}"))
        self.assertFalse(bi.APP_ID.endswith("}}"),
                         "多一个右花括号会让卸载键名变成 {GUID}}_is1")

    def test_guid_body_is_well_formed(self):
        body = bi.APP_ID[2:-1]
        self.assertRegex(body, r"^[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$")


class UninstallDataSafetyTest(unittest.TestCase):
    """卸载不能静默删登录态：这是不可逆的用户数据。

    两条防线必须同时存在，只满足一条都不算安全：
    交互路径——默认按钮是「否」（MB_DEFBUTTON2），且被抑制时返回 IDNO；
    静默路径——先判 UninstallSilent 直接跳过删除分支，不依赖任何返回值。
    """

    def test_uses_suppressible_msgbox(self):
        self.assertIn("SuppressibleMsgBox", bi.ISS_TEMPLATE)

    def test_no_plain_msgbox(self):
        plain = re.findall(r"(?<!Suppressible)MsgBox\(", bi.ISS_TEMPLATE)
        self.assertEqual(plain, [],
                         "静默卸载下普通 MsgBox 会被当成「是」，会删掉登录数据")

    def test_default_answer_is_no(self):
        # SuppressibleMsgBox 的最后一个参数是「被抑制时返回什么」，必须是 IDNO
        self.assertRegex(bi.ISS_TEMPLATE, r"MB_YESNO\s+or\s+MB_DEFBUTTON2,\s*IDNO\)",
                         "交互卸载必须 IDNO 兜底，且用 MB_DEFBUTTON2 让「否」成为默认按钮")

    def test_silent_uninstall_skips_deletion(self):
        """静默卸载不进删除分支：先判 UninstallSilent，再谈删除。"""
        body = bi.ISS_TEMPLATE[bi.ISS_TEMPLATE.index("procedure CurUninstallStepChanged"):]
        self.assertIn("UninstallSilent", body)
        # UninstallSilent 的分支必须出现在 DelTree 之前，且是它的前置条件
        self.assertLess(body.index("UninstallSilent"), body.index("DelTree("),
                        "静默判断必须在删除之前，否则没人回答也会删数据")
        self.assertRegex(body, r"if\s+UninstallSilent\s+then",
                         "必须是显式的静默分支，不能靠消息框返回值推断")

    def test_delete_only_on_explicit_yes(self):
        """删除只发生在显式 IDYES：DelTree 必须待在 yes 分支里，且删的是数据目录。"""
        body = bi.ISS_TEMPLATE[bi.ISS_TEMPLATE.index("procedure CurUninstallStepChanged"):]
        self.assertIn("DelTree(DataDir", body)
        self.assertLess(body.index("= IDYES"), body.index("DelTree(DataDir"),
                        "DelTree 必须在 IDYES 之后：只有用户明确选删除才执行")
        # 数据目录之外不许有第二个删除目标
        self.assertEqual(len(re.findall(r"DelTree\(", body)), 1,
                         "卸载里只允许删除数据目录这一处")

    def test_data_dir_is_outside_app_dir(self):
        self.assertIn("{localappdata}\\{#MyDataDirName}", bi.ISS_TEMPLATE)


class RenderTest(unittest.TestCase):
    """渲染产物：不留在占位符、版本只认 VERSION、架构标识不用弃用写法。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)
        self.exe = Path("C:/fake/src/tjise-activity-watch-v9.9.9-win-x64.exe")
        self.text = bi.render_iss("9.9.9", self.exe, self.out)

    def test_no_placeholder_left(self):
        self.assertEqual(re.findall(r"@@[A-Z_]+@@", self.text), [])

    def test_version_comes_from_argument(self):
        self.assertIn("AppVersion=9.9.9", self.text)
        self.assertIn("OutputBaseFilename=%s-安装版-9.9.9" % bi.PRODUCT_NAME,
                      self.text)

    def test_no_quotes_around_setup_values(self):
        # Inno 不剥离引号，加了会把引号当路径的一部分
        for line in self.text.splitlines():
            if line.startswith(("OutputDir=", "LicenseFile=")):
                self.assertNotIn('"', line)

    def test_arch_identifier_not_deprecated(self):
        self.assertIn("ArchitecturesAllowed=x64compatible", self.text)
        self.assertNotRegex(self.text, r"ArchitecturesAllowed=x64\s*$")

    def test_explicit_exe_keeps_fixed_install_name(self):
        """喂进来的 EXE 可能带版本号资产名，装到用户机器上必须仍是固定名。"""
        self.assertIn('Source: "%s"' % self.exe, self.text)
        self.assertIn('DestName: "%s"' % bi.EXE_NAME, self.text)

    def test_files_are_listed_one_by_one(self):
        """[Files] 必须逐个列文件；通配符拷贝会把构建目录里的杂物一起带走。"""
        sources = [line for line in self.text.splitlines()
                   if line.startswith("Source:")]
        self.assertEqual(len(sources), 1 + len(bi.INSTALL_FILES),
                         "应当只有 EXE + 白名单里的说明文件")
        for line in sources:
            self.assertNotIn("*", line)
            self.assertNotIn("recursesubdirs", line)

    def test_dev_only_files_never_packaged(self):
        sources = [line for line in self.text.splitlines()
                   if line.startswith("Source:")]
        for line in sources:
            self.assertNotIn("CHANGELOG", line)
            self.assertNotIn("gui-preview", line)

    def test_trouble_doc_ships_with_shortcut(self):
        self.assertIn('DestName: "遇到问题先看我.txt"', self.text)
        self.assertIn('Filename: "{app}\\遇到问题先看我.txt"', self.text)

    def test_language_falls_back_to_comment_when_missing(self):
        block = bi.languages_block(self.out, self.out / "nope.isl")
        self.assertTrue(block.startswith(";"), "没语言文件时不该硬写语言行")

    def test_vendored_language_file_is_used(self):
        block = bi.languages_block(self.out)
        self.assertIn('MessagesFile: "%s"' % bi.LANGUAGE_FILE.name, block)
        self.assertTrue((self.out / bi.LANGUAGE_FILE.name).is_file(),
                        "语言文件要复制到 .iss 旁边，否则编译时找不到")


class ProductNameTest(unittest.TestCase):
    """产品名全仓唯一：窗口、向导、卸载项、安装包文件名不许出现第二种叫法。"""

    def test_product_name_matches_window_title(self):
        """与 GUI 窗口标题同口径（local/README.md 写明）。"""
        self.assertEqual(bi.PRODUCT_NAME, "天软活动预报名工具")

    def test_template_uses_product_name_constant(self):
        self.assertIn('#define MyAppName "@@PRODUCT_NAME@@"', bi.ISS_TEMPLATE)

    def test_output_name_prefix_is_product_name(self):
        self.assertIn("OutputBaseFilename=@@PRODUCT_NAME@@-安装版-@@VERSION@@",
                      bi.ISS_TEMPLATE)

    def test_no_second_product_name_in_template(self):
        """旧叫法「天软校园活动报名工具」不得再出现在模板里。"""
        self.assertNotIn("天软校园活动报名工具", bi.ISS_TEMPLATE)
        self.assertNotIn("天软活动报名工具", bi.ISS_TEMPLATE)


class DocListTest(unittest.TestCase):
    """安装目录里的文件清单：少一份说明要拦住，多一份杂物不许放。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_required_doc_missing_stops_build(self):
        fake = ((self.root / "nope.txt", "nope.txt", True),)
        with mock.patch.object(bi, "INSTALL_FILES", fake):
            with self.assertRaises(SystemExit) as ctx:
                bi.doc_lines()
        self.assertIn("nope.txt", str(ctx.exception))

    def test_optional_doc_missing_is_skipped(self):
        (self.root / "a.txt").write_text("x", encoding="utf-8")
        fake = ((self.root / "a.txt", "a.txt", True),
                (self.root / "b.lock", "b.lock", False))
        with mock.patch.object(bi, "INSTALL_FILES", fake):
            lines = bi.doc_lines()
        self.assertIn("a.txt", lines)
        self.assertNotIn("b.lock", lines)

    def test_repo_docs_exist(self):
        for src, dest_name, required in bi.INSTALL_FILES:
            if required:
                self.assertTrue(Path(src).is_file(), "%s 不存在：%s" % (dest_name, src))

    def test_trouble_doc_covers_installed_form(self):
        text = (bi.HERE / "遇到问题先看我.txt").read_text(encoding="utf-8")
        for key in ("开始菜单", "%LOCALAPPDATA%", "卸载", "WebView2", "grab.log"):
            self.assertIn(key, text)
        self.assertIn("启动不了点我.txt", text, "要明确告诉绿色版用户去看另一份")


class TroubleDocCopyTest(unittest.TestCase):
    """排障文档的话术契约：面向陌生读者，不要求上传原始数据。"""

    @classmethod
    def setUpClass(cls):
        cls.text = (bi.HERE / "遇到问题先看我.txt").read_text(encoding="utf-8")

    def test_no_first_person_or_version_hardcode(self):
        """文档会随包流转到陌生同学手上：没有「我」，也不写死版本号。"""
        self.assertNotIn("发给我", self.text)
        self.assertNotIn("装出来的", self.text)
        self.assertNotRegex(self.text, r"安装版-\d+\.\d+\.\d+")

    def test_no_grab_words(self):
        for word in ("抢名额", "抢不到", "碰碰运气", "扫码登录"):
            self.assertNotIn(word, self.text, "排障话术不用抢类词：%s" % word)

    def test_no_raw_upload_requests(self):
        """不得要求上传登录文件或原始日志；反馈入口是 issue/邮箱。"""
        self.assertNotIn("把下面两样发", self.text)
        self.assertNotIn("上传", self.text)
        self.assertIn("不要把 session.json", self.text,
                      "必须明确警告登录文件不能外发")
        for channel in ("Issues", "feng.ye147800@gmail.com"):
            self.assertIn(channel, self.text, "反馈入口要写明：%s" % channel)

    def test_product_name_is_unified(self):
        self.assertIn("天软活动预报名工具", self.text)
        self.assertNotIn("天软校园活动报名工具", self.text)


class SourceCheckTest(unittest.TestCase):
    """源里混进运行期文件，就等于把自己的偏好打进别人的安装包。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = Path(self.tmp.name)

    def test_rejects_missing_exe(self):
        with self.assertRaises(SystemExit):
            bi.check_source(self.src / bi.EXE_NAME)

    def test_accepts_clean_dir(self):
        exe = self.src / bi.EXE_NAME
        exe.write_bytes(b"stub")
        bi.check_source(exe)   # 不抛就算过

    def test_rejects_stray_runtime_files(self):
        exe = self.src / bi.EXE_NAME
        exe.write_bytes(b"stub")
        (self.src / "data").mkdir()
        (self.src / "data" / "ui.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(SystemExit):
            bi.check_source(exe)


class BuildEntryPointTest(unittest.TestCase):
    """build()：release.py 的同源入口——只吃显式 EXE，产物名可预期。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.exe = self.root / "tjise-activity-watch-v9.9.9-win-x64.exe"
        self.exe.write_bytes(b"stub-frozen-exe")
        self.out = self.root / "installer"
        self.fake_iscc = self.root / "ISCC.exe"

    def test_build_returns_expected_installer_path(self):
        """stub 掉编译器：验证路径契约与 .iss 渲染，不真调 ISCC。"""
        rendered = {}

        def fake_run(cmd, text=True):
            rendered["iss"] = Path(cmd[1]).read_text(encoding="utf-8")
            # 编译器"产出"按 OutputBaseFilename 命名的安装包
            iss = rendered["iss"]
            base = [ln for ln in iss.splitlines()
                    if ln.startswith("OutputBaseFilename=")][0].split("=", 1)[1]
            (self.out / (base + ".exe")).write_bytes(b"stub-installer")
            return mock.Mock(returncode=0)

        with mock.patch.object(bi, "probe_iscc", return_value=self.fake_iscc), \
                mock.patch.object(bi.subprocess, "run", side_effect=fake_run):
            got = bi.build(self.exe, self.out, version="9.9.9")
        self.assertEqual(got, self.out / "天软活动预报名工具-安装版-9.9.9.exe")
        self.assertIn('Source: "%s"' % self.exe, rendered["iss"])
        self.assertIn('DestName: "%s"' % bi.EXE_NAME, rendered["iss"])

    def test_build_rejects_missing_exe(self):
        with self.assertRaises(SystemExit):
            bi.build(self.root / "nope.exe", self.out, version="9.9.9")


class PortableCleanupContractTest(unittest.TestCase):
    """绿色版构建的运行期文件清理契约：五个名字一个都不能少。"""

    def test_runtime_data_files_cover_all_local_state(self):
        import make_portable as mp
        self.assertEqual(set(mp.RUNTIME_DATA_FILES),
                         {"session.json", "storage_state.json", "ui.json",
                          "ui.json.lock", "grab.log"},
                         "与 runtime_paths.py 的登录态/偏好/日志文件必须一一对齐")

    def test_cleanup_actually_deletes_by_name(self):
        import make_portable as mp
        src = mp.__file__
        text = Path(src).read_text(encoding="utf-8")
        self.assertIn("victim.unlink()", text,
                      "清理必须是真实删除动作，不是只声明清单")


class LanguageFileTest(unittest.TestCase):
    """语言文件本身：没 BOM 就是一堆乱码界面，而且编译不报错。"""

    def setUp(self):
        self.path = bi.LANGUAGE_FILE
        if not self.path.is_file():
            self.skipTest("仓库里没有 %s" % self.path.name)
        self.raw = self.path.read_bytes()

    def test_has_utf8_bom(self):
        self.assertEqual(self.raw[:3], b"\xef\xbb\xbf",
                         "Inno 靠 BOM 判断 Unicode，没有 BOM 会按 ANSI 代码页读")

    def test_decodes_as_utf8(self):
        self.raw.decode("utf-8-sig")   # 解不开就是编码被改坏了

    def test_declares_simplified_chinese(self):
        text = self.raw.decode("utf-8-sig")
        self.assertIn("LanguageName=简体中文", text)
        self.assertIn("LanguageCodePage=936", text)

    def test_keeps_core_messages(self):
        text = self.raw.decode("utf-8-sig")
        for key in ("SetupAppTitle=", "WelcomeLabel1=", "FinishedHeadingLabel="):
            self.assertIn(key, text)


class VersionTest(unittest.TestCase):
    def test_repo_version_is_semver(self):
        self.assertRegex(bi.read_version(), r"^\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
