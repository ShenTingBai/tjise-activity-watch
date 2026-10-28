#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 Inno Setup 把一份冻结 EXE 打成安装版。

正式分发形态三种（决策 013）：独立 EXE、完整 ZIP、**安装版（本脚本产出）**——
开始菜单、控制面板卸载项、卸载时可确认删除登录数据。三者必须由**同一份冻结
EXE** 派生（同源由 tools/release.py 把关，本脚本只吃显式给定的 exe 文件，
自己绝不重新构建主程序）。绿色版 zip 已停止新增发布，只保留历史资产与
make_portable.py 的构建能力。

四条硬约束，改之前先读：

1. **版本号唯一来源是仓库根 VERSION**，渲染进 .iss。这里不许出现第二个版本字面量，
   否则安装包版本号和 EXE 会对不上，卸载项里显示的是旧版本。
2. **Inno Setup 的 `{}` 是它自己的常量语法**（`{app}`、`{localappdata}`），
   所以模板绝不能走 str.format —— 用 `@@KEY@@` 占位符做纯文本替换。
3. **程序和数据必须分离**。程序可以装进 Program Files（那里对普通进程不可写），
   登录态一律在 `%LOCALAPPDATA%\\TianSoftActivityPreRegistration`。卸载时
   **由用户确认后**才删这块数据：不能因为「想干净」就静默删掉别人的登录态——
   那等于远程替用户注销登录。
4. **产品名全仓唯一**：窗口标题口径是「天软活动预报名工具」（见 local/README.md），
   安装向导、卸载项、安装包文件名必须用同一个名字，不得再引入第二种叫法。

用法：
    python local/build_installer.py                          # 打 dist/tjise-grab-exe
    python local/build_installer.py --source dist/tjise-grab-exe-onedir
    python local/build_installer.py --iscc "D:/Inno Setup 6/ISCC.exe"

@author LiXinwen
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
VERSION_FILE = PROJECT / "VERSION"
LICENSE_FILE = PROJECT / "LICENSE"
DEFAULT_SOURCE = PROJECT / "dist" / "tjise-grab-exe"
DEFAULT_OUT = PROJECT / "dist" / "installer"
EXE_NAME = "tjise-activity-watch.exe"
# 产品名唯一来源：与 GUI 窗口标题同口径（local/README.md），安装向导、卸载项、
# 安装包文件名、开始菜单全部用它。改它 = 改对外品牌，需同步 UI 与文档。
PRODUCT_NAME = "天软活动预报名工具"
# 中文向导的语言文件：跟着仓库走，不依赖本机 Inno 安装目录（官方包不带中文）。
# 来源与 BOM 说明见文件头注释。
LANGUAGE_FILE = HERE / "installer" / "ChineseSimplified.isl"

# 安装目录里**允许出现**的东西：(源文件, 装到 {app} 下的名字, 是否必需)。
# 这份清单就是「哪些文件该打包」的唯一答案，改它等于改交付形态，写清理由再动：
#   README.md          —— 使用说明，和 Release 完整 ZIP 同一份（就是 local/README.md）
#   遇到问题先看我.txt —— 安装版专属排障（绿色版是包里的「启动不了点我.txt」）
#   LICENSE            —— 协议，跟着分发走
# 刻意**不打**：CHANGELOG.md（开发记录，用户不看，还占 32 KB）、
# gui-preview.png（README 在 GitHub 上的配图，装进本地是个打不开的死文件）。
INSTALL_FILES = (
    (HERE / "README.md", "README.md", True),
    (HERE / "遇到问题先看我.txt", "遇到问题先看我.txt", True),
    (PROJECT / "LICENSE", "LICENSE", False),
)

# 卸载项的主标识：一旦发布过就**永远不能改**。改了它，Windows 会把新旧版本
# 当成两个软件并存，用户会看到两条卸载记录、两份 shortcuts。
#
# 末尾只有**一个**右花括号，看着像少写了一个 —— 不是。Inno 只对**左**花括号
# 转义（`{{` 出 `<`），右花括号是普通字符。写成 `{{GUID}}` 会得到 `{GUID}}`，
# 卸载注册表键名就成了 `{GUID}}_is1`（已实测，多一个 `}`）。正确写法是 `{{GUID}`。
APP_ID = "{{8B1E9C4A-7F3D-4E2A-9C61-5D8F0A2B7E14}"
DATA_DIR_NAME = "TianSoftActivityPreRegistration"

