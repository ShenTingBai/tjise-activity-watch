"""手动查询固定仓库的正式版本；不接触凭证、数据根或安装流程。

@author LiXinwen
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import socket
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

LATEST_API = "https://api.github.com/repos/ShenTingBai/tjise-activity-watch/releases/latest"
LATEST_URL = "https://github.com/ShenTingBai/tjise-activity-watch/releases/latest"
TIMEOUT_S = 8
MAX_BYTES = 256 * 1024


def parse_version(value: str) -> tuple[int, int, int]:
    """只比较正式三段版本，避免1.10被字典序排到1.9之前。"""
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("invalid version")
    match = re.fullmatch(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value)
    if not match:
        raise ValueError("invalid version")
    return tuple(int(part) for part in match.groups())


def _result(current: str, state: str, text: str, latest=None, error_code="") -> dict:
    return {"state": state, "current_version": current, "latest_version": latest,
            "url": LATEST_URL, "checked_at": datetime.now(timezone.utc).isoformat(),
            "text": text, "error_code": error_code}


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


def check_latest_release(current_version: str) -> dict:
    """用户每次主动检查最多发一个GET，失败给分类而不泄露响应原文。"""
    try:
        parse_version(current_version)
        request = Request(LATEST_API, headers={"Accept": "application/vnd.github+json",
                          "User-Agent": "TianSoftActivityPreRegistration/" + current_version})
        with urlopen(request, timeout=TIMEOUT_S) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("response too large")
        return evaluate_latest_release(current_version, json.loads(raw.decode("utf-8")))
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
