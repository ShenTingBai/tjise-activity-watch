"""一键触发服务：手机点开链接 → 立刻抓一次并推送到飞书。

只依赖标准库（http.server），Python 3.6+ 可跑。

启动：
    TRIGGER_PORT=18080 TRIGGER_TOKEN=一串随机字符 python3 scripts/trigger_server.py

访问：
    http://服务器IP:18080/run/<TOKEN>     触发一次
    http://服务器IP:18080/health          健康检查

安全说明：
- 路径里的 TOKEN 就是口令，猜不到才能触发；泄漏了改环境变量重启即可。
- 只做 GET，不接受任何参数，不接受上传。
- 60 秒冷却，防止连点刷屏。
"""
import html
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from pathlib import Path


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Python 3.6 没有 http.server.ThreadingHTTPServer，这里自己拼一个。"""
    daemon_threads = True

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("TRIGGER_PORT", "18080"))
TOKEN = os.environ.get("TRIGGER_TOKEN", "")
COOLDOWN = 60

_lock = threading.Lock()
_last_run = [0.0]

PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>学生活动速递</title>
<style>
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
margin:0;padding:20px;background:#f7f8fa;color:#1f2329;line-height:1.6}}
.card{{max-width:640px;margin:0 auto;background:#fff;border-radius:12px;
padding:20px;box-shadow:0 1px 4px rgba(0,0,0,.06)}}
h1{{font-size:17px;margin:0 0 4px}} .sub{{color:#8f959e;font-size:13px;margin-bottom:16px}}
pre{{white-space:pre-wrap;word-break:break-word;font-size:14px;margin:0;
font-family:inherit}}
.ok{{color:#00a870}} .err{{color:#d83931}} .warn{{color:#ff8800}}
</style></head><body><div class="card">
<h1>{title}</h1><div class="sub">{sub}</div><pre>{body}</pre>
</div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "TjiseTrigger/1.0"

    def _send(self, code, title, sub, body, cls=""):
        page = PAGE.format(title=html.escape(title), sub=html.escape(sub),
                           body=html.escape(body))
        data = page.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")

        if path == "/health":
            return self._send(200, "运行正常", "服务在线", "ok")

        if not TOKEN:
            return self._send(500, "未配置口令", "服务端缺少 TRIGGER_TOKEN", "")

        if path != "/run/" + TOKEN:
            return self._send(403, "无权访问", "链接里的口令不正确", "")

        with _lock:
            now = time.time()
            wait = COOLDOWN - (now - _last_run[0])
            if wait > 0:
                return self._send(200, "刚刚才跑过",
                                  "冷却中", "还有 %d 秒，稍等一下再点。" % int(wait))
            _last_run[0] = now

        try:
            proc = subprocess.run(
                [sys.executable, "scripts/run.py", "--force"],
                cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                universal_newlines=True, encoding="utf-8", timeout=180)
            out = (proc.stdout or "").strip()
            ok = proc.returncode == 0
            title = "已抓取并推送" if ok else "执行完成但有异常"
            sub = time.strftime("%Y-%m-%d %H:%M:%S") + "　退出码 %d" % proc.returncode
            return self._send(200 if ok else 500, title, sub, out)
        except subprocess.TimeoutExpired:
            return self._send(504, "超时", "抓取超过 180 秒没返回", "稍后再试，或看服务器日志。")
        except Exception as exc:
            return self._send(500, "出错了", type(exc).__name__, str(exc))

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))


def main():
    if not TOKEN:
        print("✗ 必须设置 TRIGGER_TOKEN 环境变量", file=sys.stderr)
        return 1
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("触发服务已启动： 0.0.0.0:%d" % PORT, file=sys.stderr)
    print("触发地址： http://<服务器IP>:%d/run/%s" % (PORT, TOKEN), file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
