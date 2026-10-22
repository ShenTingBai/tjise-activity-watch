# 配置与环境变量参考

所有可调项都在这一页。改之前先弄清楚三件事：

1. **配置文件只有两处**：`server/config.json`（服务器版）和 `local/config.json`（本机版，可选）。两者都不入库。
2. **环境变量不进配置文件**：凭据类、路径类、owner 身份走环境变量（systemd 的 `Environment=` 或 crontab 行首）。
3. **改完要重启对应进程**：cron 任务下一轮自动生效，systemd 服务要 `systemctl restart`。

---

## 1. 配置文件

### 1.1 `server/config.json`（服务器版）

从 `server/config.example.json` 复制而来。所有字段都在 `notify` 与 `filter` 两个对象下。

| 键 | 类型 | 必填 | 默认/留空时 | 作用 | 代码位置 |
|---|---|---|---|---|---|
| `notify.channel` | string | 否 | — | 通道显示名，只用于日志区分（如 `飞书群`） | `notify.py` |
| `notify.platform` | string | **是** | — | 走哪条通道：`feishu`（应用/webhook 统一入口）或 `webhook`（纯群机器人） | `notify.py` `send()` |
| `notify.webhook_url` | string | 二选一 | 留空则不走 webhook | 群自定义机器人的 Webhook 地址 | `notify.py` `send()` |
| `notify.secret` | string | 否 | `""` | Webhook 开启「签名校验」时的密钥 | `notify.py` |
| `notify.feishu_app.app_id` | string | 用私聊/@ 时必填 | `""` | 自建应用 App ID（`cli_` 开头） | `notify.py` / `bot.js` |
| `notify.feishu_app.app_secret` | string | 同上 | `""` | 自建应用 App Secret | 同上 |
| `notify.feishu_app.api_base` | string | 否 | `https://open.feishu.cn/open-apis` | 飞书 API 根地址（换测试环境才改） | `notify.py` |
| `notify.users_file` | string | 用私聊时必填 | `bot/data/users.json` | 机器人记录的会话表，私聊 chat_id 从这里找 | `notify.py` `_p2p_chat_id()` |
| `notify.private_chat_id` | string | 否 | 取 `users.json` 里 owner 那条 | owner 私聊会话 ID；填了必须与 `users.json` 一致，否则私聊静默 | `notify.py` `_p2p_chat_id()` |
| `filter.keyword_blocklist` | string[] | 否 | `[]` | 活动标题关键词黑名单，命中即不推送 | `fetch.py` / `run.py` / `scan.py` |

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

---

## 2. 环境变量

### 2.1 Python 脚本（cron 调用）

| 变量 | 默认 | 谁读 | 作用 |
|---|---|---|---|
| `SCHOOL_ORIGIN` | `https://xg.tjise.edu.cn` | `common.py` | 学校站点根地址，**换学校只改这一处**（`API_BASE`、`HOME_URL` 都由它拼） |
| `PRIVATE_OWNER_OPEN_ID` | 空 | `notify.py`、`grab_signup.py` | owner 的 `open_id`；空 = 私聊全部关闭 |
| `WATCHLIST_FILE` | `server/data/watchlist.json` | `watchlist.py` | 预报名清单路径（测试用） |
| `GRAB_SCRIPT` | `/root/tjise-login/grab_signup.py` | `watchlist_runner.py` | 抢报名脚本绝对路径 |
| `GRAB_STATUS_DIR` | `server/data` | `watchlist_runner.py` | `systemd-run` 退出码落盘目录 |
| `TRIGGER_PORT` | `18080` | `trigger_server.py` | 一键触发服务端口 |
| `TRIGGER_TOKEN` | 空 | `trigger_server.py` | 触发口令；**为空时服务拒绝启动** |
| `PYTHONIOENCODING` | — | 全部 | 建议 `utf-8`（Windows 控制台打印符号会崩） |

### 2.2 机器人进程（systemd `tjise-bot.service`）

| 变量 | 默认 | 作用 |
|---|---|---|
| `FEISHU_APP_ID` | 空 | 应用 ID（必填） |
| `FEISHU_APP_SECRET` | 空 | 应用密钥（必填） |
| `PRIVATE_OWNER_OPEN_ID` | 空 | owner `open_id`；空 = 预报名按钮点了也无效 |
| `APP_DIR` | `/root/tjise-activity-watch` | 调 Python 脚本的工作目录 |
| `PY_BIN` | `/usr/bin/python3` | Python 解释器路径 |
| `BOT_OPEN_ID` | 空 | 机器人的 `open_id`，留空则启动时调 `/open-apis/bot/v3/info` 自动取 |

### 2.3 变量注入位置

cron 里没有 systemd 的环境变量，必须**写在同一行**（或 crontab 顶部统一 export）：

```cron
*/15 * * * * cd /root/tjise-activity-watch && PRIVATE_OWNER_OPEN_ID=ou_xxx /usr/bin/python3 scripts/token_probe.py >> data/token_probe.log 2>&1
```

需要 owner 变量的 cron 任务：`run.py --with-grab`（07:00）、`remind.py`（07:05）、`token_probe.py`、`net_probe.py`、`watchlist_runner.py`。其余（10:30/16:30/21:30 推送、`scan.py`、`token_report.py`）只发群，不需要。

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
    "channel": "飞书群",
    "platform": "feishu",
    "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/xxxx",
    "secret": "",
    "feishu_app": {
      "app_id": "cli_xxxxxxxx",
      "app_secret": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    },
    "users_file": "bot/data/users.json",
    "private_chat_id": "oc_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
  },
  "filter": {
    "keyword_blocklist": ["测试", "已取消"]
  }
}
```

配合 systemd 环境变量（见 2.2）与 crontab（见 2.3）即为线上形态。

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
| 私聊收不到，日志有 `[i] 未配置 PRIVATE_OWNER_OPEN_ID` | 那条 cron/systemd 没注入变量 |
| 预报名按钮点击无效 | `PRIVATE_OWNER_OPEN_ID` 与 `users.json` 的键不一致 |
| 活动被过滤掉了 | `filter.keyword_blocklist` 命中标题 |
| 换学校后全 404 | `SCHOOL_ORIGIN` 没改，或该校接口结构不同（本项目按天软学生端适配） |

---

## 5. 安全约定

- `config.json`、`credentials.json`、`storage_state.json` 权限 **600**，且都在 `.gitignore` 里。
- 日志与卡片不含账号、密码、验证码、token、姓名；`PRIVATE_OWNER_OPEN_ID` 是本人公开账号标识，可以出现在 systemd 单元里，但不写进卡片正文。
- 不要把 `config.json` 贴到群里或提 PR —— 需要别人帮排查时，粘贴前先删掉 `app_secret` / `webhook_url` / `chat_id`。
