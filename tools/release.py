#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打一条 Release：本地资产准备与外部发布分离，publish 必须显式打开。

三个阶段（默认只做 check，零副作用；三阶段互斥）：

    python tools/release.py --check               # 只读检查：版本、资产命名、预期清单、发布门禁预演
    python tools/release.py --prepare             # 本地准备：构建 onefile EXE + 组装 EXE/ZIP 资产（不打 tag、不上传）
    python tools/release.py --publish [--yes]     # 外部发布：核验资产 → 干净 main 门禁 → 人工确认 → tag → 上传

副作用边界（各阶段）：
- check 只读零副作用；
- prepare 只写 dist/ 本地文件，可在 feature 分支跑（产物仅供预览，manifest 会如实记录
  来源 commit、分支与 source_dirty；脏构建不得作为正式候选发布）；
- publish 在任何 tag/上传**之前**逐项核验候选资产（版本/tag/完整 HEAD 来源/显式 source_dirty=false/
  EXE/ZIP/worker 齐全/大小与 SHA-256 一致/ZIP 白名单成员无多余），任何一项不符
  直接拒绝并提示显式 --prepare，绝不静默复用旧候选或现场补构建；人工确认**之后**再复核
  「同一干净 main/完整 HEAD/VERSION/资产指纹」——确认等待期间任何变化都中止；
  tag 显式绑定已核验提交、远端 tag（ls-remote 独立核验，peeled 优先）必须指向同一提交，
  缺失才创建/推送，绝不 force 改写；`gh release create --verify-tag` 禁止 gh 自动补 tag；
  已有 Release 默认不覆盖附件（要覆盖必须显式 --clobber）。

兼容输入：位置参数 tag 只作校验（必须与 VERSION 推导一致，否则拒绝），
**不再单独触发发布**——要发布必须显式 --publish。

本机正式资产 = 便携入口 EXE + 完整 ZIP（+manifest 清单）。绿色版/服务器版打包
（build_portable / zip_portable / zip_server）保留为**另行授权的兼容发行流程**，
publish 不会隐式触发，也不会隐式同步 local/server 远端分发分支。

产物（版本号唯一来源是仓库根 VERSION，本工具不再自定版本）：

    dist/release-<tag>/天软活动预报名工具-<tag>-win-x64.zip   完整便携包（推荐下载：独立 EXE + 说明）
    dist/release-<tag>/天软活动预报名工具-<tag>-win-x64.exe   独立可用单文件（内置 grab-worker）
    dist/release-<tag>/manifest.json                          版本/完整 HEAD/来源是否干净/每文件 SHA-256

@author LiXinwen
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
GH = Path(os.environ.get("GH_EXE", r"D:/Env/gh/bin/gh.exe"))
PROXY = "http://127.0.0.1:7897"     # api.github.com 在本机要走代理，git push 不用

PRODUCT_NAME = "天软活动预报名工具"
VERSION_FILE = ROOT / "VERSION"

# 服务器版打包时排除的东西：密钥、运行时数据、产物
SERVER_EXCLUDE = {"config.json", "data", "dist", "reports",
                  "__pycache__", "node_modules"}

# 服务器 ZIP 只允许这些顶层成员：任何 data/、config.json、缓存、日志都进不来。
# 与 SERVER_EXCLUDE（复制时忽略）是两道独立防线：先排除，再按成员白名单复核。
SERVER_ALLOWED_TOP = {"bot", "deploy", "docs", "scripts", "tests",
                      "README.md", "CHANGELOG.md", "LICENSE",
                      "requirements.txt", "config.example.json"}

# 完整便携 ZIP 的白名单：只放这四样，任何 data/config/log/缓存/外置 worker 都进不来
# （worker 已作为数据资源内置进主 EXE，单文件独立可用——五成员旧口径已被 2026-10-25 决定取代）
ZIP_MEMBERS = ("EXE", "README.md", "启动说明.txt", "LICENSE")

