# 本机版界面重做任务书（给 Codex）

这份文档是「界面该怎么长、怎么接」的权威说明。**实现契约在 §5，照着写就行。**
本轮同时扩展本机多活动计划：`local/app.py` 负责计划协调，`local/grab.py` 保持单活动报名流程，
并把活动结束时间透传给界面。

---

## 1. 范围

### 命名契约（验收条款）

- 本工具正式名称统一为**「天软活动预报名工具」**：页面 `document.title`、WebView 窗口标题、帮助弹窗标题、README 与本规范一致。
- 窄窗口（≤860px 视口）品牌区可使用短标签**「天软预报名工具」**，但不得改正式窗口标题。
- 「准点」是早期视觉原型（`design/prototype/`）的历史称呼，只允许出现在历史说明与设计资料里，**不得**作为运行时产品名、窗口标题或帮助标题出现。
- 名称契约由 `local/tests/test_ui_contract.py` 的 NameContractTests 静态守护。

| 可以重写 | 本轮仍不能动 |
|---|---|
| `local/ui/index.html` | `local/requirements.txt`、启动 bat |
| `local/ui/styles.css` | `server/**`、`local/data/**` |
| `local/ui/app.js` | 学校站点请求方式、登录与单活动页面操作 |

界面可以完全推翻重做，只要 §5 的接口调用和 §6 的文案信息量不丢。单活动 `grab()` 保留兼容，
新的多活动入口使用 `start_plan()`；不能通过开多个应用实例代替计划协调。

## 2. 用户与场景

一个校园活动「到点自动报名」工具，只服务天津市大学软件学院（天软）专升本学生。
使用方式：解压绿色版 → 双击 `4-图形界面.bat` → 出窗口。用户是同学，不是开发者，
**不会看 README，也不会调参数**。

窗口背后是 `grab.py`：登录（弹真实 Chrome 手动登录一次）、拉活动列表、到点抢报名。

优先级：
1. 一眼看出现在什么状态（登录有没有过期、有多少能报、下一个还有多久开放）
2. 可以把多场活动加入一个计划；相同报名开放时刻的任务同时提交，不同批次分别等待
3. 选择前能看出活动举办时段冲突；冲突覆盖整个计划，不只检查同一开放批次
4. 失败看得懂（名额满 / 页面没放开 / 登录失效 / 断网）且单项失败不覆盖其他任务
5. 气质克制、干净，不像后台管理系统

## 3. 真实场景清单（设计必须覆盖）

| 场景 | 界面该做什么 |
|---|---|
| 第一次打开、没登录过 | 明确引导点「登录」，不要显示一堆接口异常 |
| 登录态还有 3 小时 | 胶囊显示剩余时间；剩不到 2 小时给橙色提醒 |
| 列表几百条、能报的只有几条 | 默认筛「未报名」，支持关键词 + 状态 + 时间组合 |
| 活动还没开放 | 「距开放」每秒走字，让人知道程序在等 |
| 开放那一刻 | 忙碌态清晰（按钮禁用 + 进度感），别让人以为卡死 |
| 抢成功 | 强反馈（绿）+ 提示去活动页确认一眼 |
| 没抢到 | 强反馈（红）+ 具体原因 + 下一步建议 |
| 同一报名开放时刻的多场活动 | 按活动举办起止时间判断：不重叠可同时加入；重叠时提示只能选其中一场 |
| 不同报名开放时刻的多场活动 | 可以提前加入计划，按各自开放时刻依次等待；举办时段冲突仍要阻止 |
| 计划部分成功 | 每场保留自己的成功、失败、已报名或取消状态，计划显示部分完成 |
| 锁屏/最小化回来 | 状态仍准确，不能停在「正在抢」的假象 |
| 断网 | 说清是网络问题，不要笼统写「失败」 |

## 4. 信息架构（可以重排）

```
┌────────────────────────────────────────────────────────────┐
│ 标题 + 副标题            [状态胶囊] [使用说明] [刷新] [登录] │
├────────────────────────────────────────────────────────────┤
│ [搜索框] [全部|未报名|已报名] [时间▾] [排序▾]        共 N 个 │
├────────────────────────────────────────────────────────────┤
│ #  │ 距开放 │ 报名开始 │ 状态 │ 活动名        │ 地点       │
├────────────────────────────────────────────────────────────┤
│ 报名计划：3 场 · 18:47 同时提交 2 场 · 明天 09:00 等待 1 场    │
│ [开始计划] [停止计划] [清空计划]          □显示浏览器窗口     │
│ 逐场状态：等待 / 提交中 / 成功 / 失败 / 已取消                 │
├────────────────────────────────────────────────────────────┤
│ 运行日志                                     [展开] [清空]  │
└────────────────────────────────────────────────────────────┘
```

