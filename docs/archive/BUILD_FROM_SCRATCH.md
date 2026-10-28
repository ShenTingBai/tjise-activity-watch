# 校园活动助手 · 从零搭建指南

> **这份文档给谁看**
> 给「想在同校复刻一套」的同学，以及他手上的 AI 助手。
> 全文不含任何真实账号、密钥、服务器地址，所有这类信息一律写成 `<占位符>`，
> 由使用者自己填写。**看到占位符就说明这里必须换成你自己的，不能照抄。**

---

## 0. 交给 AI 时的开场白

把整份文档发给你的 AI，然后这样说：

> 我要按这份文档搭一套校园活动推送。我的信息如下：
> - 学校系统已注册，学号 `<学号>`
> - 飞书是我校在用的（或个人版可用）
> - 我有一台 Linux 云服务器，IP `<IP>`，root 权限，系统版本 `<版本>`
> - 我的宿舍群已有 `<N>` 人
>
> 请按文档顺序执行，每完成一个阶段先跑该阶段的验收清单，
> **验收不通过不要继续往下做**。遇到需要我手动操作的地方（浏览器登录、飞书后台点按钮），
> 停下来告诉我具体点哪里。

**给 AI 的硬性约束**（请务必遵守）：

1. 不要跳过第 1 阶段直接写代码——数据源不通，后面全是白费。
2. 所有密钥只写进服务器本地配置文件，权限 `600`，**不要写进代码、不要提交到 Git**。
3. 涉及飞书后台配置的步骤，人来点，AI 只负责验证结果。
4. 每阶段结束必须实际跑一次，贴出真实输出作为证据，不允许"应该可以了"。
5. 不要复用别人的 token / webhook / 服务器，那是别人的东西。

---

## 1. 系统全景

```
┌─────────────┐   登录态    ┌──────────────┐   抓取    ┌──────────────┐
│ 学校门户     │ ─────────> │ 抓取脚本      │ ────────> │ 活动数据      │
│ (浏览器)     │  token      │ (Python)     │           │ JSON / 摘要   │
└─────────────┘             └──────┬───────┘           └──────┬───────┘
                                   │                          │
                        定时任务 / 事件触发                     │
                                   ▼                          ▼
                        ┌──────────────────────────────────────────┐
                        │              飞书                         │
                        │  ① 群自定义机器人（webhook，单向推送）      │
                        │  ② 自建应用机器人（长连接，可 @ 交互）      │
                        └──────────────────────────────────────────┘
```

**这套系统里有两个机器人，职责严格分开，不要合并：**

| 机器人 | 干什么 | 触发方式 |
|--------|--------|----------|
| 自定义机器人（webhook） | 定时推送、告警 | **只它主动发**，从不读消息 |
| 应用机器人（长连接） | 响应提问、卡片按钮 | **只有被 @ 才动**，不 @ 绝不响应 |

> 这是硬性要求，不是优化项：群里有室友在聊天，一个会自己插话的机器人非常烦人。
> 应用机器人必须做到「**不 @ 它，它就完全不存在**」。实现见 6.5 第 0 条。

---

## 2. 准备清单

| 项目 | 说明 | 谁准备 |
|------|------|--------|
| 学校系统账号 | 能登录门户、能看到活动列表 | 你 |
| 一台 Linux 云服务器 | 1 核 2G 起，能访问外网 | 你 |
| 飞书群 | 一个你和目标用户都在的群 | 你 |
| 飞书自建应用 | 开放平台创建，需管理员审核发布 | 你 |
| 本地电脑 | 用于首次登录、导出登录态 | 你 |

服务器建议：Alibaba Cloud Linux 3 / Ubuntu 20.04+ / Debian 11+ 均可。
本文命令以 **Alibaba Cloud Linux 3 + Python 3.6 + Node 20** 为基准，
其他系统把 `dnf` 换成 `apt` 即可。

---

