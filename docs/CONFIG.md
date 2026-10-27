# 配置与环境变量参考

所有可调项都在这一页。改之前先弄清楚三件事：

1. **配置文件只有两处**：`server/config.json`（服务器版）和 `local/config.json`（本机版，可选）。两者都不入库。
2. **环境变量不进配置文件**：凭据类、路径类、owner 身份走环境变量（systemd 的 `Environment=` 或 crontab 行首）。
3. **改完要重启对应进程**：cron 任务下一轮自动生效，systemd 服务要 `systemctl restart`。

---

## 1. 配置文件

### 1.1 `server/config.json`（服务器版）

从 `server/config.example.json` 复制而来。所有字段都在 `notify`、`filter`、`watch` 三个对象下。

| 键 | 类型 | 必填 | 默认/留空时 | 作用 | 代码位置 |
|---|---|---|---|---|---|
| `notify.channel` | string | 否 | — | 通道名，只用于日志区分；除 `qq` 外都走 webhook 分支 | `notify.py` |
| `notify.platform` | string | **是** | — | 走哪条通道：`feishu`（应用/webhook 统一入口）或 `webhook`（纯群机器人） | `notify.py` `send()` |
| `notify.webhook_url` | string | 二选一 | 留空则不走 webhook | 群自定义机器人的 Webhook 地址（自动推送 + 动态事件都走它） | `notify.py` `send()` |
| `notify.secret` | string | 否 | `""` | Webhook 开启「签名校验」时的密钥 | `notify.py` |
| `notify.feishu_app.app_id` | string | 用私聊时必填 | `""` | **owner 私聊应用**的 App ID（`cli_` 开头）；群查询应用不需要写进配置 | `notify.py` / `bot.js` |
| `notify.feishu_app.app_secret` | string | 同上 | `""` | owner 私聊应用的 App Secret | 同上 |
| `notify.feishu_app.api_base` | string | 否 | `https://open.feishu.cn/open-apis` | 飞书 API 根地址（换测试环境才改） | `notify.py` |
| `notify.users_file` | string | 用私聊时必填 | `bot/data/users.json` | 机器人记录的会话表，私聊 chat_id 从这里找 | `notify.py` `_p2p_chat_id()` |
| `notify.private_chat_id` | string | 否 | 取 `users.json` 里 owner 那条 | owner 私聊会话 ID；填了必须与 `users.json` 一致，否则私聊静默 | `notify.py` `_p2p_chat_id()` |
| `filter.keyword_blocklist` | string[] | 否 | `[]` | 活动标题关键词黑名单，命中即不推送（只影响推送，不影响动态监测的事件判定） | `fetch.py` / `run.py` / `scan.py` |
| `watch.enabled` | bool | 否 | `true` | 关掉后 `activity_watch.py` 打印一行跳过，日报与临近提醒不受影响 | `activity_watch.py` |
| `watch.day_seconds` | int | 否 | `180` | 白天（07:00–22:59）真正访问学校的间隔 | `common.load_watch_config()` |
| `watch.night_seconds` | int | 否 | `600` | 夜间（23:00–06:59）间隔 | 同上 |

`watch` 段的脏值（负数、字符串、0）一律回落到默认值：宁可慢一点，也不能因为一个错字变成每分钟打学校接口。最小可用值是 30 秒。

**没有「临近开放加密」这一档**：报名窗口中位数 24 小时，开放前后多问几次换不到信息，
却会把日请求量从三百多抬到一千四。开放前 10 分钟的提醒由 `scan.py` 独立负责，
两条链路不重复承担同一件事。真的需要更高频，那是单独的设计（见 TIMING 第 7.4 节）。

**私聊为什么总是「静默失败」**：只要下面任一条件不满足，`_p2p_chat_id()` 就返回空，代码选择**不发也不报错**（防止误发给别人）：

1. 环境变量 `PRIVATE_OWNER_OPEN_ID` 有值
2. `users.json` 里有这条 `open_id` 的记录
3. 该记录 `chat_type == "p2p"`
4. 如果填了 `private_chat_id`，必须与记录里的 `chat_id` 相等

排查看 `data/remind.log` 或 systemd 日志里的 `[i] 未配置 PRIVATE_OWNER_OPEN_ID，私人提醒已关闭`。

### 1.2 `local/config.json`（本机版，可选）

```json
{
  "feishu_webhook": ""
}
```

| 键 | 必填 | 说明 |
|---|---|---|
| `feishu_webhook` | 否 | 群自定义机器人地址；填了抢报名结果会发飞书卡片，留空只在控制台打印 |

本机版**不需要** `app_id` / `app_secret`：它只推送结果，不接收消息，也不发私聊。

### 1.3 三个机器人分别读哪份配置

