"""在 nginx vhost 里插入一条触发路由（幂等，带回滚）。"""
import os
import subprocess
import sys

CONF = "/www/server/panel/vhost/nginx/html_<旧服务器IP>.conf"
BACKUP = CONF + ".bak-tjise"
MARK = "# tjise-trigger"
TOKEN = os.environ.get("TRIGGER_TOKEN", "")  # 口令不入库：export TRIGGER_TOKEN=... 后再跑

BLOCK = """    %s
    location = /%s {
        proxy_pass http://127.0.0.1:18080/run/%s;
        proxy_set_header Host $host;
        proxy_read_timeout 200s;
    }

""" % (MARK, TOKEN, TOKEN)

src = open(CONF, encoding="utf-8").read()
if MARK in src:
    print("已存在触发路由，跳过")
    sys.exit(0)

anchor = "    location / {"
if anchor not in src:
    print("✗ 找不到 location / 锚点，未修改")
    sys.exit(1)

with open(BACKUP, "w", encoding="utf-8") as f:
    f.write(src)

new = src.replace(anchor, BLOCK + anchor, 1)
open(CONF, "w", encoding="utf-8").write(new)
print("已写入配置，备份在 %s" % BACKUP)

test = subprocess.run(["nginx", "-t"], stdout=subprocess.PIPE,
                      stderr=subprocess.STDOUT, universal_newlines=True)
print(test.stdout.strip())
if test.returncode != 0:
    open(CONF, "w", encoding="utf-8").write(src)
    print("✗ 配置校验失败，已回滚")
    sys.exit(1)

reload_ = subprocess.run(["nginx", "-s", "reload"], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, universal_newlines=True)
print("reload:", reload_.stdout.strip() or "ok")