## 3. 第 1 阶段：验证数据源（必做，别跳）

**目标**：确认能在不打开浏览器的情况下拿到活动数据。

### 3.1 找接口

1. 用 Chrome 登录学校门户，进入活动列表页。
2. 打开开发者工具 → Network → 勾选 Fetch/XHR → 刷新页面。
3. 找到返回活动列表的那条请求，记录：
   - **URL**（例：`https://<域名>/dr/api/<模块>/listActivity`）
   - **Method**（POST / GET）
   - **请求体字段**（分页、筛选条件）
   - **鉴权方式**：看 Header 里是 `token`、`Authorization` 还是 Cookie

4. 再找一条「详情」请求，确认活动字段名。

### 3.2 记录关键字段

把返回的 JSON 存一份，逐个确认下列语义（字段名各校不同，这里用常见命名）：

| 含义 | 本项目字段 | 备注 |
|------|-----------|------|
| 活动 ID | `id` | 去重用 |
| 活动名称 | `hdmc` | |
| 活动类型 | `hdlx` | 用于分类筛选 |
| 报名开始 | `bmkssj` | **最关键**，决定"什么时候该抢" |
| 报名截止 | `bmjssj` | |
| 活动开始 | `hdkssj` | 用于排除已开始的活动 |
| 活动结束 | `hdjssj` | |
| 地点 | `hddd` | |
| 我是否已报名 | `bmzt`（1=已报） | 个人数据，不能进群 |
| 海报 | `hbzj` | 可选 |

> ⚠️ **不要假设字段名**。用真实返回的 JSON 校对，少一个字段后面就会静默漏数据。

### 3.3 验收

用 curl 带 token 请求一次，返回 `code: 1` 且列表非空：

```bash
curl -s -X POST "https://<域名>/dr/api/<模块>/listActivity" \
  -H "Content-Type: application/json" \
  -H "token: <你的token>" \
  -d '{"pageSize":10,"pageNum":1,"hdmc":"","hdzt":null,"jssj":"","kssj":""}'
```

**通不过就不要往下走。** 常见原因：字段少了、缺 Referer、token 取错。

---

## 4. 第 2 阶段：本地跑通抓取

### 4.1 目录结构

```
<项目名>/
├── config.json          # 推送配置（含密钥，权限 600）
├── data/
│   ├── session.json     # 登录态
│   ├── state.json       # 去重、防重复推送
│   └── token_cache.json # 飞书 token 缓存
├── scripts/
│   ├── common.py        # 路径、配置、状态读写、锁
│   ├── login.py         # 取登录态
│   ├── fetch.py         # 抓取 + 过滤
│   ├── render.py        # 文案排版
│   ├── card.py          # 飞书卡片
│   ├── notify.py        # 推送（群 webhook / 私聊）
│   ├── run.py           # 定时清单入口
│   ├── scan.py          # 报名冲刺扫描
│   └── remind.py        # 私人提醒
└── bot/bot.js           # 飞书应用机器人（长连接）
```

### 4.2 取登录态

站点通常把登录态放在 `localStorage`。用 Playwright 打开页面读出来：

```python
# 关键逻辑：读 localStorage 里的 token
READ_JS = """() => ({
  token: localStorage.getItem('xgUniToken') || '',
  user: JSON.parse(localStorage.getItem('user') || '{}')
})"""
```

第一次需要人工登录（可能有图形验证码），登录完成后脚本轮询读取，拿到就写入
`data/session.json`。

**验收**：`session.json` 里有 token、姓名、学号。

### 4.3 过滤规则（设计要点，不是可选）

这一节决定了推送有没有价值，务必按此设计：

```
保留条件（全部满足）：
  1. 报名开始时间 > 现在          ← 只推"还没开放报名"的
  2. 报名截止时间 > 现在
  3. 活动开始时间 > 现在          ← 已开始的活动推了没意义
  4. 性别匹配（若活动分男女场次）
  5. 未命中关键词黑名单

不按"我是否已报名"过滤 ← 群是公用的，你报了别人还要抢
```