允许的改动：区块顺序（前提：不用滚动就能同时看到「状态 + 列表 + 主按钮」）、
表格列（地点可以并到活动名下面当副标题）、日志默认高度、
把「显示浏览器窗口」「打开数据目录」收进二级菜单或图标按钮。

### 组件与状态矩阵

| 组件 | 状态 |
|---|---|
| 状态胶囊 | 未登录(灰) / 有效·约剩 N 小时(绿) / 临近过期(橙,<2h) / 失效(红) / 进行中(蓝) |
| 主按钮 | 默认 / hover / active / disabled(无有效计划或忙碌) / busy |
| 表格行 | 默认 / hover / 计划中 / 活动时段冲突 / 已报名(灰) / 空列表 |
| 筛选胶囊 | 选中 / 未选中 / hover |
| 计划条 | 空 / 已选 / 有冲突 / 运行中 / 部分完成 / 完成 / 已停止 |
| 任务条 | 等待开放 / 提交中 / 成功 / 已报名 / 失败 / 已取消 |
| 结果条 | 成功(绿) / 失败(红) / 部分完成(橙) / 隐藏 |
| 日志 | 收起(6 行) / 展开(十几行) / 空 |

## 5. 接口契约（权威，按此实现）

页面通过 `window.pywebview.api.*` 调后端，通过 `window.onBackend.*` 收事件。
**数据类调用是异步的**：方法立刻返回 `{ok:true, queued:true}`，结果由事件推回。

### 5.1 方法

| 方法 | 参数 | 立即返回 | 结果从哪来 |
|---|---|---|---|
| `bootstrap()` | — | `{version, prefs, help, login:{session_present, session_expired, hours_left}, busy}` | 直接返回（同步，别当异步用） |
| `help()` | — | `{title, subtitle, steps:[{no,title,text}], note}` | 直接返回 |
| `front_ready()` | — | `{ok:true}` | 直接返回（可选调用，冒烟用） |
| `check()` | — | `{ok:true, queued:true}` | `onBackend.check` |
| `list_activities()` | — | `{ok:true, queued:true}` | `onBackend.activities` |
| `login()` | — | `{ok:true}` 或 `{ok:false, text}` | `onBackend.done`（`kind:"login"`） |
| `grab(activity_id, name, at, headed)` | `at` = `YYYY-MM-DD HH:MM:SS` 或空串；`headed` = bool | 同上 | `onBackend.done`（`kind:"grab"`） |
| `start_plan(items, headed)` | `items` 为活动对象数组，至少含 `id/name/sign_start/act_start/act_end`；`headed` = bool | `{ok:true, queued:true, plan_id}` 或 `{ok:false,text,conflicts?}` | `onBackend.plan`、`onBackend.task`、`onBackend.plan_done` |
| `cancel_task(plan_id, task_id)` | 两个字符串 | `{ok:bool, text?}` | `onBackend.task` |
| `stop_plan(plan_id)` | 计划 id | `{ok:bool, text?}` | `onBackend.task`、`onBackend.plan_done` |
| `stop()` | — | `{ok:bool, text?}` | 兼容旧 UI：停止当前计划或单活动任务 |
| `open_data()` | — | `{ok:true}` | 直接返回 |
| `save_prefs(patch)` | 对象，浅合并写入统一数据根下的 `ui.json`（先确保数据目录存在） | `{ok:true}` | 直接返回 |

> `bootstrap()`/`help()` 是唯一可以在 `boot()` 里 `await` 的；其余别 `await` 结果数据。

### 5.2 事件

