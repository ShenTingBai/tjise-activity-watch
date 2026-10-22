# 运维手册

## 1. 机器与目录

| 项 | 值 |
|----|----|
| 服务器 | `<你的服务器IP>`（Ubuntu 22.04.5，root） |
| 私钥 | 本机 `C:/Users/29522/.ssh/develop` |
| 时区 | Asia/Shanghai |
| Python | 3.10.12（系统自带） |
| Node | v20.20.2（`/usr/local/bin/node`，playwright 要求 ≥18） |
| Chromium | `/root/.cache/ms-playwright/chromium-1248`（由 playwright 管理） |
| Swap | 1G（`/swapfile`，已写 fstab） |

```
/root/tjise-activity-watch/      应用与脚本
├── server/scripts/                     生产脚本（cron 用相对路径调用）
├── server/bot/                         飞书长连接机器人（bot.js + node_modules）
├── server/deploy/                      部署脚本与 systemd 模板
├── data/                        运行数据（session.json、state.json、日志）
└── reports/                     推送产物

/root/tjise-bot/                 机器人进程目录（bot.js 软链自应用目录）+ data/users.json
/root/tjise-login/               登录态守护：server_refresh.js、storage_state.json、
                                 credentials.json、auto_login.py、grab_signup.py、refresh.log
```

权限约定：目录 700，`credentials.json` / `storage_state.json` / `data/session.json` 600。

## 2. systemd

| 单元 | 类型 | 作用 |
|------|------|------|
| `tjise-login.service` | simple | `node server_refresh.js --daemon`：每 2h 校验 token，成功即刷新会话 |
| `tjise-bot.service` | simple | `node bot.js`：飞书长连接，@ 查询与私聊按钮回调 |
| `tjise-trigger.service` | simple | `python3 server/scripts/trigger_server.py`：HTTP 一键触发（18080） |
| `tjise-autologin.service` | oneshot | `python3 /root/tjise-login/auto_login.py --preempt` |
| `tjise-autologin.timer` | timer | `OnCalendar=*:0/5`，每 5 分钟唤起上面的 oneshot |

```bash
systemctl status tjise-bot tjise-login tjise-trigger tjise-autologin.timer
systemctl restart tjise-bot          # 改完 bot.js 必须重启
systemctl restart tjise-login        # 改完 server_refresh.js 必须重启
journalctl -u tjise-bot -n 100 --no-pager
```

`tjise-bot` 的 `PRIVATE_OWNER_OPEN_ID` 放在 drop-in `tjise-bot.service.d/owner.conf`，
`tjise-autologin.service` 里也单独配了一份——两个必须一致，否则私聊静默关闭。

## 3. crontab

```cron
0 7  * * * cd /root/tjise-activity-watch && PRIVATE_OWNER_OPEN_ID=ou_xxx /usr/bin/python3 scripts/run.py --with-grab >> data/cron.log 2>&1
5 7  * * * cd /root/tjise-activity-watch && PRIVATE_OWNER_OPEN_ID=ou_xxx /usr/bin/python3 scripts/remind.py            >> data/remind.log 2>&1
30 10 * * * cd /root/tjise-activity-watch && /usr/bin/python3 scripts/run.py                                          >> data/cron.log 2>&1
30 16 * * * cd /root/tjise-activity-watch && /usr/bin/python3 scripts/run.py                                          >> data/cron.log 2>&1
30 21 * * * cd /root/tjise-activity-watch && /usr/bin/python3 scripts/run.py --late-checkin --with-open                >> data/cron.log 2>&1
*/5  * * * * cd /root/tjise-activity-watch && /usr/bin/python3 scripts/scan.py                                        >> data/scan.log 2>&1
*/15 * * * * cd /root/tjise-activity-watch && PRIVATE_OWNER_OPEN_ID=ou_xxx /usr/bin/python3 scripts/token_probe.py    >> data/token_probe.log 2>&1
*/30 * * * * cd /root/tjise-activity-watch && /usr/bin/python3 scripts/token_report.py                                >> data/token_report.log 2>&1
*/5  * * * * cd /root/tjise-activity-watch && PRIVATE_OWNER_OPEN_ID=ou_xxx /usr/bin/python3 scripts/net_probe.py      >> data/net_probe.log 2>&1
*/1  * * * * cd /root/tjise-activity-watch && PRIVATE_OWNER_OPEN_ID=ou_xxx /usr/bin/python3 scripts/watchlist_runner.py >> data/watchlist.log 2>&1
```

要点：

- 只有需要发**私聊**的行才带 `PRIVATE_OWNER_OPEN_ID`；群推送不需要。
- 所有行都 `cd` 到应用目录——脚本里用的是 `data/...` 相对路径。
- 改完用 `crontab -l` 复核，别用编辑器覆盖整个 crontab（历史上因此丢过行）。

## 4. 日志

| 文件 | 内容 |
|------|------|
| `data/cron.log` | 定时推送的成功/失败、异常堆栈 |
| `data/remind.log` | 07:05 私聊提醒 |
| `data/scan.log` | 报名冲刺 / 晚归签到 |
| `data/token_probe.log` | 每 15 分钟一行：`时间 token=指纹 saved_at=… age=… result=ok|fail|…` |
| `data/token_report.log` | token 寿命汇总 |
| `data/net_probe.log` | 出网自检：`result=ok|fail|http-error` |
| `data/watchlist.log` | 预报名状态流转 |
| `/root/tjise-login/refresh.log` | 守护进程每次校验结果 |
| `/root/tjise-login/auto_login.log` | 无人值守重登的尝试与结果 |