**为什么只推"待开放"**：名额有限的报名，开放后几分钟就被抢完。
推"已开放"等于告诉用户"你错过了"。开放前 10 分钟的一条冲刺提醒才是真正有用的。

### 4.4 验收

```bash
python3 scripts/fetch.py            # 打印 SUMMARY_JSON_BEGIN ... END
```

输出里的「待开放」条数应该和你在网页上看到的一致。

---

## 5. 第 3 阶段：飞书群推送（webhook）

### 5.1 建机器人

飞书群 → 设置 → 群机器人 → 添加机器人 → **自定义机器人** → 复制 Webhook 地址。

### 5.2 发消息

```python
# 文本
{"msg_type": "text", "content": {"text": "..."}}

# 卡片（推荐）
{"msg_type": "interactive", "card": {
   "config": {"wide_screen_mode": True},
   "header": {"template": "blue",
              "title": {"tag": "plain_text", "content": "标题"}},
   "elements": [
     {"tag": "div", "text": {"tag": "lark_md", "content": "**加粗**\n正文"}},
     {"tag": "hr"},
     {"tag": "note", "elements": [{"tag": "plain_text", "content": "脚注"}]}
   ]}}
```

标题栏配色：信息类 `blue`、提醒类 `orange`、成功 `green`、告警 `red`。

### 5.3 文案排版规则（踩过坑，直接照做）

每条活动固定 3–4 行，**第一行必须是"什么时候该动手"**：

```
📋 待开放报名 2 项

▸ 10-21 12:00 开放
　基于 Spring Cloud 的微服务电商平台设计与实现
　实践学习｜教学A301
　截止 10-21 17:59｜活动 10-21 18:30
```

- 名称独占一行，不要和类型挤在一起
- 类型 / 地点 / 时间降级为缩进辅助行
- **时间一律写绝对日期**（`10-21 12:00`），不要写「明天」——群消息会被往回翻，相对时间会失真
- 空值不占位，不写「—」

### 5.4 推送与回复必须同构

定时推送和 @ 回复是两条通道，但用户看到的是**同一个产品**，
所以两边必须由同一个函数生成，不能各写一套。

```python
def pending_card(items, now, type_filter="", buttons="pending",
                 extra_footer=(), subtitle=None):
    # 标题、副标题、条目、页脚都在这里定义
    ...
```

- 推送时 `buttons=None`（webhook 卡片的按钮回调不回应用，放上去是死按钮）
- @ 回复时带按钮
- 推送独有的提示（另有 N 项可报名、签到提醒）走 `extra_footer`，**只追加页脚，不改主体**

**加一条自检守住它**：同一批数据分别过两个入口，刨掉按钮区后逐字段比对。
不做这一步，两次改文案就会悄悄分叉，而人眼很难发现。

### 5.5 验收

群里出现一张卡片，内容与 `fetch.py` 输出一致。

---

## 6. 第 4 阶段：应用机器人（可 @ 交互）

这一步最麻烦，也最容易卡住。**顺序不能乱**。

### 6.1 建应用 + 开权限

开放平台 → 创建企业自建应用 → 添加能力「机器人」。

**必须开通的权限**（缺一个就有一类功能不可用）：

| 权限 | 作用 | 缺了会怎样 |
|------|------|-----------|
| `im:message` | 收发单聊/群消息 | 什么都做不了 |
| `im:message.group_at_msg:readonly` | 接收群内 @ 消息 | 群里 @ 无反应 |
| `im:message.p2p_msg:readonly` | 接收单聊消息 | **私聊无反应** |
| `im:message:send_as_bot` | 以应用身份发消息 | 推送报权限错误 |

### 6.2 事件订阅

开发配置 → 事件与回调：

