/* 状态仓库：常量、引导文案、全局状态 S（唯一状态持有者）。 */
const $ = (selector) => document.querySelector(selector);

const DEFAULT_HELP = {
  title: "使用帮助",
  subtitle: "使用说明、版本更新与本机数据",
  steps: [
    { no: 1, title: "登录账号", text: "选择「登录」，在打开的浏览器中完成登录。下次启动会检查已保存的登录状态；只有提示失效时才需要重新登录。" },
    { no: 2, title: "设置报名计划", text: "选择活动，设置尝试时长和最多提交次数，再选择「开始报名计划」。预报名会在开放后尝试提交，已开放活动会在计划启动后尝试提交。" },
    { no: 3, title: "查看报名结果", text: "运行期间可最小化窗口，但需保持电脑唤醒与联网。结束后查看各活动的报名结果；开始计划不代表报名成功。" },
  ],
  note: "适用于天津市大学软件学院（天软）专升本学生使用的活动中心。活动要求与报名结果以学校活动页为准。",
};

/* 与 grab.py / app.py 对齐的常量 */
const EXPIRING_H = 2;        // 登录剩余不到 2 小时转橙色提醒
const TOKEN_LIFETIME_H = 17.5; // app.py TOKEN_LIFETIME_H：登录状态寿命估算
const RETRY_WINDOW_S = 120;  // grab.py --window：开放后最多再试 120 秒
const SLEEP_GAP_MS = 120e3;  // 两次心跳间隔超过 2 分钟：多半是电脑睡过去了
const WEEK = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

/* 引导内容版本：与软件版本（VERSION 文件）独立，内容有实质变化时 +1，
 * 完成或跳过过旧版的用户会在新版再自动弹一次；同版绝不重复自动弹。
 * 2：六步顺序与高亮目标都没变，只纠正「选中即执行」的误导并补清次数口径。 */
const TOUR_VERSION = 2;
/* GitHub 最新版本页：只有用户点按钮时才由后端用系统浏览器打开，启动时不联网检查 */
const RELEASES_LATEST_URL = "https://github.com/ShenTingBai/tjise-activity-watch/releases/latest";
/* 六步主线：登录状态 → 活动与模式 → 筛选 → 选择与冲突 → 重试与开始 → 运行与结果。
 * target 对应 index.html 的 data-tour-target；目标隐藏/窄窗时退回底部静态说明，
 * 引导本身从不代替用户点登录、选活动或开始报名。 */
const TOUR_STEPS = [
  { target: "login", title: "登录账号",
    text: "这里是登录状态和「登录 / 刷新」入口。选择「登录」后，在打开的浏览器中完成登录。下次启动会检查已保存的登录状态；只有提示失效时才需要重新登录。" },
  { target: "list", title: "查看活动",
    text: "「预报名」等待学校开放，「立即报名」在计划启动后尝试提交。选择活动不会自动开始报名。" },
  { target: "filters", title: "筛选活动",
    text: "按名称、地点或时间查找活动，也可筛选签到和签退要求。未说明的要求以学校活动页为准。" },
  { target: "select", title: "选择活动",
    text: "勾选要报名的活动。举办时间重叠的活动不能同时加入计划。" },
  { target: "start", title: "设置并开始",
    text: "设置尝试时长和最多提交次数，再选择「开始报名计划」。达到尝试时长、提交次数上限或报名截止中的任一条件后，工具停止尝试。" },
  { target: "run", title: "查看结果",
    text: "可最小化窗口，但不要关闭工具或让电脑睡眠。任务卡显示每场结果，整块大屏的结论要等整个计划结束；显示「待核对」时，到学校活动页确认。" },
];

