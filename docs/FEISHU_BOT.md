# 飞书机器人：三个角色，三条权限边界

这份文档解决服务器版**唯一一个不看文档就绝对跑不通**的环节：飞书这边要建什么、开什么权限、订阅什么事件、配置从哪抄。

> 界面名称以 2026-10 的飞书开放平台为准。飞书改版频繁，如果某一步找不到，按「它让你填什么」反推——下面每一步都写了**为什么**。

---

## 0. 先分清三个角色（这一步错了，后面全白配）

系统里有**三个**机器人，权限从「只能发消息」到「能替你报名」，必须分开建、分开跑：

| | A. 自动推送 | B. 群查询 | C. owner 私聊 |
|---|---|---|---|
| 建在哪 | 群设置 → 群机器人 → 自定义机器人 | [开发者后台](https://open.feishu.cn/app) 创建自建应用 | 另一个自建应用（推荐）或同一个 |
| 拿到什么 | 一个 `webhook_url` | `app_id` + `app_secret` | `app_id` + `app_secret` |
| 进程角色 | 无进程（Webhook 直发） | `BOT_ROLE=group_query` | `BOT_ROLE=private_owner` |
| 能做什么 | 只推送群消息 | 群里被 @ 后**只读查询** | 只服务本人：预报名、取消、到点报名 |
| 不能做什么 | 收不到任何消息 | 任何报名动作、任何个人数据 | 回应群消息（群消息进来就被丢掉） |
| 配置位置 | `config.notify.webhook_url` | systemd 环境变量 | `config.notify.feishu_app` + systemd 环境变量 |
| 配置耗时 | 2 分钟 | 15-30 分钟（含管理员审核） | 15-30 分钟 |

三条边界是硬性的，不是建议：

1. **公共群只读**：群里 @ 一下只能拿到「待开放报名 / 报名已开放 / 全部」三种查询结果，按钮也只有这三个视图。群里点「预报名」会被拒绝，文案是「群里只提供查询；报名动作只对本人私聊开放」。
2. **私聊只认一个主人**：`PRIVATE_OWNER_OPEN_ID` 那一个人的私聊才可能触发预报名、取消、报名。别人私聊机器人问「我的报名」，得到的是固定拒绝——不透露任何个人数据，也不引导去别的通道。
3. **角色必须显式声明**：`BOT_ROLE` 缺失或写错，进程**直接拒绝启动**。宁可少一个机器人在线，也不要出现「一个进程同时拥有两种权限」的隐式入口。

> 一个飞书应用能不能同时跑两个角色？技术上可以，但建议拆成两个：同一个应用双开会互相抢事件（同一件事被两个进程各答一次），而且一旦有人把群机器人拉进新群，群入口就会继承应用的全部权限。

---

## 1. 角色 A：群自定义机器人（只推送）

1. 打开要接收推送的飞书群 → 右上角 `···` → **设置** → **群机器人** → **添加机器人**
2. 搜索 **自定义机器人** → 添加
3. 起个名字（如「校园活动推送」）→ 复制 **Webhook 地址**
4. 若开启「签名校验」，把签名密钥也记下
5. 填进 `server/config.json`：

```json
{
  "notify": {
    "channel": "webhook",
    "platform": "feishu",
    "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx",
    "secret": "开启了签名校验才填，否则留空"
  }
}
```

6. 验证（在 `server/` 目录下）：

```bash
python3 scripts/notify.py "机器人连通测试"
```

群消息到了就说明 A 完成。**注意：自定义机器人收不到任何消息**，所以它做不了 @ 查询，也发不了私聊。

> 动态监测（`scripts/activity_watch.py`）产生的「活动动态」卡片也走这条 Webhook，和日报、临近提醒同一条通道。

---

## 2. 角色 B / C：两个自建应用

### 2.1 创建应用，拿凭据

1. 打开 <https://open.feishu.cn/app> → **创建企业自建应用**
2. 填名称（如「校园活动查询」「校园活动助手」）、描述、图标 → 创建
3. 进入应用 → **凭证与基础信息** → 复制 **App ID**（`cli_` 开头）和 **App Secret**

两个应用分别填到不同位置——这是最容易混的地方：

| 应用 | 凭据填到哪 | 谁用 |
|---|---|---|
| 群查询应用 | `tjise-bot-group.service` 的 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | `bot.js`（`BOT_ROLE=group_query`） |
| owner 私聊应用 | `server/config.json` 的 `notify.feishu_app`，以及 `tjise-bot-owner.service` 的 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | `notify.py`（私聊卡片）+ `bot.js`（`BOT_ROLE=private_owner`） |

为什么 owner 应用要额外写进 `config.json`：Python 侧只发**私聊**（预报名卡片、结果回报、卡片原地更新），它必须用「能和 owner 私聊的那个应用」的凭据，否则消息发出去对方收不到。

> App Secret 等同密码。它只出现在 `server/config.json` 与 systemd 环境变量里，`config.json` 已在 `.gitignore` 中，**永远不要提交**。

### 2.2 开启机器人能力

两个应用都要：应用详情 → **添加应用能力** → 找到 **机器人** → 添加。

不开这一步，应用发不出任何消息（`im.message.create` 会返回权限错误）。

### 2.3 申请权限（按角色分开开）

**群查询应用**（只读，权限最小）：

| 权限码 | 控制台里的名字（大意） | 干什么用 | 不开会怎样 |
|---|---|---|---|
| `im:message:send_as_bot` | 以应用的身份发消息 | 回查询卡片 | 一条消息都发不出去 |
| `im:message.group_at_msg:readonly` | 接收群聊中 @ 机器人消息 | 群里 @ 查询 | 群里 @ 了也不理 |

**owner 私聊应用**（有个人动作）：

| 权限码 | 控制台里的名字（大意） | 干什么用 | 不开会怎样 |
|---|---|---|---|
| `im:message:send_as_bot` | 以应用的身份发消息 | 私聊卡片、结果回报 | 一条消息都发不出去 |
| `im:message.p2p_msg:readonly` | 读取用户发给机器人的单聊消息 | 私聊里问「我的报名」、点预报名 | 私聊发过去毫无反应 |

补充说明：

- **卡片按钮回调不需要单独开权限**，它是「回调配置」里的事件（见 2.4）。
- **不需要**通讯录权限：机器人的 `open_id`、用户的 `open_id`、群的 `chat_id` 都能直接从消息事件里拿到，本项目不用通讯录接口。
- 群查询应用**不要**开 `im:message.p2p_msg:readonly`：它没有私聊入口，开了只是多一份权限。

### 2.4 事件订阅：必须选「长连接」

这是最容易踩错的一步。

两个应用都要：应用详情 → **事件与回调** → **事件配置**：

1. 订阅方式选 **使用长连接接收事件**（不要选「将事件发送至开发者服务器」，那需要公网地址和验签）
2. 添加事件：
   - 群查询应用：`im.message.receive_v1`
   - owner 私聊应用：`im.message.receive_v1`
3. 同一页面的 **回调配置** 里添加（两个应用都加，群查询应用加了也只会收到查询按钮的回调）：
   - `card.action.trigger`（卡片按钮点击）

**为什么是长连接**：机器人进程在自己机器/服务器上主动连飞书，不需要公网 IP、不需要开 80/443、不需要 HTTPS 证书。代价是**机器人进程必须一直活着**——进程停了，消息就积压在那里，等进程起来才补投。

选错成「发送到开发者服务器」的典型症状：配置保存不了（要求填 URL），或者填了内网地址后事件永远收不到。

### 2.5 发布版本，等管理员审核

应用详情 → **版本管理与发布** → **创建版本** → 填版本号（如 `1.0.0`）→ 申请发布。

- 自己是管理员：直接通过
- 不是管理员：需要企业管理员在「管理后台 → 应用审核」里通过

**没发布的应用，权限和事件都不生效**——配置全对但机器人一声不响，九成是卡在这里。

### 2.6 把机器人拉进群、和它私聊

1. 群查询应用：群里 → 设置 → 群机器人 → 添加 → 选择它
2. owner 私聊应用：在飞书搜索应用名，直接发一条消息（如 `帮助`）

> 私聊必须先由**你本人**给机器人发过一条消息，机器人才能记住你的 `chat_id`（飞书的限制：应用不能凭空给陌生人发私聊）。这也是 `PRIVATE_OWNER_OPEN_ID` 那套「先学后用」的原因。

### 2.7 收集 chat_id 与 open_id

这一步不用调接口，机器人自己会记。**先让 owner 私聊实例跑起来**，然后：

1. **私聊** 给 owner 应用发任意一句话（例如 `帮助`）
2. 看文件 `server/bot/data/users.json`：

```json
{
  "ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx": {
    "open_id": "ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "chat_type": "p2p",
    "chat_id": "oc_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "seen_at": "2026-10-22T02:15:31.000Z"
  }
}
```

| 要的值 | 从哪拿 | 填到哪 |
|---|---|---|
| 你的 `open_id`（`ou_` 开头） | `users.json` 的键 | 两个 unit 的 `PRIVATE_OWNER_OPEN_ID` |
| 你的私聊 `chat_id`（`oc_` 开头） | 同一条记录的 `chat_id` | `config.notify.private_chat_id`（可留空） |
| 群 `chat_id` | 机器人日志里 `query: ... | group | ...` 那行 | 一般不用填，群推送走 webhook |

> `users.json` 只记**私聊**记录。现在群消息根本进不到 owner 实例（`BOT_ROLE` 门禁先挡掉），所以「群消息把 p2p 记录覆盖成 group」这条老问题从结构上消失了。

### 2.8 填配置与环境变量

`server/config.json`：

```json
{
  "notify": {
    "channel": "webhook",
    "platform": "feishu",
    "webhook_url": "角色 A 的地址",
    "feishu_app": {
      "app_id": "cli_owner_xxxxxxxx",
      "app_secret": "xxxxxxxx"
    },
    "users_file": "bot/data/users.json",
    "private_chat_id": "oc_xxxxxxxx"
  },
  "watch": {
    "enabled": true,
    "day_seconds": 180,
    "night_seconds": 600
  }
}
```

`tjise-bot-group.service`：

```ini
Environment=BOT_ROLE=group_query
Environment=FEISHU_APP_ID=cli_group_xxxxxxxx
Environment=FEISHU_APP_SECRET=xxxxxxxx
Environment=APP_DIR=/root/tjise-activity-watch
```

`tjise-bot-owner.service`：

```ini
Environment=BOT_ROLE=private_owner
Environment=FEISHU_APP_ID=cli_owner_xxxxxxxx
Environment=FEISHU_APP_SECRET=xxxxxxxx
Environment=PRIVATE_OWNER_OPEN_ID=ou_xxxxxxxx
Environment=APP_DIR=/root/tjise-activity-watch
```

三项必须一致：`PRIVATE_OWNER_OPEN_ID` = `users.json` 的键 = 私聊记录里的 `open_id`；`private_chat_id` = 那条记录的 `chat_id`。

> `BOT_ROLE` 缺少或写成别的值，进程会打印 `BOT_ROLE 缺失或非法` 并退出；`private_owner` 角色缺 `PRIVATE_OWNER_OPEN_ID` 同样拒绝启动——没有主人的私聊机器人等于「谁都能用的报名机器人」。

### 2.9 验证顺序（别跳步）

```bash
# 1) 两个角色分别起来了吗
systemctl status tjise-bot-group tjise-bot-owner --no-pager
journalctl -u tjise-bot-owner -n 20 --no-pager   # 应看到「长连接已启动｜角色 private_owner」
journalctl -u tjise-bot-group -n 20 --no-pager   # 应看到「长连接已启动｜角色 group_query」

# 2) 应用凭据对吗
cd /root/tjise-activity-watch && python3 scripts/selfcheck.py

# 3) 私聊能发出去吗
PRIVATE_OWNER_OPEN_ID=ou_xxx python3 -c "
import sys; sys.path.insert(0,'scripts'); import notify
print(notify.send_private('私聊通道测试'))"

# 4) 群里 @ 一下，看日志有没有 'role: group_query'
journalctl -u tjise-bot-group -f

# 5) 角色门禁自检（不建立长连接、不发消息）
BOT_ROLE=group_query node bot.js --selftest
BOT_ROLE=private_owner PRIVATE_OWNER_OPEN_ID=ou_xxx node bot.js --selftest
```

---

## 3. 排错对照表

| 现象 | 错误码/日志 | 原因 | 处理 |
|---|---|---|---|
| 进程起来就退出 | `BOT_ROLE 缺失或非法` | systemd 单元没有 `BOT_ROLE`（多半是沿用了旧单实例单元） | 换成 `tjise-bot-group` / `tjise-bot-owner` 两个单元 |
| 进程起来就退出 | `private_owner 角色缺少 PRIVATE_OWNER_OPEN_ID` | owner 实例没注入主人 open_id | 补环境变量，或改 drop-in |
| 群里 @ 没反应 | 日志 `chat type not allowed for role private_owner group` | 群消息发到了 owner 实例 | 检查群机器人绑的是哪个应用；群查询要单独的实例 |
| 群里点按钮提示「群里只提供查询」 | — | 群入口只读，这是设计 | 正常保护，不是 bug |
| 私聊发不出去 | `99992351` | `open_id` 不是这个应用可见的用户，或填错 | 用 `users.json` 里的键，别手抄 |
| 发消息报错 | `230002` / 机器人不在群 | 应用没被拉进群，或群 ID 错 | 群里重新添加机器人 |
| 卡片发不出去 | `200671` | 卡片 JSON 不合法（如按钮 value 用了 dict） | 用 `card.py` 里的构造器，别手写 |
| 推送失败 | `11232` | 整点系统压力限流（10:00、17:30 前后高发） | 代码已有 1.5/3/6s 重试；仍失败就错峰 |
| 群里 @ 没反应 | 日志无 `query:` 行 | 没订阅 `im.message.receive_v1`，或订阅方式不是长连接 | 回 2.4 |
| 群里 @ 没反应 | 日志有 `no-openid` | `/open-apis/bot/v3/info` 没取到 `BOT_OPEN_ID` | 检查应用是否已发布 |
| 点了按钮没反应 | 无任何日志 | 没订阅 `card.action.trigger` | 回 2.4 的回调配置 |
| 按钮点了提示「只对私聊里的本人有效」 | — | 点击人不是 `PRIVATE_OWNER_OPEN_ID`，或卡片被转发到别的会话 | 正常保护，不是 bug |
| 一切正常但私聊静默 | `[i] 未配置 PRIVATE_OWNER_OPEN_ID，私人提醒已关闭` | cron/systemd 没注入环境变量 | 见 CONFIG.md 环境变量表 |
| token 类错误 | `19001` / `99991663` | 应用凭据错，或 tenant token 过期 | 检查 `app_secret`，换掉后重启服务 |

---

## 4. 安全边界

三种入口的登录来源、可见数据与允许动作（部署者会话是唯一的学校登录）：

| 入口 | 登录来源 | 可见数据 | 允许动作 | 禁止动作 |
|---|---|---|---|---|
| 群 webhook 自动推送 | 服务器上的部署者会话 | 公开活动字段 | 只发消息 | 任何报名动作 |
| 群 @ 查询（group_query） | 服务器上的部署者会话 | 公开活动字段 | 只读回答 | 任何报名动作、收集成员学校凭证 |
| 私聊机器人（private_owner） | 服务器上的部署者会话 | 部署者本人的预报名清单 | 部署者本人的报名与取消 | 替群成员报名 |

- **群成员不登录学校系统**：群查询是部署者会话的只读代答，不是匿名访问学校接口；
  也不要在群里向机器人提供学校账号或密码。
- **App Secret 不入库**：只在 `server/config.json` 与 systemd 环境变量里；`config.json` 必须 600。
- **权限最小化**：群查询应用只开「发消息 + 群 @ 消息」两项；它没有私聊权限，也没有回调以外的任何写权限。
- **机器人只服务本人**：预报名/报名动作要 `PRIVATE_OWNER_OPEN_ID` + 点击人 + 会话三重一致；别人私聊机器人问「我的报名」，会得到「只能本人查」。
- **公共群不承诺任何动作**：群卡片只写活动状态与时间，不写「提交次数」「已报名」这类个人执行结论。
- **应用不要对外发布到应用广场**：这是自用工具，公开发布会带来审核与合规负担。

---

## 5. 一句话记住分工

**自动推送只发消息，群查询只回答问题，owner 私聊才动手。** 三者共用的只有同一份学校公开活动数据。

想在自己电脑上报一两场、不想碰这些：用本机版 `local/`，它不需要创建应用，登录一次就能用。
