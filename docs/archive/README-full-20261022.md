# 智慧元小元 · 学生活动定时抓取

每天早/中/晚自动抓取 `xg.tjise.edu.cn` 学生端里「可报名」的活动，生成日报并播报。

## 为什么这么实现

站点是 uni-app 单页应用，后端接口在 `/dr/api` 前缀下（不是 `/api`，那个路径被 nginx 挡死，
任何裸请求都 403）。接口本身是普通的 REST，只要带上 `token` 头就能直接取数据，
所以不需要每次都开浏览器——只有登录这一步用浏览器。

登录 token 存在前端的 `localStorage.xgUniToken` 里，浏览器还可能通过 cookie 维持续期状态。`login.py` 用真实 Chrome 读一次，
把 token 存到 `data/session.json`；`fetch.py` 之后就用这个 token 直接发请求。

## 接口

```
POST https://xg.tjise.edu.cn/dr/api/rjyHd/listActivity
Header: token: <xgUniToken>
Body:   {"pageSize":100,"pageNum":1,"hdmc":"","hdzt":null,"jssj":"","kssj":""}
```

返回 `{"code":1,"data":{"count":287,"list":[...]}}`，单条字段：

| 字段 | 含义 |
|------|------|
| `id` | 活动 ID |
| `hdmc` | 活动名称 |
| `hdlx` | 类型（实践学习 / 宿舍文化 / 就业创业） |
| `hddd` | 地点 |
| `hdkssj` / `hdjssj` | 活动起止时间 |
| `bmkssj` / `bmjssj` | **报名**起止时间 |
| `bmzt` | 报名状态：0 未报名，1 已报名 |
| `hbzj` | 海报文件 ID |

token 失效时返回 `{"code":4001,"message":"身份验证失败！"}`。

### 站点地址只配一处（换学校用）

`scripts/common.py` 里的 `SCHOOL_ORIGIN` 是**唯一**的学校域名来源，
`API_BASE`、`ACTIVITY_API`、`HOME_URL`、`REFERER` 全由它拼出来：

```python
SCHOOL_ORIGIN = (os.environ.get("SCHOOL_ORIGIN") or "https://xg.tjise.edu.cn").rstrip("/")
API_BASE = f"{SCHOOL_ORIGIN}/dr/api"
ACTIVITY_API = f"{API_BASE}/rjyHd/listActivity"
HOME_URL = f"{SCHOOL_ORIGIN}/stu/"
```

`login.py`、`explore.py`、`probe_login.py` 都从 `common` 导入这份值，不在各自文件里另写域名。
Node 侧（`deploy/server_refresh.js`）读的是**同一个环境变量**，默认值也相同，
但它是独立的 JS 文件、没法 import Python 常量：**改默认学校要同时改 `scripts/common.py`
和 `deploy/server_refresh.js`**，否则两边会指向不同域名。

换学校时一般不用改代码，设环境变量即可：

```bash
export SCHOOL_ORIGIN="https://new-school.example.edu.cn"
```

注意 `export` 只对**当前 shell** 生效。cron 和 systemd 各有自己的环境：
crontab 要在任务行前写 `SCHOOL_ORIGIN=...`（或写在文件头的变量区），
systemd unit 要加 `Environment=SCHOOL_ORIGIN=...`。只 export 不写这两处，
定时任务仍然用默认域名——表现为「手动跑能通，cron 里一直抓旧学校」。

默认值仍是 `https://xg.tjise.edu.cn`，不设环境变量时行为和以前完全一致。
端点路径（`/dr/api/rjyHd/listActivity`、`/stu/`）是站点自己的路由，不随 origin 变化；
`fetch.py` 的字段映射（`hdmc`/`bmkssj` 等）也是这所学校特有的。换成路由或字段不同的站点，
除了 origin 还要改字段映射和解析逻辑——这个开关只解决「同一套系统、换个域名」的情况。

改完跑 `python scripts/test_endpoints.py`，它会分别验证默认值和覆盖值，
并静态检查几个入口脚本里没有残留写死的域名。

## 目录

```
tjise-activity-watch/
├─ config.json              # 过滤规则 + 推送配置
├─ scripts/
│  ├─ login.py              # 登录 / 同步 token（会话有效时静默完成）
│  ├─ fetch.py              # 调接口 + 过滤 + 生成日报
│  ├─ notify.py             # 推送到飞书/钉钉/企微/QQ
│  ├─ run.py                # 定时任务入口：抓取 → 推送
│  ├─ common.py             # 配置、会话、状态读写
│  └─ explore.py            # 排查用：重新探索接口（页面改版时用）
├─ bot/
│  ├─ bot.js                # 飞书机器人：解析意图、查询、回卡片
│  └─ handled_messages.js   # 已处理消息 ID 的持久化去重表
├─ data/
│  ├─ session.json          # token 与用户信息
│  ├─ chrome-profile/       # Chrome 登录态（token 失效时用它静默刷新）
│  ├─ state.json            # 见过的活动，用来标记「新增」
│  └─ latest.json           # 最近一次结构化结果
└─ reports/                 # 运行时生成，删掉会自动重建，不进仓库
   ├─ latest.md             # 最新日报
   └─ YYYY-MM-DD.md         # 按天归档
```