| 事件 | 载荷 |
|---|---|
| `onBackend.log(line)` | 字符串，脚本原话，可能带 `[X]` `[i]` `▶` 前缀 |
| `onBackend.activities(payload)` | `{ok:true, items:[...]}` 或 `{ok:false, text, items:[]}` |
| `onBackend.check(payload)` | `{ok, hours_left, session_present, session_expired, text, reason, hint, result}`；`reason` 是输出中第一条 `[X]` 来源行（附加字段），`result ∈ {valid, expired, unknown, none}`（附加字段，见 §5.9） |
| `onBackend.done(payload)` | `{ok, code, reason, label, kind}`，`kind` 是 `"login"` 或 `"grab"` |
| `onBackend.plan(payload)` | `{plan_id, status, tasks:[{task_id,activity_id,name,sign_start,sign_end,act_start,act_end,signup_mode,retry_for_s,max_attempts,status,…运行时 detail}]}`；任务快照携带模式、策略与真实 checkpoint（见 §5.10） |
| `onBackend.task(payload)` | `{plan_id, task_id, activity_id, name, sign_start, sign_end, signup_mode, retry_for_s, max_attempts, status, ok?, code?, reason?, detail?}`；提交中/终态另带 `run_started_at/page_opened_at/watch_started_at/retry_deadline/attempts/last_reason/stop_reason` |
| `onBackend.plan_done(payload)` | `{plan_id, status, ok, summary}`；`status` 为 `completed` / `partial` / `failed` / `stopped` |

约定：
- **登录三态**（验收条款）：「没有登录」= `session.json` 不存在、无法解析或缺少 `token`（`session_present:false`，按钮显示「登录」，胶囊「未登录」）；仅「登录过但 token 已过期或校验失败」（`session_present:true` 且 `ok:false`/过期）才算登录态失效（按钮「重新登录」）。`hours_left=0` 不能单独代表首次未登录；缺 token 时 `hours_left` 必须为 `null`，不得按 `saved_at` 报假的小时数。三态由 `login_state()` 统一产出，`bootstrap` 与 `check` 载荷口径一致。
- **check 归因看来源行**（验收条款）：`reason`/`hint`/`result` 都基于输出中第一条 `[X]` 来源行判定，失败输出末尾的「请重新登录」指引不得参与归因——HTTPError、URLError、超时、接口异常和认不出的失败一律 `unknown`，只有 4001/明确身份验证失败是 `expired`；网络异常不得转化为重新登录提示。`_do_check` 捕获 worker 启动/解析异常并保证发出一次 `unknown` 结论。
- `grab` 的 `ok` 由后端从输出里判断（含「报名成功」且退出码 0），`reason` 已去掉 `[X]` 前缀的来源行。
- `login` 的 `ok` 只看退出码；成功后**前端自己调一次 `list_activities()`** 刷新列表。
- `reason` 里可能已经带「没抢到：」这种前缀，展示前请去重（`^\[X\]\s*`、`^没抢到[：:]`）。
- `start_plan` 在后端再次校验活动 id、报名开放时间、活动举办区间和重复项；前端预检只用于即时提示，不能代替后端校验。
- `sign_start` 只决定提交批次：完整时间戳相同的任务归为同一批并发启动；不同批次按时间顺序等待。
- `act_start` / `act_end` 只决定活动举办冲突。按半开区间 `[act_start, act_end)` 比较：
  `a.start < b.end && b.start < a.end` 才算重叠；起止相接暂不算重叠。字段缺失、非法或结束不晚于开始时，活动不能进入可执行计划。
- 冲突检查覆盖整个计划，不因活动属于不同报名开放批次而跳过；冲突时保留已有选择并返回冲突双方，不能自动替换。
- 每个任务都有稳定 `task_id`；单项失败、已报名或取消只更新该任务，其他任务继续。停止计划只取消尚未提交的任务。

### 5.3 活动项字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `index` | int | 后端序号，**不要拿来定位** |
| `id` | string | 活动 id，抢报名用它 |
| `name` | string | 活动名，可能 20+ 字 |
| `place` | string | 地点，可能为空 |
| `sign_start` / `sign_end` | string | `YYYY-MM-DD HH:MM:SS`，可能为空 |
| `act_start` / `act_end` | string | 活动举办起止时间；完整本地日期时间，任一缺失或非法都不能通过计划校验 |
| `signed` | bool | 是否已报名 |
| `checkin_required` / `checkout_required` | bool \| null | 活动是否要求签到/签退；接口字段未识别或无法归一化时为 `null`，不得当作无需 |
| `state` | string | 后端给的中文状态：`未开放` / `开放中` / `已报名` |
| `seconds_to_open` | int \| null | 距报名开放秒数；正数=未开放，≤0=已开放，null=时间未知 |

### 5.4 计划和任务状态

计划状态：`ready`（已校验）/ `running`（至少一个任务等待或提交）/ `partial`（有成功也有失败或取消）/
`completed`（全部结束且无失败）/ `failed`（全部任务失败）/ `stopped`（用户停止，未提交任务已取消）。