ISCC_CANDIDATES = (
    Path("C:/Program Files (x86)/Inno Setup 6/ISCC.exe"),
    Path("C:/Program Files/Inno Setup 6/ISCC.exe"),
    Path("C:/Program Files (x86)/Inno Setup 5/ISCC.exe"),
)


def read_version() -> str:
    """构建前读 VERSION 并校验格式：版本不合法就不打，不产出版本对不上号的包。"""
    try:
        text = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit("[iss] 读不到 VERSION（%s）：%s" % (VERSION_FILE, exc))
    if not re.fullmatch(r"\d+\.\d+\.\d+", text):
        raise SystemExit("[iss] VERSION 格式非法：%r（只允许 major.minor.patch）" % text)
    return text


def probe_iscc(explicit: str = "") -> Path:
    """找 Inno Setup 编译器：显式路径 > 常见安装路径 > 注册表卸载项。

    找不到必须明确报错并给出下载页——「假装成功」只会让用户以为包生成了。
    """
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise SystemExit("[iss] 指定的 ISCC.exe 不存在：%s" % path)
        return path
    for path in ISCC_CANDIDATES:
        if path.is_file():
            return path
    found = _iscc_from_registry()
    if found:
        return found
    raise SystemExit(
        "[iss] 没找到 Inno Setup 编译器（ISCC.exe）。\n"
        "      安装后重试：https://jrsoftware.org/isdl.php\n"
        "      或显式指定：python local/build_installer.py --iscc \"<ISCC.exe 路径>\""
    )