LAUNCH_NOTE = """{name} v{version}（Windows x64）

第一次使用：
1. 把整个 zip 解压到任意目录（路径别带中文和空格），双击「{exe_name}」。
2. 首次打开点右上角「登录」，在弹出的浏览器里完成一次登录；之后重开会自动复用，只有失效才需要重登。

必读：
- 主 EXE 独立可用：程序组件（grab-worker）已内置，单独拷走 EXE 也能直接用；
  完整 ZIP 是「EXE + 说明 + 协议」的分发形式，两者内容一致，推荐下载 ZIP。
- 需要 Windows 10/11 与 WebView2 运行时（一般自带），浏览器用电脑上的 Chrome 或 Edge。
- 登录态等数据存在本机 %LOCALAPPDATA%\\TianSoftActivityPreRegistration\\data，
  替换新版 EXE 不需要重新登录。

只给本人账号使用；请遵守学校关于活动的管理规定。
"""


def read_version() -> str:
    """版本唯一来源：仓库根 VERSION；格式非法直接失败，不产出版本对不上号的资产名。"""
    try:
        text = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit("[release] 读不到 VERSION（%s）：%s" % (VERSION_FILE, exc))
    if not re.fullmatch(r"\d+\.\d+\.\d+", text):
        raise SystemExit("[release] VERSION 格式非法：%r（只允许 major.minor.patch）" % text)
    return text


def tag_of(version: str) -> str:
    return "v" + version


def exe_asset_name(version: str) -> str:
    """下载资产的唯一英文基名。

    GitHub 会把中文前缀规范化成别的形式（已发布 v1.1.0 就靠人工补救过一次），
    所以下载名只用 ASCII，中文产品名只出现在 Release 标题/说明里。
    manifest、prepare、verify、publish 共用这一个函数，不产生手工映射。
    """
    return "tjise-activity-watch-v%s-win-x64.exe" % version


def zip_asset_name(version: str) -> str:
    return "tjise-activity-watch-v%s-win-x64.zip" % version


def run(cmd: list[str], cwd: Path = ROOT, env: dict | None = None) -> str:
    res = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, env=env,
                         encoding="utf-8", errors="replace")
    if res.returncode != 0:
        raise SystemExit("命令失败：%s\n%s\n%s"
                         % (" ".join(cmd), res.stdout[-1500:], res.stderr[-1500:]))
    return res.stdout


def git_state() -> dict:
    """只读采集当前分支 / 脏文件 / HEAD（短与完整），不做任何门禁判断。"""
    return {
        "branch": run(["git", "branch", "--show-current"]).strip(),
        "dirty": run(["git", "status", "--porcelain"]).strip(),
        "commit": run(["git", "rev-parse", "--short", "HEAD"]).strip(),
        "full": run(["git", "rev-parse", "HEAD"]).strip(),
    }


def source_commit() -> str:
    """外部发布门禁：Release 只能从干净 main 读取，避免把设计分支或半成品发布出去。

    返回**完整** HEAD：manifest 来源核验与 tag 指向核验都用它（短哈希可撞前缀）。
    """
    state = git_state()
    if state["branch"] != "main":
        raise SystemExit("外部发布必须从 main 运行，当前分支是 %s（准备阶段可在 feature 分支，"
                         "发布阶段不行——也不允许临时切 main 发布半成品）"
                         % (state["branch"] or "（分离 HEAD）"))
    if state["dirty"]:
        raise SystemExit("外部发布要求工作区干净，请先提交或清理改动：\n%s" % state["dirty"])
    return state["full"]


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_manifest(stage_dir: Path, version: str, tag: str, files: list[Path],
                   build_state: dict | None = None) -> Path:
    """资产证据：版本、tag、来源分支、**完整 HEAD**、来源是否干净、每个文件的 SHA-256 与大小。

    source_dirty=true 的产物只能当预览：main 干净不等于资产来自干净提交，
    发布侧会拒绝脏候选，绝不把它说成来源于干净提交。
    """
    state = build_state or git_state()
    manifest = {
        "version": version,
        "tag": tag,
        "branch": state["branch"],
        "commit": state["full"],
        "commit_short": state["commit"],
        "source_dirty": bool(state["dirty"]),
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "files": {p.name: {"sha256": sha256_of(p), "bytes": p.stat().st_size}
                  for p in files},
    }
    path = stage_dir / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def build_exe_onefile() -> Path:
    """调用 local/build_exe.py 构建 onefile 主 EXE（worker 已内置为数据资源，产物在 dist/tjise-grab-exe）。"""
    print("[prepare] 构建 onefile EXE（内置 grab-worker）...")
    run([sys.executable, str(ROOT / "local" / "build_exe.py"), "--mode", "onefile"])
    out = DIST / "tjise-grab-exe"
    main = out / "tjise-activity-watch.exe"
    if not main.is_file():
        raise SystemExit("构建产物缺失：%s" % main)
    return out