任务状态：`queued`（排队）/ `waiting`（等待报名开放）/ `submitting`（提交中）/ `success`（报名成功）/
`already_signed`（已经报过）/ `failed`（报名失败）/ `canceled`（已取消）。

活动可以同时满足“报名开放批次相同”和“举办时段不重叠”；前者允许并发，后者决定是否允许共存。
二者不能互相替代。

### 5.5 本地偏好（`data/ui.json`）

| 键 | 含义 |
|----|------|
| `welcome_seen` | 首次说明弹窗是否已看过 |
| `filter` | `{q, status:"all"\|"free"\|"signed", time:"all"\|"today"\|"week", sort:"open"\|"name", checkin:"all"\|"yes"\|"no", checkout:"all"\|"yes"\|"no"}` |
| `log_open` | 日志是否展开 |
| `headed` | 计划内任务是否显示浏览器窗口 |
| `sound` | 计划结束时是否播放提示音 |

### 5.6 签到/签退筛选与未知值边界（验收条款）

签到/签退是**活动要求字段**，不是「本人是否已签到/签退」；字段语义按下列条款验收，字段名与代码一致（`checkin_required` / `checkout_required`，取 `true` / `false` / `null`）：

1. **归一化只认明确别名**：`grab.py` 的 `CHECKIN_FLAG_NAMES` / `CHECKOUT_FLAG_NAMES` 命中且可解析才归一化；字段缺失、空值、未知字符串、未确认别名一律 `null`。`null` 表示「接口还没说」，不得降级为无需。
2. **三态透传**：`list --json`、`demo_items`、`activities` 事件载荷原样携带两项 tri-state 字段；报名请求参数不因此改变。
3. **筛选语义**：`全部` 保留 `null`；`需要` 只显示 `true`；`无需` 只显示 `false`。`null` 在需要/无需两个筛选里都不可见，防止未知被误判。
4. **行内文案**：`null` 显示「签到待说明」「签退待说明」（两项都未知时可合并为「签到/签退待说明」），有文字不只靠颜色。
5. **偏好保存**：`filter.checkin` / `filter.checkout` 随其他筛选一起保存到 `data/ui.json` 并在下次启动恢复。
6. **筛选隔离**：筛选只影响时刻表可见行。全局「下一场开放」、已选计划集合、冲突校验（含跨开放批次）、任务执行与结果全部基于全量数据计算，不得被筛选结果污染。
7. **静态回归**：`local/tests/test_ui_contract.py` 用 UTF-8 直读 `index.html` / `app.js` / `grab.py` / `app.py` 核对控件、字段名、unknown 保留规则、保存键与桥接方法名；改丢任何一条测试必须失败并指出缺口。

### 5.7 原型未覆盖的真实业务边界（验收条款）

原型是单活动状态机，以下边界以运行时代码为准，页面不得为截图伪造状态：

1. **接口失败**：`activities` 事件失败时保留 `{ok:false, text, items:[]}` 原话；界面空态区分「没登录 / 登录失效 / 连不上 / 其他」，给出的下一步是「重试 / 登录」，不写技术词。连点「刷新」不会堆起多个 check/list 子进程（`app.py` 刷新去重）。
2. **未知字段**：签到/签退见 §5.6；其余学校侧文案（失败消息等）没有真实样本前按原话展示，界面只做关键词归因，归因不命中时显示学校原话。
3. **计划恢复**：`bootstrap()` 在计划运行中附带 `plan` 快照（`{plan_id, status, tasks[]}`）；页面重载/锁屏回来后据此恢复任务列表与「停止」按钮。恢复只回放显示，不重新提交——提交只发生在 `start_plan`，旧任务结果不会被当成新结果重播（`plan_done` 不重发）。
4. **冻结 worker**：PyInstaller 冻结后 `app.py` 调同目录 `grab-worker.exe`，与普通 Python 调 `grab.py` 使用同一 `worker_command` 构造器和参数序（`--activity-id/--name/--at/[--headed]`），事件顺序一致。
5. **登录/计划互斥**：登录、单活动 grab、计划共用一个「忙」判定（子进程存活、计划 ready/running、计划任务进程任一存活）；互斥期间 `login()` / `start_plan()` 返回 `{ok:false}` 与人话原因，停止计划后即解除。
6. **已开放场次的提交窗口**：报名开放时刻已过去时（无论 UI 计划路径还是 `grab.py` 命令行路径），`--at` 一律换成「现在」——重试窗口从现在起算 120 秒，避免一次都不试就误报「名额已满」。
7. **结果复核**：任何一场失败后，界面自动静默复查一次登录态与列表（不改写已展示的结论）；用户手动刷新/F5 走同一通道。成功结果的最终确认永远是「去活动页看一眼」，界面不代学校作保。
8. **停止时限与顺序**（验收条款）：`stop_plan` 幂等（重复调用不报错不挂起）；顺序固定为「先置停协调器（等待/排队任务标 canceled），再对仍存活的 worker 按进程树终止（`taskkill /F /T`，覆盖 onefile 引导进程的孙进程：真实 worker、Playwright node、chromium）」。从用户确认停止到 worker 终止 ≤1 秒，不等 Playwright 自然超时。取消路径的监控循环只等进程死透、不等输出管道 EOF；任务收尾为 `canceled`，计划发出唯一一次 `plan_done`。界面在确认停止后立即显示「正在停止…」并保持 busy 锁定直到 `plan_done` 落地。
9. **重试窗口是进程级硬截止**（验收条款）：`at + window` 秒之外进程不得再发起提交尝试；循环内所有阻塞等待（`reload` / `expect_response` / `click` / 确认 sleep）都按剩余窗口钳制，窗口耗尽立即收尾。退出码契约：成功 0 / 登录失败 3 / 报名失败 2 / Ctrl+C 中断 130；`cmd_grab` 的 `finally` 只负责关闭浏览器，不得用 `return` 覆盖退出码。