def _iscc_from_registry() -> Path | None:
    """从「卸载或更改程序」里反查 Inno Setup 自己的安装位置。"""
    try:
        import winreg  # noqa: PLC0415 - 只有 Windows 上才有这个模块
    except ImportError:
        return None
    roots = (
        (getattr(winreg, "HKEY_LOCAL_MACHINE", None),
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1"),
        (getattr(winreg, "HKEY_LOCAL_MACHINE", None),
         r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1"),
    )
    for root, subkey in roots:
        if root is None:
            continue
        try:
            with winreg.OpenKey(root, subkey) as key:
                location = winreg.QueryValueEx(key, "InstallLocation")[0]
        except OSError:
            continue
        candidate = Path(location) / "ISCC.exe"
        if candidate.is_file():
            return candidate
    return None


ISS_TEMPLATE = """; ============================================================
; 本文件由 local/build_installer.py 依据 VERSION 渲染生成。
; 请勿手工编辑 —— 下次构建会覆盖，改动请改生成器。
; ============================================================

#define MyAppName "@@PRODUCT_NAME@@"
; 安装目录名刻意用英文：中文路径会让一部分老工具链、日志采集和命令行
; 排障都变麻烦；而开始菜单、卸载项里显示的仍是上行的中文 MyAppName。
#define MyAppDirName "TjiseActivityWatch"
#define MyAppExeName "@@EXE_NAME@@"
#define MyAppPublisher "LiXinwen"
#define MyDataDirName "@@DATA_DIR_NAME@@"

[Setup]
AppId=@@APP_ID@@
AppName={#MyAppName}
AppVersion=@@VERSION@@
AppVerName={#MyAppName} @@VERSION@@
AppPublisher={#MyAppPublisher}
; {autopf} 会跟着安装权限自动解析：默认 per-user 装到
; %LOCALAPPDATA%\\Programs（免管理员），用户选「为所有用户」则落到 Program Files。
; 两种情况下登录态都写 %LOCALAPPDATA%（Program Files 对普通进程不可写）。
DefaultDirName={autopf}\\{#MyAppDirName}
DefaultGroupName={#MyAppName}
DisableDirPage=auto
DisableProgramGroupPage=auto
@@LICENSE_LINE@@OutputDir=@@OUT_DIR@@
OutputBaseFilename=@@PRODUCT_NAME@@-安装版-@@VERSION@@
UninstallDisplayName={#MyAppName} @@VERSION@@
UninstallDisplayIcon={app}\\{#MyAppExeName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
; x64compatible（Inno 6.3+ 才有）比旧的 x64 准：后者已被官方标记弃用，
; 编译时每次都会刷一条 Warning。含义是「64 位 Windows 即可」，ARM64 上也
; 能装（PyInstaller 产出的 x64 exe 在 ARM64 上走系统仿真）。
ArchitecturesAllowed=x64compatible
; per-user 优先：普通学生机器不该被迫走 UAC。仍允许用户在安装时切到「为所有用户」。
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
CloseApplications=force

; 官方 Inno Setup **发行包不带**简体中文语言文件，所以这里引用仓库自带的
; local/installer/ChineseSimplified.isl（构建时复制到 .iss 旁边再按裸文件名引用）。
; 文件万一不在就退成一行注释、向导走英文——硬写死会在缺文件的机器上直接编译失败。
[Languages]
@@LANGUAGES@@

[Tasks]
Name: "desktopicon"; Description: "在桌面上创建快捷方式"; GroupDescription: "附加任务"; Flags: unchecked

[Files]
; 白名单，一个文件一行。**不要**改回 `Source: "...\\*"` 的整目录拷贝：
; 构建目录里躺着 CHANGELOG、README 配图、以及开发者误放的东西，整目录拷贝
; 会一起塞进别人电脑，事后还没人知道少了/多了什么。
; 主 EXE 显式 DestName：release.py 会把带版本号的资产名 EXE 喂进来，
; 装到用户机器上的名字必须仍是固定的 EXE_NAME，否则快捷方式对不上。
Source: "@@EXE_SRC@@"; DestDir: "{app}"; DestName: "@@EXE_NAME@@"; Flags: ignoreversion
@@DOC_LINES@@

[Icons]
Name: "{group}\\{#MyAppName}"; Filename: "{app}\\{#MyAppExeName}"
Name: "{group}\\遇到问题先看我"; Filename: "{app}\\遇到问题先看我.txt"
Name: "{group}\\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\\{#MyAppName}"; Filename: "{app}\\{#MyAppExeName}"; Tasks: desktopicon

[Code]
// 卸载收尾：数据目录不在安装目录下（Program Files 不可写），所以必须单独处理。
// 只问一次、默认「否」——保留登录态是安全侧，删掉是不可逆的。
//
// 两条独立的防线，缺一条都会让登录态在无人确认的情况下被删掉：
// 1) 静默卸载（/VERYSILENT、/SUPPRESSMSGBOXES）根本不进删除分支：消息框被
//    抑制时的返回值不是「用户的选择」，不能拿来当删除依据；
// 2) 交互式卸载用 SuppressibleMsgBox + MB_DEFBUTTON2 + IDNO：默认焦点落在
//    「否」，直接回车或关闭对话框都保留数据。
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\\{#MyDataDirName}');
    if DirExists(DataDir) then
    begin
      if UninstallSilent then
      begin
        // 静默卸载：没人回答过这个问题，数据一律保留
        Log('静默卸载：保留数据目录 ' + DataDir);
      end
      else if SuppressibleMsgBox('要连同登录数据一起删除吗？' + #13#10 + #13#10 +
                '删掉：下次使用需要重新登录。' + #13#10 +
                '保留：以后重新安装可以直接用。' + #13#10 + #13#10 +
                '数据目录：' + DataDir,
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
      begin
        DelTree(DataDir, True, True, True);
      end;
    end;
  end;
end;
"""


def languages_block(out_dir: Path, language_file: "Path | None" = None) -> str:
    """决定 [Languages] 段写什么：仓库自带的中文语言文件优先。

    语言文件**跟着仓库走**，不从 Inno 安装目录里找。理由是官方发行包（6.7.3）
    的 Languages 目录里没有中文，靠 `compiler:Languages\\...` 引用等于「我这台能
    编、别人那台报错」。仓库里带一份，谁 clone 都能编出中文向导。

    找不到就返回一行注释：向导走英文，但安装包本身完全可用。
    """
    source = Path(language_file) if language_file is not None else LANGUAGE_FILE
    if not source.is_file():
        return ("; 未找到 %s，向导使用英文（把语言文件放回该路径即可用中文）"
                % source.name)
    _warn_if_no_bom(source)
    # 复制到 .iss 旁边再按裸文件名引用：绕开空格/中文路径在 MessagesFile 里的
    # 引号解析问题，也让 dist/installer 成为自足的编译现场。
    shutil.copy2(source, Path(out_dir) / source.name)
    return 'Name: "chinesesimplified"; MessagesFile: "%s"' % source.name


def _warn_if_no_bom(path: Path) -> None:
    """没有 BOM 的 .isl 会被 Inno 按系统 ANSI 代码页读，中文直接乱码。

    官方 issrc 里的原文件就是无 BOM 的，照抄进来一定踩这个坑，所以进仓库时
    补了 BOM；这里只提醒，不拦编译——文案乱码能看见，拦下来会挡住整个打包。
    """
    if path.read_bytes()[:3] != b"\xef\xbb\xbf":
        print("[iss] 警告：%s 没有 UTF-8 BOM，中文向导可能显示乱码" % path.name,
              flush=True)


def doc_lines() -> str:
    """按 INSTALL_FILES 生成 [Files] 里的说明/协议行。

    必需项缺一个就报错——「装完没有使用说明」这种包不该被生产出来；
    可选项（LICENSE）缺了只是少打一份，不挡构建。
    """
    lines = []
    missing = []
    for src, dest_name, required in INSTALL_FILES:
        if not Path(src).is_file():
            if required:
                missing.append("%s（%s）" % (dest_name, src))
            continue
        lines.append('Source: "%s"; DestDir: "{app}"; DestName: "%s"; '
                     'Flags: ignoreversion' % (src, dest_name))
    if missing:
        raise SystemExit("[iss] 缺少要装进安装目录的文件：%s" % "、".join(missing))
    return "\n".join(lines)


def render_iss(version: str, exe: Path, out_dir: Path) -> str:
    """把模板里的 @@KEY@@ 全部替换掉；漏一个就报错，绝不产出半成品。

    exe 是**显式给定的冻结 EXE 文件**（不是目录）：调用方负责它和独立 EXE
    资产同源；本脚本不构建主程序，装进去的就是这份文件的字节。

    路径一律用 Windows 反斜杠写进 [Setup]/[Files]：Inno 虽然也认正斜杠，
    但 Source 通配符混用两种分隔符时行为不够直观，统一一种省得排查。

    [Setup] 段的值**不加引号**——Inno 不会剥离引号，加了会把引号当成
    路径的一部分，结果就是「找不到 license 文件」。
    """
    license_line = ""
    if LICENSE_FILE.is_file():
        license_line = "LicenseFile=%s\n" % LICENSE_FILE
    mapping = {
        "@@VERSION@@": version,
        "@@PRODUCT_NAME@@": PRODUCT_NAME,
        "@@EXE_NAME@@": EXE_NAME,
        "@@DATA_DIR_NAME@@": DATA_DIR_NAME,
        "@@APP_ID@@": APP_ID,
        "@@EXE_SRC@@": str(Path(exe)),
        "@@DOC_LINES@@": doc_lines(),
        "@@OUT_DIR@@": str(out_dir),
        "@@LICENSE_LINE@@": license_line,
        "@@LANGUAGES@@": languages_block(out_dir),
    }
    text = ISS_TEMPLATE
    for key, value in mapping.items():
        text = text.replace(key, value)
    left = re.findall(r"@@[A-Z_]+@@", text)
    if left:
        raise SystemExit("[iss] 模板占位符未全部替换：%s" % ", ".join(sorted(set(left))))
    return text


def check_source(exe: Path) -> None:
    """打包前先确认这份冻结 EXE 像个真产物，而不是半成品或错路径。

    顺手把「不会进包的东西」念出来：EXE 所在目录通常是 build_exe.py 的产物堆，
    里面有 CHANGELOG、配图等，用户不该收到。白名单在 [Files] 里把关，
    这里只负责别让人误以为它们会被打进去。
    """
    exe = Path(exe)
    if not exe.is_file():
        raise SystemExit(
            "[iss] 打包源 EXE 不存在：%s\n"
            "      先构建 EXE：python local/build_exe.py [--mode onedir]" % exe)
    source = exe.parent
    if (source / "data").is_dir():
        stray = [p.name for p in (source / "data").iterdir() if p.is_file()]
        if stray:
            raise SystemExit(
                "[iss] EXE 旁边 data/ 里还有本机残留：%s\n"
                "      这些是运行期文件，打进安装包会把你的偏好带给别人。\n"
                "      make_portable.py 会自动清；这里请先手工删除。"
                % ", ".join(stray))
    allowed = {exe.name, EXE_NAME} | {dest for _, dest, _ in INSTALL_FILES}
    extra = sorted(p.name for p in source.iterdir()
                   if p.is_file() and p.name not in allowed)
    if extra:
        print("[iss] 源目录里这些文件不会打包：%s" % "、".join(extra), flush=True)


def build(exe: "Path | str", out_dir: "Path | str",
          version: "str | None" = None, iscc: "Path | str | None" = None) -> Path:
    """从一份显式冻结 EXE 构建安装包，返回安装包路径（tools/release.py 的同源入口）。

    版本缺省读 VERSION；ISCC 缺省自动探测（找不到就报错停止，绝不假装成功）。
    返回的路径按「产品名-安装版-版本.exe」精确命名——目录里若有别的旧安装包，
    一律不算本次产物。
    """
    exe = Path(exe).resolve()
    out_dir = Path(out_dir).resolve()
    version = read_version() if version is None else version
    out_dir.mkdir(parents=True, exist_ok=True)
    check_source(exe)
    iscc = probe_iscc() if iscc is None else Path(iscc)
    iss_path = out_dir / "installer.generated.iss"
    iss_path.write_text(render_iss(version, exe, out_dir), encoding="utf-8")
    print("[iss] 已渲染：%s" % iss_path, flush=True)
    print("[iss] 版本（VERSION 唯一来源）：%s　源 EXE：%s" % (version, exe), flush=True)
    print("[iss] 编译器：%s" % iscc, flush=True)
    result = subprocess.run([str(iscc), str(iss_path)], text=True)
    if result.returncode != 0:
        raise SystemExit("[iss] ISCC 编译失败，退出码 %d" % result.returncode)
    expected = out_dir / ("%s-安装版-%s.exe" % (PRODUCT_NAME, version))
    if not expected.is_file():
        raise SystemExit("[iss] ISCC 返回成功但没找到 %s，检查 OutputDir 配置"
                         % expected.name)
    print("[iss] 安装包：%s　%.1f MB"
          % (expected, expected.stat().st_size / 1024 / 1024), flush=True)
    print("[iss] 提示：安装后有开始菜单项，卸载时可选择是否删除登录数据。",
          flush=True)
    return expected


def main() -> int:
    parser = argparse.ArgumentParser(description="用 Inno Setup 构建安装版")
    parser.add_argument("--source", default=str(DEFAULT_SOURCE),
                        help="打包源目录，默认 dist/tjise-grab-exe")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="渲染产物与安装包输出目录，默认 dist/installer")
    parser.add_argument("--iscc", default="", help="ISCC.exe 路径（自动探测失败时用）")
    parser.add_argument("--no-build", action="store_true",
                        help="只渲染 .iss 不调用 ISCC（用于查看内容/离线 CI）")
    args = parser.parse_args()

    version = read_version()
    source = Path(args.source).resolve()
    exe = source / EXE_NAME
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # --no-build 只渲染给人和 CI 看，不需要编译器；
    # 语言文件跟仓库走，渲染阶段就能定下来，与装没装 Inno 无关。
    if args.no_build:
        check_source(exe)
        iss_path = out_dir / "installer.generated.iss"
        iss_path.write_text(render_iss(version, exe, out_dir), encoding="utf-8")
        print("[iss] 已渲染：%s" % iss_path, flush=True)
        print("[iss] 版本（VERSION 唯一来源）：%s　源 EXE：%s" % (version, exe),
              flush=True)
        print("[iss] --no-build：跳过编译", flush=True)
        return 0
    return 0 if build(exe, out_dir, version=version, iscc=args.iscc or None) else 1


if __name__ == "__main__":
    raise SystemExit(main())