def stage_local_assets(version: str) -> dict:
    """准备阶段：构建 + 按白名单组装完整 ZIP 与单文件 EXE 资产，写 manifest。

    只产生本地文件：不打 tag、不上传、不访问网络；OUT/data 里的旧登录态
    永远进不了 ZIP（白名单按文件名收集，data 目录不在清单里）。
    构建前后来源（HEAD/脏状态）必须一致：构建中途改代码的产物不能当候选。
    """
    tag = tag_of(version)
    pre = git_state()
    out = build_exe_onefile()
    post = git_state()
    if (pre["full"], pre["dirty"]) != (post["full"], post["dirty"]):
        raise SystemExit("[prepare] 构建期间源码发生变化（%s%s → %s%s），本次产物不能当候选；"
                         "稳定后请重跑 --prepare"
                         % (pre["commit"], "*" if pre["dirty"] else "",
                            post["commit"], "*" if post["dirty"] else ""))
    stage_dir = DIST / ("release-" + tag)
    shutil.rmtree(stage_dir, ignore_errors=True)
    stage_dir.mkdir(parents=True)

    # 白名单暂存目录：成员名固定，绝不整目录拷贝（防止 data/config/log 混入）
    staging = stage_dir / ".zip-root"
    staging.mkdir()
    shutil.copy2(out / "tjise-activity-watch.exe", staging / exe_asset_name(version))
    shutil.copy2(ROOT / "local" / "README.md", staging / "README.md")
    # 启动说明：输入先统一成 LF 再按 CRLF 输出——源文件若被 CRLF 检出
    # （fresh clone + autocrlf），字面量自带 \r\n，直接 newline 翻译会产生 CR CR LF
    (staging / "启动说明.txt").write_text(
        LAUNCH_NOTE.format(name=PRODUCT_NAME, version=version,
                           exe_name=exe_asset_name(version)).replace("\r\n", "\n"),
        encoding="utf-8", newline="\r\n")
    if not (ROOT / "LICENSE").is_file():
        raise SystemExit("缺少 LICENSE，发行包必须带协议")
    shutil.copy2(ROOT / "LICENSE", staging / "LICENSE")

    zip_path = stage_dir / zip_asset_name(version)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for member in ZIP_MEMBERS:
            name = exe_asset_name(version) if member == "EXE" else member
            zf.write(staging / name, name)

    exe_path = stage_dir / exe_asset_name(version)
    shutil.copy2(out / "tjise-activity-watch.exe", exe_path)
    shutil.rmtree(staging, ignore_errors=True)

    manifest = write_manifest(stage_dir, version, tag, [zip_path, exe_path],
                              build_state=post)
    for path in (zip_path, exe_path):
        print("   %s  %.1f MB" % (path.name, path.stat().st_size / 1024 / 1024))
    print("[prepare] 资产就绪：%s" % stage_dir)
    print("[prepare] 清单：%s（版本/完整 HEAD/来源%s/SHA-256）"
          % (manifest, "脏（仅供预览，不得发布）" if post["dirty"] else "干净"))
    return {"zip": zip_path, "exe": exe_path, "manifest": manifest}


def check_only(version: str) -> None:
    """默认阶段：只读检查，输出资产命名、ZIP 白名单与发布门禁预演结果。"""
    tag = tag_of(version)
    print("[check] 版本（VERSION 唯一来源）：%s → tag %s" % (version, tag))
    print("[check] 资产命名：%s / %s" % (zip_asset_name(version), exe_asset_name(version)))
    print("[check] 完整 ZIP 白名单成员：%s"
          % "、".join(exe_asset_name(version) if m == "EXE" else m for m in ZIP_MEMBERS))
    missing = [str(p) for p in (ROOT / "local" / "build_exe.py", ROOT / "local" / "README.md",
                                ROOT / "LICENSE", VERSION_FILE) if not p.is_file()]
    if missing:
        raise SystemExit("[check] 缺少输入文件：%s" % "、".join(missing))
    state = git_state()
    gate_ok = state["branch"] == "main" and not state["dirty"]
    print("[check] 当前 %s@%s，工作区%s；外部发布门禁（main+干净）现在%s"
          % (state["branch"] or "（分离 HEAD）", state["commit"],
             "干净" if not state["dirty"] else "有未提交改动",
             "会通过" if gate_ok else "不会通过（prepare 不受影响，publish 前先合并回 main）"))
    stage_dir = DIST / ("release-" + tag)
    manifest = stage_dir / "manifest.json"
    if manifest.is_file():
        try:
            info = json.loads(manifest.read_text(encoding="utf-8"))
            print("[check] 已有候选：%s（来源 %s@%s，%s）——%s"
                  % (stage_dir, info.get("branch"), (info.get("commit") or "")[:12],
                     "脏构建，仅供预览" if info.get("source_dirty") else "来源干净",
                     "正式发布前必须在干净 main 重新 --prepare 并通过核验"
                     if info.get("source_dirty") or info.get("commit") != state["full"]
                     else "publish 会逐项核验后使用"))
        except (OSError, json.JSONDecodeError):
            print("[check] 已有候选 manifest 读不了：%s（publish 会拒绝）" % manifest)
    else:
        print("[check] 尚无 %s 候选：publish 只认显式 --prepare 的产物，不会现场补构建" % tag)
    print("[check] 本阶段零副作用：不构建、不打 tag、不上传")


