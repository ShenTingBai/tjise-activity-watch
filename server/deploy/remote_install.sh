#!/usr/bin/env bash
# 服务器端安装：检查环境 → 连通性自检 → 写 crontab
# 由 deploy.sh 自动调用，也可手动： bash remote_install.sh /path/to/app
set -euo pipefail

APP="${1:-$HOME/tjise-activity-watch}"
cd "$APP"

echo "── 环境检查 ──"
PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  echo "✗ 没找到 python3，请先安装： sudo apt install -y python3   或   sudo yum install -y python3"
  exit 1
fi
"$PY" - <<'PYEOF'
import sys
assert sys.version_info >= (3, 6), f"需要 Python 3.6+，当前 {sys.version.split()[0]}"
print(f"✓ Python {sys.version.split()[0]}")
PYEOF

echo
echo "── 时区检查 ──"
TZNAME="$(date +%Z)"
echo "当前时区：$(date '+%Y-%m-%d %H:%M %Z')"
case "$TZNAME" in
  CST|UTC+8) echo "✓ 是中国时区，cron 时间可以直接用" ;;
  *) echo "! 不是中国时区，cron 会按服务器时区触发。建议先执行："
     echo "    sudo timedatectl set-timezone Asia/Shanghai" ;;
esac

echo
echo "── 连通性自检（真实请求一次接口） ──"
if "$PY" scripts/fetch.py > /tmp/tjise-check.log 2>&1; then
  echo "✓ 接口可访问，token 有效"
else
  RC=$?
  echo "✗ 自检失败（退出码 $RC）："
  tail -5 /tmp/tjise-check.log
  echo
  echo "  退出码 2 通常是 token 过期，在本机重新 login 后跑 deploy/sync_token.sh"
  exit 1
fi

echo
echo "── 写入 crontab ──"
# 动态监测每分钟唤醒一次，但**不是**每分钟都访问学校：activity_watch.py 自己按
# next_poll_at 控频（白天 3 分钟 / 夜间 10 分钟，不做临近开放加密）。
# 用 cron 每分钟唤醒而不是写死 3 分钟，是为了让「刚开放那一分钟」能被赶上。
# watchlist_runner 的每分钟执行保持不变：它只对齐 owner 自己的清单。
CRON_LINES="0 7 * * * cd $APP && $PY scripts/run.py >> $APP/data/cron.log 2>&1
5 7 * * * cd $APP && $PY scripts/remind.py >> $APP/data/remind.log 2>&1
30 10 * * * cd $APP && $PY scripts/run.py >> $APP/data/cron.log 2>&1
30 16 * * * cd $APP && $PY scripts/run.py >> $APP/data/cron.log 2>&1
30 21 * * * cd $APP && $PY scripts/run.py --late-checkin --with-open >> $APP/data/cron.log 2>&1
*/5 * * * * cd $APP && $PY scripts/scan.py >> $APP/data/scan.log 2>&1
* * * * * cd $APP && $PY scripts/activity_watch.py >> $APP/data/activity_watch.log 2>&1"

( crontab -l 2>/dev/null | grep -v 'tjise-activity-watch' || true; echo "$CRON_LINES" ) | crontab -
echo "✓ 已安装定时任务：清单 07:00 / 10:30 / 16:30 / 21:30，私人提醒 07:05，扫描每 5 分钟（全天），动态监测每分钟唤醒（自行控频）"
echo
crontab -l | grep 'tjise-activity-watch'
echo
echo "日志： $APP/data/cron.log"
echo "手动试跑： cd $APP && $PY scripts/run.py"

echo
echo "── 机器人角色实例 ──"
# 三个机器人，三条完全不同的权限边界：
#   1) 自动推送：群自定义机器人 Webhook，只能发消息，收不到任何事件（配置里填即可）
#   2) 群查询：  自建应用 + BOT_ROLE=group_query，只读，必须被 @
#   3) owner 私聊：自建应用 + BOT_ROLE=private_owner，只有本人能触发预报名/报名动作
# 两个应用可以共用同一份凭证，但**不建议**：同一个应用双开会互相抢事件，
# 而且一旦有人把群机器人拉进新群，群入口就会继承应用的全部权限。
GROUP_APP_ID="${FEISHU_GROUP_APP_ID:-}"
GROUP_APP_SECRET="${FEISHU_GROUP_APP_SECRET:-}"
OWNER_APP_ID="${FEISHU_OWNER_APP_ID:-}"
OWNER_APP_SECRET="${FEISHU_OWNER_APP_SECRET:-}"
OWNER_OPEN_ID="${PRIVATE_OWNER_OPEN_ID:-}"

write_bot_unit() {
  # $1=unit 名 $2=BOT_ROLE $3=app_id $4=app_secret
  cat > "/etc/systemd/system/$1.service" <<UNIT
[Unit]
Description=tjise activity bot ($2)
After=network.target

[Service]
Type=simple
WorkingDirectory=/root/tjise-bot
Environment=BOT_ROLE=$2
Environment=FEISHU_APP_ID=$3
Environment=FEISHU_APP_SECRET=$4
Environment=PRIVATE_OWNER_OPEN_ID=$OWNER_OPEN_ID
Environment=APP_DIR=$APP
ExecStart=/usr/bin/node bot.js
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
  echo "  已写入 /etc/systemd/system/$1.service（BOT_ROLE=$2）"
}

if [ -z "$GROUP_APP_ID" ] || [ -z "$GROUP_APP_SECRET" ] || [ -z "$OWNER_APP_ID" ] || [ -z "$OWNER_APP_SECRET" ]; then
  echo "! 未提供完整的机器人凭据，跳过 systemd 单元生成。"
  echo "  需要这四个环境变量（缺一个就生成不了两个角色实例）："
  echo "    FEISHU_GROUP_APP_ID / FEISHU_GROUP_APP_SECRET   群查询应用（只读）"
  echo "    FEISHU_OWNER_APP_ID / FEISHU_OWNER_APP_SECRET   owner 私聊应用（有报名动作）"
  echo "    PRIVATE_OWNER_OPEN_ID                          owner 的 open_id（必填）"
  echo "  然后重新执行本脚本，或按 docs/OPERATIONS.md 手工写单元。"
elif [ -z "$OWNER_OPEN_ID" ]; then
  echo "! 缺少 PRIVATE_OWNER_OPEN_ID，owner 私聊实例起来也会立刻退出，已跳过单元生成。"
else
  write_bot_unit "tjise-bot-group" "group_query" "$GROUP_APP_ID" "$GROUP_APP_SECRET"
  write_bot_unit "tjise-bot-owner" "private_owner" "$OWNER_APP_ID" "$OWNER_APP_SECRET"
  systemctl daemon-reload || true
  echo "  启用： systemctl enable --now tjise-bot-group tjise-bot-owner"
  echo "  注意：旧的单实例 tjise-bot.service 没有 BOT_ROLE，新版会拒绝启动；"
  echo "        请 systemctl disable --now tjise-bot 后再启用上面两个。"
fi