### 5.8 报名截止与用户重试策略（验收条款）

报名窗口由 `sign_start` / `sign_end` 决定，自动重试由**持续时长**与**最大提交次数**共同限制，任一先到即停；`sign_end` 永远是绝对硬上限。

1. **策略控件**：计划操作区提供「重试」下拉（2 分钟 / 10 分钟 / 30 分钟 / 1 小时 / 直到报名截止）与「最多次数」数字输入（`0=不限`）；默认 2 分钟 + 不限，随偏好保存恢复（`prefs.retry_policy`）。长窗口不会默认跨天运行——只有用户明确选「直到报名截止」才允许持续到 `sign_end`。
2. **有效截止**：`effective_deadline = min(起算点 + retry_for_s, sign_end)`；未来活动从 `sign_start` 起算，已开放活动从启动计划起算；`retry_for_s=0`（直到截止）但 `sign_end` 缺失时回退 120 秒，绝不无限运行。
3. **载荷链路**：`start_plan(items, headed, retry_policy)` → `TaskSpec(sign_end, retry_for_s, max_attempts)`（计划快照携带，重载不丢）→ worker `--sign-end / --retry-for / --max-attempts`；脏策略值在后端回落默认（120 / 0）。
4. **attempts 计数**（验收条款）：只有真正发起 `signUp` 请求的点击计数（worker 输出「第 N 次提交：原因」行）；页面刷新、等待与 `is_joined` 查询一律不计，也不得把刷新/查询次数显示成提交次数。界面显示「第 N 次提交 / 最近原因 / 重试截止 / 报名截止」，不显示虚构的 N/N 总数。
5. **可重试原因**：报名人数已满、未开放、无响应、网络错误按可重试处理，沿用 2→8 秒低频轮询继续，直到策略或截止；「登录失效」「已报名」「成功」是立即终态，不因重试策略继续刷新。
6. **终态与停止原因**：任务终态携带 `stop_reason ∈ {success, already_signed, login_invalid, max_attempts, retry_duration, sign_end, user_stopped, process_error}`；失败终态保留最后一个学校原话并追加到达的停止条件（达到最大提交次数 / 重试时长到了 / 报名已截止）。worker 结构化行 `[retry] attempts=N stop_reason=X retry_deadline=<epoch> sign_end=<epoch> last_reason=<原文>` 是 telemetry 唯一来源。
7. **路线图**：时间线同时保留学校原定开放时间和本次实际执行时间：`本次启动`、`活动页已打开`、`盯按钮` 使用 worker 真实事件时间，`原定开放` 使用活动的 `sign_start`（已过去的时间显示完整日期），最后一站使用 telemetry 的真实重试截止（策略时长或报名截止截断后的值），不再用 `sign_start + 120` 冒充。

### 5.9 登录态持久化与启动校验（验收条款）

