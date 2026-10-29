# -*- coding: utf-8 -*-
"""手机受限 HTTP 适配器（标准库 http.server）。

只做传输层：路由、Host/Origin/CSRF/会话校验、请求体上限、安全响应头。
业务判定全在 application/phone_service 与 domain/phone_rules，这里不碰 Api。
绑定只允许回环或 Tailscale 专属 IPv4（由调用方先校验）；异常一律回可读
错误码，绝不回堆栈、不记 body/cookie/配对码。

@author LiXinwen
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from domain.phone_rules import (
    CSRF_HEADER, MAX_BODY_BYTES, host_matches, origin_matches,
    parse_json_body,
)

# 固定静态资源表：只认这几条路径，杜绝目录遍历/任意文件读取
PHONE_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
SECURITY_HEADERS = (
    # 页面只能用本地资源；验证码图用 data: 内联，不放开外域
    ("Content-Security-Policy",
     "default-src 'self'; script-src 'self'; style-src 'self'; "
     "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
)


class PhoneHttpServer:
    """ThreadingHTTPServer 的薄包装：start/stop/address，线程随进程收尾。"""

    def __init__(self, host: str, port: int, service, ui_dir: "Path | None" = None):
        self._host = str(host)
        self._service = service
        self._ui_dir = Path(ui_dir) if ui_dir else Path(__file__).resolve().parents[2] / "ui" / "m"
        self._httpd = None
        self._thread = None
        self._want_port = int(port)

    def start(self) -> None:
        handler = _make_handler(self._service, self._host, self._ui_dir)
        httpd = ThreadingHTTPServer((self._host, self._want_port), handler)
        httpd.daemon_threads = True
        httpd.timeout = 5.0
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever,
                                        name="phone-http", daemon=True)
        self._thread.start()

    def address(self) -> str:
        if self._httpd is None:
            return ""
        return "http://%s:%d" % (self._host, self._httpd.server_address[1])

    def stop(self) -> None:
        httpd = self._httpd
        self._httpd = None
        if httpd is None:
            return
        try:
            httpd.shutdown()
        except Exception:  # noqa: BLE001 - 关闭尽力而为
            pass
        try:
            httpd.server_close()
        except Exception:  # noqa: BLE001
            pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


def _make_handler(service, bind_host: str, ui_dir: Path):
    """按服务实例生成处理器类：Host/Origin 用服务的字面 origin，不收外部配置。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "tjise-phone"
        sys_version = ""
        # 慢速客户端也不能把线程挂死：连接级 socket 超时
        timeout = 10

        # BaseHTTPRequestHandler 默认把请求行打进 stderr；这里不落任何访问日志
        def log_message(self, fmt, *args):
            return

        # 非法方法一律拒绝：不给默认 501 页面，也不回服务器版本
        def do_PUT(self):  # noqa: N802
            self._deny("method_not_allowed", 405)

        def do_DELETE(self):  # noqa: N802
            self._deny("method_not_allowed", 405)

        def do_PATCH(self):  # noqa: N802
            self._deny("method_not_allowed", 405)

        def do_OPTIONS(self):  # noqa: N802
            self._deny("method_not_allowed", 405)

        def do_HEAD(self):  # noqa: N802
            self._deny("method_not_allowed", 405)

        # ---- 通用出口 ----
        def _send(self, status: int, body: bytes, content_type: str,
                  extra: "list | None" = None) -> None:
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                for key, value in SECURITY_HEADERS:
                    self.send_header(key, value)
                for key, value in (extra or []):
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)
            except Exception:  # noqa: BLE001 - 客户端断开等不外抛
                pass

        def _json(self, status: int, payload: dict, extra: "list | None" = None) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8", extra)

        def _deny(self, code: str, status: int = 403) -> None:
            # 只回错误码与一句话，不回堆栈、不回路径、不回请求内容
            self._json(status, {"ok": False, "error": code})

        def _expected_port(self) -> int:
            # 测试可用端口 0 换临时端口：校验必须认实际监听端口，不是写死 8765
            try:
                return int(self.server.server_address[1])
            except Exception:
                return 8765

        def _check_host(self) -> bool:
            return host_matches(self.headers.get("Host"), bind_host,
                                self._expected_port())

        def _check_origin(self) -> bool:
            return origin_matches(self.headers.get("Origin"), bind_host,
                                  self._expected_port())

        def _read_body(self):
            raw_len = self.headers.get("Content-Length")
            if raw_len is None:
                return None, "empty_body"
            try:
                length = int(raw_len)
            except (TypeError, ValueError):
                return None, "bad_length"
            if length < 0 or length > MAX_BODY_BYTES:
                return None, "body_too_large"
            if length == 0:
                return None, "empty_body"
            return self.rfile.read(length), ""

        def _require_json_type(self) -> bool:
            ctype = str(self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            return ctype == "application/json"

        # ---- 路由 ----
        def do_GET(self):  # noqa: N802 - 基类命名
            try:
                self._route_get()
            except Exception:  # noqa: BLE001 - 任何异常都不回堆栈
                self._deny("internal", 500)

        def do_POST(self):  # noqa: N802
            try:
                self._route_post()
            except Exception:  # noqa: BLE001
                self._deny("internal", 500)

        def _route_get(self):
            if not self._check_host():
                return self._deny("bad_host")
            path = self.path.split("?", 1)[0]
            if path in ("/api/state",):
                return self._get_state()
            if path in PHONE_STATIC:
                return self._get_static(path)
            return self._deny("not_found", 404)

        def _route_post(self):
            if not self._check_host():
                return self._deny("bad_host")
            if not self._check_origin():
                return self._deny("bad_origin")
            if not self._require_json_type():
                return self._deny("bad_content_type")
            path = self.path.split("?", 1)[0]
            if path == "/pair":
                return self._post_pair()
            if path == "/api/action":
                return self._post_action()
            return self._deny("not_found", 404)

        # ---- 静态页 ----
        def _get_static(self, path):
            name, ctype = PHONE_STATIC[path]
            target = (ui_dir / name).resolve()
            # 固定映射之外的文件永远取不到：目录穿越无从谈起
            if not target.is_file():
                return self._deny("not_found", 404)
            try:
                body = target.read_bytes()
            except Exception:  # noqa: BLE001
                return self._deny("not_found", 404)
            self._send(200, body, ctype)

        # ---- 配对 ----
        def _post_pair(self):
            raw, error = self._read_body()
            if error:
                return self._deny(error, 400)
            data, error = parse_json_body(raw)
            if error:
                return self._deny(error, 400)
            result = service.pair(data.get("code"), ip=self.client_address[0])
            if not result.get("ok"):
                return self._json(403, result)
            self._json(200, {"ok": True, "csrf_header": result.get("csrf_header", CSRF_HEADER),
                             "csrf": result.get("csrf", ""),
                             "expires_in": result.get("expires_in", 0)},
                       extra=[("Set-Cookie", result.get("cookie", "")),
                              ("Set-Cookie", result.get("csrf_cookie", ""))])

        # ---- 状态（只读本机缓存，绝不触发学校流量）----
        def _get_state(self):
            if not self._check_host():
                return self._deny("bad_host")
            session = service.authenticate(self.headers.get("Cookie"))
            if session is None:
                return self._deny("need_pair", 401)
            cursor = None
            if "?" in self.path:
                for piece in self.path.split("?", 1)[1].split("&"):
                    key, _, value = piece.partition("=")
                    if key == "after":
                        cursor = value
            self._json(200, service.safe_state(cursor))

        # ---- 动作 ----
        def _post_action(self):
            session = service.authenticate(self.headers.get("Cookie"))
            if session is None:
                return self._deny("need_pair", 401)
            header = str(self.headers.get(CSRF_HEADER) or "")
            if not header or header != session.get("csrf"):
                return self._deny("bad_csrf")
            raw, error = self._read_body()
            if error:
                return self._deny(error, 400)
            data, error = parse_json_body(raw)
            if error:
                return self._deny(error, 400)
            self._json(200, service.dispatch(data.get("action"), data.get("args")))

    return Handler