- **事件配置**：订阅方式选「使用长连接接收事件」，添加 `im.message.receive_v1`
- **回调配置**：订阅方式选「使用长连接接收回调」，添加 **`card.action.trigger`**（卡片按钮）

### 6.3 发版 + 重启（关键！）

> 改完权限和事件，**必须创建版本并发布**，然后**重启你的长连接进程**。
> 长连接在建立那一刻绑定了当时的权限，不重启永远收不到新权限的事件。
> 这个坑会让人以为配置没生效，浪费大量时间。

### 6.4 机器人代码骨架（Node）

```javascript
const lark = require('@larksuiteoapi/node-sdk');
const ws = new lark.WSClient({ appId, appSecret });
const dispatcher = new lark.EventDispatcher({}).register({

  'im.message.receive_v1': async (data) => {
    // ① 只处理真人消息！机器人发的消息也会推过来
    if ((data.sender?.sender_type || 'user') !== 'user') return;

    // ② 去重（飞书会重投）
    // ③ 取文本：JSON.parse(message.content).text
    // ④ 解析意图 → 查询 → 回复
  },

  'card.action.trigger': async (data) => {
    // 按钮值可能带引号，必须容错
    let v = String(data.action?.value ?? '').trim();
    if (v.startsWith('"') && v.endsWith('"')) v = v.slice(1, -1);
    const m = v.match(/v:([a-z]+)/i);
    // 返回 { toast, card: { type: 'raw', data: 新卡片 } } 可原地刷新
  },
});

ws.start({ eventDispatcher: dispatcher });
```

### 6.5 四条硬规则

0. **群里必须 @ 才响应**。飞书会把群里的消息都推给你的应用，不加这道门，
   室友随口说句话它就会跳出来回一句。

   ```javascript
   function isAddressedToBot(msg, chatType, botOpenId) {
     if (chatType !== 'group') return true;        // 私聊一律响应
     const mentions = msg.mentions || [];
     if (!mentions.length) return false;
     return mentions.some(m => (m.id || {}).open_id === botOpenId);
   }
   ```

   自己的 `open_id` 启动时查一次：`GET /open-apis/bot/v3/info` → `bot.open_id`。
   **取不到时不要放开限制**，宁可启动失败，也不要变成"说话就回"。

1. **过滤非真人消息**。群里有别的机器人（webhook 推送）时，它的消息也会推给你的应用，
   不过滤就会「自动推送 → 机器人自己回复」，看起来像幽灵行为。
2. **按钮 value 用字符串**（如 `v:pending`），不要传对象，部分版本会报 200671；
   且回调返回的值**可能带引号**，解析必须容错。
3. **机器人自定义菜单只支持单聊**，群聊里不显示。群里要用「按钮卡片」代替菜单。

### 6.6 验收

- 群里 @ 它说「帮助」，有回复
- @ 它说「菜单」，出现带按钮的卡片
- 点按钮，卡片原地刷新

---

## 7. 第 5 阶段：定时任务

```cron
# 清单推送
0  7  * * *  cd <项目> && python3 scripts/run.py
30 10 * * *  cd <项目> && python3 scripts/run.py
30 16 * * *  cd <项目> && python3 scripts/run.py
30 21 * * *  cd <项目> && python3 scripts/run.py --late-checkin

# 私人提醒（只发私聊）
5  7  * * *  cd <项目> && python3 scripts/remind.py

# 报名冲刺扫描：每 5 分钟，全天
*/5 * * * *  cd <项目> && python3 scripts/scan.py
```

### 7.1 为什么扫描要全天

有些活动的报名是 `00:00` 开闸的。如果扫描只在白天跑，到早上它已经算「已开放」，
会被清单排除 —— **两条路都漏掉，一次提醒都没有**。
全天扫描才能在 23:55 发现「5 分钟后开放」。

### 7.2 必须做的两个防护