1. **统一数据根**：开发/绿色版沿用脚本旁 `local/data`；冻结 EXE（主程序与 grab-worker）默认 `%LOCALAPPDATA%\TianSoftActivityPreRegistration\data`，环境变量 `TJISE_DATA_DIR` 可覆盖（相对路径先 resolve）。主程序通过 `_env()` 把解析好的数据根传给 worker，两侧读写同一份 `session.json` / `storage_state.json`。
2. **旧数据一次性迁移**：冻结进程首次使用新数据根时，只把旧 EXE 旁 `data/` 中目标缺失的 `session.json` / `storage_state.json` / `ui.json` / `grab.log` 以「临时文件 + `os.replace`」原子搬入，**不覆盖已有新文件**；迁移失败不阻断启动。会话文件不含账号密码。
3. **构建不删登录态**：`build_exe.py` 清理输出目录时保留 `OUT/data`（旧版 EXE 旁数据留给迁移），`rmtree` 不得作用于整个输出目录。
4. **启动校验状态**（界面验收）：无 session → 「未登录 + 登录」；有 session → 先显示「正在检查登录态」，不得提前写成「登录有效」；真实校验（`check_token` 访问学校接口）成功 → 「登录有效」并提示「已复用本机登录态」；接口返回 4001 → 「登录态失效 + 重新登录」；网络/接口异常 → 「暂时无法确认登录态」，保留已有会话与「刷新」重试，**不得**误报成首次未登录或强制重登。`check` 事件附 `result ∈ {valid, expired, unknown, none}` 附加字段（旧字段 `ok/session_present/hours_left` 语义不变）。
5. **寿命口径**：`saved_at` 只用于「约剩 N 小时」提醒（约 10–17 小时为实测区间，非保证值），不得作为放行或拦截依据；登录有效性只由学校接口真实校验决定。
6. **开始按钮门禁**（验收条款）：`updateControls()` 只有 `S.login.result === "valid"` 或 demo 模式（`bootstrap` 带 `demo:true`）允许「开始报名计划」；checking / unknown / none / expired 一律禁用开始，刷新与登录按钮保持可用。`refresh()` 的 check 调用失败（桥接/脚本异常）必须清除「检查中」、落 `unknown` 并更新胶囊与按钮，不得停留在「正在检查登录态」。后端 `start_plan` 是最终防线：非 demo 且最近一次 check 结论不是 `valid`（含尚未检查、checking、unknown、none、expired）一律拒绝并返回下一步提示；demo 模式继续可运行演示计划。

### 5.10 双模式与真实时间线（验收条款）

预报名与立即报名/抢名额是两个正式产品模式，加上已截止共三种状态；活动举办时间 `act_start`/`act_end` 只用于冲突检查，**不参与报名资格判断**，不因举办时间已开始或结束改变模式与门禁。

