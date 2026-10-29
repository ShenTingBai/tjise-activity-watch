# -*- coding: utf-8 -*-
"""更新用例：GitHub 最新版本检查、安装器下载校验、启动安装。

端口（fetch / download / publish / emit）由 interface 注入；本模块不直接
import app，也不读版本文件、不拼文件路径。状态字段挂在调用方传入的
state 上（Api 实例），便于界面重载后按 request_id 对账。

@author LiXinwen
"""
from __future__ import annotations

from typing import Protocol


class UpdateState(Protocol):
    """更新用例需要的状态面（由 Api 实例满足；测试可给内存替身）。"""

    _lock: object
    _emit: object
    _updating: bool
    _update_result: "dict | None"
    _update_installer: "dict | None"
    _update_installer_path: str
    _update_cancelled: bool
    _closing: bool


def run_check(state, request_id: str, *, fetch, current_version: str,
              latest_url: str) -> None:
    """后台更新检查：结论写入 state 并推 update 事件（单飞由调用方负责）。"""
    result = {"state": "error", "current_version": current_version,
              "latest_version": None, "url": latest_url,
              "text": "检查没能完成，请重试。", "error_code": "unexpected_error",
              "installer": None}
    try:
        checked = fetch(current_version)
        if isinstance(checked, dict):
            result = checked
    except Exception:
        # 最后兜底仍须解锁；普通更新错误不进入组件/登录错误通道。
        pass
    finally:
        result = dict(result, request_id=request_id)
        installer = result.get("installer")
        with state._lock:
            state._update_result = result
            state._update_installer = installer if isinstance(installer, dict) else None
            # 新一轮检查结论取代旧产物：之前校验过的安装器不再对应当前结论
            state._update_installer_path = ""
            state._update_cancelled = False
            state._updating = False
            closing = state._closing
        if not closing:
            state._emit("update", result)


def run_download(state, request_id: str, installer: dict, *,
                 download, publish, current_version: str,
                 latest_url: str) -> None:
    """流式下载安装器并校验（download 端口），进度/结论经 publish 推给界面。"""
    # 下载结论继承最近一次检查的上下文（版本/发布页/安装器），界面不必再拼
    base = dict(state._update_result or {})
    base.update({"request_id": request_id, "current_version": current_version,
                 "url": latest_url, "installer": installer,
                 "error_code": ""})
    total = installer.get("bytes") if isinstance(installer.get("bytes"), int) else 0
    step = 1024 * 1024
    last_sent = [0]

    def progress(done: int, expect: int) -> None:
        # 进度按 1 MiB 步进回推：64 KiB 一块全部回推会把界面打满
        if done == expect or done - last_sent[0] >= step:
            last_sent[0] = done
            publish(dict(base, state="downloading",
                         text="正在下载安装包…",
                         downloaded=done, total_bytes=total), False)

    publish(dict(base, state="downloading", text="正在下载安装包…",
                 downloaded=0, total_bytes=total), False)
    try:
        got = download(
            installer["url"], name=installer["name"],
            expect_bytes=installer.get("bytes") or 0,
            expect_sha256=installer.get("sha256") or "",
            cancel=lambda: state._update_cancelled or state._closing,
            progress=progress)
    except Exception:
        # 只兜未预期异常；可预期失败由下载器分类返回，不在这层吞掉
        got = {"ok": False, "error_code": "unexpected_error",
               "text": "下载没能完成，请重试。"}
    if got.get("ok"):
        path = str(got.get("path") or "")
        with state._lock:
            state._update_installer_path = path
        publish(dict(base, state="ready_to_install",
                     text="安装包已下载并校验通过，可以启动安装程序。",
                     installer_path=path), True)
        return
    code = str(got.get("error_code") or "unexpected_error")
    if code == "cancelled":
        publish(dict(base, state="available", error_code=code,
                     text="已取消下载，可重新下载。"), True)
    else:
        publish(dict(base, state="failed", error_code=code,
                     text=str(got.get("text") or "下载没能完成，请重试。")), True)