def build_portable() -> None:
    """兼容发行流程（另行授权）：绿色版目录构建。publish 不会隐式调用。"""
    print("构建绿色版 ...")
    run([sys.executable, str(ROOT / "local" / "make_portable.py")])


def zip_portable(tag: str) -> Path:
    """兼容发行流程（另行授权）：绿色版压缩包。publish 不会隐式调用。"""
    src = DIST / "tjise-grab-portable"
    if not src.is_dir():
        raise SystemExit("没找到 %s，先运行 local/make_portable.py" % src)
    out = DIST / ("tjise-grab-portable-%s.zip" % tag)
    out.unlink(missing_ok=True)
    shutil.make_archive(str(out.with_suffix("")), "zip",
                        root_dir=str(DIST), base_dir="tjise-grab-portable")
    return out


def server_manifest_path(tag: str) -> Path:
    """服务器 ZIP 的 provenance 边车：与 zip 同目录，解压前就能核验。"""
    return DIST / ("tjise-server-%s.manifest.json" % tag)


def _zip_member_map(zip_path: Path) -> dict:
    """读出 zip 成员（去掉顶层目录名）→ {sha256, bytes}，越界成员直接拒绝。"""
    members: dict[str, dict] = {}
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            rel = name.split("/", 1)[1] if "/" in name else name
            top = rel.split("/", 1)[0]
            if top not in SERVER_ALLOWED_TOP:
                raise SystemExit(
                    "[server] ZIP 成员越界：%s（只允许 %s）"
                    % (rel, "、".join(sorted(SERVER_ALLOWED_TOP))))
            if rel == "config.json" or rel.startswith("data/"):
                raise SystemExit("[server] ZIP 混入运行时数据：%s" % rel)
            members[rel] = {
                "sha256": hashlib.sha256(zf.read(info)).hexdigest(),
                "bytes": info.file_size,
            }
    return members


def zip_server(tag: str) -> Path:
    """兼容发行流程（另行授权）：服务器版源码包 + manifest/SHA 边车。

    publish 不会隐式调用；生成的 manifest 记录版本、完整 HEAD、来源是否干净、
    ZIP 自身 SHA-256 与每个成员的 SHA-256/大小，可离线重算核验（P2-5）。
    """
    out = DIST / ("tjise-server-%s.zip" % tag)
    out.unlink(missing_ok=True)
    staging = DIST / ("tjise-server-" + tag)   # 解压后的顶层目录名，别带点（Windows 会当隐藏目录）
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(
        ROOT / "server", staging,
        ignore=shutil.ignore_patterns(*SERVER_EXCLUDE))
    # 服务器版单独带一份运维文档，克隆下来就能查
    shutil.copytree(ROOT / "docs", staging / "docs")
    shutil.copy2(ROOT / "README.md", staging / "README.md")
    shutil.copy2(ROOT / "CHANGELOG.md", staging / "CHANGELOG.md")
    # 协议跟着分发走：没有 LICENSE 的 zip 别人拿到只有"保留所有权利"的默认解释
    if (ROOT / "LICENSE").exists():
        shutil.copy2(ROOT / "LICENSE", staging / "LICENSE")
    shutil.make_archive(str(out.with_suffix("")), "zip",
                        root_dir=str(DIST), base_dir=staging.name)
    shutil.rmtree(staging, ignore_errors=True)

    members = _zip_member_map(out)
    state = git_state()
    manifest = {
        "schema": 1,
        "kind": "server",
        "version": VERSION_FILE.read_text(encoding="utf-8").strip(),
        "tag": tag,
        "branch": state["branch"],
        "commit": state["full"],
        "commit_short": state["commit"],
        "source_dirty": bool(state["dirty"]),
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        "zip": {"name": out.name, "sha256": sha256_of(out),
                "bytes": out.stat().st_size},
        "files": members,
    }
    path = server_manifest_path(tag)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print("[server] %s（%d 个成员，manifest：%s）" % (out.name, len(members), path.name))
    return out


