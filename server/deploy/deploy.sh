#!/usr/bin/env bash
# 一键部署到 Linux 服务器
#
# 用法：
#   SSH_KEY=~/.ssh/goal.pem bash deploy/deploy.sh root@1.2.3.4
#   SSH_KEY=~/.ssh/goal.pem SSH_PORT=2222 bash deploy/deploy.sh root@1.2.3.4
#
set -euo pipefail

HOST="${1:?用法: SSH_KEY=密钥路径 bash deploy/deploy.sh user@host}"
APP_DIR='~/tjise-activity-watch'

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

if [ ! -f data/session.json ]; then
  echo "✗ 本地还没有登录态，先在本机跑： python scripts/login.py"
  exit 1
fi

echo "[1/5] 测试 SSH 连接"
if ! ssh "${SSH_ARGS[@]}" "$HOST" 'echo ok' >/dev/null 2>&1; then
  echo "✗ 连不上 $HOST，检查 IP / 端口 / 私钥 / 安全组是否放行"
  exit 1
fi
echo "✓ 连接正常"

echo "[2/5] 打包（排除浏览器配置和原始响应）"
TMP="$(mktemp -d)"
tar --exclude='data/chrome-profile' --exclude='data/raw' --exclude='__pycache__' \
    --exclude='reports/*' \
    -czf "$TMP/bundle.tar.gz" \
    scripts config.json deploy/remote_install.sh

echo "[3/5] 上传"
scp "${SCP_ARGS[@]}" "$TMP/bundle.tar.gz" "$HOST:/tmp/tjise-bundle.tar.gz"
scp "${SCP_ARGS[@]}" data/session.json  "$HOST:/tmp/tjise-session.json"

echo "[4/5] 解包并安装"
ssh "${SSH_ARGS[@]}" "$HOST" "APP_DIR=$APP_DIR bash -s" <<'REMOTE'
set -euo pipefail
APP_DIR="${APP_DIR/#\~/$HOME}"
mkdir -p "$APP_DIR"
tar -xzf /tmp/tjise-bundle.tar.gz -C "$APP_DIR"
mkdir -p "$APP_DIR/data" "$APP_DIR/reports"
cp /tmp/tjise-session.json "$APP_DIR/data/session.json"
chmod 600 "$APP_DIR/data/session.json"
rm -f /tmp/tjise-bundle.tar.gz /tmp/tjise-session.json
bash "$APP_DIR/deploy/remote_install.sh" "$APP_DIR"
REMOTE

rm -rf "$TMP"
echo "[5/5] 完成"
echo "以后 token 过期： python scripts/login.py 然后 SSH_KEY=密钥 bash deploy/sync_token.sh $HOST"
