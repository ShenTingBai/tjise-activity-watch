# 变更记录

按时间倒序。只记"改了什么、为什么"，不逐文件列清单。

## 2026-10-22（本机整理 + 旧机清理）

- **项目结构整理**：README 从 752 行拆成「入口 + 5 篇专题文档」；历史文档移入 `docs/archive/`；
  一次性排查脚本移入 `scripts/dev/`；离线测试移入 `tests/`；新增 `config.example.json`、`requirements.txt`。
- **静默失败修复**：`remind.py` / `scan.py` 在 token 失效、无 token、网络异常时原来直接 `return 2` 不留痕迹，
  现改为各打一行带原因的日志（不新增告警，告警仍由 `run.py` 与探针负责）。
- **旧服务器清理**：`<旧服务器IP>` 上的应用目录、systemd 单元、crontab 残留行、迁移备份、
  nginx 里的 trigger 路由全部删除；旧机不再承担任何职责。
- 卡片消息统一：所有机器人回复改走飞书卡片（带标题/分隔线），纯文本只作兜底。

## 2026-10-22（推送事故与加固）

- 飞书整点限流 `code=11232` 会导致卡片被拒、退回纯文本：`notify.py` 对 11232 做 1.5/3/6s 重试（最多 4 次）。
- 出网自检 `net_probe.py` 上线（每 5 分钟 GET 静态页，连续 2 次失败告警，区分整机断网与学校站点不通）。
- 自动登录误报修复：探针 `http-error` 不再被当成登录态失效；新增 `_session_fresh`（10 分钟内刚更新就跳过）、
  `_clear_alert_state`（确认有效时清零告警冷却）、异常信息带类型与简短原因。
- cron 增加 `PRIVATE_OWNER_OPEN_ID` 注入不完整的排查与修复（`awk -F=` 取错列的坑）。

## 2026-10-22（停机事故）

- 新 ECS 出口带宽为 0，表现为 TCP 可连但 TLS 握手超时：token_probe 连续 27 次 `http-error`、
  07:00 推送失败、07:05 提醒静默。恢复带宽后重启 `tjise-login`，手工补发当天私聊提醒。
- 07:00 日报未补发（10:30 定时推送很快到，避免一小时内两条清单）。

## 2026-10-21/22（迁移）

- 从 `<旧服务器IP>`（Alibaba Cloud Linux 3）迁到 `<你的服务器IP>`（Ubuntu 22.04）：
  Python 3.10、Node 20、playwright 1.64 + chromium-1248、1G swap、时区 Asia/Shanghai。
- 迁移方式：tar 备份（不含 node_modules）→ 新机还原 → 校验 token → 切 cron/systemd → 对账补发。
- 迁移当夜 21:30 推送漏发（token 17:45 已失效），22:35 手工 `run.py --late-checkin --with-open` 补发。

## 2026-10-22（预报名）

- 私聊卡片「预报名」按钮上线：`watchlist.py`（清单状态机）、`watchlist_runner.py`（每分钟对齐）、
  `grab_card.py`（卡片快照与整卡刷新）、`bot.js`（按钮回调、owner 双校验）。
- 修复：已报名的活动不应出现预报名按钮（按 `bmzt==1` 过滤）；点击后按钮被旧卡片盖回（改为回调响应直接带新卡）。
- 修复：卡片只推 owner 私聊，群里不出现个人化信息。

## 2026-10-22（无人值守登录）

- `auto_login.py` 上线：Playwright headless + 验证码 OCR（单张约 60%，3 次重试）+ 原子写会话文件；
  `tjise-autologin.timer` 每 5 分钟跑一次 `--preempt`，16h 主动换新。
- 凭据放 `/root/tjise-login/credentials.json`（600），日志不写账号/密码/验证码/token/姓名。

## 2026-10-21（登录态与告警）

- token 寿命实测：约 17.5 小时（单次观测），文档从"3 小时"改为"以 token_probe 为准"。
- `token_probe.py` 增加连续 2 次失败私聊告警、同周期只提醒一次、恢复清零、6h 冷却。

## 更早

- 站点接口、抓取、去重基线、飞书卡片与群机器人、私聊提醒、`scan.py` 报名冲刺等基础能力，
  细节见 `docs/archive/README-full-20261022.md`。