def verify_server_zip(tag: str) -> dict:
    """重算成员哈希与 ZIP 哈希；任何不一致都拒绝，不用最低版本冒充 provenance。"""
    zip_path = DIST / ("tjise-server-%s.zip" % tag)
    manifest_path = server_manifest_path(tag)
    if not zip_path.is_file():
        raise SystemExit("[server] 找不到 %s" % zip_path)
    if not manifest_path.is_file():
        raise SystemExit("[server] 缺 manifest：%s（先运行 zip_server）" % manifest_path)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit("[server] manifest 读不了：%s" % exc)
    if manifest.get("zip", {}).get("sha256") != sha256_of(zip_path):
        raise SystemExit("[server] ZIP 哈希与 manifest 不一致：候选被改动过")
    members = _zip_member_map(zip_path)
    if members != manifest.get("files"):
        raise SystemExit("[server] ZIP 成员与 manifest 不一致：成员被增删或替换")
    if manifest.get("version") != VERSION_FILE.read_text(encoding="utf-8").strip():
        raise SystemExit("[server] manifest 版本与 VERSION 不一致")
    return {"manifest": manifest, "members": len(members)}


def verify_release_assets(version: str) -> dict:
    """发布前资产核验（独立函数，do_publish 在任何 tag/上传之前调用）。

    逐项核对：manifest 存在且 version/tag 一致、来源=当前完整 HEAD 且非脏构建、
    EXE/ZIP 齐全、大小与 SHA-256 与 manifest 一致、ZIP 成员恰为白名单（无 data 混入）。
    任何一项不符都直接拒绝并提示显式 --prepare——绝不静默复用旧候选、不现场补构建；
    「当前在干净 main」本身不构成绕过理由：main 干净不等于资产来自 main。
    """
    tag = tag_of(version)
    stage_dir = DIST / ("release-" + tag)
    manifest_path = stage_dir / "manifest.json"
    if not manifest_path.is_file():
        raise SystemExit("[publish] %s 缺 manifest：没有可核验的 prepare 产物。"
                         "请先在目标来源上显式运行 --prepare；publish 不会现场补构建"
                         % stage_dir)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit("[publish] manifest 读不了（%s）：候选不完整，请重新 --prepare" % exc)
    if not isinstance(manifest, dict):
        raise SystemExit("[publish] manifest 根不是对象：候选清单损坏，请重新 --prepare")
    if manifest.get("version") != version or manifest.get("tag") != tag:
        raise SystemExit("[publish] manifest 版本/tag（%s/%s）与当前候选（%s/%s）不一致："
                         "旧候选不得发布，请重新 --prepare"
                         % (manifest.get("version"), manifest.get("tag"), version, tag))
    # source_dirty 只认显式布尔 false：字段缺失/null/0/"false" 一律来源未知，不得发布
    dirty_flag = manifest.get("source_dirty")
    if dirty_flag is not False:
        raise SystemExit("[publish] manifest.source_dirty 缺失或不是布尔 false（%r）："
                         "来源未知不得发布；请在干净 main 上重新 --prepare" % (dirty_flag,))
    state = git_state()
    if manifest.get("commit") != state["full"]:
        raise SystemExit("[publish] 候选来源 %s ≠ 当前 HEAD %s：这份资产不是当前提交构建的，"
                         "不得发布；请在当前来源重新 --prepare"
                         % (manifest.get("commit"), state["full"]))
    exe_path = stage_dir / exe_asset_name(version)
    zip_path = stage_dir / zip_asset_name(version)
    for path in (exe_path, zip_path):
        if not path.is_file():
            raise SystemExit("[publish] 资产缺失：%s（请重新 --prepare，不要拼接旧产物）" % path)
        meta = (manifest.get("files") or {}).get(path.name)
        if not isinstance(meta, dict):
            raise SystemExit("[publish] manifest 里没有 %s 的清单记录" % path.name)
        if path.stat().st_size != meta.get("bytes"):
            raise SystemExit("[publish] %s 大小与 manifest 不符（%d ≠ %s）：文件被动过，拒绝发布"
                             % (path.name, path.stat().st_size, meta.get("bytes")))
        if sha256_of(path) != meta.get("sha256"):
            raise SystemExit("[publish] %s SHA-256 与 manifest 不符：文件被动过，拒绝发布" % path.name)
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        expected = {exe_asset_name(version) if m == "EXE" else m for m in ZIP_MEMBERS}
        if names != expected:
            extra = "、".join(sorted(names - expected)) or "（缺：%s）" % "、".join(sorted(expected - names))
            raise SystemExit("[publish] ZIP 成员与白名单不符（%s）：不得携带任何 data/config/log" % extra)
        # ZIP 内主 EXE 与单独资产 EXE 必须字节一致：同一份独立 EXE 的两种分发形态，
        # 不一致说明候选装配损坏（或混入了旧构建的 worker/入口）
        inner = zf.read(exe_asset_name(version))
        if hashlib.sha256(inner).hexdigest() != sha256_of(exe_path):
            raise SystemExit("[publish] ZIP 内主 EXE 与单独 EXE 不一致：候选装配损坏，"
                             "请重新 --prepare（不得手工拼包）")
    return {"stage": stage_dir, "exe": exe_path, "zip": zip_path, "manifest": manifest_path}