`config.json`、`data/`、`bot/data/`、`reports/` 都是运行时文件，`.gitignore` 已排除；
仓库里只放代码和文档。`bot/data/` 是机器人自己的状态（去重表），和 `data/` 分开，
这样同步 `data/session.json` 时不会连带覆盖机器人状态。

## 用法

```bash
# 登录（过期时才需要，平时会自动静默同步）
python scripts/login.py

# 只看不推
python scripts/run.py --dry

# 正常跑一轮
python scripts/run.py
```

## 过滤规则

推送目标是**男生宿舍 5 人群**，所以过滤规则全部固定，不做成个人化配置：

| 规则 | 说明 |
|------|------|
| 只推待开放报名 | 报名已开始 / 已截止 / 活动已开始的一律不推 |
| 过滤女生场次 | 账号性别是男，名字里带「(女生)」的场次直接跳过 |
| 不按本人报名状态过滤 | 群是公用的，账号主人报了名不影响别人看到 |
| `keyword_blocklist` | 唯一可配置项，名称命中这些词就跳过，默认空 |

`config.json` 因此只剩一个 `filter.keyword_blocklist`。

## 退出码

| 码 | 含义 | 处理 |
|----|------|------|
| 0 | 正常 | — |
| 1 | 配了推送但发送失败 | 检查 `config.json` → `notify` |
| 2 | token 失效 | 重跑 `scripts/login.py` |
| 3 | 接口异常 / 解析不出活动 | 跑 `scripts/explore.py` 看接口 |
| 4 | `state.json` 损坏 | 从备份恢复，或删掉重建（会重新推一遍） |
| 5 | 没配推送通道，消息没发出去 | 填 `notify.channel` 或 `notify.webhook_url` |

## 推送配置

> 当前状态：**已接入飞书群机器人**，`config.json` 里 Webhook 已填好，可直接推送。
> 换群或换通道时改 `notify` 即可。

在 `config.json` 的 `notify` 里选一种通道。**群机器人 Webhook 不需要授权，拿到地址填进去就行。**

### 飞书群机器人（推荐）

1. 飞书群 → 设置 → 群机器人 → 添加机器人 → 自定义机器人
2. 复制 Webhook 地址，填到 `webhook_url`
3. 如果开了「签名校验」，把密钥填到 `secret`

```json
"notify": {
  "channel": "webhook",
  "platform": "feishu",
  "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/xxxx",
  "secret": ""
}
```

### 钉钉群机器人

群设置 → 智能群助手 → 添加机器人 → 自定义 → Webhook。
开了「加签」就把密钥填 `secret`，脚本会自动算签名。

```json
"platform": "dingtalk",
"webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=xxxx"
```

### 企业微信群机器人

群设置 → 群机器人 → 添加 → 复制 Webhook。

```json
"platform": "wecom",
"webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx"
```

### QQ（OneBot v11）

需要自己跑 NapCat / Lagrange / go-cqhttp，填接口地址和群号：

```json
"channel": "qq",
"qq": { "api_base": "http://127.0.0.1:3000", "token": "", "group_id": "群号" }
```

### 没配会怎样

`channel` 为空或 `webhook_url` 为空时**不发送、也不静默当成功**，返回退出码 5 并在日志里写明。
这是有意的设计：每天四轮都「成功」却一条消息都收不到，是最难发现的一种故障，
所以宁可让 cron 把非零退出码暴露出来。
配了但发送失败返回退出码 1。

## 部署到 Linux 服务器（7×24，不依赖本机）

脚本只用 Python 标准库（urllib / json / datetime），服务器上**不需要装任何第三方包**，
Python 3.6+ 即可（已实测 Alibaba Cloud Linux 3 自带的 3.6.8 可正常运行）。

```bash
# 本机执行，一条命令装完（含连通性自检 + 写 crontab）
SSH_KEY=~/.ssh/goal.pem bash deploy/deploy.sh root@<旧服务器IP>

# 非 22 端口再加
SSH_KEY=私钥路径 SSH_PORT=2222 bash deploy/deploy.sh root@1.2.3.4
```

它会做四件事：打包上传 → 服务器上验证 Python 和时区 → 真实请求一次接口确认 token 可用 →
写入 4 条清单任务（07:00 / 10:30 / 16:30 / 21:30）、1 条私人提醒（07:05）
和 1 条全天每 5 分钟的报名扫描任务。

装完路径在 `~/tjise-activity-watch`，日志在 `data/cron.log`。

### token 过期怎么办

在本机重新登录，然后只同步会话文件：

```bash
python scripts/login.py            # 本机（浏览器会话还在的话会静默刷新）
bash deploy/sync_token.sh root@1.2.3.4
```

