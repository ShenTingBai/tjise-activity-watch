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
# owner 的 open_id：cron 里的 token_probe / net_probe / watchlist_runner 和
# 07:00 的 --with-grab 都要靠它做私聊回执；没配置时降级为不带 owner 环境。
OWNER_OPEN_ID="${PRIVATE_OWNER_OPEN_ID:-}"
OWNER_ENV=""
if [ -n "$OWNER_OPEN_ID" ]; then
  OWNER_ENV="PRIVATE_OWNER_OPEN_ID=$OWNER_OPEN_ID "
fi
echo "── 写入 crontab ──"
# 动态监测每分钟唤醒一次，但**不是**每分钟都访问学校：activity_watch.py 自己按
# next_poll_at 控频（白天 3 分钟 / 夜间 10 分钟，不做临近开放加密）。
# 用 cron 每分钟唤醒而不是写死 3 分钟，是为了让「刚开放那一分钟」能被赶上。
# watchlist_runner 的每分钟执行保持不变：它只对齐 owner 自己的清单。
# 注意：这里必须写全服务器上真实的全部任务——写入方式是「先删掉所有含
# tjise-activity-watch 的旧行再追加」，漏写一条就等于把那条任务停掉。
# 按注释行不能带这个路径，否则每次安装都会重复堆积。
CRON_LINES="0 7 * * * cd $APP && ${OWNER_ENV}$PY scripts/run.py --with-grab >> $APP/data/cron.log 2>&1
5 7 * * * cd $APP && ${OWNER_ENV}$PY scripts/remind.py >> $APP/data/remind.log 2>&1
30 10 * * * cd $APP && $PY scripts/run.py >> $APP/data/cron.log 2>&1
30 16 * * * cd $APP && $PY scripts/run.py >> $APP/data/cron.log 2>&1
30 21 * * * cd $APP && $PY scripts/run.py --late-checkin --with-open >> $APP/data/cron.log 2>&1
*/5 * * * * cd $APP && $PY scripts/scan.py >> $APP/data/scan.log 2>&1
*/15 * * * * cd $APP && ${OWNER_ENV}$PY scripts/token_probe.py >> $APP/data/token_probe.log 2>&1
*/30 * * * * cd $APP && $PY scripts/token_report.py >> $APP/data/token_report.log 2>&1
*/5 * * * * cd $APP && ${OWNER_ENV}$PY scripts/net_probe.py >> $APP/data/net_probe.log 2>&1
*/1 * * * * cd $APP && ${OWNER_ENV}$PY scripts/watchlist_runner.py >> $APP/data/watchlist.log 2>&1
* * * * * cd $APP && $PY scripts/activity_watch.py >> $APP/data/activity_watch.log 2>&1"

( crontab -l 2>/dev/null | grep -v 'tjise-activity-watch' || true; echo "$CRON_LINES" ) | crontab -
echo "✓ 已安装定时任务：清单 07:00（--with-grab）/ 10:30 / 16:30 / 21:30，私人提醒 07:05，扫描每 5 分钟，令牌探活每 15 分钟、令牌报告每 30 分钟、出网自检每 5 分钟、预报名 runner 每分钟、动态监测每分钟唤醒（自行控频）"
echo
crontab -l | grep 'tjise-activity-watch'
echo
echo "日志： $APP/data/cron.log"
echo "手动试跑： cd $APP && $PY scripts/run.py"

echo
echo "── 机器人实例（单应用 combined 双入口） ──"
# 三个业务入口只有两个通道：
#   1) 自动推送：群自定义机器人 Webhook，只能发消息，收不到任何事件（配置里填即可）
#   2) 查询 + 报名：同一个自建应用，BOT_ROLE=combined 单进程按 chat_type 分流——
#      群里必须 @ 才回答公开查询（只读）；owner 私聊才允许个人查询与预报名/报名动作。
# 同一个 App ID/Secret 只能跑一条长连接：拆成 group_query / private_owner 两个
# 单元会互相抢事件，所以这里只生成一个 combined 单元。
APP_ID="${FEISHU_APP_ID:-}"
APP_SECRET="${FEISHU_APP_SECRET:-}"
# OWNER_OPEN_ID 已在写 crontab 前从 PRIVATE_OWNER_OPEN_ID 读好，这里不再重复赋值

if [ -z "$APP_ID" ] || [ -z "$APP_SECRET" ]; then
  echo "! 未提供机器人凭据，跳过 systemd 单元生成。"
  echo "  需要这些环境变量（两个入口共用同一份自建应用凭据）："
  echo "    FEISHU_APP_ID / FEISHU_APP_SECRET   自建应用凭据（群查询与私聊同一个）"
  echo "    PRIVATE_OWNER_OPEN_ID              owner 的 open_id（必填）"
  echo "  然后重新执行本脚本，或按 docs/OPERATIONS.md 手工写单元。"
elif [ -z "$OWNER_OPEN_ID" ]; then
  echo "! 缺少 PRIVATE_OWNER_OPEN_ID：群查询能用，但 owner 私聊会被整体拒绝，已跳过单元生成。"
else
  cat > /etc/systemd/system/tjise-bot.service <<UNIT
[Unit]
Description=Campus activity Feishu bot (combined: group query + owner private chat)
After=network.target

[Service]
Type=simple
WorkingDirectory=/root/tjise-bot
# combined：一个长连接进程分流两个入口；同一个 App 禁止再起第二个单元
Environment=BOT_ROLE=combined
Environment=FEISHU_APP_ID=$APP_ID
Environment=FEISHU_APP_SECRET=$APP_SECRET
Environment=PRIVATE_OWNER_OPEN_ID=$OWNER_OPEN_ID
Environment=APP_DIR=$APP
ExecStart=/usr/bin/node bot.js
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
  echo "  已写入 /etc/systemd/system/tjise-bot.service（BOT_ROLE=combined）"
  systemctl daemon-reload || true
  echo "  启用： systemctl enable --now tjise-bot"
  echo "  迁移：拆分时代的 tjise-bot-group / tjise-bot-owner 是同 App 双进程，必须先停："
  echo "        systemctl disable --now tjise-bot-group tjise-bot-owner"
  echo "  回滚：systemctl disable --now tjise-bot，并把 bot.js 恢复到拆分版本后"
  echo "        再重新启用上面两个拆分单元（旧版值只认 group_query / private_owner）。"
fi