1. **模式三分类**（验收条款）：`signup_plan.classify_signup_mode(sign_start, sign_end, now)` 是唯一口径——`sign_end` 能解析且已过去 → `closed`；`sign_start` 在未来 → `pre_register`；其余（已开放，含 `sign_end` 缺失/非法）→ `instant_signup`。缺失截止沿用 worker 的安全回退，不误当成已截止。前端 `modeOf()` 只做展示与即时门禁，后端 `start_plan` 是最终权威；`TaskSpec` 在计划创建时定格 `signup_mode`，之后时间推移不改写快照。
2. **已截止门禁**（验收条款）：已截止活动保留在「全部」列表查看，但复选框、整行点击、前端 `validateSelection`、后端 `validate_plan`（`sign_end_passed`）与 `start_plan` 一致拒绝，拒绝信息带具体活动名与报名截止时间；可报名统计（状态胶囊、大屏指标、`isEligibleForPlan`）排除已截止。
3. **开放优先**：`nextOpen()` 同时返回 `openId`/`futureId`；存在未报名的开放活动时，大屏目标场与快捷「选这一场」优先 `openId`，只有没有开放活动才指向未来最近一场。混合计划中已开放批次先于未来批次执行（`group_by_sign_start` 按时间从早到晚）。
4. **载荷链路**：`start_plan` 的 items 显式携带 `sign_end` 与 `signup_mode`（后端仍以自身活动快照为权威）；`plan`/`task` 事件与 `bootstrap().plan` 快照携带 `signup_mode`、原定 `sign_start`、`sign_end`、`retry_for_s`、`max_attempts` 与运行时 detail；混合计划允许 `pre_register` 与 `instant_signup` 共存，每场任务卡显示自己的模式。
5. **运行时 checkpoint 字段**（验收字段）：`run_started_at`（本次任务实际启动）、`page_opened_at`（详情页实际加载完成）、`watch_started_at`（实际开始盯按钮）、`retry_deadline`（本次实际重试截止）、`attempts`/`last_reason`（最近提交）。时间戳取事件实际到达时刻；worker 的「详情页已加载」「开始盯按钮」事件只在对应 checkpoint 真正发生后输出。协调器持久化 detail：checkpoint 字段先到先得（迟到/重放事件不得改写已发生的节点，空值不落库），`attempts`/`last_reason` 跟随最新一次提交。
6. **预报名提前准备**（验收条款）：协调器按每场准备时刻调度——`pre_register` 在原定开放前 `lead_s`（默认 100 秒，快照携带）启动 worker，worker 立即打开详情页、开放前 5 秒进入盯按钮、开放后才提交；`instant_signup` 在计划启动后立即执行；同一准备时刻的任务同时启动。**一场在重试中不得阻塞其他场次按各自准备时刻启动**（不做批次间 join）；单场取消、计划停止与唯一一次 `plan_done` 语义不变。
7. **页面重载恢复**（验收条款）：`bootstrap()` 的 plan 快照 = 静态 TaskSpec + 动态 timeline detail；前端 `handlePlan`/`handleTask`/`ingestBootstrapPlan` 必须经 `mergeTask` 合并，快照缺运行时字段时不得用空值抹掉已收到的 checkpoint。恢复只回放显示，不重新提交。
8. **统一有效截止**（验收条款）：路线图、任务卡、LED、终态文案共用 `effectiveRetryDeadline(task)`——telemetry 的 `retry_deadline` 优先；否则未开放场次从 `sign_start` 起算、已开放场次从 `run_started_at`/`submitSeen` 起算，按任务 `retry_for_s`（0=直到 `sign_end`，缺 `sign_end` 回退 120 秒）推导，`sign_end` 永远截断。worker 侧重试起算点在打开页面前固定（`retry_anchor`）：已开放场次从 worker 启动时刻起算，**慢页面不得把用户配置的重试窗口整体后移**。登录到期检查同口径：未来场次比较「原定开放 + 重试窗口」，已开放场次比较「本次执行窗口」。
9. **路线图只显示真实 checkpoint**（验收条款）：五个站点（本次启动 / 活动页已打开 / 盯按钮 / 原定开放 / 本次重试到）各自按真实 checkpoint 与时间判断 done/now/todo；前三个站点的站点时间只来自真实事件，未发生时显示「待启动 / 待打开 / 待发生」，**不得**用 `armedAt`、`open-100`、`open-5` 生成估算时间冒充事件时间；历史「原定开放」是参考节点（照常标 done），不得抢占当前执行节点；历史时间显示完整日期。
10. **命令行双时间**：`grab.py grab` 对已开放场次同时输出「原定开放」（学校原定 `sign_start`）与「本次执行」（替换后的 `at`），并标明按立即报名/抢名额执行；重试逻辑、`sign_end` 硬截止与退出码契约（0/2/3/130）不变。

## 6. 文案要求

- 中文、第二人称、短句；不用「请」「您」这类客套，不用感叹号堆情绪。
- 不写技术词：不要「token」「接口」「子进程」「退出码」；用「登录态」「活动列表」「脚本」。
- 错误必须回答：**发生了什么 + 现在能做什么**。
- 可以优化措辞，但不能丢信息（下面这些信息点必须还在）。

必须保留的信息点：

1. 空列表：「这里还没有活动」+「点「刷新」拉一次；提示登录态失效就先点「登录」」
2. 未登录 / 失效：胶囊显示状态，主按钮仍是「登录 / 重新登录」
3. 成功：「报名成功：<活动名>」+ 建议去活动页确认一眼
4. 失败：「没抢到：<原因>」，原因可能为「名额已满」「页面还没放开」「登录态失效」「连不上学校站点」
5. 使用说明：三步（先登录 / 选活动 / 保持在线）+ 边界提醒（只给本人账号用，不要多开）

## 7. 视觉方向（建议，不强制）

- 浅色底 + 白卡片 + 单一强调色（现用深绿 `#1F6F4A`，可换但整体统一）。
- 字号阶梯 12/13/14/16/20/24，行高 1.5–1.7，表格正文 13px。
- 8px 栅格；圆角 8（控件）/12（卡片）/999（胶囊）；阴影最多两层，不外发光。
- 只用系统字体栈（微软雅黑 / PingFang / system-ui），**不要引外部字体或图标库**（可内联 SVG）。
- 对比度按 WCAG AA：正文 ≥4.5:1，已报名的灰字也要看得见。