`sync_token.sh` 会先在本机验证 token 有效，再上传并在服务器端复验，避免传上去才发现是坏的。

### 当前部署状态

已部署到阿里云轻量服务器 `<旧服务器IP>`：

| 项 | 值 |
|----|-----|
| 路径 | `/root/tjise-activity-watch` |
| 日志 | `data/cron.log` |
| 定时 | 清单 07:00 / 10:30 / 16:30 / 21:30；私人 07:05；扫描 每 5 分钟（全天） |
| 时区 | CST（已确认） |
| Python | 3.6.8（系统自带，未额外安装） |

服务器出网到 `xg.tjise.edu.cn` 与 `open.feishu.cn` 均已实测通畅。

### 部署时要整组更新的文件

单个文件单独上传很容易把服务器留在「一半新一半旧」的状态，下面两组必须成对替换：

| 原子单元 | 包含 | 为什么要一起换 |
|----------|------|----------------|
| 机器人 | `bot/bot.js` + `bot/handled_messages.js` | `bot.js` 里 `require('./handled_messages')`，只传一个会启动失败或行为不一致 |
| token 续期 | `deploy/server_refresh.js` + `deploy/atomic_json.js` | `server_refresh.js` 用 `atomic_json.js` 的 `atomicWriteJson()` 写会话，缺了会在 `require` 阶段就报错 |

`bot.js` 的去重缓存位置是 `bot/data/handled_messages.json`（不是 `data/`），
更新代码不会动它；要清空去重记录就删这个文件再重启服务。

### 模板与 dist

`deploy/make_template.sh` 从当前工作区打一个可分发模板（`dist/campus-activity-bot-template*.zip`）：

```bash
bash deploy/make_template.sh
```

它现在**逐项显式复制**依赖文件（`bot/bot.js`、`bot/handled_messages.js`、两个机器人测试、
`deploy/server_refresh.js`、`deploy/atomic_json.js`、`deploy/test_atomic_json.js` 等），
缺任何一个都会让脚本因 `set -e` 直接失败——以前那行 `2>/dev/null || true` 会把缺文件悄悄放过，
打出来的包里少东西，装到服务器上才暴露。模板里的 README 和服务说明也写了
`PRIVATE_OWNER_OPEN_ID`，照着模板部署不会漏掉这个变量。

`dist/` 里现有的模板 zip 是**旧版本**，不含上述新增文件；要分发就先重跑
`deploy/make_template.sh`，不要直接搬旧包。

## 一键触发（手机上点一下）

不想等到下次定时，想立刻看最新活动时，打开这个链接：

```
http://<你的域名>/<TRIGGER_TOKEN>
```

打开后会执行一次抓取，把结果推到飞书群，同时网页上直接显示抓到了什么。
存成手机浏览器书签，随时点。

- 60 秒冷却，连点不会刷屏
- 路径里那串就是口令，别人不知道就打不开（访问其他路径会被 nginx 拦掉）
- 走的是 nginx 反代到 `127.0.0.1:18080`，服务本身不直接对外，服务器防火墙没动过

### 实现方式

`scripts/trigger_server.py` 是个只依赖标准库的小 HTTP 服务（systemd 管理，名为 `tjise-trigger`），
监听 `127.0.0.1:18080`。nginx 里加了两条 location：

- `https://<你的域名>/<TOKEN>` → 反代到触发服务
- `http://<你的域名>/<TOKEN>` → 同上（因为站点证书已过期，走 HTTP 避免安全警告）

配置备份在 `/www/server/panel/vhost/nginx/html_<旧服务器IP>.conf.bak-tjise*`，
出问题直接把备份覆盖回去再 `nginx -s reload`。

### 换口令

```bash
# 服务器上改环境变量后重启
systemctl edit --full tjise-trigger     # 改 TRIGGER_TOKEN
systemctl daemon-reload && systemctl restart tjise-trigger
```
改完记得同步更新 nginx 里 location 的路径，否则旧链接仍然可达。

## 飞书机器人（群里 @ 一下）

除了定时推送和一键链接，还部署了一个飞书机器人：在群里 @ 它 说「活动」，
它会抓一次最新数据并回复（回复内容不重复推群，只走机器人消息）。

### 架构

- 代码：`bot/bot.js`，服务器路径 `/root/tjise-bot`
- 依赖：`@larksuiteoapi/node-sdk`（Node 20）
- 服务：`tjise-bot`（systemd，`Restart=always`）
- 接入方式：**长连接（WebSocket）**，不需要公网域名和证书
- 收到 `im.message.receive_v1` → 执行 `scripts/query.py` 查询 → 回复卡片或摘要
- 消息去重**落盘**，进程重启后飞书重投的旧消息不会再答一遍（见下节）

### 开放平台需要开的配置