def remote_tag_commit(tag: str) -> str:
    """只读查询 origin 上同名 tag 的最终提交（peeled 优先，轻量 tag 取其值）。

    EXTRA-1：ls-remote 的 pattern 按完整引用名尾部匹配——只给精确 `<tag>` 一个
    pattern 时，附注 tag 的 `<tag>{}` peeled 行**不会**被返回，解析器只能拿到
    tag 对象 SHA，与提交 SHA 比对必然误报指向不符。所以同时请求
    `refs/tags/<tag>` 与 `<tag>^{}` 两个 pattern（peeled 行仍优先取用）。
    返回 "" 表示查询成功且 origin 上没有这个 tag；查询命令失败直接抛错拒绝，
    绝不把「查不到」和「查询失败」混为一谈——后者可能是网络/权限问题，
    当成不存在会绕过远端核验。
    """
    out = run(["git", "ls-remote", "--tags", "origin", tag, tag + "^{}"])
    peeled = ""
    lightweight = ""
    for line in out.strip().splitlines():
        sha, _, ref = line.partition("\t")
        ref = ref.strip()
        if ref == "refs/tags/%s^{}" % tag:       # 附注 tag 的 peeled 值（最终提交）优先
            peeled = sha.strip()
        elif ref == "refs/tags/%s" % tag:        # 轻量 tag 行（或附注 tag 对象本身）
            lightweight = sha.strip()
    return peeled or lightweight


def ensure_tag(tag: str, expect_commit: str) -> None:
    """让 origin 上的 tag 指向已核验的 expect_commit；任何不一致都在写入前拒绝，不 force。

    矩阵：远端已正确 → 复用；远端缺失且本地正确 → 显式 push 本地已核验 tag；
    两边都缺 → 创建显式绑定 expect_commit 的新附注 tag 再 push；本地或远端指向
    不一致 → 拒绝（不重写历史 tag）。push 完成后再查一次远端指向确认落地。
    """
    local_pointed = ""
    if run(["git", "tag", "-l", tag]).strip():
        local_pointed = run(["git", "rev-parse", tag + "^{}"]).strip()   # 解引用附注 tag 到提交
        if local_pointed != expect_commit:
            raise SystemExit("[publish] 本地 tag %s 指向 %s，与本次发布提交 %s 不一致。"
                             "不重写历史 tag：请核对该 tag 属于哪次发布，或修正 VERSION 后重来"
                             % (tag, local_pointed[:12], expect_commit[:12]))
    remote_pointed = remote_tag_commit(tag)
    if remote_pointed and remote_pointed != expect_commit:
        raise SystemExit("[publish] origin 上的 tag %s 指向 %s，与本次发布提交 %s 不一致："
                         "GitHub Release 会使用远端 tag，不得在错误指向上传。不 force 改写，"
                         "请人工核对该 tag 属于哪次发布" % (tag, remote_pointed[:12], expect_commit[:12]))
    if remote_pointed == expect_commit:
        print("   tag %s 已存在且远端指向本次提交，复用" % tag)
        return
    # 走到这里：远端缺失，且本地要么缺失要么已核对一致
    if not local_pointed:
        # 显式把新 tag 绑定到已核验提交——不靠「执行当时的 HEAD」默认值
        run(["git", "tag", "-a", tag, expect_commit, "-m", "%s %s" % (PRODUCT_NAME, tag)])
    run(["git", "push", "--quiet", "origin", tag])
    pushed = remote_tag_commit(tag)
    if pushed != expect_commit:
        raise SystemExit("[publish] push 后远端 tag %s 指向 %s ≠ %s：落地核验失败，"
                         "请人工检查远端状态后再试" % (tag, pushed[:12] or "（空）", expect_commit[:12]))
    print("   已创建并推送 tag %s → %s" % (tag, pushed[:12]))


