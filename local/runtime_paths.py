#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""登录态与偏好文件的统一数据根：开发/绿色版与冻结 EXE 共用同一套口径。

- 开发与绿色版：沿用脚本旁边的 data/（local/data、便携目录 data），语义不变；
- 冻结主程序与 grab-worker.exe：默认 %LOCALAPPDATA%/TianSoftActivityPreRegistration/data，
  同一台机器上关闭重开、替换 EXE 或重建 EXE 后仍复用同一份登录态；
- 环境变量 TJISE_DATA_DIR 可覆盖（相对路径先 resolve），便于测试与高级用户迁移；
- 冻结进程首次使用新数据根时，把旧 EXE 旁 data 里的会话/偏好文件原子搬过来：
  只补缺失、绝不覆盖目标已有文件，也不读取或改写 token 内容。

@author LiXinwen
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

# 统一数据根的应用目录名（%LOCALAPPDATA% 下）
APP_DIR_NAME = "TianSoftActivityPreRegistration"
# 迁移白名单：登录态与偏好；grab.log 只在缺失时一并带走，方便排查
MIGRATE_FILES = ("session.json", "storage_state.json", "ui.json", "grab.log")


def _base_dir() -> Path:
    """解析本进程的数据根（只算路径，不创建目录）。"""
    override = os.environ.get("TJISE_DATA_DIR", "").strip()
    if override:
        # 相对路径锚定当前工作目录，避免冻结进程拿到一个随启动目录漂移的根
        return Path(override).resolve()
    if getattr(sys, "frozen", False):
        local = os.environ.get("LOCALAPPDATA", "").strip()
        # LOCALAPPDATA 缺失（精简系统/服务账户）时回退 home/AppData/Local
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / APP_DIR_NAME / "data"
    return Path(__file__).resolve().parent / "data"


DATA_DIR = _base_dir()
SESSION_FILE = DATA_DIR / "session.json"
STATE_FILE = DATA_DIR / "storage_state.json"
UI_STATE_FILE = DATA_DIR / "ui.json"
LOG_FILE = DATA_DIR / "grab.log"


def ensure_data_dir() -> Path:
    """确保数据根存在；冻结进程首次使用新根时顺带完成旧数据迁移。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if getattr(sys, "frozen", False):
        _migrate_legacy_data()
    return DATA_DIR


def _legacy_data_dir() -> Path:
    """统一数据根之前的老位置：EXE 旁边的 data/。"""
    return Path(sys.executable).resolve().parent / "data"


def _migrate_legacy_data() -> None:
    """旧 EXE 旁 data → 统一数据根：只补缺失文件，临时文件 + os.replace 原子落位。

    任何失败都不阻断启动（打印一句可读提示）——迁移丢了还能手动拷贝，
    启动失败才是真问题。绝不覆盖目标已有文件，避免旧数据倒灌新数据。
    """
    legacy = _legacy_data_dir()
    try:
        if legacy.resolve() == DATA_DIR.resolve():
            return
        moved = 0
        for name in MIGRATE_FILES:
            source = legacy / name
            target = DATA_DIR / name
            if not source.is_file() or target.exists():
                continue
            # 先拷成数据根同盘的临时文件再原子替换：中途断电不会留下半个文件
            handle, temp_name = tempfile.mkstemp(dir=str(DATA_DIR),
                                                 prefix=name + ".", suffix=".tmp")
            os.close(handle)
            try:
                shutil.copy2(source, temp_name)
                os.replace(temp_name, target)
                moved += 1
            finally:
                Path(temp_name).unlink(missing_ok=True)
        if moved:
            print("[i] 已把 %d 个旧数据文件搬到统一数据目录：%s" % (moved, DATA_DIR),
                  file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - 迁移是尽力而为，不让它挡住启动
        print("[!] 旧登录数据迁移失败（不影响启动，可手动拷贝）：%s" % exc,
              file=sys.stderr)