1. **应用功能 → 机器人**：启用（否则 API 返回 `app do not have bot`）
2. **权限管理**，开通：
   - `im:message`（获取与发送单聊、群组消息）
   - `im:message.group_at_msg:readonly`（接收群聊中 @机器人 消息）
   - `im:message.p2p_msg:readonly`（**接收用户发给机器人的单聊消息**，
     没有这条私聊消息进不来，个人查询就用不了）
   - `im:message:send_as_bot`（以应用身份发消息）
3. **事件与回调 → 订阅方式**：选「使用长连接接收事件」，并添加事件
   `im.message.receive_v1`（接收消息）
4. **创建版本 → 申请发布**，管理员审核通过后才生效
5. 在目标群：群设置 → 群机器人 → 添加机器人 → 选择该应用

### 两个机器人，职责分开

| 机器人 | 干什么 | 触发条件 |
|--------|--------|----------|
| 自定义机器人（webhook） | 定时推送、告警 | 只它主动发，从不读消息 |
| 应用机器人（长连接） | 回应提问、卡片按钮 | **只有被 @ 才动** |

群里有室友在聊天，所以应用机器人必须做到「不 @ 它，它完全不存在」：

- 群聊：`message.mentions` 里必须包含本机器人 `open_id`，否则直接 return，一个字节都不发
- 私聊：不需要 @（私聊本身就是对它说的）
- 自己的 `open_id` 在启动时查一次 `/open-apis/bot/v3/info`，取不到时**不放开限制**

### 只处理真人消息

飞书会把**群里其他机器人发的消息**也推给我们的应用（`sender_type` 为 `bot`）。
如果不挡，webhook 定时推送一到群里，机器人就会把它当成用户提问再回一张卡片，
看起来像「自动推送触发了 @机器人」。

`bot.js` 的 `isUserMessage()` 只放行 `sender.sender_type === 'user'`：
`app`、`bot`，以及**缺失该字段的消息一律跳过**。字段缺失说明事件结构变了
（SDK 换版本、模拟事件），这时不能默认当真人处理，否则这道门会悄悄失效。

> 实测：webhook 发一条文本后，日志出现 `skip non-user message: bot text`，没有回复。

### 消息去重（跨进程重启）

飞书会重投事件，进程重启后也可能再收到同一批旧消息。以前去重只存在内存 `Set` 里，
`systemctl restart tjise-bot` 一重启就清空，群里会看到机器人把刚才的问题又答一遍。

现在由 `bot/handled_messages.js` 落盘：

| 项 | 值 |
|----|----|
| 位置 | `bot/data/handled_messages.json`（Linux 下目录 0700、文件 0600） |
| 内容 | 只有 `message_id` 和毫秒时间戳，**不存消息正文、用户 ID、凭证** |
| 保留 | 48 小时过期，最多 2000 条，超出按时间淘汰最旧的 |
| 保证范围 | 只覆盖这个窗口：超过 48 小时或已被淘汰的消息，飞书再投会当作新消息处理 |
| 写入 | 先写临时文件再 rename，重启时不会读到写了一半的 JSON |

两个边界值得记住：

- **只有处理成功才记**。回卡片/回文本失败时这条消息不落盘，飞书重投还能再试一次；
  否则一次失败的查询会被永久当成「答过了」，用户等半天什么也没有。
- **同一进程内的并发重投靠 `inFlight` 挡**：两条事件几乎同时到达时文件里还没有它，
  只靠文件挡不住，会重复执行两次。

缓存文件解析失败时**不会静默重建**（静默清空等于把这 48 小时的记录全丢掉，
重启后群里突然重复回复，日志里还看不出是缓存坏了）。现在这一步发生在**启动时**：
`bot.js` 在建立长连接之前先把缓存读一遍，坏文件会让进程直接 `exit 1`，
systemd 日志里能看到 `[bot] 去重缓存无法加载：cannot parse handled message cache ...`。
**修好（或从备份恢复）那个文件之后重启服务**才会恢复——进程不会自己重试，
也不会拿空表顶上。文件不存在属于正常首次启动，不报错。

自检：`node bot/test_handled_messages.js`，用例覆盖跨实例重启去重、处理失败可重试、
返回值非 `true`（如 `"ok"`、`1`）不算成功、处理函数抛错可重试、并发同 ID 只跑一次、
过期+容量淘汰、损坏文件 fail-closed、临时目录生命周期。

### 在群里怎么用

直接 @ 它 说关键词，不用打命令：

| 说什么 | 得到什么 |
|--------|----------|
| 「待报名」／随便说 | 还没开放报名的活动 |
| 「报名中」 | 已开放、还能报的（名额可能已满，碰运气） |
| 「全部」 | 两种都列 |
| 「类型」 | 有哪些活动类型和各自数量 |
| 「实践学习」「宿舍文化」「就业创业」 | 按类型筛 |
| 「帮助」 | 菜单 |

类型和视图能组合，比如「就业 报名中」= 就业创业里已经开放报名的。