def release_notes(tag: str) -> str:
    """取 CHANGELOG 里最新一节，并附上与资产名同源的下载行。

    下载名不再手写：GitHub 曾把中文前缀规范化，人工补救过一次（R16）。
    """
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    buf: list[str] = []
    for i, line in enumerate(lines):
        if line.startswith("## "):
            if buf:
                break
            buf.append(line)
            continue
        if buf:
            buf.append(line)
    body = "\n".join(buf).strip()
    version = tag[1:] if tag.startswith("v") else tag
    download = ("**下载**：`%s`（单 EXE） · `%s`（完整 ZIP）\n\n---\n\n"
                % (exe_asset_name(version), zip_asset_name(version)))
    return (body or tag) + "\n\n" + download


def publish(tag: str, assets: list[Path], clobber: bool = False) -> None:
    env = dict(os.environ, HTTPS_PROXY=PROXY)
    exists = subprocess.run([str(GH), "release", "view", tag],
                            capture_output=True, text=True, env=env,
                            encoding="utf-8", errors="replace")
    if exists.returncode == 0:
        if not clobber:
            raise SystemExit("[publish] Release %s 已存在：默认不覆盖已有附件。"
                             "确认要覆盖时显式加 --clobber（旧附件会被替换）" % tag)
        print("   Release %s 已存在，按 --clobber 覆盖附件" % tag)
        run([str(GH), "release", "upload", tag, *[str(a) for a in assets],
             "--clobber"], env=env)
        return
    notes_file = DIST / (".notes-" + tag + ".md")
    notes_file.write_text(release_notes(tag), encoding="utf-8")
    # --verify-tag：远端缺 tag 时让 gh 中止，禁止它从默认分支自动补一个
    # 未经我们核验的 tag（发布前 ensure_tag 已把远端 tag 对齐到已核验提交）
    run([str(GH), "release", "create", tag,
         "--verify-tag",
         "--title", "%s %s" % (PRODUCT_NAME, tag),
         "--notes-file", str(notes_file),
         *[str(a) for a in assets]], env=env)
    notes_file.unlink(missing_ok=True)


def confirm_publish(tag: str, assume_yes: bool) -> None:
    """外部发布的 user-gate：要么显式 --yes，要么人工输入 yes；其他输入一律中止。"""
    if assume_yes:
        print("[publish] 使用 --yes 跳过交互确认（仍然要求 main+干净工作区+资产核验通过）")
        return
    answer = input("即将创建 tag %s 并上传 GitHub Release（外部副作用）。输入 yes 继续："
                   % tag).strip().lower()
    if answer != "yes":
        raise SystemExit("未确认，已中止（没有创建 tag，也没有上传任何内容）")


def asset_digest(assets: dict) -> dict:
    """候选资产指纹：EXE/ZIP/manifest 的 SHA-256（确认前后比对用，防等待期被替换）。"""
    return {key: sha256_of(assets[key]) for key in ("zip", "exe", "manifest")}


