#!/usr/bin/env bash
# 打一个可交付的模板包：去掉所有个人数据，配置改成占位符。
#
#   bash deploy/make_template.sh
# 输出：dist/campus-activity-bot-template.zip
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAME="campus-activity-bot-template"
OUT="$ROOT/dist"
STAGE="$OUT/$NAME"

rm -rf "$STAGE"
mkdir -p "$STAGE/scripts" "$STAGE/bot" "$STAGE/deploy" "$STAGE/docs"

# 代码（排除缓存）
for f in "$ROOT"/scripts/*.py; do
  cp "$f" "$STAGE/scripts/"
done

# 机器人是「主程序 + 去重表」两个文件，缺一个就起不来，必须逐项显式复制。
# 这里故意不写 `2>/dev/null || true`：文件被改名或删掉时让 set -e 当场失败，
# 而不是打出一个少文件的包，装到服务器上才发现。
cp "$ROOT"/bot/bot.js "$STAGE/bot/"
cp "$ROOT"/bot/handled_messages.js "$STAGE/bot/"
cp "$ROOT"/bot/package.json "$STAGE/bot/"
cp "$ROOT"/bot/test_handled_messages.js "$STAGE/bot/"
cp "$ROOT"/bot/test_bot_runtime.js "$STAGE/bot/"

# token 续期脚本依赖 atomic_json.js（原子写会话文件），同样成对复制
cp "$ROOT"/deploy/remote_install.sh "$STAGE/deploy/"
cp "$ROOT"/deploy/sync_token.sh "$STAGE/deploy/"
cp "$ROOT"/deploy/server_refresh.js "$STAGE/deploy/"
cp "$ROOT"/deploy/tjise-login.service "$STAGE/deploy/"
cp "$ROOT"/deploy/atomic_json.js "$STAGE/deploy/"
cp "$ROOT"/deploy/test_atomic_json.js "$STAGE/deploy/"
cp "$ROOT"/docs/archive/BUILD_FROM_SCRATCH.md "$STAGE/docs/"

# 配置模板：全部占位符
cat > "$STAGE/config.json" <<'JSON'
{
  "filter": {
    "keyword_blocklist": []
  },
  "notify": {
    "channel": "webhook",
    "platform": "feishu",
    "webhook_url": "<你的飞书群自定义机器人 Webhook 地址>",
    "secret": "",
    "feishu_app": {
      "app_id": "<owner 私聊应用的 App ID>",
      "app_secret": "<owner 私聊应用的 App Secret>"
    },
    "users_file": "/root/tjise-bot/data/users.json",
    "private_chat_id": ""
  },
  "watch": {
    "enabled": true,
    "day_seconds": 180,
    "night_seconds": 600
  }
}
JSON

# 三个机器人，三个角色单元。**不再提供**没有 BOT_ROLE 的单服务模板：
# 新版 bot.js 缺角色会直接拒绝启动，留一个「看起来能跑」的旧单元只会浪费时间。
cat > "$STAGE/deploy/tjise-bot-group.service" <<'UNIT'
[Unit]
Description=Campus activity Feishu bot (group query, read-only)
After=network.target

[Service]
Type=simple
WorkingDirectory=/root/tjise-bot
# 群查询角色：只回答被 @ 的群消息，任何预报名/报名动作一律拒绝
Environment=BOT_ROLE=group_query
Environment=FEISHU_APP_ID=<群查询应用的 App ID>
Environment=FEISHU_APP_SECRET=<群查询应用的 App Secret>
Environment=APP_DIR=/root/tjise-activity-watch
ExecStart=/usr/bin/node bot.js
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

cat > "$STAGE/deploy/tjise-bot-owner.service" <<'UNIT'
[Unit]
Description=Campus activity Feishu bot (owner private chat)
After=network.target

[Service]
Type=simple
WorkingDirectory=/root/tjise-bot
# owner 私聊角色：只处理 PRIVATE_OWNER_OPEN_ID 本人的私聊，才有预报名/报名动作
Environment=BOT_ROLE=private_owner
Environment=FEISHU_APP_ID=<owner 私聊应用的 App ID>
Environment=FEISHU_APP_SECRET=<owner 私聊应用的 App Secret>
# 私人查询的主人（飞书 open_id）。它和 BOT_OPEN_ID 是两个不同的值；
# 不填 = 该实例直接拒绝启动（不会退化成「谁都能报名」）。
Environment=PRIVATE_OWNER_OPEN_ID=<你的 open_id>
Environment=APP_DIR=/root/tjise-activity-watch
ExecStart=/usr/bin/node bot.js
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

# 空的数据目录（保留结构）
mkdir -p "$STAGE/data"
cat > "$STAGE/data/.gitkeep" <<'EOF'
EOF

# 说明
cat > "$STAGE/README.md" <<'MD'
# 校园活动助手 · 模板

这是从零搭建指南的参考实现，**不含任何真实账号、密钥、服务器信息**。

## 怎么用

1. 先读 `docs/BUILD_FROM_SCRATCH.md`，按阶段验证，不要一上来就搬代码
2. 填 `config.json` 里的占位符（`<...>`）
3. `data/session.json` 需要你先跑一次登录拿到

## 关键约定

- 所有 `<占位符>` 都必须换成你自己的
- **三个机器人，三条权限边界**：群自定义机器人（Webhook）只推送；
  `tjise-bot-group`（`BOT_ROLE=group_query`）只读群查询；
  `tjise-bot-owner`（`BOT_ROLE=private_owner`）只服务 owner 私聊。
  旧版单实例 `tjise-bot.service` 没有 `BOT_ROLE`，新版会直接拒绝启动，别再沿用。
- `PRIVATE_OWNER_OPEN_ID` 是「唯一的主人」，和 `BOT_OPEN_ID`（机器人自己）不是一回事；
  07:05 的私人提醒 cron 也要注入同一个值，否则会出现「群里不回答但每天照样私发」的半开状态
- 动态监测 `scripts/activity_watch.py` 由 cron 每分钟唤醒，自己按
  `data/activity_watch.json` 里的 `next_poll_at` 控频（白天 3 分钟 / 夜间 10 分钟，
  不做临近开放加密）；它只读公开活动，不参与预报名与报名
- 学校接口字段在本项目里是 `hdmc/bmkssj/hdjssj` 等，**换学校必须重新核对**；
  只换域名时设 `SCHOOL_ORIGIN` 即可，但 Python 与 Node 两边默认值要同时改
- `config.json` 含密钥，权限设 600，不要提交到公开仓库

## 验收

按文档第 9 节的 12 条清单逐条验证，全部通过再上线。
MD

cd "$OUT" && rm -f "$NAME.zip" && zip -qr "$NAME.zip" "$NAME"
echo "✓ 已生成：$OUT/$NAME.zip"
echo "  文件数：$(find "$STAGE" -type f | wc -l)"
du -sh "$OUT/$NAME.zip" | awk '{print "  体积："$1}'