解析逻辑在 `bot/bot.js` 的 `parseIntent()`，查询在 `scripts/query.py`：
后面这个用 `pending` / `open` / `all` / `types` 加 `--type` 参数，命令行也能直接用。

### 运维

```bash
systemctl status tjise-bot
journalctl -u tjise-bot -f          # 实时日志
systemctl restart tjise-bot         # 改了 App Secret 之后重启
```

## 推送节奏

**清单只列「还没开放报名」的活动。**

已开放报名的、活动已经开始的一律不推——名额往往在开放后几分钟内就没了，
这时候推过来已经来不及。抢名额靠下面的冲刺提醒，清单只负责让你知道「什么时候该去抢」。

具体过滤条件（`fetch.py`）：

- 报名已经开始（`bmkssj <= now`）→ 排除
- 活动已经开始（`hdkssj <= now`）→ 排除
- 报名已截止（`bmjssj <= now`）→ 排除
- 已经报过名、性别不符、命中黑名单 → 排除

| 时点 | 内容 |
|------|------|
| 07:00 | 待开放报名清单 |
| 10:30 | 待开放报名清单 |
| 16:30 | 待开放报名清单 |
| 21:30 | 待开放报名清单 + **晚归签到提醒**（22:45–23:59） |

**不做「无变化精简」。** 早期版本在没有变化时只发一行摘要，
但卡片是有结构的（标题/条目/页脚），一行摘要没法表达，
结果主通道发全文、文本兜底发一行——反而制造了差异。现在统一发完整版，
条目本来就少（通常 1-2 条），不至于让人觉得吵。

### 公用群的两条规矩

推送目标是**男生宿舍 5 人群**，所以有两处刻意不做「个人化」：

1. **个人数据只在私聊里给，而且群里不出现任何「私聊」字样。**
   机器人用的是绑定账号的 token，查出来的报名记录只属于这个账号。
   群里问「我的」只回一句「报名记录属于个人数据，不在群里展示」，**不引导去私聊**
   ——避免室友看到后误会还有别的通道。
   私聊时按 `chat_type === 'p2p'` 放行，查 `query.py mine`；本机也可 `python scripts/query.py mine`。
2. **不按「本人是否已报名」过滤活动。** 清单和冲刺提醒都是给全群看的，
   不能因为账号主人报了名，就让其他人看不到这个活动。

**判断标准**：只要一个功能依赖 `bmzt`（报名状态）或 `session`（个人身份），
它就只能出现在私聊里，不能进群。

### 推送与回复共用一套版式

定时推送（webhook）和 @ 回复（应用机器人）**由同一个函数 `card.pending_card()` 生成**，
所以在群里看到的版式、字段顺序、措辞完全一致：

```
标题：待开放报名 N 项
副标题：更新于 HH:MM
正文：▸ 10-21 12:00 开放 / 名称 / 类型｜地点 / 截止｜活动
页脚：开放前 10 分钟会单独提醒
```

- 条目渲染统一走 `_pending_md()`，字段顺序固定：**开放时间 → 名称 → 类型｜地点 → 截止｜活动**
- 页脚用 `BASE_FOOTER` 常量，改一处两边同时变
- 推送独有的上下文（另外 N 项可报名、晚归签到）作为**页脚追加行**，不破坏主体结构

**唯一差异：推送不带按钮。** webhook 自定义机器人的卡片，按钮回调不会回到应用，
放上去就是死按钮。宁可没有，也不给一个点了没反应的入口。

`selfcheck.py` 里有一项 `check_parity()` 专门守这个：把同一批数据分别喂给
`digest_card()` 和 `pending_card()`，刨掉按钮区后逐字段比对，不一致就报错。
这样以后改文案时，两边不会悄悄跑偏。

### 卡片按钮与群内菜单

**飞书的机器人自定义菜单只在单聊显示，群聊里不显示**（官方限制）。
所以群里用「按钮卡片」代替菜单：@它 说「菜单」，返回一张带 `待报名 / 报名中 / 全部`
三个按钮的卡片，点按钮原地刷新，不用打字。

按钮回调走 `card.action.trigger`，需要后台一次性配置：

1. 开发配置 → 事件与回调 → **回调配置** → 订阅方式选「使用长连接接收回调」
2. 在「已订阅的回调」里添加 **卡片回传交互（card.action.trigger）**
3. 创建版本并发布，然后 `systemctl restart tjise-bot`

> 只加权限/事件不重启长连接是不生效的——这点已经踩过一次。

按钮的 `value` 用的是字符串（`v:pending`），飞书部分版本对 dict 值会报 200671。

### token 缓存

`notify._tenant_token()` 把 `tenant_access_token` 缓存到 `data/token_cache.json`，
有效期 7200 秒，提前 5 分钟刷新。实测首次申请 0.138 秒，缓存命中 0.000 秒。
文件权限 `600`。

### 测试

**离线测试**（不读 `config.json`/`data/`、不连网、不启动浏览器，可以在任何目录里跑）：