**防重复推送**：`state.json` 记 `last_push_ts`，60 分钟内不重复推。
且要「先占位、再发送、失败回滚」，否则两个进程同时到点会发两条。

**并发写保护**：多个脚本读写同一个 `state.json`，必须加文件锁 + 原子写
（写临时文件再 `os.replace`），否则会互相覆盖、生成损坏的 JSON。

### 7.3 验收

手动跑一次 `run.py`，再立刻跑第二次，第二次应输出「跳过」。

---

## 8. 第 6 阶段：token 自动续期（服务器）

**问题**：token 寿命有限（当前样本约 17.5 小时，实际以 token_probe 为准），人不可能一直守着。

**方案**：把「打开网页读 token」这件事搬到服务器，用无头浏览器。

### 8.1 判断能否用这个方案

先确认登录态存在哪里（浏览器 F12 → Application → Local Storage / Cookies）：

- 如果存在 **localStorage** 且没有 auth cookie → 本文方案适用
- 如果有长期 cookie（几十天）→ 更简单，直接带 cookie 请求即可
- 如果每次都必须过验证码且无长期状态 → 只能半自动，见 8.5

### 8.2 安装

```bash
# 服务器
mkdir -p /root/<项目>-login && cd /root/<项目>-login
npm init -y && npm i playwright
npx playwright install chromium          # 约 120MB

# 缺库时（Alibaba Cloud Linux 3）
dnf install -y at-spi2-atk mesa-libgbm at-spi2-core

# 验证
./node_modules/.bin/playwright --version
```

> 报 `exitCode=127` 基本都是缺上面三个库，`ldd <chrome路径> | grep "not found"` 可以确认。

### 8.3 导出登录态（在你电脑上）

```python
# 读出 localStorage 并存成 JSON
store = page.evaluate(
    "() => Object.fromEntries(Object.keys(localStorage).map(k => [k, localStorage.getItem(k)]))")
json.dump(store, open("storage_state.json", "w"))
```

传到服务器：

```bash
scp storage_state.json root@<IP>:/root/<项目>-login/
```

### 8.4 服务器续期脚本

```javascript
// 核心流程
const browser = await chromium.launch({ args: ['--no-sandbox'] });
const page = await (await browser.newContext()).newPage();
await page.goto('<门户首页>');
await page.evaluate(s => { for (const k in s) localStorage.setItem(k, s[k]); }, store);
await page.reload();
await page.waitForTimeout(6000);           // 等前端自己续期

const token = await page.evaluate(() => localStorage.getItem('xgUniToken'));
// ★ 必须用真实接口验证，不能只看"读到了"
const ok = await verifyToken(token);       // 调一次列表接口，code === 1
if (ok) writeSession(token);
```

> **少走弯路的两个点**：
> ① 校验接口的请求体要和抓取脚本**完全一致**，少字段返回的不是"登录失效"而是别的错误码，容易误判；
> ② 接口路径别猜，从 Network 面板复制。

调度：`20 */2 * * * cd /root/<项目>-login && node refresh.js >> refresh.log 2>&1`

### 8.5 失效时的人工兜底

登录态彻底失效（改密码、强制重登）时，服务器也拿不到 token：

1. 本地重新登录一次
2. 重新导出 `storage_state.json` 并上传
3. 服务器立刻跑一次续期脚本确认

**一定要配失效告警**，否则会静默断掉。

---

## 9. 总验收清单

按顺序全部通过才算完成：

