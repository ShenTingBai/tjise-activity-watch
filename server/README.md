# 服务器版 · 校园活动推送机器人

常驻 Linux 的那一套：定时抓 `xg.tjise.edu.cn` 的活动，推到飞书群和私聊，@ 机器人可查询，私聊点「预报名」到点自动报名，登录态失效时用验证码 OCR 无人值守重登。

> **适用对象**：天津市大学软件学院（天软）**专升本**学生。接口与页面按该校学生端写死，换学校需自行适配。

> 这个目录同时是 GitHub **`server` 分支**的根目录（分发快照）。只想下载部署的话，直接下
> [Releases](https://github.com/ShenTingBai/tjise-activity-watch/releases) 里的 `tjise-server-*.zip`，
> 或 `git clone -b server`。改代码请回 `main` 分支。

## 它做什么

三个业务入口，两条通道：**自动推送只发消息、群查询只回答问题、owner 私聊才动手**。群查询与 owner 私聊由**同一个飞书自建应用**、一个长连接进程分流（见 [docs/FEISHU_BOT.md](../docs/FEISHU_BOT.md)）。

| 能力 | 触发 | 说明 |
|------|------|------|
| 定时日报 | cron 07:00 / 10:30 / 16:30 / 21:30 | 群卡片 + 给 owner 私发带「预报名」按钮的卡片 |
| 动态发现 | cron 每分钟唤醒，自行控频 | `activity_watch.py`：新发布 / 转开放 / 改期才发一条；不报名、不碰个人清单 |
| 群里 @ 查询 | 飞书群 @机器人（同一自建应用，`BOT_ROLE=combined`） | 只读：待开放 / 报名已开放 / 全部，卡片回复，没有任何个人动作 |
| 私人提醒 | cron 07:05 | 只发 owner 私聊：今天要参加的活动 |
| 预报名 | owner 私聊卡片按钮（同一自建应用，`BOT_ROLE=combined`） | 点一下排队，可再点取消；群里点会被拒绝 |
| 到点自动报名 | watchlist_runner（每分钟） | **只执行 owner 自己清单里的场次**，开放时刻点「立即报名」，结果私聊回报 |
| 登录态续期 | `tjise-login` 守护进程 | 每 2 小时校验，成功即刷新会话文件 |
| 失效重登 | `tjise-autologin.timer`（每 5 分钟） | 探针判失效才动作；用满 8h 主动换新；验证码 OCR（ddddocr 主 + Tesseract 兜底），最多 5 次、间隔 30 秒 |
| 监控告警 | token 探针 / 出网自检 | 连续 2 次失败才私聊告警，同一周期只提醒一次 |

> 公共群这条线是**只读**的：任何一张群卡片都没有预报名、立即报名或重试按钮。
> 报名动作只存在于 owner 私聊（卡片按钮）与本机工具里。

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

## 配置真源与交付证据

- **学校地址只有一个来源**：`scripts/common.py` 的 `SCHOOL_ORIGIN`（可用同名环境变量覆盖）。
  `scripts/`、`deploy/`（含 `auto_login.py`、`grab_signup.py`）一律从它派生 `API_BASE` / `REFERER`，
  任何脚本里再出现写死的域名都会被 `tests/test_endpoints.py` 的静态扫描判失败。
- **预报名清单是并发安全的**：`scripts/watchlist.py` 的 `add` / `remove` / `mark` 都走
  `watchlist.lock` 的读改写事务（POSIX `flock`，Windows `msvcrt`），runner 每分钟对齐时
  也按同一把锁写状态；清单 JSON 解析失败会明确报错，不会用空清单覆盖。
- **unit 名不截断 activity_id**：`watchlist_runner` 用「安全编码 + 完整 id 的 SHA-256 前 8 位」
  生成 systemd unit 与状态文件名，前 8 位相同的两场活动不再串号。
- **服务器 ZIP 带 provenance**：`tools/release.py` 的 `zip_server()` 会生成
  `tjise-server-<tag>.manifest.json`（版本、完整 HEAD、来源是否干净、ZIP 与每个成员的 SHA-256/大小），
  `verify_server_zip()` 可离线重算核验；ZIP 顶层成员只允许
  `bot/deploy/docs/scripts/tests/README.md/CHANGELOG.md/LICENSE/requirements.txt/config.example.json`，
  `data/`、`config.json` 混入即拒绝。
- **依赖锁定**：`requirements.txt` 是安装下限；`requirements.lock` 只有在当前部署能证明
  实际版本时才生成并随 ZIP 分发。无法证明时本文明确标 blocked，不用 `>=` 冒充锁定。

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
python3 scripts/activity_watch.py --dry-run  # 看动态监测这一轮会发什么，不发送不写状态
python3 scripts/watchlist.py list            # 看预报名清单
python3 scripts/selfcheck.py                 # 运行时自检
python3 tests/test_endpoints.py              # 站点地址测试（离线）
python3 -m unittest tests.test_activity_watch -v   # 动态发现边界测试（离线）
node bot/test_bot_runtime.js                 # 机器人 combined 双入口测试（离线）
BOT_ROLE=combined node bot/bot.js --selftest      # 打印允许/拒绝的事件类型
```

## 配置

复制 `config.example.json` 为 `config.json` 再填，含 webhook / 应用密钥，**不要入库**。
字段含义、默认值、环境变量与三套可抄示例见 [docs/CONFIG.md](../docs/CONFIG.md)；
飞书应用怎么建、开哪些权限、订阅什么事件见 [docs/FEISHU_BOT.md](../docs/FEISHU_BOT.md)。

`PRIVATE_OWNER_OPEN_ID` 决定私聊只认谁，必须与 `bot/data/users.json` 里那条 p2p 记录对得上，否则 owner 私聊整体拒绝（群查询不受影响）。

## 文档

| 文档 | 内容 |
|------|------|
| [docs/FEISHU_BOT.md](../docs/FEISHU_BOT.md) | 机器人创建、权限、事件订阅、拿 chat_id/open_id、错误码排错 |
| [docs/CONFIG.md](../docs/CONFIG.md) | 配置项与环境变量全表、三套示例 |
| [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) | 数据流、模块职责、接口清单、去重机制 |
| [docs/OPERATIONS.md](../docs/OPERATIONS.md) | 服务器布局、systemd、crontab、日志、部署更新、回滚 |
| [docs/LOGIN.md](../docs/LOGIN.md) | token 寿命实测、守护续期、验证码 OCR 无人值守登录 |
| [docs/GRAB.md](../docs/GRAB.md) | 预报名状态机、到点自动报名时间线 |
| [docs/TERMS_AND_PRIVACY.md](../docs/TERMS_AND_PRIVACY.md) | 使用条款与隐私告知（服务器版的数据边界） |
| [docs/TIMING_AND_DEDUP_ANALYSIS.md](../docs/TIMING_AND_DEDUP_ANALYSIS.md) | 推送时点与去重推导、请求量账本 |

## 边界（部署者是唯一的凭证持有人）

服务器版的所有学校会话与飞书凭据都由**部署者本人**管理：

- **凭证由部署者保管**：学校会话（`/root/tjise-login`）、飞书应用凭据（`config.json`，
  权限 600）、`credentials.json`（唯一放账号密码的地方，`set_credentials.py` 交互写入）。
  它们只存在于部署者的服务器上，不入库、不外发。
- **群聊只读**：群机器人用部署者会话查询公开活动字段并推送/答问；**群成员不登录学校
  系统，也不要向机器人提供学校账号或密码**——这不是匿名访问学校接口，而是部署者会话
  的只读代答。
- **私聊才处理个人操作**：仅 `PRIVATE_OWNER_OPEN_ID` 对应的部署者本人可以预报名、
  收到结果回报；机器人不替任何群成员执行报名。
- **公共群不含个人状态**：群机器人只回答公开活动查询，不含个人报名状态，也不提供任何报名按钮。
- **动态发现不执行动作**：`activity_watch.py` 只读学校公开活动，永不调用报名入口，也不读写个人清单。
- 日志不写账号、密码、验证码、token、姓名；`credentials.json` 与 `storage_state.json` 权限 600。
- 只有部署者点了「预报名」的活动才会自动提交，不做无差别报名。
- 向群成员提供推送服务前，请先向他们说明 [docs/TERMS_AND_PRIVACY.md](../docs/TERMS_AND_PRIVACY.md)
  的数据边界：服务器版的数据处理者是你（部署者）本人。

---

想在自己电脑上报一两场、不想维护服务器：用仓库的 [`local` 分支](https://github.com/ShenTingBai/tjise-activity-watch/tree/local)（本机版）。