```bash
node bot/test_handled_messages.js      # 去重表：重启/重试/并发/过期/坏文件
node bot/test_bot_runtime.js           # 机器人：owner 绑定、查询失败、去重、启动失败
node deploy/test_atomic_json.js        # 原子写：新建/覆盖/rename 失败/临时文件清理
python scripts/test_notify.py          # 45 项推送成败判定（按平台区分业务码）
python scripts/test_runtime_safety.py  # owner 绑定、通知返回值、锁内占位回滚、扫描分批
python scripts/test_endpoints.py       # SCHOOL_ORIGIN 默认值与覆盖值
```

**联网 / 写真实数据的脚本**（会请求学校接口，`test_dedup.py` 还会备份并改写真实
`data/state.json`；只在服务器或本机有有效登录态时手动跑，CI 和隔离验证里不要调用）：

```bash
python3 scripts/selfcheck.py   # 状态完整性 / 锁 / 并发 / token 缓存 / 卡片大小
python3 scripts/regress.py     # 16 项回归：清单、扫描、私人提醒、各视图、防重复
python3 scripts/test_dedup.py  # 去重基线：会跑 fetch.py 并临时改写 data/state.json
node bot.js --selftest         # 机器人：意图解析、卡片构造、按钮回调（会真的跑查询）
```

### 卡片消息

飞书消息优先用 **interactive 卡片**发送（`scripts/card.py`），比纯文本多出：
标题栏配色（待开放蓝、可报名橙、我的报名绿）、条目间分隔线、页脚注。

卡片构造或发送失败时**自动退回纯文本**，不会因为格式问题丢消息——`run.py` 里
`send_card(card, msg)` 的第二个参数就是兜底文案。

**唯一的例外是飞书整点限流**：`code=11232`（官方文档：系统压力导致的限流）会让
卡片被直接拒收。`notify._post_retry` 只对这种可重试的业务码退避重发（1.5s / 3s / 6s，
最多 4 次），内容错、鉴权错不重发。2026-10-22 10:30 那次「样式变成纯文本」就是
11232 限流后退回兜底文案，不是卡片本身坏了（同一份卡片走应用私聊通道可正常渲染）。

机器人在群里回复同样是卡片：`query.py --format card` 输出卡片 JSON，
`bot.js` 拿到后按 `msg_type: interactive` 发送，失败再退回文字。

### 文案格式

统一由 `scripts/render.py` 排版，清单、机器人、冲刺提醒共用一套。每条活动固定 3-4 行：

```
📋 待开放报名 1 项

▸ 10-20 12:00 开放
　基于 Spring Cloud 的微服务电商平台设计与实现
　实践学习｜教学A301
　截止 10-20 17:59｜活动 10-20 18:30

开放前 10 分钟会单独提醒。
```

- 第一行是**什么时候该动手**（待开放看「开放时间」，已开放看「截止时间」）
- 名称独占一行，不和类型挤在一起
- 类型/地点/时间降级为小字辅助行，全角空格缩进
- 时间统一写成 `10-20 12:00` 这种绝对日期，不用「今天/明天」——群里翻记录时相对说法会失真
- 最多列 **8 条**（`common.MAX_LIST_ITEMS`），超出补一行「……另有 N 项」。
  卡片和纯文本兜底**共用同一个上限**，否则同一批活动两条通道条数不同，会被当成漏推
- 记入「已推送」的也只有这 8 条：没显示出来的活动下次仍会带 🆕，
  不会因为被折叠就再也不提

### 报名冲刺提醒（`scan.py`）

每 5 分钟扫一次，发现「有活动在 10 分钟内开放报名」就单独推一条短消息，
实际提前量在 5–10 分钟之间。已提醒过的活动记在 `state.json` 的 `reminded_signup` 里，
不会重复喊，记录保留最近两天。

命中多的时候**按 5 条一批**发：00:00 开闸那种时刻可能一次命中十几条，
塞进一条消息会刷屏，也会让最后几条来不及看。每批独立处理，
「读 state 判断有没有提醒过 → 渲染 → 发送 → 写 reminded_signup」都在**同一把锁里**，
所以两个重叠的 cron 进程不会各发一遍；发送失败的那一批不写占位，下个周期整批重试。

**扫描是全天的**，不是只在白天。原因：有些活动的报名是在 `00:00` 开闸的
（例如 `bmkssj = 10-19 00:00:00`），如果扫描 7 点才开始，到 7 点它已经算「已开放」、
会被清单排除，等于两条路都漏掉。全天扫描能在 23:55 就发现「5 分钟后开放」。
每天约 288 次请求，接口本身很快。`PAGE_SIZE=500`：接口一次就能把全部活动
（2026-10 时 285 条）给全，所以每轮只打 1 个请求，不再按 100 条翻三页。

### 晚归签到提醒

签到规则（从系统接口读到的）：

| 时段 | 时间 |
|------|------|
| 早签 | 06:30 – 08:30 |
| 晚签 | 20:00 – 22:44 |
| 晚归 | 22:45 – 23:59 |