日志里**不应出现** token、姓名、学号、密码、验证码；看到这类内容按事故处理。

### 日志轮转

上面所有 `*.log` 都由 `deploy/logrotate/tjise` 统一轮转：每周一次、保留 8 份、超过 5MB 提前切、切后 gzip。

```bash
sudo install -m 644 deploy/logrotate/tjise /etc/logrotate.d/tjise
sudo logrotate -d /etc/logrotate.d/tjise   # 只看不切
```

配置用 `copytruncate` 而不是默认的"改名 + 新建"：cron 和服务都是 `>>` 追加写、不重开文件，改名会让它们继续写进已改名的旧文件，新文件一直是空的。原地截断则不换 inode，追加写自动接上。

## 5. 日常操作

```bash
# 手动跑一轮推送（不写去重状态）
cd /root/tjise-activity-watch && python3 scripts/run.py --dry

# 强制真推一次（会写 announced，慎用）
cd /root/tjise-activity-watch && python3 scripts/run.py --force

# 补发今天的私聊提醒（07:05 漏发时）
cd /root/tjise-activity-watch && PRIVATE_OWNER_OPEN_ID=ou_xxx python3 scripts/remind.py

# 换 token（本机登录后）
scp -i ~/.ssh/develop data/storage_state.json root@<你的服务器IP>:/root/tjise-login/
ssh root@<你的服务器IP> 'cd /root/tjise-login && node server_refresh.js'

# 看预报名清单
cd /root/tjise-activity-watch && python3 scripts/watchlist.py list

# 手工加/删预报名
python3 server/scripts/watchlist.py add --id <id> --name <名称> --at "2026-10-30 12:00:00"
python3 server/scripts/watchlist.py remove --id <id>
```

## 6. 部署更新

本地改完 → 自检 → 传文件 → 重启对应服务：

```bash
# 1) 本地自检
python -m py_compile server/scripts/*.py
node --check server/bot/bot.js
python server/tests/test_endpoints.py && python server/tests/test_notify.py

# 2) 传文件（只传改动的那几个，别整套覆盖 data/）
scp -i ~/.ssh/develop scripts/<file>.py root@<你的服务器IP>:/root/tjise-activity-watch/scripts/
scp -i ~/.ssh/develop server/bot/bot.js        root@<你的服务器IP>:/root/tjise-bot/bot.js

# 3) 重启
ssh root@<你的服务器IP> 'systemctl restart tjise-bot && systemctl is-active tjise-bot'
```

`data/` 与 `config.json` 属于线上状态，**不要**从本地覆盖。

## 7. 回滚

- 脚本级：保留上一版文件（`scp` 前先 `cp x.py x.py.bak-YYYYMMDD`），出问题换回并重启服务。
- 整机级：历史迁移时旧服务器（<旧服务器IP>）已清理，回滚只能靠本仓库代码 + `data/state.json` 重建；
  如需再迁移，按「备份 → 还原目录 → 装 cron/systemd → 校验 token → 手工推一轮」的顺序做。

## 8. 故障处理

| 现象 | 排查顺序 |
|------|----------|
| 群里没消息 | `cron.log` 有没有 `[✓] 飞书 卡片推送成功` → 失败看错误码（11232 = 飞书整点限流，已自动重试）→ 再看 token |
| 私聊没消息 | `remind.log` / `watchlist.log` 是否出现「未配置 PRIVATE_OWNER_OPEN_ID」；`users.json` 的 p2p 记录与 `private_chat_id` 是否一致 |
| 登录态失效 | `token_probe.log` 尾部 `result=fail` → 看 `/root/tjise-login/auto_login.log` 是否已自动重登 → 不行就本机 `login.py` 后传 `storage_state.json` |
| 接口 timeout / handshake | `net_probe.log`：`net=school-only` 是学校侧，`net=down` 是整机断网；ECS 带宽为 0 时表现为 TLS 握手超时 |
| 自动登录失败 | `auto_login.log` 里 `login_rejected` / `ocr_incomplete`；确认 `credentials.json` 存在且 600、账号密码没改 |
| 机器人不响应 | `systemctl status tjise-bot`、`journalctl -u tjise-bot -n 50`；长连接断开会自动重连，反复断查应用凭据 |
| 按钮点了没反应 | 私聊卡片按钮只对 owner 私聊生效（`operator.open_id` + `chat_id` 双校验）；群里点会 toast 拒绝 |

## 9. 迁移记录（2026-10-21）

- 从 `<旧服务器IP>`（Alibaba Cloud Linux 3）迁到 `<你的服务器IP>`（Ubuntu 22.04）。
- 旧机：cron 行注释前缀 `#MIGRATED-20261021-2230`、服务 `disable --now`，验证后于 2026-10-22 彻底删除应用目录、systemd 单元、nginx 中的 trigger 路由与备份。
- 教训：迁移当夜新 ECS 出口带宽为 0，表现为「TCP 能连、SSH banner 超时、TLS 握手超时」，一度误判为 fail2ban；这类故障先查带宽/安全组。
- 旧机上手机一键触发链接（`<你的域名>/0f4b…` → `127.0.0.1:18080`）随旧机清理失效；如需恢复，应在新机部署 HTTPS 反代指向 `tjise-trigger`。
