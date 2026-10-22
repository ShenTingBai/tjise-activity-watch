# 服务器版 · 校园活动推送机器人

常驻 Linux 的那一套：定时抓 `xg.tjise.edu.cn` 的活动，推到飞书群和私聊，@ 机器人可查询，私聊点「预报名」到点自动报名，登录态失效时用验证码 OCR 无人值守重登。

> **适用对象**：天津市大学软件学院（天软）**专升本**学生。接口与页面按该校学生端写死，换学校需自行适配。

> 这个目录同时是 GitHub **`server` 分支**的根目录（分发快照）。只想下载部署的话，直接下
> [Releases](https://github.com/ShenTingBai/tjise-activity-watch/releases) 里的 `tjise-server-*.zip`，
> 或 `git clone -b server`。改代码请回 `main` 分支。

## 它做什么

| 能力 | 触发 | 说明 |
|------|------|------|
| 定时日报 | cron 07:00 / 10:30 / 16:30 / 21:30 | 群卡片 + 给 owner 私发带「预报名」按钮的卡片 |
| 群里 @ 查询 | 飞书群 @机器人 | 待开放 / 已开放 / 全部，卡片回复 |
| 私人提醒 | cron 07:05 | 只发 owner 私聊：今天要参加的活动 |
| 预报名 | 私聊卡片按钮 | 点一下排队，可再点取消；到点由 runner 拉起抢报名 |
| 到点抢报名 | watchlist_runner（每分钟） | 开放时刻点「立即报名」，结果私聊回报 |
| 登录态续期 | `tjise-login` 守护进程 | 每 2 小时校验，成功即刷新会话文件 |
| 失效重登 | `tjise-autologin.timer`（每 5 分钟） | 探针判失效才动作；16h 主动换新；验证码 OCR，最多 3 次 |
| 监控告警 | token 探针 / 出网自检 | 连续 2 次失败才私聊告警，同一周期只提醒一次 |

## 目录

```
server/
├── scripts/        # 生产脚本，cron / systemd 直接调，目录名是相对的，别改
├── bot/            # 飞书机器人（长连接 + 卡片回调）
├── deploy/         # systemd 单元、server_refresh.js、auto_login.py、部署脚本
├── tests/          # 离线测试，不碰真实数据
├── docs/           # 运维文档（main 里在仓库根 docs/，分发到 server 分支时铺到根）
├── config.example.json
└── requirements.txt
```

运行时会生成 `data/`（会话、状态、日志）与 `reports/`，两者都不入库。

## 部署

前置：Ubuntu 22.04 / root / 能出网（学校站点 + `open.feishu.cn`）。

```bash
apt install -y python3 cron tesseract-ocr
# Node 20（playwright 驱动需要 >=18）
cd /tmp && curl -fsSLO https://npmmirror.com/mirrors/node/v20.20.2/node-v20.20.2-linux-x64.tar.xz \
  && tar -xJf node-v20.20.2-linux-x64.tar.xz -C /usr/local --strip-components=1

mkdir -p /root/tjise-activity-watch /root/tjise-bot /root/tjise-login
# 把本分支内容放进去，再按 docs/OPERATIONS.md 装 systemd 单元和 crontab
```

完整步骤（三个 systemd 单元、crontab 九条、日志位置、更新与回滚）见 [docs/OPERATIONS.md](../docs/OPERATIONS.md)。

## 常用命令

```bash
python3 scripts/run.py --dry                 # 看这一轮会推什么，不发
python3 scripts/run.py --force               # 忽略「今天已推过」，真推一次
python3 scripts/query.py pending             # 命令行查待开放活动
python3 scripts/watchlist.py list            # 看预报名清单
python3 scripts/selfcheck.py                 # 运行时自检
python3 tests/test_endpoints.py              # 站点地址测试（离线）
```

## 配置

复制 `config.example.json` 为 `config.json` 再填，含 webhook / 应用密钥，**不要入库**。
字段含义、默认值、环境变量与三套可抄示例见 [docs/CONFIG.md](../docs/CONFIG.md)；
飞书应用怎么建、开哪些权限、订阅什么事件见 [docs/FEISHU_BOT.md](../docs/FEISHU_BOT.md)。

`PRIVATE_OWNER_OPEN_ID` 决定私聊只发给谁，必须与 `bot/data/users.json` 里那条 p2p 记录对得上，否则私聊通道静默关闭。

## 文档

| 文档 | 内容 |
|------|------|
| [docs/FEISHU_BOT.md](../docs/FEISHU_BOT.md) | 机器人创建、权限、事件订阅、拿 chat_id/open_id、错误码排错 |
| [docs/CONFIG.md](../docs/CONFIG.md) | 配置项与环境变量全表、三套示例 |
| [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) | 数据流、模块职责、接口清单、去重机制 |
| [docs/OPERATIONS.md](../docs/OPERATIONS.md) | 服务器布局、systemd、crontab、日志、部署更新、回滚 |
| [docs/LOGIN.md](../docs/LOGIN.md) | token 寿命实测、守护续期、验证码 OCR 无人值守登录 |
| [docs/GRAB.md](../docs/GRAB.md) | 预报名状态机、到点抢报名时间线 |
| [docs/TIMING_AND_DEDUP_ANALYSIS.md](../docs/TIMING_AND_DEDUP_ANALYSIS.md) | 推送时点与去重推导、请求量账本 |

## 边界

- 只服务**本人账号**：不代他人报名、不做多账号批量报名、不占位。
- 日志不写账号、密码、验证码、token、姓名；`credentials.json` 与 `storage_state.json` 权限 600。
- 只有你点了「预报名」的活动才会自动抢，不做无差别报名。

---

想在自己电脑上抢一两场、不想维护服务器：用仓库的 [`local` 分支](https://github.com/ShenTingBai/tjise-activity-watch/tree/local)（本机版）。
