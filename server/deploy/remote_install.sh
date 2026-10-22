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
CRON_LINES="0 7 * * * cd $APP && $PY scripts/run.py >> $APP/data/cron.log 2>&1
5 7 * * * cd $APP && $PY scripts/remind.py >> $APP/data/remind.log 2>&1
30 10 * * * cd $APP && $PY scripts/run.py >> $APP/data/cron.log 2>&1
30 16 * * * cd $APP && $PY scripts/run.py >> $APP/data/cron.log 2>&1
30 21 * * * cd $APP && $PY scripts/run.py --late-checkin --with-open >> $APP/data/cron.log 2>&1
*/5 * * * * cd $APP && $PY scripts/scan.py >> $APP/data/scan.log 2>&1"

( crontab -l 2>/dev/null | grep -v 'tjise-activity-watch' || true; echo "$CRON_LINES" ) | crontab -
echo "✓ 已安装定时任务：清单 07:00 / 10:30 / 16:30 / 21:30，私人提醒 07:05，扫描每 5 分钟（全天）"
echo
crontab -l | grep 'tjise-activity-watch'
echo
echo "日志： $APP/data/cron.log"
echo "手动试跑： cd $APP && $PY scripts/run.py"