def do_publish(tag: str, assume_yes: bool, version: str, clobber: bool = False) -> None:
    """外部发布：门禁 → 资产核验 → 人工确认 → 复核 → tag → 上传，副作用全部在核验之后。

    正式本机资产 = 便携入口 EXE + 完整 ZIP + manifest；publish 不自动构建、
    不调用 make_portable/绿色版组包，也不隐式同步 local/server 分发分支
    （兼容发行流程另行授权）。
    人工确认可能等待很久：确认后、第一次 Git 写入前必须重验「同一干净 main、
    同一完整 HEAD、同一 VERSION、同一批资产」——期间任何变化都中止并要求
    重新核验确认，绝不静默改用新候选；上传前再比对一次资产指纹。
    """
    commit_before = source_commit()   # 门禁一：干净 main，返回完整 HEAD（确认前快照）
    derived = tag_of(version)
    if tag != derived:                # 门禁二：tag 只由 VERSION 推导
        raise SystemExit("[publish] tag %s 与 VERSION 推导的 %s 不一致："
                         "tag 只能由 VERSION 推导，不接受手写覆盖" % (tag, derived))
    verified = verify_release_assets(version)   # 门禁三：资产与来源逐项核验（任何副作用之前）
    digest_before = asset_digest(verified)
    confirm_publish(tag, assume_yes)            # 门禁四：人工确认（在 tag/上传之前）

    # FG1：确认可能等了很久——确认后、第一次 Git 写入前，重验来源与资产都没变
    commit = source_commit()
    if commit != commit_before:
        raise SystemExit("[publish] 确认期间 HEAD 从 %s 变成 %s（切分支/新提交？）："
                         "确认结论只对当时的来源有效，已中止；请重新核验后再确认发布"
                         % (commit_before[:12], commit[:12]))
    rechecked = verify_release_assets(version)   # VERSION/来源/资产任何一项变了都会在这里拒绝
    if asset_digest(rechecked) != digest_before:
        raise SystemExit("[publish] 确认期间候选资产被替换（哈希变化）：确认结论已失效，"
                         "已中止；请重新核验资产后再确认发布")
    assets = rechecked
    print("[publish] 来源：main@%s；目标 tag：%s" % (commit[:12], tag))

    print("[1/2] tag ...")
    ensure_tag(tag, commit)

    # 临近上传最后再比对一次资产指纹：tag 与上传之间也不该有替换窗口
    if asset_digest(verify_release_assets(version)) != digest_before:
        raise SystemExit("[publish] 上传前发现候选资产被替换（哈希变化）：已中止上传")
    print("[2/2] 上传 Release ...")
    publish(tag, [assets["zip"], assets["exe"], assets["manifest"]], clobber)

    print("\n完成：https://github.com/ShenTingBai/tjise-activity-watch/releases/tag/%s" % tag)
    print("[提示] 绿色版/服务器版打包与 local/server 分发分支同步属兼容发行流程，需另行授权执行。")


def main() -> int:
    ap = argparse.ArgumentParser(description="打 Release（版本号读 VERSION，默认只做只读检查）")
    ap.add_argument("tag", nargs="?",
                    help="兼容校验输入：必须与 VERSION 推导一致；不再单独触发发布")
    ap.add_argument("--check", action="store_true", help="只读检查（默认行为，可省略）")
    ap.add_argument("--prepare", action="store_true",
                    help="本地准备 EXE/ZIP 资产（不打 tag、不上传、不要求 main）")
    ap.add_argument("--publish", action="store_true",
                    help="外部发布：核验资产 + 干净 main + 人工确认，才打 tag 并上传 GitHub")
    ap.add_argument("--yes", action="store_true", help="publish 的非交互确认（其余门禁照常）")
    ap.add_argument("--clobber", action="store_true",
                    help="允许覆盖已有 Release 的附件（默认拒绝覆盖，保护已发布产物）")
    args = ap.parse_args()
    stages = [args.check, args.prepare, args.publish]
    if sum(1 for s in stages if s) > 1:
        raise SystemExit("[release] --check / --prepare / --publish 三阶段互斥，一次只选一个")
    version = read_version()
    derived = tag_of(version)
    if args.tag:
        given = args.tag if args.tag.startswith("v") else "v" + args.tag
        if given != derived:
            raise SystemExit("[release] 位置 tag %s 与 VERSION 推导的 %s 不一致："
                             "tag 只由 VERSION 推导" % (given, derived))
        if not any(stages):
            raise SystemExit("[release] 位置 tag 不再单独触发发布；请显式 --publish"
                             "（或 --check / --prepare）")

    if args.publish:
        do_publish(derived, args.yes, version, args.clobber)
        return 0
    if args.prepare:
        stage_local_assets(version)
        return 0
    check_only(version)   # 默认与 --check：零副作用
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