| 角色 | 读什么 | 写什么 | 碰不到什么 |
|---|---|---|---|
| 自动推送（Webhook 自定义机器人） | `notify.webhook_url` / `notify.secret` | 群消息 | 不读会话、不写状态、收不到任何事件 |
| 群查询（`BOT_ROLE=group_query`） | 环境变量 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`；查活动时读 `session.json` | 只有 `bot/data/handled_messages.json`（去重表） | 不写 `users.json`、不查 `mine`、不调 `watchlist.py` |
| owner 私聊（`BOT_ROLE=private_owner`） | 环境变量 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `PRIVATE_OWNER_OPEN_ID` | `users.json`（私聊会话）、`watchlist.json`（预报名） | 不回应群消息（群消息进进程就被丢掉） |

`notify.feishu_app` 只给 Python 侧的**私聊**用（预报名卡片、结果回报、卡片原地更新），
所以它必须填 owner 私聊应用的凭据；群查询应用只需要环境变量。

### 1.4 运行时会生成的状态文件

| 文件 | 谁写 | 语义 | 能否手删 |
|---|---|---|---|
| `data/state.json` | run/scan | 推送进度（`announced` / `last_push_ts` / `reminded_signup`） | 删了会重推，不建议 |
| `data/activity_watch.json` | activity_watch | 公开活动的发现时刻、上次状态、待推送事件 | 删了会把现有活动全当「首次发现」重发一轮 |
| `data/watchlist.json` | bot / runner | owner 的预报名清单 | 删了等于放弃所有预报名 |
| `bot/data/users.json` | owner 实例 | 私聊收件人表（只记 p2p） | 删了私聊静默，需本人重新私聊一次 |

---

## 2. 环境变量

### 2.1 Python 脚本（cron 调用）

| 变量 | 默认 | 谁读 | 作用 |
|---|---|---|---|
| `SCHOOL_ORIGIN` | `https://xg.tjise.edu.cn` | `common.py` | 学校站点根地址，**换学校只改这一处**（`API_BASE`、`HOME_URL` 都由它拼） |
| `PRIVATE_OWNER_OPEN_ID` | 空 | `notify.py`、`grab_signup.py`、`bot.js` | owner 的 `open_id`；空 = 私聊全部关闭，`private_owner` 实例拒绝启动 |
| `WATCHLIST_FILE` | `server/data/watchlist.json` | `watchlist.py` | 预报名清单路径（测试用） |
| `GRAB_SCRIPT` | `/root/tjise-login/grab_signup.py` | `watchlist_runner.py` | 抢报名脚本绝对路径 |
| `GRAB_STATUS_DIR` | `server/data` | `watchlist_runner.py` | `systemd-run` 退出码落盘目录 |
| `TRIGGER_PORT` | `18080` | `trigger_server.py` | 一键触发服务端口 |
| `TRIGGER_TOKEN` | 空 | `trigger_server.py` | 触发口令；**为空时服务拒绝启动** |
| `PYTHONIOENCODING` | — | 全部 | 建议 `utf-8`（Windows 控制台打印符号会崩） |

> `activity_watch.py` 不需要任何环境变量：轮询间隔在 `config.json` 的 `watch` 段，状态文件固定在 `data/`。
> 时区也不靠环境变量——业务时间统一按 `common.business_now()`（固定 +08:00）算，
> 服务器时区设错也不会把「待开放」判成「已结束」。

### 2.2 机器人进程（两个 systemd 单元）

| 变量 | 默认 | 群查询实例 | owner 私聊实例 | 作用 |
|---|---|---|---|---|
| `BOT_ROLE` | 空 | `group_query` | `private_owner` | **必填**，缺失或写错直接拒绝启动 |
| `FEISHU_APP_ID` | 空 | 必填 | 必填 | 各自应用的 ID |
| `FEISHU_APP_SECRET` | 空 | 必填 | 必填 | 各自应用的密钥 |
| `PRIVATE_OWNER_OPEN_ID` | 空 | 不用 | **必填** | owner `open_id`；空则该实例拒绝启动 |
| `APP_DIR` | `/root/tjise-activity-watch` | 必填 | 必填 | 调 Python 脚本的工作目录 |
| `PY_BIN` | `/usr/bin/python3` | 可选 | 可选 | Python 解释器路径 |
| `BOT_OPEN_ID` | 空 | 可选 | 可选 | 机器人的 `open_id`，留空则启动时调 `/open-apis/bot/v3/info` 自动取 |

两个实例的 `BOT_ROLE` 必须是不同的值：同值双开只会抢同一批事件，
而漏写 `BOT_ROLE` 的旧单实例单元在新版会直接退出（这是有意的，见 `docs/FEISHU_BOT.md` §0）。

### 2.3 变量注入位置

cron 里没有 systemd 的环境变量，必须**写在同一行**（或 crontab 顶部统一 export）：

```cron
*/15 * * * * cd /root/tjise-activity-watch && PRIVATE_OWNER_OPEN_ID=ou_xxx /usr/bin/python3 scripts/token_probe.py >> data/token_probe.log 2>&1
```