按目前约定**只提醒晚归**，且并进 21:30 那条清单的末尾，不单独发。
文案是通用的（「晚归的同学记得签」），因为这是宿舍群广播，不是个人状态查询。

## 私人提醒（只在私聊）

`scripts/remind.py` 每天 **07:05** 检查「我已报名、且活动就在今天」的场次，
有就发一条**私聊卡片**（绿色标题栏），没有就静默退出。卡片发不出去时退回下面这段纯文本。

```
🎟 今天（10-20）有 1 个活动要参加

▸ 18:30
　基于 Spring Cloud 的微服务电商平台设计与实现
　实践学习｜教学A301
```

**为什么走私聊**：报名状态 `bmzt` 属于个人数据，群里另外三个人看不到、也不需要看。
这条线完全不碰群推送，走 `notify.send_private()` → `im/v1/messages`。

需要的配置（`config.json` → `notify`）：

| 键 | 说明 |
|----|------|
| `feishu_app.app_id` / `app_secret` | 拿来换 tenant_access_token |
| `users_file` | 机器人记录的发送者表，从中取私聊会话 ID |
| `private_chat_id` | 也可以直接写死私聊会话 ID，优先用这个 |

### 私人查询只对 owner 开放

个人报名记录不再「谁都查」，由环境变量 `PRIVATE_OWNER_OPEN_ID` 指定唯一的主人：

- 它和 `BOT_OPEN_ID`（机器人自己的 open_id）**是两个不同的东西**，不要混用。
  `BOT_OPEN_ID` 只用于判断群里有没有 @ 到机器人。
- `bot` 服务（systemd）和 **07:05 的私人提醒 cron** 都要注入这个变量。
  只给一边配的话，会出现「群里不回答、但每天私聊照发」这种半开状态。
- 变量缺失或为空 = 私人查询**整体关闭**（不是退回「谁是第一个私聊的人」）：
  bot 会回固定拒绝文案，`send_private()` 直接返回 `None` 并且不发任何请求。
- 群聊里说「我的」永远只回「不在群里展示」，也不会引导你私聊——
  群消息和私聊是两条独立的授权路径。
- 要让机器人能主动私聊，先由 owner 本人私聊它一次，`users.json` 里建立
  `chat_type: "p2p"` 的记录。群消息不会写这条记录（不会覆盖已有的 p2p 映射）。
- 如果显式配了 `private_chat_id`，它必须和 owner 记录里的 `chat_id` 一致；
  不一致时按「配置冲突」处理、不发送，只在日志里说明原因，不会退而求其次挑一个会话。

### 私聊收不到消息怎么查

1. 权限里必须有 `im:message.p2p_msg:readonly`，且**改完要重新发布版本**
2. **发布后要重启机器人**：长连接建立时绑定的是当时的权限，
   不重启的话旧连接收不到单聊事件
3. 让机器人先收到一条私聊，`/root/tjise-bot/data/users.json` 里有了记录，才谈得上主动推送

## 预报名（私聊点按钮，到点自动报名）

07:00 那轮群推送之后，机器人会**在你自己的私聊**里再发一张「预报名清单」卡片：
每条待开放活动后面一个「预报名」按钮。看中哪一场就点一下，到开放时间自动去抢，
结果私聊告诉你。

```
┌ 预报名清单 3 项 ──────────────┐
│ ▸ 10-23 19:00 开放            │
│   网络安全·数字时代大长城      │
│   实践学习｜教学 A301          │
│   [ 预报名 ]                    │
└────────────────────────────────┘
```

**群聊里永远没有这张卡**：按钮、预报名状态、报名结果都属于个人操作，
群卡片保持纯公共信息（名称/类型/地点/时间），群里所有人看到的内容一致。

卡片上**只会出现还能报的活动**：已经报上名的、报名已截止的、活动已开始的，
一律不进卡片。留着「已报名 + 预报名按钮」这种组合，只会让人怀疑点了会不会重复报名。

### 点一下之后发生了什么

1. 按钮回调带过来活动 id、名称、开放时间，机器人先校验「点击人 = owner」且「会话 = owner 的私聊」，
   任一不满足直接拒绝（卡片被转发给别人也点不动）。
2. 写入 `data/watchlist.json`，状态 `queued`，回复一条私聊确认。
3. **同一张卡片原地更新**：那一项的按钮变成「取消预报名」，正文补一句「已预报名 ✅」。
   飞书只能按 message_id 整卡覆盖，所以发送时把列出的条目存进 `data/grab_card.json`
   （消息 ID + 当时的条目快照），点按钮时按快照重建整卡再 `PATCH` 回去。
   快照丢了也不影响报名动作，只是按钮不会翻转。
4. `scripts/watchlist_runner.py` 每分钟跑一次，到「开放时间 − 100 秒」时用
   `systemd-run` 拉起 `grab_signup.py`（脱离 cron 生命周期，重启不影响那一场）。