const S = {
  items: [],
  byId: new Map(),
  rowEls: new Map(),
  rowStates: new Map(),
  selected: new Set(),
  selectionNotice: "",
  filter: { q: "", status: "all", time: "all", sort: "open", checkin: "all", checkout: "all" },
  listState: "idle",
  /* 最近一次列表失败的原话：完整失败说明只归列表空态，结果框不再复读 */
  listError: "",
  /* preLoginSnapshot：一次重新登录前的完整登录结论快照。取消/超时/失败时整份
   * 回滚，避免异步尝试的中途状态把仍有效的旧凭证画成「登录已失效」。 */
  login: { hours: null, ok: null, expireAt: null, savedAt: null, present: null, checking: false, loginPending: false, result: null, preLoginSnapshot: null },
  /* 程序组件自检结论（worker_missing/worker_permission/spawn_failed）：非空时组件
   * 问题优先于登录/网络文案，登录与开始禁用。check/list 两分支按「刷新轮次」
   * 协同恢复：任一分支报组件故障立即挂横幅，另一分支成功/普通 unknown 不得撤；
   * 本轮两分支都完成且无故障才撤旧横幅（FB4 的两到达顺序一致语义） */
  runtime: { error: null, checkPending: false, listPending: false, roundError: null },
  /* 更新只读GitHub，单独记录结果；不能借用报名/组件错误通道。 */
  update: { state: "idle", requestId: "", result: null, timer: null },
  /* FB1 轻量「开始提交中」标志：成功/拒绝/reject 均释放，只做即时按钮反馈，
   * 后端最终接纳点的原子重检仍是最终权威 */
  starting: false,
  clock: { state: "unknown", offset: null, uncertainty: null, checkedAt: null, detail: "" },
  online: navigator.onLine !== false,
  sleepGap: 0,
  busy: false,
  stopping: false,
  /* 后端 bootstrap 的 demo 标记：演示模式不要求登录校验 valid 即可开始计划 */
  demo: false,
  /* 计划事件归属（P1）：后端每次最终接纳新计划递增 plan_generation，
   * 事件/快照据此判定归属；这里记录已见过的最大代次，旧代次事件不得改写当前计划 */
  maxPlanGen: 0,
  plan: { id: null, gen: 0, status: "idle", tasks: new Map() },
  /* 本进程已确认的报名成功（activity_id → {source, atMs}）。学校列表短暂滞后时
   * 仍把这场留在「已报名」并禁止再次选择；超过窗口后以学校列表为最终权威。 */
  confirmed: new Map(),
  /* 独立北京时间锚点：ingestBusinessClock 维护，businessNowMs 消费（决策 012）。
   * anchorEpochMs 对应 perfAt 那一刻；sampleAgeMs 是快照原地的授时年龄。 */
  businessClock: { anchorEpochMs: 0, perfAt: 0, sampleAgeMs: 0, uncertaintyMs: 0,
                   bridgeMs: 0, gen: -1, owner: "", state: "unavailable",
                   source: "", reason: "",
                   syncState: "idle", syncError: "", attempted: false },
  /* 上一帧的运行阶段：跨过开放时刻/进入真正提交时要重画一次大屏摘要 */
  lastRunPhase: null,
  prefs: {},
  /* 使用条款同意状态（决策 013）：version 是后端 bootstrap 带来的当前条款
   * 版本；acceptedVersion 是本机 ui.json 里已同意的版本；declined 记「这一版
   * 已经拒绝过」（重启时由 prefs.terms_declined_version 恢复）；unknown 记
   * 「条款状态没读到」，是 fail-closed 的第二种形态——拒绝不退出、不再强弹，
   * 但登录/列表/报名计划保持停用。 */
  terms: { version: "", acceptedVersion: "", acceptedAt: "", declined: false,
           prompted: false, unknown: false,
           /* 交互收口的会话态（只存内存，不新增 prefs 键）：saving=正在保存；
            * requestSeq=请求代号，超时/关闭时自增使迟到回执失效；unconfirmed=
            * 10 秒没拿到可确认结果（既非成功也非失败）；viewOnly=已同意同版的
            * 只读查看；source/focusBack=打开来源与要还回去的焦点；closeHandled=
            * 程序性关闭自己处理焦点与引导，close 监听不再重复处理；tourYield=
            * 打开条款时让位的引导；deferredIntro=boot 时被条款推迟的首次欢迎/引导 */
           saving: false, requestSeq: 0, unconfirmed: false, viewOnly: false,
           source: "", focusBack: null, closeHandled: false, tourYield: false,
           deferredIntro: null },
  /* 后端 bootstrap 的 lifecycle 快照：上次关闭时长/质量 + 登录估算（只提示，不参与门禁） */
  lifecycle: null,
  /* 六步引导运行态：open=遮罩在场；paused=计划运行中暂停；restoreFocus=结束后要还的焦点 */
  tour: { open: false, step: 0, paused: false, restoreFocus: null },
  /* 计划级重试策略：-1=自动延长（最多25分钟）；时长 0=直到报名截止（缺截止回退 120 秒）；次数 0=不限 */
  retryPolicy: { retry_for_s: 120, max_attempts: 0 },
  /* 三种重试模式的权威选项与默认值（bootstrap.retry_options 同源覆盖）：
   * 桌面白名单与手机设置都按这份口径，不各自维护第二套清单 */
  retryOptions: {
    default: 120,
    fallback_seconds: 120,
    adaptive_cap_seconds: 1500,
    choices: [
      { value: 120, label: "2 分钟" },
      { value: 600, label: "10 分钟" },
      { value: 1800, label: "30 分钟" },
      { value: 3600, label: "1 小时" },
      { value: -1, label: "自动延长（最多25分钟）" },
      { value: 0, label: "直到报名截止" },
    ],
  },
  logOpen: false,
  /* 最近 40 条运行记录：成功结论只写一次，重复事件不刷第二遍（RC-06） */
  recentLogs: [],
  recheckPending: false,
  savePending: {},
  saveTimer: null,
  /* 每一轮 check/list 的请求 ID：迟到事件按 ID 丢弃，不能覆盖新轮（R11） */
  round: { check: "", list: "", checkTimer: null, listTimer: null,
           /* gen 是「刷新消费代次」：每次 refresh 递增，同轮重复只生效一次，
            * 新一轮允许用同一个 owner 结果收口（FR04）。applied 只按
            * branch:gen:request 记账，不再跨轮永久拦截。 */
           gen: { check: 0, list: 0 }, cache: [], applied: {},
           /* 一次性副作用（日志/提示）按请求幂等：跨轮重放同一 owner 时
            * 状态要恢复，但同一句话不能刷两遍。 */
           notified: {} },
  /* 开始请求的对账状态：submitting / reconciling / accepted / rejected / unknown。
   * gen 是查询代次：只有同一请求、同一代次的回复才能落地。 */
  startRequest: { id: "", state: "idle", timer: null, queryTimer: null,
                  reconcile: 0, gen: 0 },
};

/* 轮次 ID：同一轮的所有事件共享，旧轮迟到消息不得改写新轮 */