需要 owner 变量的 cron 任务：`run.py --with-grab`（07:00）、`remind.py`（07:05）、`token_probe.py`、`net_probe.py`、`watchlist_runner.py`。
其余（10:30/16:30/21:30 推送、`scan.py`、`activity_watch.py`、`token_report.py`）只发群或不发消息，不需要。
`activity_watch.py` 连 token 都不用配：它读 `data/session.json`，和 `fetch.py` 同一条登录态。

---

## 3. 三套可直接抄的示例

### 3.1 只要群推送（最小可用）

```json
{
  "notify": {
    "channel": "飞书群",
    "platform": "webhook",
    "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/xxxx",
    "secret": ""
  }
}
```

效果：07:00/10:30/16:30/21:30 四轮日报 + 冲刺提醒。**没有** @ 查询、私聊、预报名。

### 3.2 全功能服务器

```json
{
  "notify": {
    "channel": "webhook",
    "platform": "feishu",
    "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/xxxx",
    "secret": "",
    "feishu_app": {
      "app_id": "cli_owner_xxxxxxxx",
      "app_secret": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    },
    "users_file": "bot/data/users.json",
    "private_chat_id": "oc_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
  },
  "filter": {
    "keyword_blocklist": ["测试", "已取消"]
  },
  "watch": {
    "enabled": true,
    "day_seconds": 180,
    "night_seconds": 600
  }
}
```

配合两个机器人单元的 systemd 环境变量（见 2.2）与 crontab（见 2.3）即为线上形态。
三件事必须同时成立，否则会出现「半开」状态：

1. Webhook 填了 → 群里有日报和动态事件；
2. `tjise-bot-group` 起来了 → 群里 @ 能查；
3. `tjise-bot-owner` 起来了且 `PRIVATE_OWNER_OPEN_ID` 与 `users.json` 的键一致 → 私聊能收预报名卡片。

只满足 1、3 不满足 2：群里 @ 永远没反应，但私聊照常收卡片——这类半开状态最容易被误判成「机器人挂了」。

### 3.3 本机版

```json
{
  "feishu_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/xxxx"
}
```

---

## 4. 校验与常见配置错误

```bash
cd server
python3 -c "import sys;sys.path.insert(0,'scripts');import notify;print(list(notify.load_config()))"
python3 scripts/selfcheck.py          # 凭据、缓存、锁、卡片构造
```

| 现象 | 多半是 |
|---|---|
| 群里收不到推送，日志有 `code=11232` | 整点限流，不是配置错；等重试或错峰 |
| 群里收不到推送，日志有 `19001` | `app_secret` 错或应用未发布 |
| 机器人进程起来就退出，日志 `BOT_ROLE 缺失或非法` | 单元没写 `BOT_ROLE`，多半是沿用了旧的单实例单元 |
| 群里 @ 没反应，日志 `chat type not allowed for role` | 群消息发到了 owner 实例（或反之），检查群机器人绑的是哪个应用 |
| 群里点预报名提示「只提供查询」 | 群入口只读，这是设计；要报名走 owner 私聊或本机工具 |
| 动态监测不产生事件，`data/activity_watch.json` 里 `next_poll_at` 还没到 | 正常：每分钟唤醒不等于是每分钟抓取，按 `watch.*_seconds` 控频 |
| 动态监测报 `activity_watch.json 解析失败` | 状态文件被写坏；原文件未改动，修复或删除后重启（删除会重发一轮首次发现） |
| 私聊收不到，日志有 `[i] 未配置 PRIVATE_OWNER_OPEN_ID` | 那条 cron/systemd 没注入变量 |
| 预报名按钮点击无效 | `PRIVATE_OWNER_OPEN_ID` 与 `users.json` 的键不一致，或卡片被转发到别的会话 |
| 活动被过滤掉了 | `filter.keyword_blocklist` 命中标题（只影响推送，动态监测仍会记录状态） |
| 换学校后全 404 | `SCHOOL_ORIGIN` 没改，或该校接口结构不同（本项目按天软学生端适配） |

---

## 5. 安全约定

- `config.json`、`credentials.json`、`storage_state.json` 权限 **600**，且都在 `.gitignore` 里。
- 日志与卡片不含账号、密码、验证码、token、姓名；`PRIVATE_OWNER_OPEN_ID` 是本人公开账号标识，可以出现在 systemd 单元里，但不写进卡片正文。
- **owner-only 是结构性的，不是文案上的**：`private_owner` 实例缺 `PRIVATE_OWNER_OPEN_ID` 不启动；卡片动作要「点击人 + 会话」双重一致；群实例根本没有个人视图与个人动作的代码路径。其他部署者自行配置自己的 owner，本项目不提供多用户模型。
- 不要把 `config.json` 贴到群里或提 PR —— 需要别人帮排查时，粘贴前先删掉 `app_secret` / `webhook_url` / `chat_id`。
