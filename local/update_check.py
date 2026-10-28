"""手动查询固定仓库的正式版本并挑选可校验的安装器资产；不接触凭证、数据根或安装流程。

@author LiXinwen
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

LATEST_API = "https://api.github.com/repos/ShenTingBai/tjise-activity-watch/releases/latest"
LATEST_URL = "https://github.com/ShenTingBai/tjise-activity-watch/releases/latest"
REPO_DOWNLOAD_PREFIX = "/ShenTingBai/tjise-activity-watch/releases/download/"
TIMEOUT_S = 8
MAX_BYTES = 256 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
# 与 updater.DEFAULT_MAX_BYTES 同源口径：安装包下载上限（约 300 MB）
MAX_INSTALLER_BYTES = 300 * 1024 * 1024
MANIFEST_ASSET_NAME = "manifest.json"


def parse_version(value: str) -> tuple[int, int, int]:
    """只比较正式三段版本，避免1.10被字典序排到1.9之前。"""
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("invalid version")
    match = re.fullmatch(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value)
    if not match:
        raise ValueError("invalid version")
    return tuple(int(part) for part in match.groups())


def installer_asset_name(version: str) -> str:
    """安装器下载资产名：只用 ASCII（GitHub 会把中文前缀规范化成别的形式）。"""
    parse_version(version)
    return "tjise-activity-watch-installer-v%s-win-x64.exe" % version


def is_release_asset_url(url, name) -> bool:
    """只接受本仓库 GitHub Release 资产的 HTTPS 下载地址，且路径尾段必须等于资产名。"""
    if not isinstance(url, str) or not isinstance(name, str) or not name:
        return False
    if not url or len(url) > 2048:
        return False
    try:
        parts = urlparse(url)
    except ValueError:
        return False
    if parts.scheme != "https" or (parts.hostname or "").lower() != "github.com":
        return False
    path = parts.path
    return (path.startswith(REPO_DOWNLOAD_PREFIX)
            and path.endswith("/" + name)
            and len(path) > len(REPO_DOWNLOAD_PREFIX) + len(name))


def select_release_assets(payload: dict, version: str) -> dict:
    """从 Release 资产里挑安装器与 manifest；命名或地址不符的条目一律不选中。"""
    assets = payload.get("assets") if isinstance(payload, dict) else None
    if not isinstance(assets, list):
        return {"installer": None, "manifest_url": ""}
    wanted = installer_asset_name(version)
    installer = None
    manifest_url = ""
    for item in assets:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        url = item.get("browser_download_url")
        if name == wanted and is_release_asset_url(url, name):
            size = item.get("size")
            if (isinstance(size, int) and not isinstance(size, bool)
                    and 0 < size <= MAX_INSTALLER_BYTES):
                installer = {"name": name, "url": url, "bytes": size}
        elif name == MANIFEST_ASSET_NAME and is_release_asset_url(url, name):
            manifest_url = url
    return {"installer": installer, "manifest_url": manifest_url}


def parse_manifest(raw, version: str, installer_name: str) -> dict:
    """校验 manifest 结构并取出安装器条目；任何不符都拒绝，不给可下载结论。"""
    if not isinstance(raw, (bytes, bytearray)):
        raise ValueError("manifest not bytes")
    data = json.loads(bytes(raw).decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("manifest not an object")
    if data.get("version") != version:
        raise ValueError("manifest version mismatch")
    files = data.get("files")
    if not isinstance(files, dict):
        raise ValueError("manifest files missing")
    entry = files.get(installer_name)
    if not isinstance(entry, dict):
        raise ValueError("installer entry missing")
    sha = entry.get("sha256")
    size = entry.get("bytes")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
        raise ValueError("bad sha256")
    if (not isinstance(size, int) or isinstance(size, bool)
            or size <= 0 or size > MAX_INSTALLER_BYTES):
        raise ValueError("bad bytes")
    return {"sha256": sha.lower(), "bytes": size}


def _result(current: str, state: str, text: str, latest=None, error_code="") -> dict:
    return {"state": state, "current_version": current, "latest_version": latest,
            "url": LATEST_URL, "checked_at": datetime.now(timezone.utc).isoformat(),
            "text": text, "error_code": error_code, "installer": None}


def _fetch_bytes(url: str, limit: int, user_agent: str) -> bytes:
    """按上限拉一份响应体；超限按无效响应处理，不把大响应整个读进内存。"""
    request = Request(url, headers={"Accept": "application/vnd.github+json",
                      "User-Agent": user_agent})
    with urlopen(request, timeout=TIMEOUT_S) as response:
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("response too large")
    return raw


def evaluate_latest_release(current_version: str, payload: dict) -> dict:
    """远端落后于本机候选时只说明预览状态，不建议用户降级。"""
    current = parse_version(current_version)
    if not isinstance(payload, dict) or payload.get("draft") is not False or payload.get("prerelease") is not False:
        raise ValueError("not a published stable release")
    latest = parse_version(payload.get("tag_name"))
    version = ".".join(map(str, latest))
    if latest > current:
        return _result(current_version, "available", "发现新版本 " + version, version)
    if latest == current:
        return _result(current_version, "current", "当前为最新正式版", version)
    return _result(current_version, "preview", "当前为预览版本，最新正式版 " + version, version)


def _attach_installer(result: dict, payload: dict) -> dict:
    """可用版本补上安装器与 manifest 校验信息；缺安装器或清单不可用时退回发布页状态。

    网络类失败重新抛出，交回 check_latest_release 统一分类成 error——
    「拿不到清单」和「清单内容不对」不是一回事，前者可重试。
    """
    version = result["latest_version"]
    picked = select_release_assets(payload, version)
    installer = picked["installer"]
    if installer is None or not picked["manifest_url"]:
        return dict(result, state="no_installer", error_code="no_installer",
                    text="发现新版本 " + version + "，但没有可校验的安装包，请前往下载页手动下载。")
    try:
        raw = _fetch_bytes(picked["manifest_url"], MAX_MANIFEST_BYTES, "TianSoftActivityPreRegistration-manifest")
        entry = parse_manifest(raw, version, installer["name"])
    except (ValueError, TypeError):
        return dict(result, state="no_installer", error_code="manifest_invalid",
                    text="发现新版本 " + version + "，但安装包校验信息不可用，请前往下载页手动下载。")
    if entry["bytes"] != installer["bytes"]:
        return dict(result, state="no_installer", error_code="manifest_invalid",
                    text="发现新版本 " + version + "，但安装包大小与校验信息不一致，请前往下载页手动下载。")
    return dict(result, installer=dict(installer, sha256=entry["sha256"], bytes=entry["bytes"]))


def check_latest_release(current_version: str) -> dict:
    """用户每次主动检查最多两个 GET（Release 元数据 + manifest），失败给分类而不泄露响应原文。"""
    try:
        parse_version(current_version)
        user_agent = "TianSoftActivityPreRegistration/" + current_version
        payload = json.loads(_fetch_bytes(LATEST_API, MAX_BYTES, user_agent).decode("utf-8"))
        result = evaluate_latest_release(current_version, payload)
        if result["state"] != "available":
            return result
        return _attach_installer(result, payload)
    except HTTPError as exc:
        if exc.code == 404:
            return _result(current_version, "unavailable", "暂无正式发布")
        if exc.code in (403, 429):
            return _result(current_version, "error", "GitHub暂时限制查询，请稍后重试或查看发布页。", error_code="rate_limited")
        return _result(current_version, "error", "GitHub暂时无法响应，请稍后重试。", error_code="http_error")
    except (TimeoutError, socket.timeout):
        return _result(current_version, "error", "连接超时，请重试或查看发布页。", error_code="timeout")
    except URLError as exc:
        code = "timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else "network_error"
        return _result(current_version, "error", "暂时无法连接GitHub，请检查网络后重试。", error_code=code)
    except (ValueError, UnicodeError, TypeError):
        return _result(current_version, "error", "版本信息无法识别，请查看发布页。", error_code="invalid_response")
    except OSError:
        return _result(current_version, "error", "暂时无法连接GitHub，请检查网络后重试。", error_code="network_error")