- [ ] 1. 带 token 请求列表接口，返回 `code: 1` 且列表非空
- [ ] 2. `fetch.py` 输出的「待开放」条数与网页一致
- [ ] 3. 群里能收到一条卡片消息
- [ ] 4. 卡片里时间是绝对日期，不是「明天」
- [ ] 5. @ 机器人说「帮助」，有回复
- [ ] 6. @ 机器人说「菜单」，出现按钮卡片
- [ ] 7. 点按钮，卡片原地刷新
- [ ] 8. 群里其他机器人发消息时，本机器人**不回复**
- [ ] 8b. 在群里**不 @** 直接说话，本机器人**不回复**；@ 它才回
- [ ] 9. 定时任务到点能自动推（把时间调到 5 分钟后验证一次）
- [ ] 10. 连续跑两次 `run.py`，第二次被"防重复"拦下
- [ ] 11. 服务器续期脚本能读到 token 并通过接口校验
- [ ] 12. 并发跑 4 个扫描进程，`state.json` 仍然可解析

---

## 10. 踩过的坑（按症状查）

| 症状 | 原因 | 解法 |
|------|------|------|
| 群里 @ 机器人没反应 | 缺 `group_at_msg:readonly`，或改了没发版 | 开权限 → 发版 → **重启进程** |
| 私聊发消息没反应 | 缺 `p2p_msg:readonly` | 同上，注意是另一条独立权限 |
| 发版后仍无反应 | 长连接绑定的是旧权限 | 必须重启长连接进程 |
| 自动推送后机器人自己回复 | 没过滤非真人消息 | 判断 `sender_type === 'user'` |
| 群里随便说话它都回 | 没做 @ 门禁 | 群聊要求 `mentions` 含自己的 open_id |
| 按钮点了没反应 | 没订阅 `card.action.trigger` 回调 | 回调配置里添加并重启 |
| 按钮点错视图 | 回调 value 带引号，解析失败 | 先剥引号再匹配 |
| 活动全都不见了 | 清单只保留"未开放"，已开放的不显示 | 这是设计，别"修" |
| 凌晨开放的活动没提醒 | 扫描只在白天跑 | 改成全天 `*/5 * * * *` |
| 偶尔推两条一样的 | 两个进程同时到点 | 加锁 + 推送占位 |
| `state.json` 变空/损坏 | 并发写没加锁 | 原子写 + 文件锁 |
| 跑一段时间后全失败 | token 过期 | 上第 8 阶段的自动续期 |
| 卡片发出去是纯文本 | `msg_type` 写错或卡片 JSON 不合法 | 先用最小卡片（只有 header）验证 |

---

## 11. 换成别的学校要改哪里

| 位置 | 改什么 |
|------|--------|
| `scripts/common.py` | `API_BASE`、`ACTIVITY_API`、`REFERER` |
| `scripts/fetch.py` | 请求体字段、`normalize()` 里的字段映射 |
| `scripts/login.py` | 登录流程、`localStorage` 的 key 名 |
| 过滤逻辑 | 各校字段语义可能不同，务必重新核对 |

**不要假设接口长得一样。** 第 1 阶段的验证必须重做一遍。

---

## 12. 安全与合规

1. **密钥不进代码库**。`config.json` 权限 `600`，`.gitignore` 排除 `data/`。
2. **个人数据不进群**。报名状态、已报名列表属于个人数据，只能私聊返回；
   群里的所有输出必须是"对所有人成立"的客观信息。
3. **控制频率**。每 5 分钟一次已经是 288 次/天，别再加密。
   高频请求可能触发学校风控，导致账号异常。
4. **不代报名**。本项目只做"读取 + 通知"，不自动提交报名。
5. **告知群成员**。群里有机器人在读数据、发消息，应当让所有人知情。

---

## 附录：最小可用版本（30 分钟）

如果你只想先跑起来看效果，可以砍掉一半功能：

| 保留 | 砍掉 |
|------|------|
| 第 1 阶段（验证接口） | 应用机器人（第 4 阶段） |
| 第 2 阶段（抓取 + 过滤） | 卡片按钮 |
| 第 3 阶段（webhook 推送） | 私人提醒 |
| 一条 cron（每天 1 次） | 冲刺扫描 |

先确认"每天能收到一条正确的活动清单"，再考虑加交互。
**能跑通的最小版本，胜过搭了一半的完整系统。**
