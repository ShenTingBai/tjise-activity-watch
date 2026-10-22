"""为触发链接单独开一条 HTTP 路由（不跟随 https 跳转），幂等 + 回滚。"""
import os
import subprocess
import sys

CONF = "/www/server/panel/vhost/nginx/html_<旧服务器IP>.conf"
BACKUP = CONF + ".bak-tjise-http"
MARK = "# tjise-trigger-http"
TOKEN = os.environ.get("TRIGGER_TOKEN", "")  # 口令不入库：export TRIGGER_TOKEN=... 后再跑

OLD = """server {
    listen 80;
    server_name <旧服务器IP> <你的域名> www.<你的域名>;
    return 301 https://$host$request_uri;
}"""

NEW = """server {
    listen 80;
    server_name <旧服务器IP> <你的域名> www.<你的域名>;

    %s
    location = /%s {
        proxy_pass http://127.0.0.1:18080/run/%s;
        proxy_set_header Host $host;
        proxy_read_timeout 200s;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}""" % (MARK, TOKEN, TOKEN)

src = open(CONF, encoding="utf-8").read()
if MARK in src:
    print("HTTP 触发路由已存在，跳过")
    sys.exit(0)
if OLD not in src:
    print("✗ 未匹配到 80 端口配置块，未修改")
    sys.exit(1)

open(BACKUP, "w", encoding="utf-8").write(src)
open(CONF, "w", encoding="utf-8").write(src.replace(OLD, NEW, 1))
print("已写入，备份 %s" % BACKUP)

test = subprocess.run(["nginx", "-t"], stdout=subprocess.PIPE,
                      stderr=subprocess.STDOUT, universal_newlines=True)
print(test.stdout.strip())
if test.returncode != 0:
    open(CONF, "w", encoding="utf-8").write(src)
    print("✗ 校验失败，已回滚")
    sys.exit(1)

r = subprocess.run(["nginx", "-s", "reload"], stdout=subprocess.PIPE,
                   stderr=subprocess.STDOUT, universal_newlines=True)
print("reload:", r.stdout.strip() or "ok")
