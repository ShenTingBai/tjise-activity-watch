# 智慧元小元 · 校园活动推送机器人

定时抓取 `xg.tjise.edu.cn`（智慧元小元学生端）的校园活动，把「今天要参加的」「即将开放报名的」推到飞书群和私聊；
群里 @ 机器人可查询，私聊点「预报名」按钮可挂号，到点自动报名；登录态由服务器守护进程自动续期，失效时用验证码 OCR 无人值守重登。

当前线上部署：**<你的服务器IP>**（Ubuntu 22.04，systemd + cron 常驻）。

## 功能一览

| 能力 | 从哪里触发 | 说明 |
|------|-----------|------|
| 定时日报 | cron 07:00 / 10:30 / 16:30 / 21:30 | 先推群卡片，再给 owner 私发一张带「预报名」按钮的卡片 |
| 群里 @ 查询 | 飞书群 @机器人 | 待开放 / 已开放 / 全部活动，卡片回复 |
| 私人提醒 | cron 07:05 | 只发 owner 私聊：今天要参加的活动 |
| 预报名 | 私聊卡片按钮 | 点一下排队，可再点「取消预报名」 |
| 到点抢报名 | watchlist_runner（每分钟） | 到开放时刻自动点「立即报名」并私聊回报结果 |
| 登录态续期 | tjise-login 守护进程 | 每 2 小时校验一次，成功即刷新会话文件 |
| 失效重登 | tjise-autologin.timer（每 5 分钟） | 探针判失效才动作；16h 主动换新；验证码 OCR + 最多 3 次 |
| 监控告警 | token 探针 / 出网自检 | 连续 2 次失败才私聊告警，同一失效周期只提醒一次 |

## 快速开始

### A. 部署到服务器（7×24，推荐）

前置：Ubuntu 22.04 / root / 能出网（需访问学校站点与 `open.feishu.cn`）。

```bash
# 1) 运行时
apt install -y python3 cron
# Node 20（playwright 驱动需要 >=18）
cd /tmp && curl -fsSLO https://npmmirror.com/mirrors/node/v20.20.2/node-v20.20.2-linux-x64.tar.xz \
  && tar -xJf node-v20.20.2-linux-x64.tar.xz -C /usr/local --strip-components=1

# 2) 目录
mkdir -p /root/tjise-activity-watch /root/tjise-bot /root/tjise-login

# 3) 依赖与文件见 docs/OPERATIONS.md「部署」
```

完整步骤、systemd 单元、crontab、日志位置见 [docs/OPERATIONS.md](docs/OPERATIONS.md)。

### B. 只在本机跑（不留服务器）

```bash
python -m pip install -r requirements.txt
python scripts/login.py      # 弹真实 Chrome 登录一次，token 存 data/session.json
python scripts/run.py --dry  # 只看不推
python scripts/run.py        # 正常跑一轮
```

本机不想装环境也行：`tjise-grab-local/` 是给同学的独立小工具（手动登录 + 抢单个活动）。

## 目录结构

```
tjise-activity-watch/
├── scripts/                 # 生产脚本（cron/systemd 直接调，路径是相对的，别随意改目录）
│   ├── run.py               # 定时推送入口（--with-grab 时附带私聊预报名卡）
│   ├── fetch.py             # 抓取 + 解析 + 去重标记
│   ├── scan.py              # 每 5 分钟：报名冲刺 / 晚归签到提醒
│   ├── remind.py            # 每天 07:05 私聊「今天要参加的活动」
│   ├── token_probe.py       # 每 15 分钟探一次 token，连续失败告警
│   ├── token_report.py      # 每 30 分钟汇总 token 寿命
│   ├── net_probe.py         # 每 5 分钟出网自检（区分断网 / 学校站点不通）
│   ├── watchlist.py         # 预报名清单（CLI + JSON）
│   ├── watchlist_runner.py  # 每分钟对齐清单，到点拉起 grab_signup
│   ├── grab_card.py         # 私聊预报名卡片的快照与整卡刷新
│   ├── card.py / render.py / notify.py   # 卡片结构 / 文案 / 发送
│   ├── common.py            # 学校地址、会话读写（唯一域名来源）
│   └── dev/                 # 一次性排查脚本，不参与线上链路
├── bot/                     # 飞书机器人（长连接，@ 查询 + 私聊按钮回调）
├── deploy/                  # 部署脚本、systemd 单元、server_refresh.js
├── tests/                   # 离线测试（不碰真实数据）
├── docs/                    # 文档（见下方索引）
├── data/                    # 运行数据：session.json、state.json、各日志（不入库）
└── reports/                 # 推送产物：latest.md、latest.json（不入库）
```

## 常用命令

```bash
python scripts/run.py --dry                 # 看这一轮会推什么，不发
python scripts/run.py --force               # 忽略「今天已推过」的限制，真推一次
python scripts/query.py [pending|open|all]  # 命令行查活动
python scripts/watchlist.py list            # 看预报名清单
python tests/test_endpoints.py              # 站点地址测试（离线）
python tests/test_notify.py                 # 推送成败判定（离线）
python scripts/selfcheck.py                 # 运行时自检
python scripts/regress.py                   # 回归（会联网）
```

服务器上的运维命令（重启服务、看日志、补发提醒）见 [docs/OPERATIONS.md](docs/OPERATIONS.md)。

## 配置

复制 `config.example.json` 为 `config.json` 再填；`config.json` 含 webhook / 密钥，**不入库**。

| 键 | 说明 |
|----|------|
| `notify.channel` / `notify.platform` | 推送通道：`webhook` / `feishu` |
| `notify.feishu_app.app_id` / `app_secret` | 飞书自建应用凭据（私聊、卡片用） |
| `notify.private_chat_id` | owner 私聊会话 ID（与 `users.json` 里那条 p2p 记录一致） |
| `filter.keyword_blocklist` | 标题关键词黑名单 |

环境变量 `PRIVATE_OWNER_OPEN_ID` 决定「私聊只发给谁」，必须与 `bot/data/users.json` 里的 p2p 记录对得上，
否则私聊通道静默关闭（`remind.log` 里会写 `[i] 未配置 PRIVATE_OWNER_OPEN_ID`）。

## 文档索引

| 文档 | 内容 |
|------|------|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 数据流、模块职责、接口清单、去重机制、推送节奏 |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | 服务器布局、systemd、crontab、日志、部署更新、回滚、故障处理 |
| [docs/LOGIN.md](docs/LOGIN.md) | token 寿命实测、守护进程续期、验证码 OCR 无人值守登录、凭据管理 |
| [docs/GRAB.md](docs/GRAB.md) | 预报名状态机、到点抢报名时间线、结果回执 |
| [docs/TIMING_AND_DEDUP_ANALYSIS.md](docs/TIMING_AND_DEDUP_ANALYSIS.md) | 推送时点与去重的设计推导、请求量账本 |
| [CHANGELOG.md](CHANGELOG.md) | 变更记录 |
| [docs/archive/](docs/archive/) | 历史文档：搭建指南、旧版 README、评审提示词 |

## 边界与安全

- 只服务**本人账号**：不代他人报名、不做多账号批量报名、不占位。
- 日志不写账号、密码、验证码、token、姓名；`credentials.json` 与 `storage_state.json` 权限 600。
- 抢报名只在用户明确点了「预报名」的清单内动作，不会无差别抢。