5. 抢完按退出码标记 `done` / `failed`，抓取脚本本身会私聊回报结果；
   那一项同时从卡片上撤掉（已报名的活动不再挂按钮）。
6. 反悔：点同一张卡片上的「取消预报名」，或

```bash
python3 scripts/watchlist.py remove --id <活动id>
python3 scripts/watchlist.py list          # 看清单和状态
```

### 为什么不用一次性 timer

以前每场活动都要人工 `systemd-run --on-calendar` 排一次，人一忘就漏，
而且「谁排了哪一场」只存在于 systemd 状态里。现在清单是唯一入口：

| 关注点 | 位置 |
|---|---|
| 卡片能不能原地改 | `data/grab_card.json`（消息 ID + 条目快照，0600） |
| 想报哪些 | `data/watchlist.json`（人可以点按钮改） |
| 什么时候拉起 | `scripts/watchlist_runner.py`（cron 每分钟） |
| 这一场结果 | `data/grab_signup.log`、`data/grab-<id前8位>.status` |

清单和 `state.json` 分开放：后者是抓取/推送进度，清空去重状态时不能连带把预报名清掉。

## 服务器自动续期（不依赖个人电脑）

token 寿命有限（当前样本约 17.5 小时，实际以 data/token_probe.log 的 15 分钟探测为准），
靠「人在电脑前重新登录」不现实。现在由服务器自己续：

- 代码：`deploy/server_refresh.js`，服务器路径 `/root/tjise-login/server_refresh.js`
- 依赖：`playwright` + Chromium（约 120MB），系统库 `at-spi2-atk / mesa-libgbm / at-spi2-core`
- 调度：systemd `tjise-login.service`，保持一个无头浏览器页面常驻；每 2 小时验证一次
- 原理：用无头浏览器带着 `storage_state.json`（兼容旧版 localStorage 快照，成功续期后自动保存 cookies + localStorage 完整快照）长期打开页面，
  让 SPA 自己续期，再读回 `xgUniToken`，**校验接口通过后才写入** `data/session.json`
- 浏览器状态变化时才写回快照，每分钟只读页面状态，不额外请求学校接口；一次性检查仍可运行：
  `/usr/bin/node /root/tjise-login/server_refresh.js`

**不需要密码，也不需要过验证码**——登录页虽有 4 位图形验证码，但这里复用的是浏览器里已有的登录态，
只有当这个状态彻底失效时才会需要人工介入。

### 状态失效了怎么办

1. 电脑上跑 `python scripts/login.py` 手动登录一次
2. 把新的登录态快照（localStorage + cookies）传到服务器：
   ```bash
   scp data/storage_state.json root@服务器:/root/tjise-login/
   ```
3. 服务器立刻跑一次 `/usr/bin/node /root/tjise-login/server_refresh.js` 确认恢复

### 失效监控与告警

- `scripts/token_probe.py` 每 15 分钟探测一次（cron `*/15 * * * *`），只追加一行到 `data/token_probe.log`；
  `scripts/token_report.py` 再把它汇总成 `reports/token-lifetime.md`，失效时刻精确到 15 分钟。
- 连续 2 次非 ok 才告警：单次失败多半是网络抖动；同一失效周期只私聊 owner 一次，
  恢复后自动清零；私聊通道不可用时才退回群通道。
- 告警复用探针那一次接口调用，**不新增学校接口请求**；文案不含 token、姓名、学号。
- 告警状态存 `data/token_probe_state.json`，与业务去重用的 `state.json` 完全隔离。
- `scripts/net_probe.py` 每 5 分钟做一次出网自检（cron `*/5 * * * *`），只 GET `/stu/`
  这个静态页、不带 token、不碰业务接口，用来把「断网」和「登录态失效」分开：
  连续 2 次失败才告警，文案会区分「整机断网」（学校站点和普通网站都连不上）
  与「只是学校站点不通」；学校站点不通时会补探一次普通网站作为对照。
  状态存 `data/net_probe_state.json`，日志 `data/net_probe.log`。

### 资源占用

跑完不会留进程（实测 `pgrep chrome-headless` 为 0），整机内存常驻约 1.2GB / 3.5GB。

> 这两个数是**抓取结束之后的单次只读快照**读到的整机常驻内存，**不是 Chromium 运行峰值**。
> 抓取那几十秒里浏览器会把内存顶高一截，快照那个时刻已经退掉了；
> 要判断服务器够不够用，得在抓取过程中连续采样（带日期的 `free -b` 记录），
> 只看这两个数会低估峰值。

## 定时任务

**执行位置是服务器 cron，本机不需要开着。**

WorkBuddy 里原有的 3 个任务（早间/午间/晚间）已**暂停**，避免和服务器重复推送
（两端的去重状态各自独立，同时跑会推两条）。需要改回本机执行时，把这三个任务重新启用并把
服务器 crontab 删掉即可。
