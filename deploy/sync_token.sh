#!/usr/bin/env bash
# 只把最新登录态同步到服务器（token 过期后，在本机 login 完跑这个）
#
# 用法：
#   SSH_KEY=~/.ssh/<你的密钥>.pem bash deploy/sync_token.sh root@<服务器IP>
#
set -euo pipefail

HOST="${1:?用法: SSH_KEY=密钥路径 bash deploy/sync_token.sh user@host}"

LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$LOCAL_DIR"

SSH_ARGS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)
SCP_ARGS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)
if [ -n "${SSH_KEY:-}" ]; then
  KEY="${SSH_KEY/#\~/$HOME}"
  [ -f "$KEY" ] || { echo "✗ 找不到私钥：$KEY"; exit 1; }
  chmod 600 "$KEY" 2>/dev/null || true
  SSH_ARGS+=(-i "$KEY")
  SCP_ARGS+=(-i "$KEY")
fi
if [ -n "${SSH_PORT:-}" ]; then
  SSH_ARGS+=(-p "$SSH_PORT")
  SCP_ARGS+=(-P "$SSH_PORT")
fi

PY="C:/Users/29522/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
[ -f data/session.json ] || { echo "✗ 本地没有 data/session.json，先跑： python scripts/login.py"; exit 1; }

echo "[1/3] 本机先验证 token 是否可用"
if ! "$PY" scripts/fetch.py > /tmp/tjise-local.log 2>&1; then
  echo "✗ 本机 token 已失效，先重新登录： python scripts/login.py"
  tail -3 /tmp/tjise-local.log
  exit 1
fi
echo "✓ token 可用"

echo "[2/3] 上传到 $HOST"
scp "${SCP_ARGS[@]}" data/session.json "$HOST:/tmp/tjise-session.json"

echo "[3/3] 覆盖服务器会话并复验"
ssh "${SSH_ARGS[@]}" "$HOST" 'bash -s' <<'REMOTE'
set -euo pipefail
APP_DIR="$HOME/tjise-activity-watch"
[ -d "$APP_DIR" ] || { echo "✗ 服务器上没有 $APP_DIR，先跑 deploy/deploy.sh"; exit 1; }
cp /tmp/tjise-session.json "$APP_DIR/data/session.json"
chmod 600 "$APP_DIR/data/session.json"
rm -f /tmp/tjise-session.json
cd "$APP_DIR"
if python3 scripts/fetch.py >/dev/null 2>&1; then
  echo "✓ 服务器端 token 已生效"
else
  echo "✗ 服务器端自检失败，手动看看： cd $APP_DIR && python3 scripts/fetch.py"
  exit 1
fi
REMOTE

echo "完成。"