## 8. 技术约束（硬要求）

1. 纯静态：`index.html` + `styles.css` + `app.js`；无构建、无 npm、无 CDN（离线可用）。
2. 内核是 WebView2（Chromium 110+）：可以用 flex/grid、CSS 变量、`dialog`、`backdrop-filter`；
   不要用需要 polyfill 的语法。JS 不要用 `type="module"`（file:// 下受限）。
3. 窗口最小 980×700，默认 1120×800；100% / 150% 缩放都不能错位。
4. 首屏骨架 300ms 内可见；几百行要能顺畅滚动。
5. 键盘：F5 刷新、Enter 抢选中项、`:focus-visible` 描边。
6. **两个必踩的坑**（写 `app.py` 时踩过，改界面时别绕回去）：
   - `js_api` 方法里不要做阻塞操作——现有的 `check`/`list_activities` 是「起线程 + 事件回推」；
     前端也必须按事件驱动写，不能假设方法直接返回数据。
   - 不要给 `Api` 实例挂公开属性；也不要试图 `import` `app.py` 里的东西到页面。

## 9. 验收清单

- [ ] 未登录 / 拉取失败 / 列表为空 三种状态都有明确引导
- [ ] 多选活动后，举办时段重叠项被明确提示并阻止启动；不重叠项可保留
- [ ] 同一 `sign_start` 的非冲突活动同时提交；不同 `sign_start` 的批次依次等待
- [ ] 冲突检查覆盖不同开放批次；缺失/非法活动时段不能静默进入计划
- [ ] 计划启动前后端都做校验，返回稳定 `plan_id/task_id`
- [ ] 单项成功、失败、已报名、取消不会覆盖其他任务；计划能显示部分完成
- [ ] 计划停止只取消尚未提交任务，已提交任务保留真实状态
- [ ] 「距开放」每秒更新，且不重排整表、不闪烁
- [ ] 可按活动是否需要签到、签退筛选；未知字段只在「全部」中保留，不误判为无需
- [ ] 抢报名成功/失败 结果条颜色+文案正确，一眼可见
- [ ] 登录成功后自动刷新列表，且不误报「没抢到」
- [ ] 使用说明可反复打开（不是只弹一次）
- [ ] 日志展开/收起正常，长日志不撑破布局
- [ ] 980×700 与 1120×800、100%/150% 缩放均无横向滚动条
- [ ] DevTools Network 只有本地文件，没有外部请求
- [ ] `python app.py --demo` 下能看到已报名/开放中/未开放、同批非冲突并发、同批冲突、跨批次冲突、部分成功和停止
- [ ] 未来场次显示「预报名」并等待原定开放；已开放场次显示「立即报名/抢名额」并立即执行；已截止场次只能查看，不能勾选或开始
- [ ] 存在开放活动时，大屏目标场与快捷「选这一场」优先开放场次，而不是未来场次
- [ ] 混合计划（预报名 + 立即报名）每场任务卡显示各自模式；计划摘要分别说明两种模式的场次
- [ ] 页面重载后路线图仍显示真实 checkpoint 与统一截止，不退回「sign_start + 120 秒」的估算时间
- [ ] 预报名场次在开放前 100 秒启动 worker 并真实打开详情页；「活动页已打开」「盯按钮」未发生时显示待打开/待发生，不显示估算时间
- [ ] 混合计划中，立即报名场次重试期间，预报名场次仍能在自己的准备时刻（原定开放前 100 秒）启动 worker
- [ ] 已开放场次页面加载慢时，重试截止仍从 worker 启动时刻起算且不越过 sign_end

## 10. 怎么跑、怎么看

```bat
cd local
python app.py            :: 真实模式（需要登录过）
python app.py --demo     :: 假数据，调样式用
```

- 截图：`Win+Shift+S`，或用脚本 `ImageGrab.grab(bbox=窗口矩形)` 抓窗口本身。
- 想看 DevTools：把 `app.py` 里 `webview.start(debug=False)` 临时改成 `debug=True`
  （这是唯一允许你碰 `app.py` 的情形——改完记得改回去）。
- 改完至少跑一遍：`python -m py_compile app.py grab.py signup_plan.py`、`node --check ui/app.js`，
  再用 `--demo` 截图自查一遍 §9。
