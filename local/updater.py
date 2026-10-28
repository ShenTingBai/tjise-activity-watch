"""完整安装器的流式下载与启动：只写临时目录，不触碰程序目录和数据目录。

下载边界：只走 HTTPS、大小上限、临时 `.part` 半成品、实时 SHA-256，
成功后 `os.replace` 原子保留，失败一律清理半成品。启动只接受临时目录里
已校验的产物，绝不执行任意路径。

@author LiXinwen
"""
from __future__ import annotations

import hashlib
from http.client import HTTPException
import os
import re
import shutil
import socket
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

CHUNK_BYTES = 64 * 1024
# 与 update_check.MAX_INSTALLER_BYTES 同源口径：安装包下载上限（约 300 MB）
DEFAULT_MAX_BYTES = 300 * 1024 * 1024
DOWNLOAD_TIMEOUT_S = 60
TEMP_PREFIX = "tjise-update-"
_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,120}")
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")


def temp_root() -> Path:
    """下载临时根：优先 D:/@Temp，缺失时退到系统临时目录（用户级 TEMP 已指向同一处）。"""
    preferred = Path("D:/@Temp")
    if preferred.is_dir():
        return preferred
    return Path(tempfile.gettempdir())


def _require_https(url: str) -> str:
    """只走 HTTPS：非 https 或没有主机名一律拒绝，不给下载机会。"""
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise ValueError("invalid url")
    parts = urlparse(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("https required")
    return url


def download_installer(url, *, name, expect_bytes, expect_sha256, root=None,
                       max_bytes=DEFAULT_MAX_BYTES, timeout_s=DOWNLOAD_TIMEOUT_S,
                       progress=None, cancel=None) -> dict:
    """流式下载安装器到临时目录并实时校验大小与 SHA-256。

    成功返回 {"ok": True, "path", "bytes", "sha256"}，目标文件已从 `.part`
    原子改名保留；可预期失败（断流、超限、大小/哈希不符、取消、写入失败）
    返回 {"ok": False, "error_code", "text"} 并清理半成品。参数非法和未预期
    异常直接抛出——失败路径不吞异常。绝不写程序目录和数据目录。
    """
    _require_https(url)
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ValueError("invalid name")
    if not isinstance(expect_sha256, str) or not _SHA256_RE.fullmatch(expect_sha256):
        raise ValueError("invalid sha256")
    if not isinstance(expect_bytes, int) or isinstance(expect_bytes, bool) or expect_bytes <= 0:
        raise ValueError("invalid expect_bytes")
    limit = max_bytes if isinstance(max_bytes, int) and not isinstance(max_bytes, bool) \
        and max_bytes > 0 else DEFAULT_MAX_BYTES
    if expect_bytes > limit:
        return {"ok": False, "error_code": "size_limit",
                "text": "安装包超过下载大小上限，已放弃下载。"}

    base = Path(root) if root is not None else temp_root()
    workdir = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=str(base)))
    part = workdir / (name + ".part")
    final = workdir / name
    digest = hashlib.sha256()
    done = 0
    kept = False
    try:
        request = Request(url, headers={
            "Accept": "application/octet-stream",
            "User-Agent": "TianSoftActivityPreRegistration-updater",
        })
        with urlopen(request, timeout=timeout_s) as response:
            while True:
                # 每块之间看一次取消标记：用户取消要能立刻停，不留半成品
                if cancel is not None and cancel():
                    return {"ok": False, "error_code": "cancelled",
                            "text": "已取消下载，临时文件已清理。"}
                chunk = response.read(CHUNK_BYTES)
                if not chunk:
                    break
                done += len(chunk)
                if done > limit:
                    return {"ok": False, "error_code": "size_limit",
                            "text": "下载内容超过大小上限，已放弃并清理。"}
                digest.update(chunk)
                try:
                    with part.open("ab") as handle:
                        handle.write(chunk)
                except OSError:
                    return {"ok": False, "error_code": "io_error",
                            "text": "临时目录写入失败，已放弃并清理。"}
                if progress is not None:
                    progress(done, expect_bytes)
        if done != expect_bytes:
            return {"ok": False, "error_code": "size_mismatch",
                    "text": "下载大小与校验信息不一致（%d / %d 字节），已放弃。" % (done, expect_bytes)}
        got = digest.hexdigest().lower()
        if got != expect_sha256.lower():
            return {"ok": False, "error_code": "hash_mismatch",
                    "text": "下载内容哈希校验未通过，已放弃并清理。"}
        os.replace(part, final)
        kept = True
        return {"ok": True, "path": str(final), "bytes": done, "sha256": got}
    except HTTPError:
        return {"ok": False, "error_code": "http_error",
                "text": "下载地址暂时无法响应，请稍后重试。"}
    except URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            return {"ok": False, "error_code": "timeout", "text": "下载超时，请重试。"}
        return {"ok": False, "error_code": "network_error",
                "text": "下载中断，请检查网络后重试。"}
    except TimeoutError:
        return {"ok": False, "error_code": "timeout", "text": "下载超时，请重试。"}
    except OSError:
        # 连接被重置/半途关闭（含 ConnectionError）都归断流；半成品在 finally 清理
        return {"ok": False, "error_code": "network_error",
                "text": "下载中断，请检查网络后重试。"}
    except HTTPException:
        return {"ok": False, "error_code": "network_error",
                "text": "下载中断，请检查网络后重试。"}
    finally:
        if not kept:
            shutil.rmtree(workdir, ignore_errors=True)


def launch_installer(path, *, root=None, launcher=None) -> dict:
    """启动已校验的安装器；只接受临时目录里的现有文件，失败不改会话和偏好。

    关闭主程序由调用方控制：这里只负责拉起安装向导，不退出、不自动重启。
    """
    if not isinstance(path, str) or not path:
        return {"ok": False, "error_code": "launch_failed", "text": "安装包路径无效。"}
    target = Path(path)
    if not target.is_file():
        return {"ok": False, "error_code": "launch_failed",
                "text": "安装包不存在，请重新下载。"}
    base = Path(root) if root is not None else temp_root()
    try:
        target.resolve().relative_to(base.resolve())
    except (ValueError, OSError):
        return {"ok": False, "error_code": "launch_failed",
                "text": "安装包不在更新临时目录，已拒绝启动。"}
    try:
        (launcher or os.startfile)(str(target))   # noqa: S606 - 用户确认后启动安装向导
    except Exception as exc:  # noqa: BLE001 - 启动失败也要给界面一个可读结论
        return {"ok": False, "error_code": "launch_failed",
                "text": "安装程序没能启动（%s）。" % type(exc).__name__}
    return {"ok": True, "path": str(target)}
