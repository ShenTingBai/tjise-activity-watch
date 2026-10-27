"use strict";

/* 天软活动预报名工具：界面逻辑。
 * 只按 local/app.py 的真实契约说话：
 *   调用  window.pywebview.api.bootstrap / check / list_activities / login / start_plan /
 *         cancel_task / stop_plan / open_data / save_prefs / front_ready
 *   事件  window.onBackend.log / check / activities / done / plan / task / plan_done
 * 多活动计划由 signup_plan.py 协调；视觉语言参照 design/prototype（候车厅 + 时刻表 + LED 大屏），
 * 但状态机以多活动业务为准，不回退单活动模型。 */

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
  login: { hours: null, ok: null, expireAt: null, savedAt: null, present: null, checking: false, loginPending: false, result: null },
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
  /* 后端 bootstrap 的 lifecycle 快照：上次关闭时长/质量 + 登录估算（只提示，不参与门禁） */
  lifecycle: null,
  /* 六步引导运行态：open=遮罩在场；paused=计划运行中暂停；restoreFocus=结束后要还的焦点 */
  tour: { open: false, step: 0, paused: false, restoreFocus: null },
  /* 计划级重试策略：时长 0=直到报名截止（缺截止回退 120 秒）；次数 0=不限 */
  retryPolicy: { retry_for_s: 120, max_attempts: 0 },
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
function newRoundId(prefix) {
  return prefix + "-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
}

/* 终态暂存：新一轮先发、旧 owner 的终态先到时，不能直接丢——等 skipped ack
 * 接续回原 ID 后还要重放。只存结构化载荷，不含凭证；最多 16 项、130 秒过期。 */
const ROUND_CACHE_LIMIT = 16;
const ROUND_CACHE_TTL_MS = 130000;

function cacheRoundPayload(branch, id, payload) {
  const key = String(id || "");
  if (!key) return;
  /* 暂存期限是经过时长，不是墙钟时刻：系统时间前移/回拨不得让刚到的终态
   * 立刻过期或被无限延长。_monoMs 与时钟锚点同一单调域。 */
  const now = _monoMs();
  S.round.cache = (S.round.cache || []).filter((item) => now - item.at < ROUND_CACHE_TTL_MS
    && !(item.branch === branch && item.id === key));
  S.round.cache.push({ branch: branch, id: key, payload: payload, at: now });
  while (S.round.cache.length > ROUND_CACHE_LIMIT) S.round.cache.shift();
}

function takeRoundPayload(branch, id) {
  const key = String(id || "");
  if (!key) return null;
  const now = _monoMs();
  const index = (S.round.cache || []).findIndex((item) => item.branch === branch
    && item.id === key && now - item.at < ROUND_CACHE_TTL_MS);
  if (index < 0) return null;
  return S.round.cache.splice(index, 1)[0].payload;
}

/* 消费代次：refresh 每发一轮就 +1；skipped 接续回原 owner 时**不**递增——
 * 那一轮要用同一个 owner 的既有结果收口。 */
function bumpRoundGeneration(branch) {
  S.round.gen = S.round.gen || { check: 0, list: 0 };
  S.round.gen[branch] = (S.round.gen[branch] || 0) + 1;
  return S.round.gen[branch];
}

/* 同一代次里，同一请求的终态只生效一次；换一轮刷新就允许重新消费。
 * 键含代次是 FR04 的修复点：跨轮永久 applied 会让新一轮干等 100/130 秒。 */
function markRoundApplied(branch, id) {
  const key = String(id || "");
  if (!key) return true;
  const gen = (S.round.gen && S.round.gen[branch]) || 0;
  const applied = S.round.applied || (S.round.applied = {});
  const slot = branch + ":" + gen + ":" + key;
  if (applied[slot]) return false;
  applied[slot] = _monoMs();
  const keys = Object.keys(applied);
  if (keys.length > 48) {
    keys.sort((a, b) => applied[a] - applied[b])
      .slice(0, keys.length - 48)
      .forEach((old) => { delete applied[old]; });
  }
  return true;
}

/* 一次性副作用：同一请求的同一类提示只写一次，换轮重放不再刷屏 */
function firstNotice(branch, id, tag) {
  const key = String(id || "");
  if (!key) return true;
  const notified = S.round.notified || (S.round.notified = {});
  const slot = branch + ":" + key + ":" + String(tag || "");
  if (notified[slot]) return false;
  notified[slot] = _monoMs();
  const keys = Object.keys(notified);
  if (keys.length > 48) {
    keys.sort((a, b) => notified[a] - notified[b])
      .slice(0, keys.length - 48)
      .forEach((old) => { delete notified[old]; });
  }
  return true;
}

/* 登录快照里的 session_present → 三态布尔：
 * 缺字段或 null 都表示「不知道」，不能 !! 成 false（那会走「首次未登录」，
 * 把演示模式/尚未核对的状态误判成没登录）。demo 一律视为未知。 */
function loginPresent(login, isDemo) {
  if (isDemo) return undefined;
  if (!login || !("session_present" in login)) return undefined;
  if (login.session_present === null) return undefined;
  return !!login.session_present;
}

function hasBridge() {
  return !!(window.pywebview && window.pywebview.api);
}

function api(name, ...args) {
  if (!hasBridge()) return Promise.reject(new Error("页面没有连接本机程序"));
  const fn = window.pywebview.api[name];
  if (typeof fn !== "function") return Promise.reject(new Error("本机程序不支持：" + name));
  try {
    return Promise.resolve(fn.apply(window.pywebview.api, args));
  } catch (err) {
    return Promise.reject(err);
  }
}

function waitForBridge(timeoutMs) {
  const limit = typeof timeoutMs === "number" ? timeoutMs : 15000;
  return new Promise((resolve) => {
    /* 15 秒承诺是经过时长：系统墙钟跳变不得提前或延后它 */
    const started = _monoMs();
    const probe = () => {
      if (hasBridge()) {
        resolve(true);
        return;
      }
      if (_monoMs() - started >= limit) {
        resolve(false);
        return;
      }
      setTimeout(probe, 50);
    };
    probe();
  });
}

async function boot() {
  const bridgeReady = await waitForBridge();
  const bootStarted = _monoMs();
  const info = bridgeReady ? await api("bootstrap").catch(() => null) : null;
  const bootRtt = _monoMs() - bootStarted;
  fillHelp((info && info.help) || DEFAULT_HELP);
  /* 当前版本唯一显示位，来自后端；打开帮助不代表已查询GitHub。 */
  $("#help-version").textContent = info && info.version ? String(info.version) : "—";
  restoreUpdate(info && info.update_check);
  if (info) {
    /* demo 标记由后端 bootstrap 带来：演示模式绕过「登录校验 valid」的开始门禁 */
    S.demo = !!info.demo;
    /* 生命周期快照（上次关闭时长/质量、登录估算）：只进时钟浮层当提示，
     * 不参与登录四态判定，也不影响开始门禁 */
    S.lifecycle = info.lifecycle || null;
    applyPrefs(info.prefs || {});
    /* 独立北京时间：bootstrap 权威快照建立锚点（决策 012），
     * 桥接往返计入误差；随后由后台轮询接管后续快照 */
    ingestBusinessClock(info.business_clock, { authoritative: true, rttMs: bootRtt });
    S.businessClock.attempted = true;
    setLogin(info.login && info.login.hours_left, null,
             loginPresent(info.login, !!info.demo), info.login);
    setBusy(!!info.busy);
    ingestBootstrapPlan(info);
    /* 首次使用：六步引导代替一次性静态弹窗；引导不会自动开始时才弹说明 */
    const tourWillAuto = tourShouldAutoStart();
    if (!S.prefs.welcome_seen) {
      savePrefs({ welcome_seen: true });
      if (!tourWillAuto) showHelp();
    }
    if (tourWillAuto) startTour(0);
  } else {
    setLogin(null, null);
    S.listState = "idle";
    render();
  }
  if (hasBridge()) {
    api("front_ready").catch(() => {});
    startClockPolling();
    pollClockNow();
    refresh(true);
  }
}

function applyPrefs(prefs) {
  S.prefs = Object.assign({}, prefs || {});
  const filter = S.prefs.filter || {};
  S.filter = {
    q: String(filter.q || ""),
    status: ["all", "free", "signed"].indexOf(filter.status) >= 0 ? filter.status : "all",
    time: ["all", "today", "week"].indexOf(filter.time) >= 0 ? filter.time : "all",
    sort: ["open", "name"].indexOf(filter.sort) >= 0 ? filter.sort : "open",
    checkin: ["all", "yes", "no"].indexOf(filter.checkin) >= 0 ? filter.checkin : "all",
    checkout: ["all", "yes", "no"].indexOf(filter.checkout) >= 0 ? filter.checkout : "all",
  };
  S.logOpen = !!S.prefs.log_open;
  /* 重试策略偏好：只认白名单值，脏数据回落默认（120 秒 / 不限） */
  const policy = S.prefs.retry_policy || {};
  const allowedFor = ["120", "600", "1800", "3600", "0"];
  S.retryPolicy = {
    retry_for_s: allowedFor.indexOf(String(policy.retry_for_s)) >= 0 ? Number(policy.retry_for_s) : 120,
    max_attempts: Number.isInteger(policy.max_attempts) && policy.max_attempts >= 0 ? policy.max_attempts : 0,
  };
  $("#q").value = S.filter.q;
  $("#time").value = S.filter.time;
  $("#sort").value = S.filter.sort;
  $("#checkin").value = S.filter.checkin;
  $("#checkout").value = S.filter.checkout;
  $("#headed").checked = !!S.prefs.headed;
  $("#retry-for").value = String(S.retryPolicy.retry_for_s);
  $("#max-attempts").value = String(S.retryPolicy.max_attempts);
  paintChips();
  setLogOpen(S.logOpen, true);
}

/* 清筛选的唯一实现：只重置 6 个筛选键与对应控件。
 * 不能拿「半个 prefs」回调 applyPrefs——那会把重试策略、浏览器开关、
 * 运行记录展开、引导版本等一起重置掉，用户设的 10 分钟/3 次就白设了。 */
function clearFilters() {
  S.filter = { q: "", status: "all", time: "all", sort: "open", checkin: "all", checkout: "all" };
  $("#q").value = S.filter.q;
  $("#time").value = S.filter.time;
  $("#sort").value = S.filter.sort;
  $("#checkin").value = S.filter.checkin;
  $("#checkout").value = S.filter.checkout;
  paintChips();
  /* 只写过滤器差量：savePrefs 按 key 合并，其他偏好原样保留 */
  savePrefs({ filter: Object.assign({}, S.filter) });
  render();
}

/* 接纳代次并入会话记录（bootstrap/终态/ack 共用）：返回可用于 S.plan.gen 的值 */
function notePlanGeneration(value) {
  const gen = Number(value);
  if (!Number.isFinite(gen)) return Number(S.plan.gen) || 0;
  S.maxPlanGen = Math.max(S.maxPlanGen || 0, gen);
  return gen;
}

function ingestBootstrapPlan(info) {
  /* 页面重载会丢 plan/task 事件：用 bootstrap 快照恢复任务列表与终态呈现。
   * 只恢复显示，不重新提交；终态走与 plan_done 相同的呈现入口（FR05）。 */
  const plan = info && info.plan;
  if (!plan || !plan.plan_id) return;
  const id = String(plan.plan_id);
  const status = String(plan.status || "running");
  const tasks = plan.tasks || [];
  const gen = notePlanGeneration(plan.plan_generation);   /* 权威快照定归属（P1） */
  if (S.plan.id !== id) {
    S.plan = { id: id, gen: gen, status: status, tasks: new Map(), armedAt: Date.now() / 1000 };
  } else {
    /* 同 plan_id 也要推进：状态可能已从 running 走到终态（FR05） */
    if (gen > (Number(S.plan.gen) || 0)) S.plan.gen = gen;
    S.plan.status = status;
  }
  tasks.forEach((task) => {
    /* 快照携带完整运行时 detail（checkpoint/模式/策略/次数）：重载后
     * 时间线显示真实节点，已提交次数也不归零 */
    const key = String(task.task_id);
    S.plan.tasks.set(key, mergeTask(S.plan.tasks.get(key) || null, task));
  });
  if (["completed", "failed", "partial", "stopped"].indexOf(status) < 0) {
    /* 运行态：恢复进度与「停止」入口 */
    setBusy(true);
    render();
    return;
  }
  /* 终态：同一入口恢复结果框、卡片次数/原因、LED/边框、停止后动作与选择签名。
   * 不置 busy、不重发 start_plan、不重复写失败日志、不触发自动刷新。 */
  presentPlanDone({ plan_id: id, status: status, summary: plan.summary || {} }, false);
}

/* FB4：组件码唯一接收点——只认可知三码；未知 error 字段保持既有业务失败路径 */
function noteRoundError(code) {
  if (!code || !RUNTIME_ERRORS[code]) return false;
  S.runtime.roundError = code;
  S.runtime.error = code;
  return true;
}

/* FB4：刷新轮次收口——一个分支终态后，若本轮两分支都完成，按该轮故障累计
 * 决定撤/留横幅（有故障保持，无故障恢复）。任一分支未完成绝不提前清。 */
function settleRefreshBranch(branch) {
  if (branch === "check") S.runtime.checkPending = false;
  else S.runtime.listPending = false;
  if (S.runtime.checkPending || S.runtime.listPending) return;
  const next = S.runtime.roundError;
  if (S.runtime.error !== next) {
    S.runtime.error = next;
    paintBanners();
    updateControls();
    render();
  }
}

function refresh(quiet) {
  if (!hasBridge()) {
    if (!quiet) setResult({ ok: false, title: "还没有连接本机程序", text: "请从图形界面启动，再点「刷新」加载活动列表。" });
    return;
  }
  const checkId = newRoundId("check");
  const listId = newRoundId("list");
  /* 每轮各自一个消费代次：这一轮的 ack/catch/终态只能收口这一轮，
   * 旧轮迟到的回调不许把新轮的 pending 提前清掉（FR04）。 */
  const checkGen = bumpRoundGeneration("check");
  const listGen = bumpRoundGeneration("list");
  S.round.check = checkId;
  S.round.list = listId;
  clearTimeout(S.round.checkTimer);
  clearTimeout(S.round.listTimer);
  S.listState = "loading";
  S.listError = "";
  /* 开启新一轮复查：两分支 pending、故障累计清零；旧横幅保持到本轮收口 */
  S.runtime.checkPending = true;
  S.runtime.listPending = true;
  S.runtime.roundError = null;
  /* 先进「检查中」：本机有会话也不提前说「登录有效」，等真实校验回来 */
  S.login.checking = true;
  paintLogin();
  render();
  /* 失联兜底：确认入队却丢终态时，不能永远停在「检查中」/骨架（R11） */
  S.round.checkTimer = setTimeout(() => {
    if (S.round.check !== checkId || !S.login.checking) return;
    S.login.checking = false;
    S.login.ok = false;
    S.login.result = "unknown";
    paintLogin();
    updateControls();
    refreshLoginEmptyState();
    addLog("[X] 检查登录状态超过 100 秒没有回应，先按「暂时无法确认」处理；点「刷新」重试。");
    settleRefreshBranch("check");
  }, 100000);
  S.round.listTimer = setTimeout(() => {
    if (S.round.list !== listId || S.listState !== "loading") return;
    S.listState = "error";
    S.listError = "活动列表超过 130 秒没有回应";
    render();
    settleRefreshBranch("list");
  }, 130000);
  api("check", checkId).then((res) => {
    if (S.round.gen.check !== checkGen) return;   // 旧轮 ack：不碰新一轮
    settleSynchronous(res, "check");
  }).catch((err) => {
    /* 桥接/脚本异常也必须离开「检查中」：落 unknown 结论并刷新胶囊与按钮，
     * 不能把界面永远停在「正在检查登录状态」；下一步是点「刷新」再试。
     * ok 必须同时置 false：旧的 valid/warn 状态不清理的话，
     * paintLogin 的 unknown 分支（要求 ok===false）接不住，顶栏会继续显示「登录有效」。 */
    if (S.round.gen.check !== checkGen || S.round.check !== checkId) return;
    clearTimeout(S.round.checkTimer);
    S.login.checking = false;
    S.login.ok = false;
    S.login.result = "unknown";
    paintLogin();
    updateControls();
    /* 结论变了，空态要跟着从「正在检查」落到「暂时无法确认」 */
    refreshLoginEmptyState();
    addLog("[X] 检查登录状态失败：" + msg(err) + "；点「刷新」再试一次");
    settleRefreshBranch("check");
  });
  api("list_activities", listId).then((res) => {
    if (S.round.gen.list !== listGen) return;     // 旧轮 ack：不碰新一轮
    settleSynchronous(res, "list");
  }).catch((err) => {
    /* 桥接/脚本异常同 Worker 失败一个口径：完整说明归列表空态，
     * 不再为同一列表错误额外弹结果框 */
    if (S.round.gen.list !== listGen || S.round.list !== listId) return;
    clearTimeout(S.round.listTimer);
    S.listState = "error";
    S.listError = msg(err);
    render();
    settleRefreshBranch("list");
  });
}

/* 同步拒绝（如正在关闭 / 正在检查）没有终态事件：本轮分支必须当场收口，
 * 不让界面挂在「检查中」等一个永远不会来的事件（R11）。 */
function settleSynchronous(res, branch) {
  if (!res) return;
  /* 去重命中在途操作（C06）：接续原请求 ID 并重绑本轮超时；
   * 原操作若已经结束，后端会把终态一起带回来，直接落地，不干等旧事件。 */
  if (res.skipped) {
    const id = String(res.request_id || "");
    if (branch === "check") {
      if (id) S.round.check = id;
      clearTimeout(S.round.checkTimer);
      S.round.checkTimer = setTimeout(() => {
        if (S.round.check !== id || !S.login.checking) return;
        S.login.checking = false;
        S.login.ok = false;
        S.login.result = "unknown";
        paintLogin();
        updateControls();
        refreshLoginEmptyState();
        settleRefreshBranch("check");
      }, 100000);
      if (res.result) {
        window.onBackend.check(res.result);
        return;
      }
      const cached = takeRoundPayload("check", id);
      if (cached) {
        /* 终态比 skipped ack 先到：接续回原 owner 后重放一次，不干等 100 秒。
         * 提示本身按请求幂等：跨轮再重放同一 owner 时状态要恢复，日志不刷屏。 */
        if (firstNotice("check", id, "replay")) addLog("[i] 已接续这次检查的原结果。");
        window.onBackend.check(cached);
      }
      return;
    }
    if (id) S.round.list = id;
    clearTimeout(S.round.listTimer);
    S.round.listTimer = setTimeout(() => {
      if (S.round.list !== id || S.listState !== "loading") return;
      S.listState = "error";
      S.listError = "活动列表超过 130 秒没有回应";
      render();
      settleRefreshBranch("list");
    }, 130000);
    if (res.result) {
      applyActivities(res.result, false);
      return;
    }
    const cachedList = takeRoundPayload("list", id);
    if (cachedList) applyActivities(cachedList, false);
    return;
  }
  if (res.ok !== false) return;
  if (branch === "check") {
    clearTimeout(S.round.checkTimer);
    S.login.checking = false;
    S.login.result = "unknown";
    S.login.ok = false;
    paintLogin();
    updateControls();
    refreshLoginEmptyState();
    addLog("[X] " + ((res && res.text) || "这次检查没能开始") + "；点「刷新」重试。");
    settleRefreshBranch("check");
    return;
  }
  clearTimeout(S.round.listTimer);
  S.listState = "error";
  S.listError = (res && res.text) || "活动列表没能开始加载";
  render();
  settleRefreshBranch("list");
}

/* 组件错误 → 一句话标题 + 恢复动作说明（后端 runtime_error 的唯一展示口径） */
const RUNTIME_ERRORS = {
  worker_missing: { title: "程序组件缺失", hint: "无法找到所需程序组件。请重新下载完整版本，再选择「重新检查」。" },
  worker_permission: { title: "程序组件无法读取", hint: "组件文件在，但被系统拦住或没有权限（刚下载的文件常见）。右键程序 → 属性 → 勾「解除锁定」，或重新解压后再点「重新检查」。" },
  spawn_failed: { title: "程序组件启动失败", hint: "组件在但没能启动。点「重新检查」再试一次；反复出现请重新下载完整版本。" },
};

function runtimeErrorCopy() {
  return RUNTIME_ERRORS[S.runtime.error] || RUNTIME_ERRORS.spawn_failed;
}

/* 报名结果回写（真实证据 P0-1）：success/already_signed 是已确认事实，
 * 立即映射到活动列表——计数、分组、行状态、复选框与冲突基线同步更新。
 * unconfirmed/failed/canceled 不写 signed，避免把未知猜成成功。 */
const SIGNED_SYNC_MS = 120000;
let confirmedRefreshTimer = null;

function planConfirmedAt() {
  const ms = businessNowMs();
  return ms == null ? Date.now() : ms;   // 仅用于界面同步窗口；门禁仍由后端裁决
}

function noteSignedTasks(tasks) {
  let changed = false;
  (tasks || []).forEach((task) => {
    const status = String((task && task.status) || "");
    if (status !== "success" && status !== "already_signed") return;
    const id = String((task && task.activity_id) || "");
    if (!id) return;
    const atMs = planConfirmedAt();
    S.confirmed.set(id, {
      source: status === "success" ? "plan_success" : "plan_already_signed",
      atMs: atMs,
    });
    const item = findItem(id);
    if (item) {
      item.signed = true;
      item.signed_source = status === "success" ? "plan_success" : "plan_already_signed";
      item.signed_confirmed_at = atMs / 1000;
    }
    /* 已成功的场次从下一次可执行选择集合移除；任务卡与结果卡保留 */
    S.selected.delete(id);
    changed = true;
  });
  return changed;
}

/* 列表载荷合并：后端已把窗口内的本地确认合并进来；这里再兜一层，
 * 覆盖「后端进程重启但界面还在」这类只有前端记得的确认。 */
function applyConfirmedToItems() {
  if (!S.confirmed.size) return;
  const nowMs = planConfirmedAt();
  S.items.forEach((item) => {
    const id = String(item.id);
    if (item.signed) {
      item.sync_pending = !!item.sync_pending && item.signed_source !== "school";
      return;
    }
    const record = S.confirmed.get(id);
    if (!record) return;
    if (nowMs - record.atMs > SIGNED_SYNC_MS) {
      S.confirmed.delete(id);       // 超过同步窗口：学校列表是最终权威
      return;
    }
    item.signed = true;
    item.signed_source = record.source;
    item.signed_confirmed_at = record.atMs / 1000;
    item.sync_pending = true;
  });
}

/* 计划收尾后安排一次有界列表刷新：只补权威列表，不重发、不改结论。
 * 刷新失败时后端仍保留已确认成功，界面下一轮成功刷新会重新合并。 */
function scheduleSignedRefresh() {
  if (confirmedRefreshTimer) clearTimeout(confirmedRefreshTimer);
  confirmedRefreshTimer = setTimeout(() => {
    confirmedRefreshTimer = null;
    if (S.busy || S.starting || S.demo) return;
    refresh(true);
  }, 800);
}

function applyActivities(payload, quiet) {
  const res = payload || {};  /* 旧轮的迟到列表不得覆盖新一轮（无 ID 的旧载荷放行）。确属本 UI 旧轮的
   * 终态先暂存：skipped ack 接续回原 owner 后还要重放。 */
  if (res.request_id && S.round.list && res.request_id !== S.round.list) {
    cacheRoundPayload("list", res.request_id, res);
    return;
  }
  if (res.request_id && !markRoundApplied("list", res.request_id)) return;
  if (res.request_id && res.request_id === S.round.list) {
    clearTimeout(S.round.listTimer);
  }
  if (!res.ok) {
    S.items = [];
    S.byId = new Map();
    S.selected.clear();
    S.listState = "error";
    /* FB4：先消费后端的 runtime_error 再渲染——check 成功而 list 起不来时
     * 组件横幅不能缺席；失败原话留给列表空态做唯一完整说明，不再生成
     * 同义结果框，也不靠清空 result 去藏重复——真实业务结果原样保留 */
    if (noteRoundError(res.runtime_error)) {
      paintBanners();
      updateControls();
    }
    S.listError = res.text || "";
    render();
    settleRefreshBranch("list");
    return;
  }
  S.listError = "";
  S.items = (res.items || []).map(normalize);
  applyConfirmedToItems();
  S.byId = new Map(S.items.map((item) => [String(item.id), item]));
  Array.from(S.selected).forEach((id) => { if (!S.byId.has(String(id))) S.selected.delete(id); });
  S.listState = "ready";
  render();
  settleRefreshBranch("list");
}

function render() {
  paintList();
  /* 活动列表异步回填后同步更新状态胶囊计数，避免真实数据已到但仍显示初始化的 0。 */
  paintChips();
  paintRail();
  paintSelected();
  paintBoardState();
  paintChecks();
  paintPlan();
  paintRoute();
  paintBanners();
  updateControls();
  tick();
  paintLed();
  /* 引导还开着时，列表重画可能挪动高亮目标：重新定位现有高亮，不重开、不假点击 */
  if (S.tour.open && !S.tour.paused) paintTour();
}

/* ---------------------------------------------------------------- 时刻表 */
function paintList() {
  const box = $("#rows");
  const listEl = $("#list");
  S.rowEls = new Map();
  listEl.classList.toggle("is-loading", S.listState === "loading" && S.items.length > 0);
  if (S.listState === "loading" && !S.items.length) {
    box.innerHTML = skeletonHtml();
    S.rowStates = new Map();
    $("#empty").hidden = true;
    return;
  }
  const rows = filtered();
  paintEmpty(rows);
  paintCount(rows);
  if (!rows.length) {
    box.innerHTML = "";
    S.rowStates = new Map();
    return;
  }
  if (S.filter.sort === "name") {
    box.innerHTML = groupHtml("全部活动", rows) ;
  } else {
    box.innerHTML = groupTimetable(rows);
  }
  S.rowStates = new Map(rows.map((item) => [String(item.id), stateOf(item)]));
  box.querySelectorAll(".row").forEach((tr) => {
    const id = tr.dataset.id;
    S.rowEls.set(id, {
      row: tr,
      check: tr.querySelector(".row-check"),
      left: tr.querySelector(".c-state .left"),
      ago: tr.querySelector(".c-state .small"),
      state: tr.querySelector(".c-state"),
    });
  });
  const nowChip = box.querySelector("[data-now]");
  if (nowChip) {
    const bnMs = businessNowMs();
    nowChip.textContent = bnMs == null ? "待校准" : hm(bnMs);
  }
}

function skeletonHtml() {
  let html = '<div class="skel" aria-label="正在加载活动列表">';
  for (let i = 0; i < 6; i += 1) html += '<div class="skel-row"><i></i><i></i><i></i><i></i><i></i></div>';
  return html + "</div>";
}

/* 按天分组的车站时刻表：正在开放 → 「现在」线 → 按天 → 待公布 → 刚截止 → 已报名 */
function groupTimetable(rows) {
  const open = [];
  const unknown = [];
  const closed = [];
  const signed = [];
  const days = new Map();
  const now = businessNowMs() || 0;   // 仅兜底分桶键；分类由 stateOf 决定
  rows.forEach((item) => {
    const st = stateOf(item);
    if (st === "open") open.push(item);
    else if (st === "waiting") {
      const key = String(startOfDay(toTs(item.sign_start) * 1000 || now));
      if (!days.has(key)) days.set(key, []);
      days.get(key).push(item);
    }
    else if (st === "unknown" && !signStartKnown(item)) unknown.push(item);
    else if (st === "unknown") {
      /* 开放时间已知、只是本机暂时没有可信现在：保留绝对日期分组，
       * 状态列标「待校准」，不能整体塞进「报名时间待公布」。 */
      const key = String(startOfDay(toTs(item.sign_start) * 1000));
      if (!days.has(key)) days.set(key, []);
      days.get(key).push(item);
    }
    else if (st === "closed") closed.push(item);
    else signed.push(item);
  });
  const byStart = (a, b) => (toTs(a.sign_start) || Infinity) - (toTs(b.sign_start) || Infinity) || String(a.name).localeCompare(String(b.name), "zh");
  let html = "";
  if (open.length) html += groupHtml("正在开放", open.sort(byStart), "open", "现在就能报");
  if (open.length || days.size) html += nowlineHtml();
  Array.from(days.keys()).sort((a, b) => Number(a) - Number(b)).forEach((key) => {
    const dayMs = Number(key);
    html += groupHtml(dayLabel(dayMs), days.get(key).sort(byStart), "day");
  });
  if (unknown.length) html += groupHtml("报名时间待公布", unknown.sort(byStart), "unknown", "公布以后才能到点报名");
  if (closed.length) html += groupHtml("刚截止", closed.sort(byStart), "closed", "截止一小时内的还留着");
  if (signed.length) html += groupHtml("已报名", signed.sort(byStart), "signed");
  return html;
}

function groupHtml(label, items, kind, note) {
  return '<section class="gsec" data-g="' + (kind || "") + '"><div class="grp"><b>' + esc(label) + "</b>" +
    (note ? "<span>" + esc(note) + "</span>" : "") + '<span class="count">' + items.length + " 场</span></div>" +
    items.map(rowHtml).join("") + "</section>";
}

function nowlineHtml() {
  const bnMs = businessNowMs();
  return '<div class="nowline" aria-hidden="true"><span class="nowline-chip">现在 <b data-now>' + (bnMs == null ? "待校准" : hm(bnMs)) + "</b></span></div>";
}

function rowHtml(item) {
  const id = String(item.id);
  const state = stateOf(item);
  const selected = S.selected.has(id);
  const invalid = !validActivityTime(item);
  const conflict = conflictIds().has(id);
  const armed = armedTaskByActivity(id);
  const classes = ["row"];
  if (selected) classes.push("is-selected");
  if (item.signed) classes.push("is-signed");
  if (state === "closed") classes.push("is-closed");
  if (state === "unknown" || invalid) classes.push("is-unknown");
  if (conflict) classes.push("is-conflict");
  if (armed) classes.push("is-armed");
  const leftText = leftTextOf(item);
  /* 已截止活动保留在列表里查看，但复选框禁用；已报名同理 */
  const checkDisabled = S.busy || !isEligibleForPlan(item);
  /* 学校常把开放时间定在 00:59:59 这种点：有秒就把秒写成小号 */
  let timeHtml = '<span class="t">--:--</span>';
  if (toTs(item.sign_start) !== null) {
    const text = String(item.sign_start || "");
    const mm = text.match(/^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})(?::(\d{2}))?/);
    if (mm) {
      timeHtml = '<span class="t">' + esc(mm[4] + ":" + mm[5]) + "</span>" + (mm[6] && mm[6] !== "00" ? '<span class="s">:' + esc(mm[6]) + "</span>" : "");
      const bnMs = businessNowMs();
      const dayDiffMs = bnMs == null ? null : startOfDay(toTs(item.sign_start) * 1000) - startOfDay(bnMs);
      if (state !== "waiting" && dayDiffMs !== null && Math.round(dayDiffMs / 86400000) !== 0) timeHtml += '<span class="d">' + esc(relDayText(toTs(item.sign_start) * 1000)) + "</span>";
    }
  }
  const sub = [];
  if (state === "open" && toTs(item.sign_end) !== null) sub.push("<span>报名截止 " + esc(whenText(toTs(item.sign_end) * 1000)) + "</span>");
  /* 活动行显示自己的执行模式：预报名 / 立即报名（抢名额）；已截止由状态格标示 */
  if (state === "waiting" || state === "open") {
    sub.push('<span class="activity-flags"><span class="activity-flag">' + esc(modeText(modeOf(item))) + "</span></span>");
  }
  sub.push("<span" + (invalid ? ' class="is-unknown"' : "") + ">" + esc(activityTimeText(item)) + "</span>");
  /* 学校列表还没同步回 signed=true：说清「已确认，只是列表在同步」，
   * 不把这场重新画成可报名。 */
  if (item.sync_pending && state === "signed") {
    sub.push('<span class="sync-note">报名已确认，学校列表正在同步</span>');
  }
  const flags = requirementFlags(item);
  return "<div class=\"" + classes.join(" ") + "\" data-id=\"" + esc(id) + "\" aria-label=\"" + esc(item.name) + "\">" +
    "<div class=\"c-sel\"><input class=\"row-check\" type=\"checkbox\" data-id=\"" + esc(id) + "\"" + (selected ? " checked" : "") + (checkDisabled ? " disabled" : "") + " aria-label=\"选择" + esc(item.name) + "\"></div>" +
    "<div class=\"c-time\">" + timeHtml + "</div>" +
    "<div class=\"c-main\"><div class=\"name\" title=\"" + esc(item.name) + "\">" + esc(item.name) + "</div><div class=\"sub\">" + sub.join("") + flags + "</div></div>" +
    "<div class=\"c-place\" tabindex=\"0\" role=\"button\" aria-expanded=\"false\" aria-label=\"地点：" + esc(item.place || "地点待定") + "，按回车展开全部\" title=\"" + esc(item.place || "地点待定") + "\">" + esc(item.place || "地点待定") + "</div>" +
    "<div class=\"c-state\">" + stateCellHtml(item, state, leftText, armed) + "</div>" +
    "</div>";
}

function stateCellHtml(item, state, leftText, armed) {
  const nextId = nextOpen().bestId;
  if (armed) return '<span class="state-tag state-armed">' + esc(taskDisplayLabel(armed)) + "</span>";
  if (state === "waiting") {
    const isNext = String(item.id) === String(nextId);
    return "<span class=\"left" + (leftText !== "待公布" && parseLeft(item) <= 600 ? " is-soon" : "") + "\">" + esc(leftText) + "</span>" +
      (isNext ? '<span class="state-tag state-next">下一场</span>' : "");
  }
  if (state === "open") return '<span class="state-tag state-open">开放中</span><span class="small" data-ago>已开放 ' + durText(-leftOf(item)) + "</span>";
  if (state === "signed") return '<span class="state-tag state-signed">已报名</span>';
  if (state === "closed") return '<span class="state-tag state-closed">已截止</span>';
  return '<span class="state-tag state-unknown">' +
    (signStartKnown(item) ? "待校准" : "待公布") + "</span>";
}

function parseLeft(item) {
  const left = leftOf(item);
  return left === null ? Infinity : left;
}

/* 空列表的登录四态 → 下一步。检查没结束时不许提前宣布有效；
 * 结论缺失或无法判定一律按 unknown 处理——把「还不知道」说成
 * 「登录是有效的」比说「暂时无法确认」更伤人。 */
function loginEmptyState() {
  if (S.login.checking) return "checking";
  if (S.login.present === false || S.login.result === "none") return "none";
  if (S.login.result === "expired") return "expired";
  if (S.login.result === "valid") return "valid";
  return "unknown";
}

/* 登录结论变化会影响空态文案（checking → valid/expired/unknown）。
 * 有列表数据时空态走筛选/错误分支，与登录无关；骨架在场时也不插手。 */
function refreshLoginEmptyState() {
  if (S.items.length) return;
  if (S.listState === "loading") return;
  paintEmpty([]);
}

function paintEmpty(rows) {
  const empty = $("#empty");
  const title = $("#empty-title");
  const text = $("#empty-text");
  const action = $("#empty-action");
  if (rows.length) {
    empty.hidden = true;
    return;
  }
  empty.hidden = false;
  action.hidden = false;
  /* 动作意图每次渲染都写清楚：handler 只认这个键，
   * 不按 items.length 猜（有缓存时的失败重试会被猜成清筛选）。 */
  action.dataset.action = "refresh";
  if (S.runtime.error) {
    /* 缺组件不许显示成「登录后查看活动」：重登解决不了缺组件。
     * 完整原因与恢复动作只在顶部横幅——这里只放未加载占位，
     * 不复读长说明，也不重复恢复按钮。 */
    title.textContent = "活动列表未加载";
    text.textContent = "组件有问题，处理办法看页面顶部的提示。";
    action.hidden = true;
    return;
  }
  if (S.listState === "error") {
    /* 普通列表失败：完整原因与「重试」唯一归这里（用真实失败原话归因） */
    const copy = listErrorCopy(S.listError || "");
    title.textContent = copy.title;
    text.textContent = copy.text;
    action.textContent = "重试";
    return;
  }
  if (S.items.length) {
    /* 有数据但都被筛掉：只有这一种空态给「清除筛选」 */
    title.textContent = "没有符合筛选的活动";
    text.textContent = "一共 " + S.items.length + " 场，换个关键词，或清除筛选看完整列表。";
    action.textContent = "清除筛选";
    action.dataset.action = "clear-filters";
    return;
  }
  if (S.listState !== "ready") {
    /* 还没成功加载过一次（idle，或首屏加载中）：未知占位，
     * 不能趁这时候说「学校没有活动」，也不能先让用户去登录 */
    title.textContent = "还没有加载活动列表";
    text.textContent = "点「刷新」加载一次；需要登录时，顶部会提示下一步。";
    action.textContent = "刷新";
    return;
  }
  /* 以下都是「学校成功返回了 0 场」：按登录状态给各自下一步 */
  const login = loginEmptyState();
  if (login === "checking") {
    /* 校验还没回来：不提前说有效，也不逼用户重登 */
    title.textContent = "正在检查登录状态";
    text.textContent = "正在核对已保存的登录，稍等一下；这段时间先不用重新登录。";
    action.textContent = "刷新";
    return;
  }
  if (login === "none") {
    title.textContent = "登录后查看活动";
    text.textContent = "点右上角「登录」，在打开的浏览器中完成登录，然后点「刷新」。";
    action.textContent = "刷新";
    return;
  }
  if (login === "expired") {
    title.textContent = "登录状态失效，先重新登录";
    text.textContent = "点右上角「重新登录」，完成后点「刷新」。";
    action.textContent = "刷新";
    return;
  }
  if (login === "unknown") {
    title.textContent = "暂时无法确认登录状态";
    text.textContent = "请检查网络后点「刷新」。";
    action.textContent = "刷新";
    return;
  }
  title.textContent = "学校当前没有返回活动";
  text.textContent = "登录是有效的，学校这次没有返回可报名活动；稍后点「刷新」再看。";
  action.textContent = "刷新";
}

function paintCount(rows) {
  $("#count").textContent = S.listState === "loading" ? "正在刷新…" : (rows.length + " / " + S.items.length + " 场");
}

/* ---------------------------------------------------------------- 大屏 */
function paintRail() {
  /* 可报名指标排除已截止：与筛选、开始门禁同一份 isEligibleForPlan 口径 */
  const free = S.items.filter(isEligibleForPlan).length;
  /* 已知零 ≠ 未知：成功返回过（或手里还有缓存快照）就写真实数字，
   * 只有还没拉到时才用破折号，避免把「0 场」说成「还没获取」。 */
  const countsKnown = S.listState === "ready" || S.items.length > 0;
  const freeKnown = bnowS() !== null;
  $("#metric-total").textContent = countsKnown ? String(S.items.length) : "—";
  /* 可报名数依赖当前是否已截止：没有可信现在时只能说「待确认」，不能报 0/6 */
  $("#metric-free").textContent = countsKnown
    ? (freeKnown ? String(free) : "待确认") : "—";
  /* metric-next（LED 标注）统一由 paintLedLabel 维护，这里不再抢写 */
  let text = "先选活动，系统会检查举办时间冲突。";
  if (!S.busy && S.login.present === false) text = "完成登录后选活动。";
  else if (S.busy && S.plan.id) {
    /* 大屏摘要与标题/LED/任务卡同一套阶段解释：等开放 / 准备提交 / 正在提交 */
    const view = executionView();
    /* 单场时不再反复说「本批 1 场 · 每场独立」：同一句话在三处复读（R18） */
    text = view.count <= 1
      ? (view.phase === "submitting" ? "正在提交这一场。"
        : view.phase === "arming" ? "已开放，正在准备提交。"
        : view.phase === "waiting" ? "这一场等开放 · 到点自动开始。"
        : "计划运行中。")
      : view.phase === "submitting" ? "正在提交 " + view.count + " 场 · 每场结果单独显示。"
      : view.phase === "arming" ? "已开放，正在准备提交。"
      : view.phase === "waiting" ? "同时开放 " + view.count + " 场 · 开放后自动开始。"
      : "计划运行中 · 每场任务独立显示结果。";
  }
  else if (S.plan.status === "failed" && planOutcome().unknown > 0) text = "计划已结束 · " + unknownLabel(planOutcome()) + "，去活动页确认。";
  else if (S.plan.status === "partial") text = "计划部分完成 · 失败的场次可以单独查看。";
  else if (S.plan.status === "completed") text = "计划已完成 · 建议去活动页确认一眼。";
  else if (S.plan.status === "stopped") {
    text = "计划已停止 · 已提交的场次保留原状态。"
      + (planOutcome().unknown ? " " + unknownLabel(planOutcome()) + "，去活动页核对。" : "");
  }
  else if (S.selected.size) text = "已选 " + S.selected.size + " 场 · 先看右侧冲突提示。";
  $("#rail-message").textContent = text;
}

/* ---------------------------------------------------------------- 运行目标 */
/* 还能动的任务状态：只有这三种参与「当前目标、当前批次、倒计时」。
 * 已取消/已成功/已报名/已失败是终态，不许再充当运行目标或剩余场次。 */
const RUNNING_STATUSES = ["queued", "waiting", "submitting"];

/* 任务 → 可显示项：列表项在就用真列表项；不在（被筛掉、刷新掉了、快照
 * 先到）就用任务自带字段现构。缺字段显示未知，绝不回退到另一场活动。 */
function taskDisplayItem(task) {
  const found = findItem(task && task.activity_id);
  if (found) return found;
  if (!task) return null;
  return {
    id: String(task.activity_id || task.task_id || ""),
    name: String(task.name || "这一场活动"),
    sign_start: task.sign_start || "",
    sign_end: task.sign_end || "",
    act_start: task.act_start || "",
    act_end: task.act_end || "",
    place: task.place || "",
    signed: false,
    fromTask: true,
  };
}

/* 工作状态 → 显示阶段（纯函数，只读真实时间与提交证据）。
 * 后端在拉起 runner **之前**就把任务置 submitting，这也包含预报名开放前
 * lead_s 秒打开活动页的预热（signup_plan.py 的 _run_one / _prepare_ts），
 * 所以原始状态不能直接当「正在提交」说话：
 *   preopen   还没到原定开放（哪怕 worker 已经在开页）
 *   preparing 已开放，但还没有一次真正发起的提交
 *   retrying  有真实提交且仍非终态（attempts 是唯一「已提交」证据）
 *   terminal  终态，不参与运行目标
 * 只影响展示，不写回 task.status、不改协调器与取消条件。 */
function taskDisplayPhase(task, now) {
  if (!task) return "terminal";
  const status = String(task.status || "");
  if (RUNNING_STATUSES.indexOf(status) < 0) return "terminal";
  const attempts = Number(task.attempts);
  if (Number.isFinite(attempts) && attempts > 0) return "retrying";
  const at = Number.isFinite(now) ? now : bnowS();
  const open = toTs(task.sign_start);
  const mode = String(task.signup_mode || "");
  /* 立即模式本来就不等开放：时间不可读时也不能说成「等待开放」 */
  if (at == null) return mode === "instant_signup" ? "preparing" : "preopen";
  /* 开放时间未知时只能「等」：不能因为算不出时间就说已经开放 */
  if (open === null) return mode === "instant_signup" ? "preparing" : "preopen";
  if (open > at) return "preopen";
  return "preparing";
}

/* 任务卡的阶段标签：raw「等待开放」在已开放时是假话，按显示阶段改写 */
function taskDisplayLabel(task) {
  const phase = taskDisplayPhase(task);
  if (phase === "preopen") return "等待开放";
  if (phase === "preparing") return "准备报名";
  if (phase === "retrying") return "提交中";
  const state = String(task && task.status || "queued");
  if (state === "failed") {
    /* 结果没确认 / 登录状态未知都不是「失败」：标签必须留出核对空间 */
    if (task.stop_reason === "unconfirmed") return "待核对";
    if (task.stop_reason === "auth_unavailable") return "未提交";
  }
  return taskLabel(state);
}

/* 任务卡的模式前缀（混合计划每场标自己的模式）：taskHtml 与 tick 共用一份 */
function taskModePrefix(task) {
  const state = String(task && task.status || "queued");
  const mode = String(task && (task.signup_mode || modeOf(task)));
  return RUNNING_STATUSES.indexOf(state) >= 0 && modeText(mode) ? modeText(mode) + " · " : "";
}

/* 任务卡的说明行：与标题/LED 同一解释，tick 每秒复用同一份 */
function taskDisplayDetail(task, now) {
  const phase = taskDisplayPhase(task, now);
  if (phase === "preopen") {
    const open = toTs(task && task.sign_start);
    return open === null ? "提前准备，开放时间待公布" : "提前准备，等到 " + fmtClock(open) + " 开放";
  }
  if (phase === "preparing") return "已开放，正在准备提交";
  if (phase === "retrying") return submittingDetail(task);
  return "";
}

/* 运行目标的唯一只读模型（标题、路线、LED、批次数共用一份）：
 * 只读 S.plan.tasks 快照，不读也不改 S.selected——原勾选继续服务草稿与重开。
 * phase: idle / preparing（还没任务）/ waiting（等开放）/ arming（已开放未提交）
 *        / submitting（已有真实提交）/ settling（全终态、plan_done 未到）。
 * 目标优先级：真实重试 > 已开放准备 > 未来预热；同阶段取更早的开放时刻，
 * 并列时保持快照顺序。未运行时返回 idle，调用方回落到用户选择的预览态。 */
function executionView() {
  const empty = { phase: "idle", display: "idle", task: null, item: null,
                  count: 0, openAt: null, retryUntil: null };
  if (!S.busy || !S.plan.id) return empty;
  const now = bnowS();
  const tasks = Array.from(S.plan.tasks.values());
  const running = tasks.filter((task) => RUNNING_STATUSES.indexOf(String(task.status)) >= 0);
  if (!running.length) {
    /* 还没有任务 = 准备中；任务都结束但 plan_done 未到 = 汇总中。
     * 两种情况都不许提前显示计划成功。 */
    return Object.assign({}, empty, { phase: tasks.length ? "settling" : "preparing" });
  }
  /* 快照顺序做初值：并列时不会被排序打乱 */
  const stages = running.map((task) => ({
    task: task,
    display: taskDisplayPhase(task, now),
    at: toTs(task.sign_start),
  }));
  const earliest = (list) => {
    let best = list[0];
    let bestAt = best.at === null ? Infinity : best.at;
    for (let i = 1; i < list.length; i += 1) {
      const at = list[i].at === null ? Infinity : list[i].at;
      if (at < bestAt) { best = list[i]; bestAt = at; }
    }
    return best;
  };
  const batchCount = (signStart) => running.filter(
    (task) => String(task.sign_start || "") === String(signStart || "")).length;

  const retrying = stages.filter((stage) => stage.display === "retrying");
  if (retrying.length) {
    /* 真正在提交：取真实重试截止最早的一场；未知截止排最后 */
    let best = retrying[0];
    let bestAt = effectiveRetryDeadline(best.task, now);
    for (let i = 1; i < retrying.length; i += 1) {
      const at = effectiveRetryDeadline(retrying[i].task, now);
      if (at !== null && (bestAt === null || at < bestAt)) { best = retrying[i]; bestAt = at; }
    }
    return { phase: "submitting", display: "retrying", task: best.task,
             item: taskDisplayItem(best.task), count: retrying.length,
             openAt: null, retryUntil: bestAt };
  }
  const arming = stages.filter((stage) => stage.display === "preparing");
  if (arming.length) {
    /* 已开放但还没发起提交：说「准备报名」，不说「还在等开放」 */
    const best = earliest(arming);
    return { phase: "arming", display: "preparing", task: best.task,
             item: taskDisplayItem(best.task), count: batchCount(best.task.sign_start),
             openAt: best.at, retryUntil: null };
  }
  const preopen = stages.filter((stage) => stage.display === "preopen");
  const best = earliest(preopen);
  return { phase: "waiting", display: "preopen", task: best.task,
           item: taskDisplayItem(best.task), count: batchCount(best.task.sign_start),
           openAt: best.at, retryUntil: null };
}

function paintSelected() {
  const box = $("#selected");
  const meta = $("#selected-meta");
  const boardText = $("#board-text");
  const items = selectedItems();
  const next = boardTargetItem();
  /* 运行中以 executionView() 为唯一目标：标题、路线、LED、批次数同源，
   * 不再从原勾选集合里补位（取消的场次不能继续当目标）。 */
  const view = executionView();
  box.className = "b-title";
  meta.innerHTML = "";
  boardText.textContent = "";
  if (!S.busy && S.login.present === false && !S.runtime.error) {
    setBoardTag("首次使用", "off", true);
    box.textContent = "登录后开始";
    boardText.textContent = "活动会在登录完成后自动加载。";
    return;
  }
  if (!S.busy && S.runtime.error && S.listState !== "ready") {
    /* 组件问题只留短状态：完整原因与恢复动作在顶部横幅，不在大屏复读 */
    setBoardTag("组件缺失", "red", true);
    box.textContent = runtimeErrorCopy().title;
    boardText.textContent = "处理办法看页面顶部的提示。";
    return;
  }
  if (!S.busy && S.listState === "error" && !S.items.length) {
    /* 列表失败只留短状态：完整原因与「重试」在列表空态 */
    setBoardTag("需重试", "red", true);
    box.textContent = "活动暂时没加载";
    boardText.textContent = "具体原因看下面列表的提示。";
    return;
  }
  if (!S.busy && S.listState === "loading" && !S.items.length) {
    /* 列表还在拉：不得把「近期没有要开放的活动」当事实说出口 */
    setBoardTag("加载中", "off", true);
    box.textContent = "活动列表正在加载";
    boardText.textContent = "正在加载活动，先等这一次加载结束。";
    return;
  }
  if (S.busy) {
    /* 运行中：标题与批次数都来自同一个执行目标，单场结束不会把这里改成结论。
     * 三个阶段分开说：等开放 ≠ 已开放待提交 ≠ 真正在提交。 */
    if (view.phase === "submitting" || view.phase === "arming" || view.phase === "waiting") {
      const tone = view.phase === "submitting" ? "red" : "amber";
      const tag = view.phase === "submitting" ? "正在提交"
        : view.phase === "arming" ? "准备报名" : "等开放";
      setBoardTag(tag, tone, true);
      box.textContent = view.item ? view.item.name : "这一场活动";
      meta.innerHTML = view.item ? boardMetaHtml(view.item) : "";
      boardText.textContent = view.count <= 1
        ? (view.phase === "submitting" ? "正在提交这一场。"
          : view.phase === "arming" ? "已开放，正在准备提交。"
          : "这一场等开放 · 到点自动开始。")
        : view.phase === "submitting"
          ? "正在提交 " + view.count + " 场 · 每场任务独立显示结果。"
          : view.phase === "arming"
            ? "已开放，正在准备 " + view.count + " 场 · 每场任务独立显示结果。"
            : "同时开放 " + view.count + " 场 · 每场结果单独显示。";
      return;
    }
    /* 没有可执行任务、plan_done 还没到：准备中 / 正在汇总，不提前报成功 */
    setBoardTag(view.phase === "settling" ? "汇总中" : "准备中", "amber", true);
    box.textContent = view.phase === "settling" ? "正在汇总结果" : "正在准备报名";
    boardText.textContent = view.phase === "settling"
      ? "每一场都结束了，正在汇总这一轮的结果。"
      : "计划已经接管，正在核对每一场的开放时间。";
    return;
  }
  if (!items.length) {
    if (next) {
      const openNow = stateOf(next) === "open";
      setBoardTag(openNow ? "开放中" : "下一场", "amber", !openNow);
      box.textContent = next.name;
      meta.innerHTML = boardMetaHtml(next);
      boardText.textContent = openNow ? "这场已经开放，现在可以选中后开始报名计划。" : "选择活动后可在此查看报名计划。";
    } else if (S.listState === "ready" && !S.items.length) {
      /* 学校成功返回 0 场：大屏只留一句短状态，完整原因在列表空态，不在这里复读 */
      setBoardTag("暂时没有", "off", true);
      box.textContent = "学校这次没有返回活动";
      boardText.textContent = "稍后点「刷新」再看。";
    } else if (S.listState === "ready" && bnowS() === null && !S.demo) {
      /* 没有可信现在：不能断言「没有可报名的场次」，也不能显示伪计数。
       * 只给短状态和单一恢复入口（打开时间详情重新校准）。 */
      setBoardTag("时间待校准", "amber", true);
      box.textContent = "时间待校准";
      boardText.innerHTML = "校准北京时间后才能判断哪些场次可以报名。" +
        '<button type="button" class="btn btn-line btn-mini" data-open-clock>查看时间详情</button>';
    } else if (S.listState === "ready") {
      /* 列表里有活动，只是没有可执行目标（全已报名 / 全已截止 / 报名时间待公布）：
       * 这不是「学校没返回」，数据仍在列表里，别替学校下结论。 */
      setBoardTag("暂时没有", "off", true);
      box.textContent = "没有可报名的场次";
      boardText.textContent = "列表里的活动已报名、已截止或还没公布报名时间。";
    } else {
      setBoardTag("暂时没有", "off", true);
      box.textContent = "近期没有要开放的活动";
      boardText.textContent = "可能还没公布报名时间，也可能都报过了。过一会儿点「刷新」看看。";
    }
    return;
  }
  const plannedUnknown = planOutcome().unknown;
  if (S.plan.status === "completed") setBoardTag("报名成功", "green", true);
  else if (S.plan.status === "partial") setBoardTag("部分完成", "amber", true);
  else if (S.plan.status === "failed" && plannedUnknown > 0) {
    setBoardTag(unknownLabel(planOutcome()).replace(/^有 /, ""), "amber", true);
  } else if (S.plan.status === "stopped" && plannedUnknown > 0) {
    /* 停止是主结论，未知并列：不亮红灯，也不说成全部取消 */
    setBoardTag("已停止 · " + unknownLabel(planOutcome()).replace(/^有 /, ""), "amber", true);
  } else if (S.plan.status === "stopped") setBoardTag("计划已停止", "off", true);
  else setBoardTag("报名计划", "amber", true);
  const groups = groupSelected(items);
  /* 混合计划摘要按场显示模式：预报名场次等开放，立即报名/抢名额场次马上执行 */
  const summary = groups.map((group) => {
    const mode = modeText(modeOf(group.items[0]));
    return shortTime(group.key) + " " + (mode ? mode + " " : "") + group.items.length + " 场";
  }).join(" · ");
  const list = items.slice(0, 4).map((item) => "<span>" + esc(item.name) + "</span>").join("");
  const extra = items.length > 4 ? "<span>还有 " + (items.length - 4) + " 场…</span>" : "";
  if (items.length === 1) {
    box.textContent = items[0].name;
    meta.innerHTML = boardMetaHtml(items[0]);
    boardText.textContent = "在下面的时刻表里确认活动，再开始报名计划。";
  } else {
    box.textContent = "已选 " + items.length + " 场";
    meta.innerHTML = boardMetaHtml(items[0]);
    boardText.innerHTML = esc(summary) + (list || extra ? '<span class="selected-list">' + list + extra + "</span>" : "");
  }
}

function setBoardTag(text, tone, dot) {
  const tag = $("#board-tag");
  const label = $("#board-tag-text");
  if (!tag || !label) return;
  tag.dataset.tone = tone || "amber";
  tag.dataset.dot = dot === false ? "false" : "true";
  label.textContent = text;
}

/* 整屏视觉的显式状态：运行中/准备中保持运行色，只有计划级终态才整屏
 * 绿/红/警示/中性；选择集合变了就回到未终结的常态（与 LED 同一口径）。 */
/* 大屏整屏状态：与卡片/总结/LED 共用同一份展示结论。
 * 含「待核对/未提交」的未知计划不得画成确定失败的红色边框——底层业务
 * status 不变，只是展示语义不能替用户宣布失败（FR06）。 */
function boardPlanState() {
  if (S.busy) return "running";
  const status = String(S.plan.status || "");
  if (["completed", "failed", "partial", "stopped"].indexOf(status) < 0) return "idle";
  if (S.plan.doneSelection !== selectionSignature()) return "idle";
  if (status !== "completed" && planOutcome().unknown > 0) return "attention";
  return status;
}

function paintBoardState() {
  const board = $("#board");
  if (!board) return;
  board.dataset.planState = boardPlanState();
}

function boardMetaHtml(item) {
  if (!item) return "";
  const placeText = String(item.place || "地点待定");
  const place = esc(placeText);
  const act = esc(activityTimeText(item));
  /* 大屏地点默认两行；按钮原生支持 Enter/Space，聚焦即可展开全文（R19）。
   * 表格的 .c-place 用同一套「两行 + 聚焦展开」口径，但互不依赖。 */
  return '<button type="button" class="b-place" data-place-toggle aria-expanded="false"'
    + ' title="' + esc("完整地点：" + placeText + "（Enter 展开/收起）") + '">'
    + '<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 21s-6-5.3-6-10a6 6 0 1 1 12 0c0 4.7-6 10-6 10z"/><circle cx="12" cy="11" r="2.2"/></svg>'
    + '<span class="b-place-text">' + place + '</span></button>'
    + '<span><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="5" width="16" height="15" rx="2"/><path d="M4 10h16M9 3v4M15 3v4"/></svg>' + act + "</span>";
}

/* 表格地点展开/收起：只改当前控件与 aria-expanded，不重绘整块，
 * 键盘焦点不会丢，也不会因此触发勾选或开始计划。 */
function togglePlace(el) {
  if (!el || !el.getAttribute) return;
  const open = el.getAttribute("aria-expanded") === "true";
  el.setAttribute("aria-expanded", open ? "false" : "true");
  if (el.classList) el.classList.toggle("is-open", !open);
}

/* 大屏地点展开/收起：按钮原生处理 Enter/Space，这里只翻转 aria-expanded。
 * 只改当前按钮，不重绘整块，键盘焦点不会丢。 */
function bindPlaceToggle() {
  const box = $("#selected-meta");
  if (!box || !box.addEventListener) return;
  box.addEventListener("click", (event) => {
    const target = event.target;
    const btn = target && target.closest ? target.closest("[data-place-toggle]") : null;
    if (!btn) return;
    const open = btn.getAttribute("aria-expanded") === "true";
    btn.setAttribute("aria-expanded", open ? "false" : "true");
  });
}

function paintBoardActions() {
  const actions = $("#route-actions");
  const quickLogin = $("#btn-board-login");
  const quickPick = $("#btn-board-pick");
  const checks = $("#plan-checks");
  const headed = $("#headed-label");
  const clear = $("#btn-clear-plan");
  const start = $("#btn-plan");
  const policyFor = $("#retry-for") ? $("#retry-for").closest(".policy-select") : null;
  const policyMax = $("#max-attempts") ? $("#max-attempts").closest(".policy-select") : null;
  const grow = actions.querySelector(".grow");
  if (!actions || !quickLogin || !quickPick) return;
  if (S.busy) {
    actions.hidden = true;
    quickLogin.hidden = true;
    quickPick.hidden = true;
    if (grow) grow.hidden = false;
    return;
  }
  actions.hidden = false;
  /* 缺组件时不给快捷登录入口（点了只会拿到结构化失败）；「刷新」可复查恢复 */
  const needLogin = S.login.present === false && !S.runtime.error;
  const next = nextOpen().bestId ? findItem(nextOpen().bestId) : null;
  /* 快捷「选这一场」只在真正空闲时占据操作区：已经结束的计划还要保留
   * 「清空选择/再看结果」入口，不能因为成功场次已从选择集合移除就把它藏掉。 */
  const showPick = !needLogin && !selectedItems().length && !S.plan.tasks.size && !!next;
  quickLogin.hidden = !needLogin;
  quickPick.hidden = !showPick;
  if (grow) grow.hidden = needLogin || showPick;
  [checks, headed, clear, start, policyFor, policyMax].forEach((el) => { if (el) el.hidden = needLogin || showPick; });
}

/* 空选状态优先展示当前开放场次；没有开放场次时再展示最近的未来场次。 */
function boardTargetItem() {
  const next = nextOpen();
  if (next.bestId) return findItem(next.bestId);
  if (next.openNow) return S.items.find((item) => !item.signed && stateOf(item) === "open") || null;
  return null;
}

/* 加入计划前的检查条：只提醒、不硬挡（登录到期与 24 小时提醒都按原型规则） */
function paintChecks() {
  const box = $("#plan-checks");
  const items = selectedItems();
  if (!items.length || S.busy) {
    box.innerHTML = "";
    return;
  }
  const checks = [];
  if (!S.online) checks.push({ tone: "bad", text: "网络断开了" });
  const expireAt = S.login.expireAt;
  /* 登录到期按「这次报名窗口」判断：未来场次等原定开放，已开放场次
   * 用本次执行窗口（现在起算的策略时长，sign_end 截断），
   * 不能拿历史 sign_start 误判「登录撑得到开放」。 */
  const executionEnd = earliestSelectedExecutionEnd();
  if (expireAt && executionEnd !== null) {
    /* 登录剩余是 saved_at + 17.5h 的估算，不是学校给的到期时刻：
     * 只能说「预计」，不能保证一定撑得到（R15） */
    if (expireAt < executionEnd * 1000) checks.push({ tone: "warn", text: "登录估算约 " + whenText(expireAt) + " 过期，可能赶不上这次报名窗口，建议先重新登录" });
    else checks.push({ tone: "ok", text: "预计可覆盖本次窗口；临近开放建议再确认" });
  }
  /* 时间检查只表达能否开始：误差、来源和返校粗测都放时间详情，不进这里 */
  if (!S.demo) {
    const ui = clockUiState();
    if (ui === "unavailable") checks.push({ tone: "warn", text: "北京时间待校准，校准后才能开始计划" });
    else if (ui === "holdover") checks.push({ tone: "warn", text: "正在使用上次校准结果，工具会自动重新校准" });
    else checks.push({ tone: "ok", text: "报名时间按北京时间执行" });
  }
  const earliest = earliestSelectedOpen();
  const bnLeft = bnLeftSec(earliest);
  if (bnLeft !== null && bnLeft > 86400) checks.push({ tone: "warn", text: "开放在 24 小时以后" });
  box.innerHTML = checks.map((chk) => '<span class="chk" data-tone="' + chk.tone + '">' + esc(chk.text) + "</span>").join("");
}

function earliestSelectedOpen() {
  let best = null;
  selectedItems().forEach((item) => {
    const ts = toTs(item.sign_start);
    if (ts !== null && (best === null || ts < best)) best = ts;
  });
  return best;
}

/* 登录到期检查用的统一时点：未来场次取「原定开放 + 重试窗口」，
 * 已开放场次取「本次执行窗口」（现在起算的策略时长，sign_end 截断）；
 * 取全计划最早的一个，与 effectiveRetryDeadline 同一套推导口径。 */
function earliestSelectedExecutionEnd() {
  const now = bnowS();
  if (now === null) return null;   /* 时钟未知：不编执行窗口，登录到期检查跳过 */
  let best = null;
  selectedItems().forEach((item) => {
    const open = toTs(item.sign_start);
    if (open === null) return;
    const retryFor = Number(S.retryPolicy.retry_for_s);
    const signEnd = toTs(item.sign_end);
    const base = open > now ? open : now;
    let end = Number.isFinite(retryFor) && retryFor > 0 ? base + retryFor
      : (signEnd !== null ? signEnd : base + RETRY_WINDOW_S);
    if (signEnd !== null) end = Math.min(end, signEnd);
    if (best === null || end < best) best = end;
  });
  return best;
}

function paintPlan() {
  const conflictBox = $("#plan-conflicts");
  const validation = validateSelection();
  const messages = [];
  if (S.selectionNotice) messages.push("<div class=\"conflict-item\"><strong>选择提醒</strong><br>" + esc(S.selectionNotice) + "</div>");
  validation.errors.forEach((error) => messages.push("<div class=\"conflict-item\"><strong>" + esc(error.title) + "</strong><br>" + esc(error.text) + "</div>"));
  validation.conflicts.forEach((conflict) => messages.push("<div class=\"conflict-item\"><strong>活动时间重叠</strong><br>「" + esc(conflict.left.name) + "」与「" + esc(conflict.right.name) + "」举办时段重叠（" + esc(shortTime(conflict.left.act_start)) + " – " + esc(shortTime(conflict.left.act_end)) + "），只能选其中一场。</div>"));
  conflictBox.innerHTML = messages.join("");
  conflictBox.hidden = messages.length === 0;

  const tasksBox = $("#plan-tasks");
  const tasks = Array.from(S.plan.tasks.values());
  /* 同一帧用同一个业务时刻；未知传 null，由任务卡显示「等待开放/待校准」 */
  const now = bnowS();
  tasksBox.innerHTML = tasks.length ? tasks.map((task) => taskHtml(task, now)).join("") : "";
  paintCountdown();
}

/* 原型中的报名线路图：同时表达学校原定开放时间和本次实际执行窗口，
 * 多活动计划仍在任务卡里显示每场状态，不把多场压成一条假线路。 */
function paintRoute() {
  const box = $("#route");
  const line = $("#route-line");
  if (!box) return;
  const target = routeTarget();
  if (!target || !toTs(target.item.sign_start)) {
    box.innerHTML = "";
    if (line) line.hidden = true;
    return;
  }
  if (line) line.hidden = false;
  const now = bnowS();
  const open = toTs(target.item.sign_start);
  const task = target.task || {};
  /* 每个站点独立判断 done/now/todo（不再是单一线性 index）：
   * 本次执行链（启动→页面→开始监测）只认真实 checkpoint——事件没发生就
   * 显示「待启动/待打开/待开始」，不得拿计划时刻或当前时间编造
   * 看起来像已发生的事件时间；原定开放与截止是时间参考节点——历史开放
   * 照样标 done，但不抢占当前执行节点。时钟未知时参考节点不判 done。 */
  const runStarted = Number(task.run_started_at) || null;
  const pageOpened = Number(task.page_opened_at) || null;
  const watchStarted = Number(task.watch_started_at) || null;
  const deadline = routeDeadline(task, open, now);
  const terminal = ["success", "already_signed", "failed", "canceled"].indexOf(String(task.status)) >= 0;
  const deadlineKnown = deadline.time !== null;
  const openDone = now !== null && now >= open;
  const stations = [
    { name: "本次启动", time: runStarted ? routeTime(runStarted) : "待启动", done: runStarted !== null },
    { name: "活动页已打开", time: pageOpened ? routeTime(pageOpened) : "待打开", done: pageOpened !== null },
    { name: "开始监测", time: watchStarted ? routeTime(watchStarted) : "待开始", done: watchStarted !== null, hot: true },
    { name: "原定开放", time: routeTime(open), done: openDone, hot: true },
    /* 截止未知时显示「待定」，不能因为 null 参与比较而被标成已发生 */
    { name: deadline.name, time: deadlineKnown ? routeTime(deadline.time) : "待定",
      done: terminal || (deadlineKnown && now !== null && now >= deadline.time) },
  ];
  const currentIndex = terminal ? stations.length : stations.findIndex((station) => !station.done);
  box.innerHTML = '<div class="route" style="--n:' + stations.length + '" aria-label="这次报名的时间点">' +
    stations.map((station, i) => {
      const state = station.done ? "done" : i === currentIndex ? (station.hot ? "hot" : "now") : "todo";
      return '<div class="stn" data-s="' + state + '"><i class="stn-dot"></i><span class="stn-name">' + esc(station.name) +
        '</span><span class="stn-time">' + esc(station.time) + "</span></div>";
    }).join("") + "</div>";
}

/* 当前执行窗口的截止：统一走 effectiveRetryDeadline（telemetry 优先，
 * 其次任务 retry_for_s/sign_end/run_started_at 推导），与任务卡、LED
 * 同一个来源，长策略不再显示成 120 秒。 */
function routeDeadline(task, open, now) {
  return { name: "本次尝试截止", time: effectiveRetryDeadline(task, now) };
}

function routeTime(ts) {
  const bnMs = businessNowMs();
  const sameDay = bnMs != null && startOfDay(ts * 1000) === startOfDay(bnMs);
  return sameDay ? fmtClock(ts) : whenText(ts * 1000);
}

function routeTarget() {
  /* 运行中用执行模型的任务；模型没给任务时再按状态兜底（不读原勾选集合） */
  const view = executionView();
  const task = view.task
    || Array.from(S.plan.tasks.values()).find((item) => RUNNING_STATUSES.indexOf(String(item.status)) >= 0)
    || null;
  if (!task) return null;
  const item = taskDisplayItem(task);
  return item ? { item: item, task: task } : null;
}

function paintCountdown() {
  const countDown = $("#countdown");
  const view = executionView();
  if (S.busy && S.plan.id) {
    /* 运行中只报当前执行目标：终态场次不参与倒计时和批次数（UX-04）。
     * 已开放但还没真正提交时给「准备中」，不把预热当提交，也不编 0 秒。 */
    if (view.phase === "submitting") {
      countDown.classList.remove("is-hot");
      countDown.textContent = view.retryUntil === null
        ? "正在提交 " + view.count + " 场 · 等待结果"
        : (function () { const l = bnLeftSec(view.retryUntil);
            return l == null ? "待校准 · 正在提交 " + view.count + " 场"
                             : fmt(Math.max(0, l)) + " · 正在提交 " + view.count + " 场"; })();
      return;
    }
    if (view.phase === "arming") {
      countDown.classList.remove("is-hot");
      countDown.textContent = "已开放 · 准备提交 " + view.count + " 场";
      return;
    }
    if (view.phase === "waiting") {
      if (view.openAt === null) {
        countDown.classList.remove("is-hot");
        countDown.textContent = "时间待公布 · 同时开放 " + view.count + " 场";
        return;
      }
      const left = bnLeftSec(view.openAt);
      if (left === null) {
        countDown.classList.remove("is-hot");
        countDown.textContent = "待校准 · 同时开放 " + view.count + " 场";
        return;
      }
      countDown.classList.toggle("is-hot", left > 0 && left <= 10);
      countDown.textContent = (left <= 0 ? "现在开放 · " : fmt(left) + " · ") + "同时开放 " + view.count + " 场";
      return;
    }
    countDown.classList.remove("is-hot");
    countDown.textContent = view.phase === "settling" ? "正在汇总结果" : "正在准备";
    return;
  }
  const selected = selectedItems();
  if (!selected.length) {
    countDown.textContent = "待选";
    countDown.classList.remove("is-hot");
    return;
  }
  const groups = groupSelected(selected);
  const first = groups[0];
  const left = leftOf(first.items[0]);
  if (left === null) {
    countDown.textContent = signStartKnown(first.items[0]) ? "时间待校准" : "时间待公布";
    countDown.classList.remove("is-hot");
    return;
  }
  /* 最后 10 秒转红：文字与颜色同时表达 */
  countDown.classList.toggle("is-hot", left > 0 && left <= 10 && !S.busy);
  countDown.textContent = (left <= 0 ? "现在开放 · " : fmt(left) + " · ") + "同时提交 " + first.items.length + " 场";
}

/* ---------------------------------------------------------------- LED 点阵 */
function ledSet(text, tone) {
  const el = $("#led");
  if (!el || !window.LED_UI) return; /* led.js 缺席时退回纯文本层，语义不丢 */
  window.LED_UI.renderLed(el, text, tone);
}

/* 点阵只承载倒计时与结论：等待/最后10秒/提交中/成功/失败/停止 */
function fmtLed(seconds) {
  const value = Math.max(0, Math.floor(seconds));
  const pad = (n) => String(n).padStart(2, "0");
  return pad(Math.floor(value / 3600)) + ":" + pad(Math.floor((value % 3600) / 60)) + ":" + pad(value % 60);
}

function selectionSignature() {
  return Array.from(S.selected).slice().sort().join(",");
}

/* LED 下方那行小字：跟点阵同一个状态源，说明屏上数字是什么 */
function paintLedLabel(prefix, value) {
  const label = $("#led-label");
  if (!label) return;
  const bold = label.querySelector("b");
  if (!bold) return;
  /* metric-next 节点常驻：顶栏指标也写它，删了会让那边拿到空引用 */
  const textNode = Array.from(label.childNodes).find((node) => node.nodeType === 3);
  if (textNode) textNode.textContent = prefix + " ";
  else label.insertBefore(document.createTextNode(prefix + " "), bold);
  bold.textContent = value === null ? "—" : value;
}

function paintLed() {
  const view = executionView();
  if (S.busy && S.plan.id) {
    if (view.phase === "submitting") {
      if (view.retryUntil === null) {
        /* 截止未知：点阵只有数字，不拿 00:00:00 冒充已到点 */
        ledSet("--:--:--", "amber");
        paintLedLabel("正在提交 " + view.count + " 场，本次尝试截止", "待定");
        return;
      }
      {
        const l = bnLeftSec(view.retryUntil);
        ledSet(l == null ? "--:--" : fmtLed(Math.max(0, l)), "amber");
      }
      paintLedLabel("正在提交 " + view.count + " 场，本次尝试截止", fmtClock(view.retryUntil));
      return;
    }
    if (view.phase === "arming") {
      /* 已开放、还没真正提交：不说「还剩 0 秒」，也不说「正在提交」 */
      ledSet("--:--:--", "amber");
      paintLedLabel("已开放，准备报名 " + view.count + " 场", "准备中");
      return;
    }
    if (view.phase === "waiting") {
      if (view.openAt === null) {
        ledSet("--:--:--", "off");
        paintLedLabel("同时开放 " + view.count + " 场，开放", "待公布");
        return;
      }
      const left = bnLeftSec(view.openAt);
      if (left === null) {
        ledSet("--:--:--", "off");
        paintLedLabel("同时开放 " + view.count + " 场，开放", "待校准");
        return;
      }
      ledSet(fmtLed(left), left <= 10 ? "red" : "amber");
      /* 直接格式化真实开放时刻：不再用 now + 已取整 left 重拼，避免 1 秒偏差 */
      paintLedLabel("同时开放 " + view.count + " 场，开放", fmtClock(view.openAt));
      return;
    }
    /* 准备中/汇总中：屏上不给假倒计时，也不提前点亮成功 */
    ledSet("--:--:--", "off");
    paintLedLabel("计划状态", view.phase === "settling" ? "汇总中" : "准备中");
    return;
  }
  /* 终态结论（✓/✕/灭灯）留在屏上，直到选择集合发生变化；变化后回到预览倒计时。
   * 有「结果待核对」的场次时不能亮失败叉：那不是一次确定的失败。 */
  const outcome = planOutcome();
  const terminal = { completed: ["✓", "green"], failed: ["✕", "red"], stopped: ["--:--:--", "off"], partial: ["--:--:--", "off"] }[S.plan.status];
  if (terminal && S.plan.doneSelection === selectionSignature()) {
    if (S.plan.status !== "completed" && outcome.unknown > 0) {
      /* 未知不亮失败叉；停止带未知时主结论仍是「已停止」 */
      ledSet("--:--:--", "off");
      paintLedLabel((S.plan.status === "stopped" ? "计划已停止，" : "") + unknownLabel(outcome), null);
      return;
    }
    ledSet(terminal[0], terminal[1]);
    paintLedLabel({ completed: "计划完成", failed: "计划失败", stopped: "计划已停止", partial: "部分完成" }[S.plan.status], null);
    return;
  }
  const selected = selectedItems();
  if (selected.length) {
    const groups = groupSelected(selected);
    const left = leftOf(groups[0].items[0]);
    if (left !== null && left > 0) {
      ledSet(fmtLed(left), left <= 10 ? "red" : "dim");
      /* 预览与运行中同样读真实时刻，倒计时取整不能用来重拼开放时间 */
      const open = toTs(groups[0].items[0].sign_start);
      paintLedLabel("最近所选开放", open === null ? "待公布" : fmtClock(open));
      return;
    }
    if (left !== null) {
      ledSet("00:00:00", "dim");
      paintLedLabel("已开放", null);
      return;
    }
  }
  const next = nextOpen();
  if (next.best !== null) {
    ledSet(fmtLed(next.best), "dim");
    const open = toTs((S.byId.get(String(next.futureId)) || {}).sign_start);
    paintLedLabel("最近开放", open === null ? "待公布" : fmtClock(open));
    return;
  }
  if (next.openNow) {
    /* 已开放就是「现在可以报」，屏上不再给任何未来场的倒计时 */
    ledSet("00:00:00", "dim");
    paintLedLabel(next.openId ? "已开放，可立即报名" : "有活动开放中", "现在");
    return;
  }
  ledSet("--:--:--", "off");
  /* 时钟未知时「最近开放」是判断不出的结论，不能写成「待公布」 */
  paintLedLabel("最近开放", (bnowS() === null && S.items.some(signStartKnown))
    ? "待校准" : "待公布");
}

/* 秒级时间戳 → 学校时区 HH:MM:SS（LED 标注与点阵同款数字口径） */
function fmtClock(ts) {
  if (ts === null || ts === undefined) return "—";
  const p = schoolParts(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return pad(p.h) + ":" + pad(p.mi) + ":" + pad(p.s);
}

/* 终态的停止条件人话：让用户知道是「到点/到次数/到截止」里的哪一个。
 * unconfirmed/auth_unavailable 不是「失败原因」，是「结果没确认」：
 * 用词必须留出核对空间，不能写成没抢到。 */
function stopReasonText(reason) {
  return {
    success: "报名成功",
    already_signed: "已经报过",
    login_invalid: "登录失效，先重新登录",
    auth_unavailable: "登录状态暂时无法确认",
    max_attempts: "达到最大提交次数",
    retry_duration: "重试时长到了",
    sign_end: "报名已截止",
    unconfirmed: "报名结果待核对",
    user_stopped: "已停止",
    process_error: "程序流程异常结束",
  }[reason] || "";
}

/* telemetry 的 retry_deadline 是 epoch 秒（后端结构化行）；空串/0 表示没有 */
function telemetryDeadline(task) {
  const epoch = Number(task && task.retry_deadline);
  return Number.isFinite(epoch) && epoch > 0 ? epoch : null;
}

/* 提交中任务行的说明文字：telemetry（第 N 次/最近原因/重试到）优先，
 * 没有 telemetry 才退回固定窗口估算。taskHtml 与每秒走字共用这一份口径。
 * 次数按「已提交 N/M 次」显示；0 表示不限，必须明说，不能只写「第 N 次」。
 * 截止可能未知（已开放又没有任何启动参考点）：说「等待结果」，不编 0 秒。 */
function submittingDetail(task) {
  const parts = [];
  const attempts = Math.max(0, Math.floor(Number(task && task.attempts) || 0));
  const cap = Number(task && task.max_attempts);
  const limited = Number.isFinite(cap) && cap > 0;
  if (attempts > 0) {
    parts.push(limited ? "已提交 " + attempts + "/" + cap + " 次"
      : "已提交 " + attempts + " 次（不限次数）");
  } else if (limited) {
    parts.push("上限 " + cap + " 次，还没发起提交");
  }
  if (task.last_reason) parts.push(task.last_reason);
  const deadline = telemetryDeadline(task);
  if (deadline) {
    parts.push("重试到 " + hm(deadline * 1000));
    return parts.join(" · ");
  }
  const fallback = retryDeadline(task);
  if (fallback === null) {
    parts.push("等待提交结果");
    return parts.join(" · ");
  }
  {
    const l = bnLeftSec(fallback);
    parts.push("重试窗口约 " + (l == null ? "待校准" : fmt(Math.max(0, l))));
  }
  return parts.join(" · ");
}

function taskHtml(task, now) {
  const state = String(task.status || "queued");
  /* 标签与说明都按显示阶段改写：raw submitting 可能只是提前开页的预热 */
  const label = taskDisplayLabel(task);
  const cls = taskClass(state, task);
  /* 混合计划每场显示自己的模式：快照带 signup_mode，旧载荷按时间现算 */
  const mode = String(task.signup_mode || modeOf(task));
  const modePrefix = taskModePrefix(task);
  let detail = task.reason || task.detail || "";
  if (RUNNING_STATUSES.indexOf(state) >= 0) {
    /* 等待 / 准备 / 提交共用一份解释；typing 的等待文案只在没有阶段说明时兜底 */
    detail = taskDisplayDetail(task, now)
      || (state === "waiting" ? (mode === "pre_register" ? "等待报名开放" : "报名已开放，马上开始") : detail);
  } else if (["failed", "canceled"].indexOf(state) >= 0) {
    if (task.stop_reason === "unconfirmed") {
      /* 已发出的请求撤不回来：只陈述「发过几次、没拿到回应」，不下失败结论 */
      const tried = Math.max(0, Math.floor(Number(task.attempts) || 0));
      detail = "已提交 " + tried + " 次，没有拿到确定回应 · 去活动页核对，本工具不会再自动重发";
    } else if (task.stop_reason === "auth_unavailable") {
      detail = "登录状态无法确认，本场没有提交 · 检查网络后刷新重试";
    } else {
      const bits = [];
      if (task.attempts) bits.push("提交了 " + task.attempts + " 次");
      const stopNote = stopReasonText(task.stop_reason);
      if (stopNote && task.stop_reason !== "success" && task.stop_reason !== "already_signed") bits.push(stopNote);
      if (bits.length) detail = (detail ? detail + "（" : "") + bits.join("，") + (detail ? "）" : "");
    }
  }
  const canCancel = ["queued", "waiting"].indexOf(state) >= 0 && S.busy;
  return "<div class=\"task-row\" data-task-id=\"" + esc(task.task_id) + "\"><div><strong class=\"task-name\" title=\"" + esc(task.name) + "\">" + esc(task.name) + "</strong><span class=\"task-detail\">" + esc(modePrefix + detail) + "</span></div><div class=\"task-actions\"><span class=\"task-state " + cls + "\">" + esc(label) + "</span>" + (canCancel ? "<button class=\"task-cancel\" type=\"button\" data-task-id=\"" + esc(task.task_id) + "\">取消</button>" : "") + "</div></div>";
}

/* 统一的有效重试截止（路线图、任务卡、LED、终态文案共用唯一来源）：
 * 优先 worker telemetry 的真实截止（epoch 秒）；没有再按任务推导。
 *
 * 推导锚点必须**不随墙钟跨过开放时刻而切换**。worker 的 retry_anchor()
 * 在 page.goto 之前取一次 max(原定开放, 启动时刻) 之后就钉死（grab.py），
 * 界面若按「open 还在未来就用 open，否则改用启动点」逐帧重算，会在开放那一秒
 * 把截止倒退 lead_s——预报名提前 100 秒开页，就会提前 100 秒归零。
 * 因此：有真实启动参考点时锚点固定为 max(原定开放, 启动点)；缺 open 只用启动点；
 * 只有「未来场且还没启动」才拿原定开放当计划参考。
 *
 * 「直到报名截止」且 sign_end 已知时，截止就是 sign_end，与启动点/开放点无关——
 * 排队中或快照字段还没齐时正会出现「有绝对截止、没有启动参考点」，这时不能判未知。
 *
 * 返回 null = 既没有绝对截止、也没有任何时间参考：调用方显示未知/预计，
 * 不拿当前时刻冒充 0 秒。缺 sign_end 的 0 时长回退 120 秒、sign_end 硬截断都保留。
 * 这里只算界面参考点，worker 的实际停止时机仍由后端决定。 */
function effectiveRetryDeadline(task, fallbackNow) {
  const telemetry = telemetryDeadline(task);
  if (telemetry !== null) return telemetry;
  const now = Number.isFinite(fallbackNow) ? fallbackNow : bnowS();
  const retryFor = task && Number.isFinite(Number(task.retry_for_s)) ? Number(task.retry_for_s) : RETRY_WINDOW_S;
  const signEnd = task ? toTs(task.sign_end) : null;
  /* 绝对截止不依赖任何起点：先把这条独立路径处理掉，别被 anchor 判空挡住 */
  if (!(retryFor > 0) && signEnd !== null) return signEnd;
  /* 计划接纳时冻结的起点最权威：worker 就是拿它算 --retry-start，
   * 界面用它推导与 worker 完全同一口径（慢启动不会让两边差出几秒） */
  const policyStart = Number(task && task.retry_started_at);
  if (Number.isFinite(policyStart) && policyStart > 0) {
    let frozen = retryFor > 0 ? policyStart + retryFor : policyStart + RETRY_WINDOW_S;
    if (signEnd !== null) frozen = Math.min(frozen, signEnd);
    return frozen;
  }
  const started = Number(task && task.run_started_at) || Number(task && task.submitSeen) / 1000 || null;
  const open = toTs(task && task.sign_start);
  let anchor = null;
  if (started !== null) anchor = open !== null ? Math.max(open, started) : started;
  else if (open !== null && open > now) anchor = open;
  if (anchor === null) return null;
  let deadline = retryFor > 0 ? anchor + retryFor : anchor + RETRY_WINDOW_S;
  if (signEnd !== null) deadline = Math.min(deadline, signEnd);
  return deadline;
}

/* 提交中的重试截止（任务卡与 LED 的旧入口）：已并入统一推导 */
function retryDeadline(task) {
  return effectiveRetryDeadline(task);
}

function updateControls() {
  const componentDown = !!S.runtime.error;
  /* 缺组件：登录/开始都禁用（点了也是结构化失败）；刷新保持可用——复查组件并恢复的唯一通道 */
  $("#btn-login").disabled = S.busy || componentDown;
  $("#btn-refresh").disabled = S.listState === "loading";
  /* 帮助任何状态都可打开；运行中只禁重看引导，原因写在按钮上。 */
  paintReplayButtons();
  const validation = validateSelection();
  /* 开始门禁：真实计划只在学校接口确认登录有效（valid）后可点；
   * checking/unknown/none/expired 一律禁用（demo 模式例外，供视觉演示）。
   * checking 必须显式排除：新一轮检查开始时不清旧 result，
   * 不排除的话上次 valid 会放行尚未确认的检查期。 */
  const loginReady = !!S.demo || (!S.login.checking && S.login.result === "valid");
  const canStart = !S.busy && !S.starting && !componentDown && loginReady && S.selected.size > 0
    && validation.errors.length === 0 && validation.conflicts.length === 0
    && S.listState === "ready";
  /* 独立北京时间门禁（决策 012）：时钟不可读或不在 ready/holdover 时开始禁用 */
  const clockDown = businessNowMs() === null && !S.demo;
  $("#btn-plan").disabled = !canStart || clockDown;
  $("#btn-plan").textContent = S.busy ? "计划运行中"
    : (S.startRequest.state === "reconciling" ? "正在确认是否启动…"
      : (S.starting ? "正在提交…"
        : (clockDown ? "北京时间未校准" : "开始报名计划")));
  $("#btn-stop").disabled = !S.busy || !S.plan.id || S.stopping;
  $("#btn-stop").textContent = S.stopping ? "正在停止…" : "停止";
  $("#btn-clear-plan").disabled = S.busy || (!S.selected.size && !S.plan.tasks.size);
  document.querySelectorAll(".row-check").forEach((check) => {
    const item = findItem(check.dataset.id);
    /* 与行渲染同一份判据：已报名/已截止/忙碌都禁用，已截止仍可查看 */
    check.disabled = S.busy || !isEligibleForPlan(item);
  });
  paintBoardActions();
}

/* ---------------------------------------------------------------- 横幅 */
function paintBanners() {
  const parts = [];
  if (S.runtime.error) {
    /* 组件问题只挂一条横幅（一个主要提示 + 恢复动作），不在满屏重复 */
    const copy = runtimeErrorCopy();
    parts.push('<div class="banner" data-tone="bad" role="alert"><span class="grow"><b>' + esc(copy.title) + "。</b>" +
      esc(copy.hint) + "</span>" +
      '<button class="btn btn-ghost" type="button" data-act="runtime-refresh">重新检查</button></div>');
  }
  if (!S.online) {
    parts.push('<div class="banner" data-tone="bad" role="alert"><span class="grow"><b>网络断开了。</b>' +
      (S.busy ? "到点时还没恢复，这次报名会失败。恢复以后看一眼计划结果。" : "恢复以后点「刷新」。") + "</span></div>");
  }
  if (S.sleepGap) {
    parts.push('<div class="banner" data-tone="warn" role="alert"><span class="grow"><b>刚才有 ' + durText(S.sleepGap / 1000) +
      " 界面没动静，电脑可能睡眠了。</b>要是睡过了开放时间，就赶不上了。看看计划和运行记录。</span>" +
      '<button class="btn btn-ghost" type="button" data-act="sleep-ok">知道了</button></div>');
  }
  $("#banners").innerHTML = parts.join("");
}

/* ---------------------------------------------------------------- 动作 */
function startPlan() {
  if (S.busy || S.starting) return;
  const validation = validateSelection();
  if (!S.selected.size) {
    setResult({ ok: false, title: "还没选活动", text: "在时刻表勾选至少一场未报名活动。" });
    return;
  }
  if (validation.errors.length || validation.conflicts.length) {
    paintPlan();
    setResult({ ok: false, title: "计划还不能开始", text: "先处理上面的检查项，再开始报名计划。" });
    return;
  }
  /* 显式传 sign_end 与 signup_mode：重载/刷新时序下后端虽以自己快照为权威，
   * 前端把当前判定一起交上去，缺口数据时后端也能按报名截止拦截 */
  const items = selectedItems().map((item) => ({
    id: item.id,
    name: item.name,
    sign_start: item.sign_start || "",
    sign_end: item.sign_end || "",
    act_start: item.act_start || "",
    act_end: item.act_end || "",
    signed: !!item.signed,
    signup_mode: modeOf(item),
  }));
  setResult(null);
  /* FB1 轻量提交中标志：只做即时按钮反馈（连点不会创建第二套提交——后端
   * 最终接纳点的原子重检是最终权威），成功/拒绝/reject 均释放。
   * 本轮再加 request_id：15 秒无响应时按 ID 对账，绝不清标志放行第二次开始。 */
  const requestId = newRoundId("start");
  if (S.startRequest) {
    /* 替换请求必须撤销旧计时器：旧 15 秒/5 秒回调不得落到这次新开始上 */
    clearTimeout(S.startRequest.timer);
    clearTimeout(S.startRequest.queryTimer);
  }
  S.startRequest = { id: requestId, state: "submitting", timer: null,
                     queryTimer: null, reconcile: 0, gen: 0 };
  S.starting = true;
  updateControls();
  S.startRequest.timer = setTimeout(() => reconcileStart(requestId), 15000);
  api("start_plan", items, $("#headed").checked,
      { retry_for_s: S.retryPolicy.retry_for_s, max_attempts: S.retryPolicy.max_attempts },
      requestId).then((res) => {
    if (!S.startRequest || S.startRequest.id !== requestId) return;
    clearTimeout(S.startRequest.timer);
    clearTimeout(S.startRequest.queryTimer);
    if (res && res.pending) {
      /* 去重命中了仍在进行的那次开始：继续对账，不能当成失败也不能再发一次 */
      S.starting = true;
      reconcileStart(requestId);
      return;
    }
    S.starting = false;
    S.startRequest.state = "settled";
    if (!res || res.ok === false) {
      setBusy(false);
      if (res && res.error && RUNTIME_ERRORS[res.error]) {
        /* FB4：开始计划的组件失败同样只挂横幅与唯一恢复入口，
         * 不生成同义失败结果框；并计入本轮刷新故障累计（迟到成功不得撤） */
        noteRoundError(res.error);
        paintBanners();
        updateControls();
        return;
      }
      showPlanError(res && res.text, res && res.conflicts, res && res.errors);
      return;
    }
    /* 后端可能在 Promise 回调之前就推来 plan/task 事件（协调器另一线程emit），
     * 这里只在计划 id 变了时才重置，避免把已收到的任务表清空。
     * ack 的接纳代次并入会话记录：归属判定与事件路由同一基准（P1）。 */
    const ackGen = notePlanGeneration(res.plan_generation);
    if (!S.plan.id || S.plan.id !== String(res.plan_id || "")) {
      S.plan = { id: String(res.plan_id || ""), gen: ackGen,
                 status: "ready", tasks: new Map(), armedAt: Date.now() / 1000 };
    } else if (ackGen > (Number(S.plan.gen) || 0)) {
      S.plan.gen = ackGen;
    }
    S.stopping = false;
    /* ack 只证明「已接纳」，不证明「此刻仍在运行」：该计划已经通过 plan_done
     * 到达终态时（终态早于 ack），保持终态呈现与 busy=false——没有第二个
     * plan_done 会再来解锁，置忙会让停止入口悬在一个永远不结束的计划上（P1） */
    if (S.plan.id === String(res.plan_id || "")
        && PLAN_TERMINAL.indexOf(S.plan.status) >= 0) {
      updateControls();
      render();
      return;
    }
    setBusy(true);
    /* 已开放场次会立即执行，不能写成「等待各自开放时间」：
     * 按两种正式模式分别说明（预报名等开放 / 立即报名抢名额）。 */
    const futureCount = items.filter((item) => item.signup_mode === "pre_register").length;
    const instantCount = items.filter((item) => item.signup_mode === "instant_signup").length;
    const parts = [];
    if (futureCount) parts.push(futureCount + " 场预报名等开放");
    if (instantCount) parts.push(instantCount + " 场立即报名马上执行");
    addLog("已安排 " + items.length + " 场活动：" + (parts.join("，") || "按各场状态执行") + "。");
    render();
  }).catch((err) => {
    if (!S.startRequest || S.startRequest.id !== requestId) return;
    clearTimeout(S.startRequest.timer);
    /* 桥接异常不等于没启动：先对账再说话，不能直接放开第二次开始 */
    S.startRequest.state = "reconciling";
    reconcileStart(requestId, msg(err));
  });
}

/* 开始请求的对账：只问后端「这次到底启没启」，绝不重发 start_plan（R11）。
 * accepted → 恢复任务与停止按钮；rejected → 如实报错；pending → 继续等；
 * unknown/超时 → 明说「结果未确认，请勿重复启动」，保留重新确认入口。 */
const START_QUERY_TIMEOUT_MS = 5000;

/* 开始请求的对账：只问后端「这次到底启没启」，绝不重发 start_plan（R11）。
 * accepted → 恢复任务与停止按钮；rejected → 如实报错；pending → 继续等；
 * unknown/超时 → 明说「结果未确认，请勿重复启动」，保留重新确认入口。
 * 每次查询有 5 秒本机响应期限与独立代次：Promise 永不 settle 也会退出查询态，
 * 迟到的旧回复不能落到后来的计划上。 */
function reconcileStart(requestId, hint) {
  if (!S.startRequest || S.startRequest.id !== requestId) return;
  S.starting = true;
  S.startRequest.state = "reconciling";
  const gen = (S.startRequest.gen || 0) + 1;
  S.startRequest.gen = gen;
  clearTimeout(S.startRequest.queryTimer);
  updateControls();
  setResult({
    ok: "partial",
    title: "正在确认是否启动",
    text: hint ? "开始请求没有正常返回（" + hint + "），正在向本机确认；请勿重复启动。"
      : "开始请求没有立刻返回，正在向本机确认；请勿重复启动。",
  });

  const settled = () => {
    clearTimeout(S.startRequest.queryTimer);
    S.startRequest.queryTimer = null;
  };
  const unknown = (prefix) => {
    if (!S.startRequest || S.startRequest.id !== requestId || S.startRequest.gen !== gen) return;
    settled();
    /* 没有确定结论：保持开始按钮禁用，避免用户重复报名；只留只读确认入口 */
    S.starting = true;
    S.startRequest.state = "unknown";
    updateControls();
    setResult({
      ok: false,
      title: "结果未确认，请勿重复启动",
      text: prefix + "先看下面的任务区或点「刷新」确认当前计划，确认没有在跑再重新开始。",
      action: { label: "重新确认", run: () => reconcileStart(requestId) },
    });
  };

  S.startRequest.queryTimer = setTimeout(() => {
    if (!S.startRequest || S.startRequest.id !== requestId || S.startRequest.gen !== gen) return;
    unknown("本机 5 秒内没有回应这次对账。");
  }, START_QUERY_TIMEOUT_MS);

  api("start_request_state", requestId).then((res) => {
    if (!S.startRequest || S.startRequest.id !== requestId) return;
    if (S.startRequest.gen !== gen) return;      /* 旧代次迟到：不得替换后来的计划 */
    settled();
    const state = res && res.state;
    if (state === "accepted") {
      adoptPlan(S.startRequest.id, res && res.plan_id, res && res.snapshot,
                !!(res && (res.finished || !res.running)), res && res.plan_generation);
      return;
    }
    if (state === "rejected") {
      S.starting = false;
      S.startRequest.state = "rejected";
      updateControls();
      showPlanError((res && res.text) || "计划没能开始", [], []);
      return;
    }
    if (state === "canceled") {
      S.starting = false;
      S.startRequest.state = "canceled";
      updateControls();
      setResult({ ok: "partial", title: "这次开始已取消", text: "可以重新选择后再开始。" });
      return;
    }
    if (state === "pending" && S.startRequest.reconcile < 8) {
      S.startRequest.reconcile += 1;
      S.startRequest.timer = setTimeout(() => reconcileStart(requestId), 2000);
      return;
    }
    unknown("没有拿到确定的启动结论。");
  }).catch(() => {
    if (!S.startRequest || S.startRequest.id !== requestId) return;
    if (S.startRequest.gen !== gen) return;
    unknown("本机没有回应这次对账。");
  });
}

/* 已确认接纳：按快照真实状态恢复。
 * 15 秒内就结束的计划不能复活成「空任务运行中」——那会给出一个永远不结束的
 * 运行态和一个点不动的停止按钮（C05）。
 * generation 是后端接纳代次：ack 承认的归属与事件路由同一基准（P1）。 */
function adoptPlan(requestId, planId, snapshot, finished, generation) {
  S.starting = false;
  if (S.startRequest && S.startRequest.id === requestId) {
    S.startRequest.state = "accepted";
    clearTimeout(S.startRequest.queryTimer);
    S.startRequest.queryTimer = null;
  }
  const id = String(planId || "");
  const status = String((snapshot && snapshot.status) || (finished ? "unknown" : "running"));
  const terminal = ["completed", "partial", "failed", "stopped"].indexOf(status) >= 0;
  const gen = notePlanGeneration(generation);
  if (id && (!S.plan.id || S.plan.id !== id)) {
    S.plan = { id: id, gen: gen, status: terminal ? status : "running", tasks: new Map(),
               armedAt: Date.now() / 1000 };
  } else if (gen > (Number(S.plan.gen) || 0)) {
    S.plan.gen = gen;
  }
  if ((snapshot && snapshot.tasks || []).length) {
    (snapshot.tasks || []).forEach((task) => {
      const key = String(task.task_id);
      const prev = S.plan.tasks.get(key) || null;
      const merged = mergeTask(prev, task);
      /* 旧进度不得把已终态场次拖回运行态（终态早于 ack，P1） */
      if (prev && TASK_TERMINAL.indexOf(String(prev.status)) >= 0
          && TASK_TERMINAL.indexOf(String(merged.status)) < 0) {
        merged.status = prev.status;
      }
      /* 后到 ack 不得清掉事件已收到的任务与 checkpoint：提交次数只增不减（P1） */
      const prevTries = Math.max(0, Math.floor(Number((prev || {}).attempts) || 0));
      const snapTries = Math.max(0, Math.floor(Number(merged.attempts) || 0));
      if (prevTries > snapTries) merged.attempts = prevTries;
      S.plan.tasks.set(key, merged);
    });
  }
  S.stopping = false;
  if (terminal) {
    S.plan.status = status;
    S.plan.doneSelection = selectionSignature();
    setBusy(false);
    /* ack 的权威终态直接走呈现入口：不过事件路由，避免被同计划
     * 重复终态分支拦截而不呈现（P1） */
    presentPlanDone({ plan_id: id, status: status,
                      summary: (snapshot && snapshot.summary) || {},
                      plan_generation: gen > 0 ? gen : undefined });
    return;
  }
  /* 同计划已可信终态：ack/对账带来的旧 running 快照不得复活忙态——
   * ack 只证明「已接纳」，不证明「此刻仍在运行」（终态早于 ack，P1） */
  if (S.plan.id === id && PLAN_TERMINAL.indexOf(S.plan.status) >= 0) {
    updateControls();
    render();
    return;
  }
  setBusy(true);
  addLog("[i] 已确认计划启动，继续显示进度。");
  render();
  api("bootstrap").then((info) => {
    if (info && info.plan) ingestBootstrapPlan({ plan: info.plan });
  }).catch(() => {});
}

function cancelTask(taskId) {
  if (!S.plan.id || !taskId) return;
  api("cancel_task", S.plan.id, taskId).then((res) => {
    if (res && res.ok === false) addLog(res.text || "这一场已经结束");
  }).catch((err) => addLog("[X] 取消这一场失败：" + msg(err)));
}

function stopPlan() {
  if (!S.plan.id) return;
  /* 先确认再停：等待任务会取消、已提交任务可能已生效；Esc/继续等待都不触发停止 */
  const dialog = $("#dlg-stop");
  if (dialog && !dialog.open) dialog.showModal();
}

function doStopPlan() {
  if (!S.plan.id) return;
  /* 立刻给「正在停止」的反馈：后端在按进程树终止 worker，
   * busy 要等 plan_done 才解锁，中间不能看起来像卡死 */
  S.stopping = true;
  paintLogin();
  updateControls();
  addLog("正在停止计划：结束还在重试的场次…");
  api("stop_plan", S.plan.id).then((res) => {
    if (res && res.ok === false) {
      S.stopping = false;
      paintLogin();
      updateControls();
      addLog(res.text || "当前没有这个计划");
    }
  }).catch((err) => {
    S.stopping = false;
    paintLogin();
    updateControls();
    addLog("[X] 停止计划失败：" + msg(err));
  });
}

function doLogin() {
  if (S.busy) return;
  /* 打开浏览器只是开始登录，不代表登录成功；在 done(login) 到达前保留真实的
   * 失效/未登录结论，避免「取消登录」期间被忙碌态误画成已登录。 */
  S.login.loginPending = true;
  setResult(null);
  setBusy(true);
  addLog("正在打开登录窗口，在弹出的浏览器里登完再回来…");
  api("login").then((res) => {
    if (res && res.demo) {
      /* 演示模式不打开学校登录页：如实说明，不当成登录失败（R20） */
      S.login.loginPending = false;
      setBusy(false);
      setResult({ ok: "partial", title: "演示模式不登录", text: res.text || "演示模式不联网。" });
      return;
    }
    if (!res || res.ok === false) {
      S.login.loginPending = false;
      if (res && res.error && RUNTIME_ERRORS[res.error]) {
        /* FB4：登录的同步组件失败只挂横幅与唯一恢复入口，
         * 不再生成「登录没能开始」同义结果框；同时计入本轮刷新故障累计——
         * 刷新尚有分支未返回时，迟到的成功 check/list 不得撤掉这个新横幅 */
        noteRoundError(res.error);
        setBusy(false);
        paintBanners();
        updateControls();
        return;
      }
      setBusy(false);
      paintBanners();
      updateControls();
      setResult({ ok: false, title: "登录没能开始", text: (res && res.text) || "再点一次「登录」试试。" });
    }
  }).catch((err) => {
    S.login.loginPending = false;
    setBusy(false);
    setResult({ ok: false, title: "登录没能开始", text: msg(err) });
  });
}

/* ---------------------------------------------------------------- 事件 */
function handleDone(payload) {
  const data = payload || {};
  if (data.kind === "login") {
    S.login.loginPending = false;
    setBusy(false);
    if (data.ok) {
      /* 登录成功=新会话已保存：旧锚点立即作废（清掉），紧接着的校验事件会带
       * 新锚点回来；这两秒里宁可显示未知，也不能拿旧凭证的剩余时间充数。 */
      setLogin(null, true, true, { estimated_expires_at: null, saved_at: null });
      setResult({ ok: true, title: "登录成功", text: "正在加载活动列表。登录剩余时间以学校校验为准。", autoClose: Date.now() + 6000 });
      doCheck();
      refresh(true);
    } else {
      /* 失败时保留上一次的 present：重登失败 ≠ 从没登录过 */
      setLogin(null, false);
      const copy = loginFailCopy(data.reason);
      setResult({ ok: false, title: copy.title, text: copy.text, action: { label: "再试一次", run: doLogin } });
    }
    return;
  }
  if (data.kind === "grab") {
    setBusy(false);
    if (data.ok) setResult({ ok: true, title: "报名成功：" + stripLabel(data.label), text: "建议去活动页确认一眼。" });
    else setResult({ ok: false, title: "报名未成功：" + cleanReason(data.reason), text: grabHint(data.reason) });
  }
}

/* 登录失败：按 grab.py login 真会打印的句子归类，回答发生了什么+现在能做什么 */
function loginFailCopy(reason) {
  const raw = String(reason || "");
  if (/窗口被关掉/.test(raw)) return { title: "登录窗口被关掉了", text: "登录还没完成，窗口就关了。点「再试一次」重新打开。" };
  if (/仍未检测到登录成功/.test(raw)) return { title: "等了 10 分钟还没登录成功", text: "浏览器窗口里一直没完成登录，超时了。点「再试一次」。" };
  if (/打不开登录页/.test(raw)) return { title: "打不开学校的登录页", text: "检查网络、校园网或代理，然后再试一次。" };
  if (/接口校验失败/.test(raw)) return { title: "登录后学校那边没校验通过", text: "登录状态存下来了，但学校那边没校验通过。再登录一次试试。" };
  if (/没找到可用的浏览器/.test(raw)) return { title: "没找到能用的浏览器", text: "这台电脑上要有 Chrome 或 Edge，装好以后再试。" };
  return { title: "登录没完成", text: cleanReason(raw) || "原因不明。可以再试一次，或者看看运行记录。" };
}

/* 运行时 checkpoint 字段：晚到的 plan 快照或恢复载荷缺这些字段时，
 * 不得用空值抹掉已经收到的真实事件（页面重载恢复与晚到 plan 事件共用）。 */
const TASK_RUNTIME_FIELDS = ["run_started_at", "worker_spawned_at", "page_opened_at", "watch_started_at",
  "retry_deadline", "retry_started_at", "attempts", "last_reason", "stop_reason"];

/* ---- 独立北京时间（决策 012）----
 * 快照锚点按真实字段推进：epoch_at_sample = epoch_s + sampled_tick_s - tick_s；
 * 页面用 performance.now 外推，桥接往返的一半计入误差与外推起点。
 * 旧代次/旧采样/其他实例的快照一律不落地；businessNowMs 每次校验状态、年龄、
 * 漂移与总误差，无效返回 null——调用方显示待校准，绝不强转 0。 */
const CLOCK_MAX_AGE_MS = 900000;        /* 锚点超过 900 秒不可用（与后端一致） */
const CLOCK_HOLDOVER_MS = 300000;       /* 超过 300 秒标「使用上次校准结果」 */
const CLOCK_MAX_UNCERTAINTY_MS = 500;   /* 总误差超过 0.5 秒不可用 */
const CLOCK_DRIFT_PPM = 100;            /* holdover 保守漂移 */
const CLOCK_POLL_MS = 10000;            /* 后台最多每 10 秒读一次本机快照 */

/* 同源单调读数：浏览器用 performance.now；node 测试桩缺 performance 时退回
 * Date.now——锚点与读数必须同一来源，外推才成立（仅测试环境影响）。 */
const _monoMs = (typeof performance !== "undefined" && typeof performance.now === "function")
  ? () => performance.now() : () => Date.now();

const CLOCK_ICONS = {
  ready: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.35"><circle cx="8" cy="8" r="6"/><path d="m5 8 2 2 4-4"/></svg>',
  holdover: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.35"><circle cx="8" cy="8" r="6"/><path d="M8 4v4l3 2"/></svg>',
  syncing: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.35"><path d="M13.5 7a5.6 5.6 0 1 0-1 4M13.5 2v5H9"/></svg>',
  unavailable: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.35"><path d="M8 2 15 14H1Z"/><path d="M8 6v4m0 2h.01"/></svg>',
  demo: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.35"><circle cx="8" cy="8" r="6"/><path d="m6.5 5 4 3-4 3Z"/></svg>',
};

const CLOCK_COPY = {
  ready: { label: "已校准", explain: "报名时间和倒计时均以北京时间为准。" },
  syncing: { label: "正在校准…", explain: "正在获取网络时间。报名计划需在校准完成后启动。" },
  holdover: { label: "使用上次校准结果", explain: "时间仍可用，工具会自动重试校准。" },
  unavailable: { label: "时间校准失败", explain: "报名计划暂不可启动。检查网络后重试。" },
  demo: { label: "演示时间", explain: "使用示例活动和时间，不会提交报名。" },
};
const CLOCK_SYNC_FAILURE = "本次校准未成功。";

/* 快照字段校验与换算：任何缺失/非数值都返回 null，不拼接半份快照。 */
function clockSnapshotFields(snap, rttMs) {
  const s = snap && typeof snap === "object" ? snap : {};
  const epoch = Number(s.epoch_s);
  const tick = Number(s.tick_s);
  const sampled = Number(s.sampled_tick_s);
  const unc = Number(s.uncertainty_s);
  const age = Number(s.age_s);
  if (![epoch, tick, sampled].every(Number.isFinite)) return null;
  if (!Number.isFinite(unc) || unc < 0) return null;
  const bridge = Number.isFinite(rttMs) && rttMs > 0 ? rttMs : 0;
  return {
    owner: String(s.owner_id || ""),
    gen: Number.isFinite(Number(s.generation)) ? Number(s.generation) : 0,
    state: String(s.state || "unavailable"),
    /* epoch_s 只对应 tick_s：采样时刻的业务 epoch 必须补上两者差 */
    epochAtSampleMs: (epoch + (sampled - tick)) * 1000,
    sampleAgeMs: Number.isFinite(age) ? Math.max(0, age * 1000)
      : Math.max(0, (sampled - tick) * 1000),
    uncertaintyMs: unc * 1000 + bridge / 2,
    bridgeMs: bridge,
    source: String(s.source || ""),
    reason: String(s.reason || ""),
    syncStatus: ["pending", "syncing", "succeeded", "failed"].includes(s.sync_status)
      ? s.sync_status : null,
    syncRound: Number.isSafeInteger(s.sync_round) && s.sync_round >= 0 ? s.sync_round : null,
  };
}

function markClockUnavailable(reason) {
  const cur = S.businessClock;
  cur.state = "unavailable";
  cur.reason = String(reason || "暂时无法获取北京时间");
  paintClockStatus();
  updateControls();
  render();
}

function ingestBusinessClock(snap, opts) {
  const options = opts || {};
  const cur = S.businessClock;
  const fields = clockSnapshotFields(snap, options.rttMs);
  if (!fields) {
    markClockUnavailable("暂时无法读取时间状态");
    return;
  }
  /* 归属：只接受同一实例；新 owner 只由权威 bootstrap 重建 */
  if (cur.owner && fields.owner && fields.owner !== cur.owner && !options.authoritative) return;
  if (fields.gen < cur.gen) return;                 /* 旧代次不覆盖新结论 */
  /* 是否完成首轮由后端报告：收到 bootstrap 并不意味着校准已经结束。 */
  const freshAttempt = fields.syncRound === null || cur.syncRound == null
    || fields.syncRound >= cur.syncRound;
  let attemptSettled = false;
  if (freshAttempt) {
    cur.syncStatus = fields.syncStatus;
    if (fields.syncRound !== null) cur.syncRound = fields.syncRound;
    /* 失败不增加授时 generation，必须按尝试编号核对，不能一直等成功代次。 */
    const ownsAttempt = fields.syncRound !== null && cur.syncRequestRound != null
      && (fields.syncRound > cur.syncRequestRound || cur.syncFollowRound === fields.syncRound);
    if (cur.syncState === "syncing" && ownsAttempt
        && (fields.syncStatus === "succeeded" || fields.syncStatus === "failed")) {
      clearTimeout(cur.syncTimer);
      cur.syncState = "idle";
      cur.syncError = fields.syncStatus === "failed" ? CLOCK_SYNC_FAILURE : "";
      attemptSettled = true;
    }
  }
  if (attemptSettled) {
    paintClockStatus();
    paintClockPopFeedback();
  }
  if (fields.gen === cur.gen && cur.anchorEpochMs
      && fields.epochAtSampleMs < cur.epochAtSampleMs) return;   /* 同代旧采样不倒退 */
  if (fields.state === "unavailable") {
    /* 同代「不可用」不得抹掉已建立的可读锚点：真正的过期由年龄/误差门禁判 */
    if (fields.gen === cur.gen && cur.anchorEpochMs
        && (cur.state === "ready" || cur.state === "holdover")) return;
    cur.state = "unavailable";
    cur.reason = fields.reason || "暂时无法获取北京时间";
    cur.gen = fields.gen;
    cur.owner = fields.owner || cur.owner;
    cur.syncState = "idle";
    paintClockStatus();
    updateControls();
    render();
    return;
  }
  const advanced = fields.gen > cur.gen;
  cur.owner = fields.owner || cur.owner;
  cur.gen = fields.gen;
  cur.state = fields.state === "holdover" ? "holdover" : "ready";
  cur.anchorEpochMs = fields.epochAtSampleMs + fields.bridgeMs / 2;
  cur.perfAt = _monoMs();
  cur.sampleAgeMs = fields.sampleAgeMs;
  cur.uncertaintyMs = fields.uncertaintyMs;
  cur.bridgeMs = fields.bridgeMs;
  cur.source = fields.source;
  cur.reason = fields.reason;
  if (advanced && !attemptSettled) {
    cur.syncState = "idle";
    cur.syncError = "";
  }
  paintClockStatus();
  updateControls();   /* 时钟门禁即时生效：不可用→开始禁用，恢复→解禁 */
  render();
}

/* 采样后经过时长（同一单调域）。快照 age_s 是采样时的锚点年龄；
 * 两者相加才是「距最近校准」的真实年龄（300/900 秒门禁用）。 */
function clockSampleElapsedMs() {
  const c = S.businessClock || {};
  const elapsed = _monoMs() - c.perfAt;
  return Number.isFinite(elapsed) && elapsed >= 0 ? elapsed : null;
}

function clockAgeMs() {
  const c = S.businessClock || {};
  if (!c.anchorEpochMs) return null;
  const elapsed = clockSampleElapsedMs();
  if (elapsed === null) return null;
  return (c.sampleAgeMs || 0) + elapsed;
}

/* 同一份只读推导：采样时误差 + 采样后漂移。
 * uncertainty_s 已经包含锚点到采样的漂移，不能再按整段年龄叠加一次；
 * 桥接往返只计一次采样误差，也不参与后续漂移。 */
function clockReading() {
  const c = S.businessClock || {};
  if (!c.anchorEpochMs) return null;
  if (c.state !== "ready" && c.state !== "holdover") return null;
  const elapsed = clockSampleElapsedMs();
  if (elapsed === null) return null;
  const age = (c.sampleAgeMs || 0) + elapsed;
  const drift = CLOCK_DRIFT_PPM * 1e-6 * elapsed;
  const uncertainty = (c.uncertaintyMs || 0) + drift;
  const valid = age <= CLOCK_MAX_AGE_MS && Number.isFinite(uncertainty)
    && uncertainty <= CLOCK_MAX_UNCERTAINTY_MS;
  return {
    nowMs: c.anchorEpochMs + elapsed,
    ageMs: age,
    uncertaintyMs: uncertainty,
    driftMs: drift,
    valid: valid,
  };
}

/* 业务当前时刻：门禁、顶栏、详情与倒计时共用同一份推导；无效返回 null。 */
function businessNowMs() {
  const reading = clockReading();
  return reading && reading.valid ? reading.nowMs : null;
}

function bnowS() {
  const m = businessNowMs();
  return m == null ? null : m / 1000;
}

function bnLeftSec(targetSec) {
  const n = bnowS();
  return (n == null || !Number.isFinite(targetSec)) ? null : targetSec - n;
}

/* 五个界面状态：演示 / 校准中 / 已校准 / 使用上次结果 / 待校准 */
function clockUiState() {
  if (S.demo) return "demo";
  const c = S.businessClock || {};
  const reading = clockReading();
  if (!reading || !reading.valid) {
    if (!c.anchorEpochMs && (c.syncStatus === "pending" || c.syncStatus === "syncing"
        || c.attempted !== true)) return "syncing";
    return "unavailable";
  }
  return reading.ageMs > CLOCK_HOLDOVER_MS ? "holdover" : "ready";
}

function clockParts(ms) {
  if (!Number.isFinite(ms)) return { time: "--:--:--", date: "日期待确认" };
  const moment = new Date(ms);
  try {
    return {
      time: new Intl.DateTimeFormat("zh-CN", {
        timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit",
        second: "2-digit", hour12: false,
      }).format(moment),
      date: new Intl.DateTimeFormat("zh-CN", {
        timeZone: "Asia/Shanghai", year: "numeric", month: "long", day: "numeric",
        weekday: "long",
      }).format(moment),
    };
  } catch (err) {
    return { time: moment.toTimeString().slice(0, 8), date: "" };
  }
}

function clockUncertaintyText(ms) {
  if (!Number.isFinite(ms) || ms <= 0) return "无法确认";
  const seconds = ms / 1000;
  if (seconds >= 1) return "约 ±" + seconds.toFixed(1) + " 秒";
  if (seconds >= 0.01) return "约 ±" + seconds.toFixed(2) + " 秒";
  return "约 ±" + seconds.toFixed(3) + " 秒";   /* 非零绝不显示成 ±0.00 */
}

function clockLastSyncText() {
  /* 最近校准 = 权威 age_s + 接入后的单调经过时长。
   * F5/bootstrap 重新接入旧锚点不会把「10 分钟前」刷新成「刚刚」。 */
  const ageMs = clockAgeMs();
  if (ageMs === null) return "尚未完成";
  const ago = Math.max(0, ageMs / 1000);
  if (ago < 60) return "刚刚";
  if (ago < 3600) return Math.floor(ago / 60) + " 分钟前";
  return Math.floor(ago / 3600) + " 小时前";
}

/* 顶栏唯一时间入口：正常只显示北京时间 + 时分秒 + 小状态图形。 */
function paintClockStatus() {
  const btn = $("#clock");
  if (!btn) return;
  const ui = clockUiState();
  const copy = CLOCK_COPY[ui] || CLOCK_COPY.unavailable;
  btn.dataset.state = ui;
  const label = $("#clock-label");
  const value = $("#clock-time");
  const icon = $("#clock-icon");
  if (label) label.textContent = ui === "demo" ? "演示时间" : "北京时间";
  if (icon) icon.innerHTML = CLOCK_ICONS[ui] || CLOCK_ICONS.unavailable;
  const nowMs = businessNowMs();
  if (value) {
    if (ui === "syncing") value.textContent = "校准中…";
    else if (ui === "unavailable") value.textContent = "待校准";
    else value.textContent = clockParts(nowMs).time;
  }
  const title = (ui === "demo" ? "演示时间" : "北京时间") + "，" + copy.label + "，查看时间详情";
  btn.title = title;
  btn.setAttribute("aria-label", title);
  btn.setAttribute("aria-expanded", clockPopOpenNow() ? "true" : "false");
}

/* 重新校准：只调用本机时钟；反馈留在时间详情，不抢占业务结论。 */
function resyncClock() {
  const c = S.businessClock;
  if (c.syncState === "syncing") return;
  c.syncState = "syncing";
  c.syncError = "";
  const request = (c.syncRequest || 0) + 1;
  c.syncRequest = request;
  c.syncRequestRound = c.syncRound;
  c.syncFollowRound = c.syncStatus === "syncing" ? c.syncRound : null;
  const startedGen = c.gen;
  paintClockStatus();
  if (clockPopOpenNow()) renderClockPop();
  paintClockPopFeedback();
  api("sync_clock").then(() => {
    if (c.syncRequest !== request || c.syncState !== "syncing") return;
    /* 结果经 business_clock 事件/轮询回收；超时无回执才在详情里报失败 */
    c.syncTimer = setTimeout(() => {
      if (S.businessClock.syncRequest === request && S.businessClock.syncState === "syncing"
          && S.businessClock.gen === startedGen) {
        S.businessClock.syncState = "idle";
        S.businessClock.syncError = "暂时无法确认本次校准结果，请稍后重试。";
        paintClockStatus();
        if (clockPopOpenNow()) renderClockPop();
        paintClockPopFeedback();
      }
    }, 12000);
    pollClockNow();
  }).catch((err) => {
    if (c.syncRequest !== request || c.syncState !== "syncing") return;
    c.syncState = "idle";
    c.syncError = "重新校准没能开始：" + msg(err);
    paintClockStatus();
    if (clockPopOpenNow()) renderClockPop();
    paintClockPopFeedback();
  });
}

/* 本机快照轮询：不访问外部网络；首校时完成、F5/可见性恢复、失效后都能更新。 */
function pollClockNow() {
  if (!hasBridge() || S.demo) return Promise.resolve(null);
  const started = _monoMs();
  return api("clock_snapshot").then((snap) => {
    ingestBusinessClock(snap, { rttMs: _monoMs() - started });
    S.businessClock.attempted = true;
    return snap;
  }).catch(() => {
    S.businessClock.attempted = true;
    markClockUnavailable("暂时无法读取时间状态");
    return null;
  });
}

function startClockPolling() {
  if (S.clockTimer) return;
  S.clockTimer = setInterval(pollClockNow, CLOCK_POLL_MS);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) pollClockNow();
  });
}

function mergeTask(prev, incoming) {
  const merged = Object.assign({}, prev || {}, incoming || {});
  TASK_RUNTIME_FIELDS.forEach((key) => {
    const value = incoming ? incoming[key] : undefined;
    if ((value === undefined || value === null || value === "") && prev && prev[key] != null) {
      merged[key] = prev[key];
    }
  });
  if (String(merged.status) === "submitting" && !merged.submitSeen) merged.submitSeen = Date.now();
  return merged;
}

/* 计划级终态与单场终态集合：终态之后的同计划旧事件只补详情，不回退结论（P1） */
const PLAN_TERMINAL = ["completed", "partial", "failed", "stopped"];
const TASK_TERMINAL = ["success", "failed", "already_signed", "canceled"];

/* 已见过的最大计划接纳代次：屏上计划的代次与会话记录取大（P1） */
function maxPlanGen() {
  const a = Number(S.maxPlanGen);
  const b = Number(S.plan && S.plan.gen);
  return Math.max(Number.isFinite(a) && a > 0 ? a : 0,
                  Number.isFinite(b) && b > 0 ? b : 0);
}

/* 计划事件归属路由（P1）：事件只有属于当前屏上计划，或携带比已见过的更大的
 * plan_generation（后端最终接纳时赋值，新计划的事件可能早于 start_plan 的
 * ack），才允许改当前状态。旧计划迟到的 plan/task/plan_done 只能进历史
 * 对账，不得清运行中计划的忙态、任务、停止入口，也不得改写已完成结论。 */
function planEventOwns(data) {
  const id = String((data && data.plan_id) || "");
  if (!id) return false;
  if (S.plan.id === id) return true;          // 当前屏上计划：同计划按幂等合并
  const gen = Number(data.plan_generation);
  if (!Number.isFinite(gen)) return !S.plan.id;   // 旧格式载荷：只在空屏时接纳
  return gen > maxPlanGen();                  // 更新的接纳（含早于 ack 的新计划）
}

/* 接纳事件时更新会话代次记录，返回事件的代次（无代次返回 0） */
function admitPlanEvent(data) {
  const gen = Number(data && data.plan_generation);
  if (!Number.isFinite(gen)) return 0;
  S.maxPlanGen = Math.max(S.maxPlanGen || 0, gen);
  return gen;
}

/* 同计划状态推进：已定终态不回退，运行态照常更新（P1） */
function advancePlanStatus(incoming) {
  if (PLAN_TERMINAL.indexOf(S.plan.status) >= 0) return;
  S.plan.status = String(incoming || "ready");
  setBusy(["ready", "running"].indexOf(S.plan.status) >= 0);
}

function handlePlan(payload) {
  const data = payload || {};
  if (!planEventOwns(data)) return;      /* 旧计划迟到事件不替换当前计划（P1） */
  const gen = admitPlanEvent(data);
  if (!S.plan.id || S.plan.id !== String(data.plan_id)) {
    S.plan = { id: String(data.plan_id || ""), gen: gen, status: String(data.status || "ready"), tasks: new Map(), armedAt: Date.now() / 1000 };
    setBusy(["ready", "running"].indexOf(S.plan.status) >= 0);
  } else {
    if (gen > (Number(S.plan.gen) || 0)) S.plan.gen = gen;
    advancePlanStatus(data.status);
  }
  /* 晚到 plan 事件经 mergeTask 合并，不抹已收到的提交次数等 telemetry */
  (data.tasks || []).forEach((task) => {
    S.plan.tasks.set(String(task.task_id),
                     mergeTask(S.plan.tasks.get(String(task.task_id)) || null, task));
  });
  render();
}

function handleTask(payload) {
  const data = payload || {};
  if (!planEventOwns(data)) return;           // 旧计划的场次事件：不改当前计划（P1）
  const gen = admitPlanEvent(data);
  if (!S.plan.id || S.plan.id !== String(data.plan_id)) {
    S.plan = { id: String(data.plan_id || ""), gen: gen, status: "running", tasks: new Map(), armedAt: Date.now() / 1000 };
  } else if (gen > (Number(S.plan.gen) || 0)) {
    S.plan.gen = gen;
  }
  const prev = S.plan.tasks.get(String(data.task_id)) || null;
  const task = mergeTask(prev, data);
  /* 已结束的场次不得被旧进度拖回运行态：状态保持先到的事实（P1） */
  if (prev && TASK_TERMINAL.indexOf(String(prev.status)) >= 0
      && TASK_TERMINAL.indexOf(String(task.status)) < 0) {
    task.status = prev.status;
  }
  S.plan.tasks.set(String(data.task_id), task);
  if (["success", "failed", "already_signed", "canceled"].indexOf(task.status) >= 0) {
    /* 报名成功是已确认事实：任务卡、列表、计数、选择门禁和冲突基线同一时刻更新 */
    if (noteSignedTasks([task])) scheduleSignedRefresh();
    showTaskResult(task);
  }
  render();
}

/* 计划级结论分类：协调器的 counts.failed 把「结果未确认」也算进失败，
 * 但对用户这是两种不同的事实——这里按逐任务 stop_reason 单列，
 * 任务卡、总结、大屏和 LED 共用这一份口径。 */
function planOutcome() {
  const counts = { success: 0, already_signed: 0, failed: 0, canceled: 0,
                   unconfirmed: 0, auth_unavailable: 0, unknown: 0 };
  S.plan.tasks.forEach((task) => {
    const state = String(task.status || "");
    if (state === "success") counts.success += 1;
    else if (state === "already_signed") counts.already_signed += 1;
    else if (state === "canceled") counts.canceled += 1;
    else if (state === "failed") {
      if (task.stop_reason === "unconfirmed") counts.unconfirmed += 1;
      else if (task.stop_reason === "auth_unavailable") counts.auth_unavailable += 1;
      else counts.failed += 1;
    }
  });
  counts.unknown = counts.unconfirmed + counts.auth_unavailable;
  return counts;
}

/* 未确认类别的统一标签：任务数、总结标题、大屏与 LED 必须同一个数字口径。
 * 两种未知同时存在时说「需确认」，不把没提交的场次混进「待核对」。 */
function unknownLabel(outcome) {
  if (!outcome || !outcome.unknown) return "";
  if (outcome.unconfirmed && outcome.auth_unavailable) {
    return "有 " + outcome.unknown + " 场需确认";
  }
  if (outcome.unconfirmed) return "有 " + outcome.unconfirmed + " 场待核对";
  return "有 " + outcome.auth_unavailable + " 场未提交";
}

/* 计划终态的唯一呈现入口：plan_done 事件与 F5/bootstrap 恢复共用一份，
 * 这样「实时结束」和「重载后恢复」不会出现两套结论（FR05）。 */
function presentPlanDone(payload, autoRefresh) {
  const data = payload || {};
  /* 终态载荷也带接纳代次：并入会话记录，旧代次事件此后不得再改写（P1） */
  const doneGen = notePlanGeneration(data.plan_generation);
  if (doneGen > (Number(S.plan.gen) || 0)) S.plan.gen = doneGen;
  S.plan.id = String(data.plan_id || S.plan.id || "");
  S.plan.status = String(data.status || "failed");
  /* 计划收尾：解除「正在停止」并解锁 busy（canceled/终态都已落定） */
  S.stopping = false;
  /* 记下终态时的选择集合：集合不变时结论留在 LED 上，变化后回到预览 */
  S.plan.doneSelection = selectionSignature();
  setBusy(false);
  /* 已确认成功的场次立即回写列表（计数/分组/门禁/冲突基线），
   * 再安排一次有界刷新去取学校权威列表；刷新失败不撤销成功事实。 */
  const signedChanged = noteSignedTasks(Array.from(S.plan.tasks.values()));
  const outcome = planOutcome();
  const successTotal = outcome.success + outcome.already_signed;
  /* 计划级结论也带上失败场次到达的停止条件（次数/时长/截止），去重后一句话说清 */
  const stopNotes = [];
  S.plan.tasks.forEach((task) => {
    if (String(task.status) !== "failed") return;
    if (task.stop_reason === "unconfirmed" || task.stop_reason === "auth_unavailable") return;
    const note = stopReasonText(task.stop_reason);
    if (note && task.stop_reason !== "process_error" && stopNotes.indexOf(note) < 0) stopNotes.push(note);
  });
  const noteText = stopNotes.length ? "（" + stopNotes.join("、") + "）" : "";
  /* 未知必须分列：待核对=已提交没回应，未提交=登录状态没能确认。
   * 用 unknown 总和冒充「待核对」会把没提交的场次也算成提交过（FR06）。 */
  const splitText = "已报名 " + successTotal + " 场 · 报名未成功 " + outcome.failed
    + " 场 · 待核对 " + outcome.unconfirmed + " 场 · 未提交 " + outcome.auth_unavailable
    + " 场 · 已取消 " + outcome.canceled + " 场";
  const unknownTail = [];
  if (outcome.unconfirmed) {
    unknownTail.push(outcome.unconfirmed + " 场已提交但没拿到确定回应，请去活动页核对，本工具不会自动重发");
  }
  if (outcome.auth_unavailable) {
    unknownTail.push(outcome.auth_unavailable + " 场因登录状态无法确认而没有提交，检查网络后刷新重试");
  }
  if (S.plan.status === "completed") {
    setResult({ ok: true, title: "报名计划完成", text: "已报名 " + successTotal + " 场，建议去学校活动页确认一眼。" });
  } else if (S.plan.status === "stopped") {
    /* 用户停止是主结论；停止前已提交却没回应的场次并列说清，不并进「取消」 */
    const extra = outcome.unknown ? "其中 " + unknownLabel(outcome).replace(/^有 /, "")
      + "，请去活动页核对。" : "";
    setResult({ ok: "partial", title: "报名计划已停止",
                text: "已提交的场次保留原状态，尚未提交的场次已取消。" + extra
                  + (outcome.unknown ? "（" + splitText + "）" : ""),
                action: { label: "重新选择活动", run: restartStoppedPlan } });
  } else if (S.plan.status === "partial") {
    setResult({ ok: "partial",
                title: "报名计划部分完成" + (outcome.unknown ? "，" + unknownLabel(outcome) : ""),
                text: "已报名 " + successTotal + " 场。" + splitText + "。"
                  + unknownTail.join("；") + (unknownTail.length ? "。" : ""),
                action: outcome.unknown ? { label: "刷新列表", run: () => refresh(false) } : undefined });
  } else if (outcome.unknown > 0) {
    /* 含未知只能用中性/部分完成视觉：不能把所有场次说成「都没抢到」 */
    setResult({ ok: "partial", title: "报名计划已结束，" + unknownLabel(outcome),
                text: splitText + "。" + unknownTail.join("；") + "。"
                  + (outcome.failed ? "其余失败场次的原因见任务状态。" : ""),
                action: { label: "刷新列表", run: () => refresh(false) } });
  } else {
    setResult({ ok: false, title: "报名计划没有成功", text: "报名未成功 " + outcome.failed + " 场" + noteText + "。逐场原因已列在上面任务状态里。", action: { label: "刷新列表", run: () => refresh(false) } });
  }
  render();
  if (signedChanged && autoRefresh !== false) scheduleSignedRefresh();
}

function handlePlanDone(payload) {
  const data = payload || {};
  if (!planEventOwns(data)) return;           // 旧计划的完成事件不得改写当前计划（P1）
  admitPlanEvent(data);
  /* 同计划已定终态：重复终态只补任务详情，不把结论改成另一状态（P1） */
  if (S.plan.id === String(data.plan_id || "")
      && PLAN_TERMINAL.indexOf(S.plan.status) >= 0) {
    (data.tasks || []).forEach((task) => {
      S.plan.tasks.set(String(task.task_id),
                       mergeTask(S.plan.tasks.get(String(task.task_id)) || null, task));
    });
    render();
    return;
  }
  presentPlanDone(data);
}

/* 停止后一键把原计划里还能报的场次重新选上 */
function restartStoppedPlan() {
  if (S.busy) return;
  S.selected.clear();
  S.plan.tasks.forEach((task) => {
    const item = findItem(task.activity_id);
    /* 重新选择也要过门禁：已截止的场次不能被重新选上 */
    if (item && isEligibleForPlan(item) && ["canceled", "failed"].indexOf(String(task.status)) >= 0) S.selected.add(String(item.id));
  });
  S.selectionNotice = "";
  setResult(null);
  render();
}

/* worker 已经就同一场写过结论时，不再叠一条同义摘要（RC-06）。
 * 只认「同一场名称 + 同一结论关键词」，不同场次的成功互不影响。 */
function loggedConclusion(name, keyword) {
  const target = String(name || "");
  return S.recentLogs.some((line) => line.indexOf(target) >= 0 && line.indexOf(keyword) >= 0);
}

function showTaskResult(task) {
  const name = task.name || "这一场活动";
  /* 计划还在跑时，单场终态只更新任务卡与运行记录：结果框和整屏留给
   * handlePlanDone 的计划级结论（单场成功不得抢写全局成功，UX-05）。 */
  const planLive = !!S.busy;
  if (task.status === "success") {
    if (!loggedConclusion(name, "报名成功")) addLog("[OK] 报名成功：" + name);
    if (!planLive) setResult({ ok: true, title: "报名成功：" + name, text: "建议去活动页确认一眼。" });
  } else if (task.status === "already_signed") {
    if (!loggedConclusion(name, "已经报过")) addLog("[i] 已经报过：" + name);
    if (!planLive) setResult({ ok: "partial", title: "已经报过：" + name, text: "这场保留为已报名，其他场次继续。" });
  } else if (task.status === "canceled") {
    addLog("[i] 已取消：" + name);
    if (!planLive) setResult({ ok: "partial", title: "已取消：" + name, text: "其他场次继续按计划处理。" });
  } else if (task.stop_reason === "unconfirmed") {
    /* 结果未确认：不说没抢到、不画失败叉；只给次数与下一步核对动作 */
    const tried = Math.max(0, Math.floor(Number(task.attempts) || 0));
    addLog("[?] 结果待核对：" + name + " · 已提交 " + tried + " 次，没有确定回应");
    if (!planLive) {
      setResult({ ok: "partial", title: "报名结果待核对：" + name,
                  text: "已提交 " + tried + " 次，但没拿到学校的确定回应。请打开活动页核对是否已报名；本工具已停止自动重发，不会自动开始新计划。",
                  action: { label: "刷新列表", run: () => refresh(false) } });
    }
  } else if (task.stop_reason === "auth_unavailable") {
    /* 网络/接口无法确认 ≠ 登录过期：不跳到「重新登录」 */
    addLog("[X] 登录状态暂时无法确认：" + name + " · 本场没有提交");
    if (!planLive) {
      setResult({ ok: "partial", title: "暂时无法验证登录状态：" + name,
                  text: "本场没有提交。先检查网络或校园网，再点「刷新」重试；不要急着重新登录。",
                  action: { label: "刷新列表", run: () => refresh(false) } });
    }
  } else {
    /* 终态文案 = 归因建议 + 学校原话 + 到达的停止条件（时长/次数/截止） */
    const bits = [grabHint(task.reason), "原话：" + cleanReason(task.reason)];
    const stopNote = stopReasonText(task.stop_reason);
    if (stopNote && task.stop_reason !== "process_error") bits.push(stopNote);
    addLog("[X] 报名未成功：" + name + " · " + cleanReason(task.reason));
    if (!planLive) setResult({ ok: false, title: "报名未成功：" + name, text: bits.join(" ") });
    /* 失败后顺手复查一次登录状态和列表：掉了登录的页面只会看到按钮不出现，
     * 复查能把这个真原因翻到登录胶囊上。只查状态，不改写已出的结论。
     * 计划还在跑也要查——不能因为改成「只更新单场反馈」就把这条副作用丢掉。 */
    if (!S.recheckPending) {
      S.recheckPending = true;
      setTimeout(() => { S.recheckPending = false; refresh(true); }, 400);
    }
  }
}

function setLogin(hours, ok, present, anchor) {
  S.login.hours = numOrNull(hours);
  S.login.ok = ok;
  /* present 是后端 session_present 的回声：false=从没登录过，true=登录过。
   * 不传（undefined）表示这次事件没说，保留上一次的判断。 */
  if (present !== undefined) S.login.present = !!present;
  /* 估算锚点（绝对时刻）：bootstrap.login / check 事件 / 登录完成后都带同一来源的
   * estimated_expires_at（epoch 秒）。收到就整份替换（重新登录=新锚点），
   * 显式 null 且本机无会话时清掉旧锚点；事件没带（undefined）保持现状。
   * 绝不按「本次收到事件的时刻 + hours」重算——那会把每次刷新都变成续期。 */
  if (anchor && typeof anchor === "object" && "estimated_expires_at" in anchor) {
    const at = numOrNull(anchor.estimated_expires_at);
    S.login.expireAt = at === null ? null : at * 1000;   /* 统一存 epoch 毫秒 */
    if (at === null) S.login.savedAt = null;
    else if (anchor.saved_at) S.login.savedAt = anchor.saved_at;
  }
  paintLogin();
  if (document.body) {
    paintRail();
    paintSelected();
    paintBoardActions();
    /* 后到的校验结论要能把空态从「正在检查」改成真实状态 */
    refreshLoginEmptyState();
  }
}

/* 当前凭证剩余小时：只从绝对锚点按 Date.now 推；没有锚点返回事件带回的静态值，
 * 两者都没有返回 null（未知，不编 17 小时）。 */
function loginHoursLeft() {
  if (S.login.expireAt !== null && S.login.expireAt !== undefined) {
    const bnMs = businessNowMs();
    if (bnMs == null) return null;   // 未校准：剩余显示未知，不拿 OS 时间冒充
    return (S.login.expireAt - bnMs) / 3600000;
  }
  return S.login.hours;
}

function expiryText() {
  const at = S.login.expireAt;
  return at ? "约到 " + whenText(at) : "未知（以学校校验为准）";
}

function paintLogin() {
  const pill = $("#pill");
  const detail = $("#status-detail");
  /* 显示用的剩余小时每次按锚点现算：胶囊里的「约剩」随每秒走字递减，
   * 不再用事件带回的静态小时数。 */
  const h = loginHoursLeft();
  const ok = S.login.ok;
  let cls = "pill";
  let text = "未登录";
  let sub = "";
  let note = "点右上角「登录」开始使用";
  let state = "none";
  if (S.busy && !S.login.loginPending) {
    cls += " is-busy";
    /* 忙时胶囊只表达登录结论，不拿「正在处理」替换它（计划进度属于报名区域） */
    text = S.login.present === false ? "未登录" : "已登录";
    sub = "";
    note = S.stopping ? "正在停止计划；已报名的场次不受影响"
      : "登录状态保持不变；计划进度见报名区域";
    state = S.login.present === false ? "none" : "ok";
  } else if (S.demo) {
    /* 演示模式必须自己说出来：不能借「登录有效」的外观掩盖没有真实会话（R20） */
    cls += " is-warn";
    text = "演示模式";
    sub = "内存样例";
    note = "不读取登录信息、不访问学校，只演示界面与流程";
    state = "demo";
  } else if (S.login.loginPending) {
    /* 登录窗口仍在等待用户完成；不能把旧的有效/失效结论伪装成成功。 */
    cls += " is-busy";
    text = "登录中…";
    note = "请在弹出的浏览器中完成登录；取消或关闭窗口后会保留原状态";
    state = "pending";
  } else if (S.login.checking || (ok === null && S.login.present === true && h !== null)) {
    /* 正在真实校验：本机有会话也不提前宣布「已登录」，等学校接口结论 */
    cls += " is-busy";
    text = "检查登录…";
    note = "正在检查登录状态，马上就好";
    state = "checking";
  } else if (S.login.present === false) {
    /* 从没登录过（session.json 缺失/解析不了/没有 token）：引导首次登录，
     * 不算失效。check 失败也是「还没登录」，不是「登录状态坏了」。 */
    cls += " is-none";
    note = "点右上角「登录」，在打开的浏览器中完成登录";
  } else if (S.login.result === "unknown" && ok === false) {
    /* 学校接口没应答/网络异常：不能误报成「重新登录」，会话原样保留 */
    cls += " is-none";
    text = "登录待确认";
    note = "暂时无法确认登录状态；检查网络后点「刷新」，先不用重新登录";
    state = "unknown";
  } else if (S.login.result === "valid" && typeof h === "number" && h <= 0) {
    /* 学校校验有效但估算已到点：仍显示有效，估算只作提醒，不得显示成失效 */
    cls += " is-ok";
    text = "已登录";
    sub = "估算已到点，以学校校验为准";
    note = "已使用保存的登录信息；估算时间只是提醒，不代表失效";
    state = "ok";
  } else if ((ok === false || (typeof h === "number" && h <= 0))
             && S.login.present !== false) {
    /* 登录过且确认失败/过期，才叫登录已失效；hours_left=0 单独出现
     * 不代表首次未登录。present 未知（老载荷）时维持保守判失效。 */
    cls += " is-bad";
    text = "登录已失效";
    note = "重新登录后可继续使用";
    state = "bad";
  } else if (typeof h === "number") {
    if (h <= EXPIRING_H) {
      cls += " is-warn";
      text = "已登录";
      sub = "约剩 " + leftText(h);
      note = "不到 2 小时，建议现在先重新登录一次";
      state = "warn";
    } else {
      cls += " is-ok";
      text = "已登录";
      sub = "约剩 " + leftText(h);
      note = S.login.result === "valid" ? "已使用保存的登录信息" : "登录状态可用，过期后重新登录一次";
      state = "ok";
    }
  } else if (ok === true) {
    cls += " is-ok";
    text = "已登录";
    note = "刚核对过，可以加载活动";
    state = "ok";
  }
  pill.className = cls;
  pill.dataset.state = state;
  pill.setAttribute("aria-label", text + (sub ? "，" + sub : ""));
  $("#pill-text").textContent = text;
  $("#pill-sub").textContent = sub;
  detail.textContent = note;
  const loginBtnText = $("#btn-login-text");
  /* 只在「确实登录过」的几种状态里叫重新登录；首次使用和未知状态都叫登录 */
  if (loginBtnText) loginBtnText.textContent = state === "bad" || state === "warn" || state === "ok" ? "重新登录" : "登录";
  const loginBtn = $("#btn-login");
  /* 按原型：未登录/失效时转实心更醒目，有效时回到描边 */
  const solid = state === "none" || state === "bad";
  loginBtn.classList.toggle("btn-solid", solid);
  loginBtn.classList.toggle("btn-line", !solid);
}

function setBusy(busy) {
  S.busy = !!busy;
  /* 计划开跑先暂停引导（运行状态优先），收尾后从原步骤继续 */
  if (S.busy && S.tour.open && !S.tour.paused) tourPauseNow();
  if (!S.busy && S.tour.paused) tourResumeNow();
  paintLogin();
  updateControls();
}

let resultTimer = null;

function setResult(result) {
  const el = $("#result");
  clearTimeout(resultTimer);
  resultTimer = null;
  if (!result) {
    el.hidden = true;
    el.textContent = "";
    el.className = "result";
    return;
  }
  el.hidden = false;
  const cls = result.ok === true ? "is-ok" : (result.ok === "partial" ? "is-partial" : "is-bad");
  el.className = "result " + cls;
  el.innerHTML = "<strong>" + esc(result.title || "") + "</strong><span>" + esc(result.text || "") + "</span>" +
    (result.action ? "<button class=\"result-action\" type=\"button\" id=\"result-action\">" + esc(result.action.label) + "</button>" : "");
  if (result.action) {
    const btn = el.querySelector("#result-action");
    btn.addEventListener("click", result.action.run);
  }
  /* 登录成功的结论 6 秒后自动收起；其他结论留到用户确认 */
  if (result.autoClose) resultTimer = setTimeout(() => { if (!S.busy) setResult(null); }, Math.max(0, result.autoClose - Date.now()));
}

function addLog(line) {
  const box = $("#log");
  const text = String(line == null ? "" : line);
  /* 同一句话（含 worker 已打印的成功结论）只留一条：coordinator 的重复终态
   * 与 UI 摘要不能给用户两个成功结论（RC-06）。 */
  if (S.recentLogs.indexOf(text) >= 0) return;
  S.recentLogs.push(text);
  if (S.recentLogs.length > 40) S.recentLogs.shift();
  const nowMs = businessNowMs();
  const stamp = nowMs === null ? "待校准" : clockParts(nowMs).time;
  /* worker 已记录事件发生的业务时刻，不能再叠加当前接收时间。 */
  const sourceTime = text.match(/^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s/);
  const hasSourceTime = sourceTime && toTs(sourceTime[1]) !== null;
  /* 每行一层小条：按脚本前缀着色（[X] 红 / [!] 橙 / [OK] 绿），原话不改 */
  const row = document.createElement("div");
  row.className = "lg";
  const kind = text.indexOf("[X]") === 0 ? "bad"
    : text.indexOf("[!]") === 0 ? "warn"
    : (text.indexOf("[OK]") === 0 || text.indexOf("▶") === 0) ? "ok" : "";
  if (kind) row.dataset.k = kind;
  row.textContent = hasSourceTime ? text : stamp + "  " + text;
  box.appendChild(row);
  while (box.childElementCount > 500) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
  /* 收起时顶栏显示最新一句 */
  const last = $("#log-last");
  if (last && text.trim()) last.textContent = stamp + "  " + text.trim();
  requestAnimationFrame(() => { box.scrollTop = box.scrollHeight; });
}

/* 每轮 check 的唯一时钟入口：结构化载荷整份替换旧状态（R08）。
 * 没有结构化结论（旧载荷/本轮没采样）就明确落 unknown——不能凭失败文本
 * 推断「本机慢 N 秒」，也不能把上一轮的 warn 无限保留。 */
function applyClock(payload) {
  const data = payload && typeof payload === "object" ? payload : null;
  const state = data && ["ok", "warn", "unknown"].indexOf(data.state) >= 0 ? data.state : "unknown";
  S.clock = {
    state: state,
    offset: numOrNull(data && data.offset_s),
    uncertainty: numOrNull(data && data.uncertainty_s),
    checkedAt: numOrNull(data && data.checked_at),
    detail: String((data && data.detail) || (data ? "" : "本轮没有结构化验时结论")),
  };
  paintClockSkew();
}

function clockCheckedText() {
  const at = S.clock.checkedAt;
  if (at === null) return "";
  return new Date(at * 1000).toLocaleString("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

/* HTTP Date 只是学校诊断：只进详情，不占用顶栏、不参与业务时间。 */
function paintClockSkew() {
  if (S.clockPopOpen) renderClockPop();
}

/* ---------------------------------------------------------------- 时间详情 */
function clockPopOpenNow() {
  return !!S.clockPopOpen;
}

function closeClockPop(restoreFocus) {
  const pop = $("#clock-pop");
  if (pop) pop.hidden = true;
  S.clockPopOpen = false;
  $("#clock").setAttribute("aria-expanded", "false");
  if (restoreFocus) $("#clock").focus();
}

function toggleClockPop() {
  const pop = $("#clock-pop");
  if (!pop) return;
  if (clockPopOpenNow()) { closeClockPop(true); return; }
  S.clockPopOpen = true;
  renderClockPop();
  pop.hidden = false;
  $("#clock").setAttribute("aria-expanded", "true");
  placeClockPop();
  const close = pop.querySelector("#clock-pop-close");
  if (close) close.focus();
}

function placeClockPop() {
  const pop = $("#clock-pop");
  const clock = $("#clock");
  if (!pop || !clock) return;
  const rect = clock.getBoundingClientRect();
  pop.style.top = Math.round(rect.bottom + 8) + "px";
  pop.style.left = Math.round(Math.max(12,
    Math.min(window.innerWidth - pop.offsetWidth - 12, rect.right - pop.offsetWidth))) + "px";
}

function renderClockPop() {
  const pop = $("#clock-pop");
  if (!pop) return;
  /* 校时轮询会更新浮层内容，但不能把用户已经展开的原生 details 收回去。 */
  const detailsState = Array.from(pop.querySelectorAll("details.records") || [])
    .map((details) => !!details.open);
  const scrollTop = Number(pop.scrollTop) || 0;
  const ui = clockUiState();
  /* 浮层状态图形与顶栏同一状态：失败/等待警告色、已校准成功色、演示中性 */
  pop.dataset.state = ui;
  const copy = CLOCK_COPY[ui] || CLOCK_COPY.unavailable;
  const reading = clockReading();
  const nowMs = reading && reading.valid ? reading.nowMs : null;
  const parts = ui === "demo"
    ? clockParts(Date.now())
    : (nowMs === null ? { time: "--:--:--", date: "日期待确认" } : clockParts(nowMs));
  const title = ui === "demo" ? "演示时间" : "北京时间";
  const c = S.businessClock || {};
  /* 详情与门禁同一份误差：采样时误差 + 采样后漂移，不再重复叠加采样前漂移 */
  const uncNow = reading && reading.valid ? reading.uncertaintyMs : null;
  const httpClock = S.clock || {};
  const httpText = httpClock.state === "ok" ? "粗测通过（学校 HTTP 时间，仅诊断）"
    : httpClock.state === "warn" ? "粗测有偏差约 " + Math.round(Math.abs(httpClock.offset || 0)) + " 秒（仅诊断）"
      : "本轮没有学校时间粗测结论";
  /* 生命周期提示：上一周期关闭到本次启动的间隔；abnormal 必须带「约」 */
  const life = S.lifecycle || {};
  let lifeText = "未知";
  if (typeof life.closed_for_s === "number") {
    lifeText = (life.close_quality === "abnormal" ? "约 " : "") + durText(life.closed_for_s)
      + (life.close_quality === "abnormal" ? "（按最后存活时刻估算，上次未正常退出）"
        : "（从上次正常关闭到本次启动）");
  } else if (life.close_quality === "unknown") {
    lifeText = "未知（上次没有可信的关闭记录）";
  }
  const loginRow = S.login.present === false ? '<dd>未登录</dd>'
    : '<dd id="clock-pop-left">' + (S.login.expireAt ? "" : "未知（以学校校验为准）") + "</dd>";
  const syncing = c.syncState === "syncing";
  pop.innerHTML =
    '<div class="pop-head"><h2 id="clock-pop-title">' + title + '</h2>' +
    '<button id="clock-pop-close" type="button" aria-label="关闭时间详情">×</button></div>' +
    '<div class="pop-time" id="clock-pop-time">' + esc(parts.time) + '</div>' +
    '<div class="pop-date">' + esc(parts.date) + '</div>' +
    '<div class="status-line state-icon" id="clock-pop-status"><span class="state-icon">' +
    (CLOCK_ICONS[ui] || CLOCK_ICONS.unavailable) + '</span><span>' + esc(copy.label) + '</span></div>' +
    '<p class="pop-explain">' + esc(copy.explain) + '</p>' +
    '<dl class="facts pop-facts">' +
    '<div><dt>最近校准</dt><dd>' + esc(ui === "demo" ? "离线示例" : clockLastSyncText()) + '</dd></div>' +
    '<div><dt>估计误差</dt><dd>' + esc(ui === "demo" ? "不适用" : clockUncertaintyText(uncNow)) + '</dd></div>' +
    '<div><dt>上次退出</dt><dd>' + esc(lifeText) + '</dd></div>' +
    '<div><dt>登录预计剩余</dt>' + loginRow + '</div>' +
    '</dl>' +
    '<details class="records"><summary>使用记录</summary><dl class="facts">' +
    '<div><dt>上次退出至本次启动</dt><dd>' + esc(lifeText) + '</dd></div>' +
    '<div><dt>登录预计剩余时间</dt>' + loginRow + '</div>' +
    '</dl><p>登录剩余时间仅供参考，以学校校验为准。</p></details>' +
    '<details class="records"><summary>时间来源</summary><dl class="facts">' +
    '<div><dt>校准来源</dt><dd>' + esc(ui === "demo" ? "演示模式"
      : (c.source || "网络时间服务")) + '</dd></div>' +
    '<div><dt>学校 HTTP 时间</dt><dd>' + esc(httpText) + '</dd></div>' +
    '</dl><p>差值只用于诊断，报名按北京时间执行。</p></details>' +
    '<div class="pop-foot"><span class="pop-note">不会修改电脑时间</span>' +
    '<button class="sync-btn" id="clock-pop-sync" type="button"' +
    ((syncing || ui === "demo") ? " disabled" : "") + '>' +
    (syncing ? "正在校准…" : (ui === "unavailable" ? "重试校准" : "重新校准时间")) +
    '</button></div>' +
    '<p class="pop-feedback" id="clock-pop-feedback" role="status"></p>';
  Array.from(pop.querySelectorAll("details.records") || []).forEach((details, index) => {
    details.open = detailsState[index] === true;
  });
  pop.scrollTop = scrollTop;
  const close = pop.querySelector("#clock-pop-close");
  if (close) close.addEventListener("click", () => closeClockPop(true));
  const sync = pop.querySelector("#clock-pop-sync");
  if (sync) sync.addEventListener("click", resyncClock);
  paintClockPopFeedback();
  paintClockPopLeft();
}

function paintClockPopFeedback() {
  const cell = document.querySelector("#clock-pop-feedback");
  if (!cell) return;
  const c = S.businessClock || {};
  if (c.syncState === "syncing") cell.textContent = "正在向时间服务核对，完成后这里会更新。";
  else if (c.syncError === CLOCK_SYNC_FAILURE) {
    /* 旧时间是否仍可用随年龄变化，不能把失败当时的结论永久存成文案。 */
    cell.textContent = businessNowMs() === null ? "本次校准未成功，请检查网络后重试。"
      : "本次校准未成功，仍使用上次校准结果。";
  }
  else if (c.syncError) cell.textContent = c.syncError;
  else if (c.reason && c.state === "unavailable") cell.textContent = String(c.reason);
  else cell.textContent = "";
}

/* 浮层里的「登录预计还剩」随每秒走字递减：打开时与 tick() 都会重写这一个 dd。 */
function paintClockPopLeft() {
  const cell = document.querySelector("#clock-pop-left");
  if (!cell) return;
  const anchor = S.login.expireAt;
  if (!anchor) { cell.textContent = "未知（以学校校验为准）"; return; }
  const nowMs = businessNowMs();
  if (nowMs === null) { cell.textContent = "待校准（先校准时间再估算）"; return; }
  const leftS = (anchor - nowMs) / 1000;
  cell.textContent = leftS > 0
    ? "约 " + durText(leftS) + "（估算，以学校校验为准）"
    : "估算已到点，以学校校验为准";
}

function setLogOpen(open, silent) {
  S.logOpen = !!open;
  const card = document.querySelector(".log-card");
  if (card) card.dataset.open = String(S.logOpen);
  $("#btn-log-toggle").textContent = S.logOpen ? "收起" : "展开";
  $("#btn-log-toggle").setAttribute("aria-expanded", String(S.logOpen));
  if (!silent) savePrefs({ log_open: S.logOpen });
}

function copyLog() {
  /* 日志正文是逐行小条：复制时按行拼回带换行的纯文本 */
  const lines = Array.from($("#log").children).map((row) => row.textContent);
  const text = lines.join("\n");
  const done = () => addLog("[i] 运行记录已复制");
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, () => addLog("[X] 复制没成功"));
  else {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); done(); } catch (err) { addLog("[X] 复制没成功"); }
    ta.remove();
  }
}

function fillHelp(help) {
  const data = help || DEFAULT_HELP;
  $("#help-title").textContent = data.title || DEFAULT_HELP.title;
  $("#help-sub").textContent = data.subtitle || "";
  $("#help-note").textContent = data.note || "";
  $("#help-steps").innerHTML = (data.steps || []).map((step) => "<li><span class=\"no\">" + esc(step.no) + "</span><div><div class=\"t\">" + esc(step.title) + "</div><div class=\"d\">" + esc(step.text) + "</div></div></li>").join("");
}

function showHelp() {
  const dialog = $("#help");
  helpStatus("");
  paintUpdate();
  if (!dialog.open) dialog.showModal();
  savePrefs({ welcome_seen: true });
}

/* 辅助动作的局部反馈：只在帮助里说下一步，不去复读到大屏或结果框。
 * 长地址/错误原话做成可选文本，不在 WebView 里导航。 */
function helpStatus(text, tone) {
  const box = $("#help-status");
  if (!box) return;
  if (!text) {
    box.hidden = true;
    box.textContent = "";
    return;
  }
  box.hidden = false;
  box.dataset.tone = tone || "warn";
  box.textContent = text;
  revealHelpStatus(box);
}

/* 矮窗里反馈可能刚好落在折叠处：用户触发的结果滚入正文可见区，头尾不动。 */
function revealHelpStatus(box) {
  if (box && $("#help").open && typeof box.scrollIntoView === "function") {
    box.scrollIntoView({ block: "nearest" });
  }
}

/* 帮助中的重播先确认非忙，再关模态、把焦点还给顶栏，最后进第1步。
 * 不复制教程长文，也不在弹窗还开着时叠一层遮罩。 */
function replayTourFrom(dialogSelector, openerSelector) {
  if (S.busy) {
    addLog("[i] 计划运行中：计划结束后再重看引导");
    return false;
  }
  const dialog = $(dialogSelector);
  if (dialog && dialog.open) dialog.close();
  const opener = $(openerSelector);
  if (opener) {
    try { opener.focus(); } catch (err) { /* 元素不可聚焦就算了，不挡住重播 */ }
  }
  startTour(0);
  return true;
}

/* 忙时只禁重播；查版本、打开目录不打断报名计划。 */
function paintReplayButtons() {
  [["#btn-help-tour", "重看引导"]].forEach((pair) => {
    const btn = $(pair[0]);
    if (!btn) return;
    btn.disabled = !!S.busy;
    btn.textContent = S.busy ? "计划结束后可重看引导" : pair[1];
    btn.title = S.busy ? "计划结束后可重看引导" : "重新看一遍六步引导";
  });
}

function paintUpdate() {
  const box = $("#update-status");
  const button = $("#btn-update-check");
  const state = S.update.state;
  box.dataset.state = state;
  button.disabled = state === "checking";
  button.textContent = state === "checking" ? "检查中…" : state === "error" ? "重试检查" : "检查更新";
  const copy = { idle: "尚未检查", checking: "正在检查…", current: "当前为最新正式版",
    available: "发现新版本 " + (S.update.result && S.update.result.latest_version || ""),
    preview: "当前为预览版本，最新正式版 " + (S.update.result && S.update.result.latest_version || ""),
    unavailable: "暂无正式发布" };
  box.textContent = state === "error" ? "暂时无法检查更新。" + (S.update.result && S.update.result.text || "请重试或前往下载页。") : copy[state] || copy.idle;
  /* 手动检查的结论带发生时间，避免把很久以前的结果误当成实时状态。 */
  const checked = $("#update-checked-at");
  const at = S.update.result && Date.parse(S.update.result.checked_at);
  checked.hidden = state === "checking" || !Number.isFinite(at);
  checked.textContent = checked.hidden ? "" : "检查时间 " + new Date(at).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function watchUpdate(requestId) {
  clearTimeout(S.update.timer);
  /* 后端请求有8秒网络超时；桥接丢事件也不能让按钮永久停在检查中。 */
  S.update.timer = setTimeout(() => {
    if (S.update.state === "checking" && S.update.requestId === requestId) {
      receiveUpdate({ request_id: requestId, state: "error", text: "没有收到检查结果，请重试。" });
    }
  }, 15000);
}

function receiveUpdate(payload) {
  const data = payload || {};
  if (!data.request_id || data.request_id !== S.update.requestId) return;
  clearTimeout(S.update.timer);
  S.update.result = data;
  S.update.state = ["available", "current", "preview", "unavailable", "error"].indexOf(data.state) >= 0 ? data.state : "error";
  paintUpdate();
  revealHelpStatus($("#update-status").parentElement);
}

function restoreUpdate(snapshot) {
  if (!snapshot || !snapshot.request_id) { paintUpdate(); return; }
  S.update.requestId = String(snapshot.request_id);
  if (snapshot.checking) {
    S.update.state = "checking";
    watchUpdate(S.update.requestId);
  } else if (snapshot.result) receiveUpdate(snapshot.result);
  paintUpdate();
}

function checkUpdate() {
  if (S.update.state === "checking") return;
  const requestId = "update-" + Date.now() + "-" + Math.random().toString(36).slice(2, 8);
  S.update.requestId = requestId;
  S.update.state = "checking";
  watchUpdate(requestId);
  paintUpdate();
  api("check_update", requestId).then((res) => {
    /* 结果可以先于ack到达：完成后不再被ack改回检查中。 */
    if (S.update.requestId !== requestId || S.update.state !== "checking") return;
    if (!res || res.ok === false || res.queued === false) {
      receiveUpdate({ request_id: requestId, state: "error", text: res && res.text || "检查没能启动，请重试。" });
    } else if (res.skipped && res.request_id) {
      S.update.requestId = String(res.request_id);
      watchUpdate(S.update.requestId);
      /* 重载时可能接上已完成的在途请求；读取本地快照，不额外发网络检查。 */
      const attachedId = S.update.requestId;
      api("bootstrap").then((info) => {
        /* 快照晚到时用户可能已开新查询；只有仍在接管同一请求才允许恢复。 */
        if (S.update.requestId === attachedId) restoreUpdate(info && info.update_check);
      }).catch(() => {});
    }
  }).catch((err) => {
    if (S.update.requestId === requestId && S.update.state === "checking") {
      receiveUpdate({ request_id: requestId, state: "error", text: msg(err) });
    }
  });
}

/* 偏好串行保存：重试旧请求时，合并后的新值必须优先，不能把较新设置写回旧值。 */
function flushPrefs() {
  if (S.saveBusy || !hasBridge() || !Object.keys(S.savePending).length) return Promise.resolve();
  const pending = S.savePending;
  S.savePending = {};
  S.saveBusy = true;
  return api("save_prefs", pending).then((res) => {
    if (!res || res.ok !== true) throw new Error((res && res.text) || "保存没有确认成功");
    S.saveFailures = 0;
    S.saveWarned = false;
  }).catch(() => {
    S.savePending = Object.assign({}, pending, S.savePending);
    S.saveFailures = (S.saveFailures || 0) + 1;
    if (S.saveFailures >= 2 && !S.saveWarned) {
      S.saveWarned = true;
      addLog("[!] 设置暂未保存，当前窗口仍使用这些设置。稍后修改设置可重试保存。");
    }
  }).finally(() => {
    S.saveBusy = false;
    if (!Object.keys(S.savePending).length) return;
    if (!S.saveFailures) flushPrefs();
    else if (S.saveFailures < 2) S.saveTimer = setTimeout(flushPrefs, 500);
  });
}

function savePrefs(patch) {
  Object.assign(S.prefs, patch || {});
  Object.assign(S.savePending, patch || {});
  S.saveFailures = 0;
  clearTimeout(S.saveTimer);
  S.saveTimer = setTimeout(flushPrefs, 250);
}

function savePrefsNow(patch) {
  /* 引导完成/跳过这类不能因关窗丢失的状态：立即落盘，不等 250ms 防抖 */
  Object.assign(S.prefs, patch || {});
  Object.assign(S.savePending, patch || {});
  S.saveFailures = 0;
  clearTimeout(S.saveTimer);
  return flushPrefs();
}

function findItem(id) { return S.byId.get(String(id)) || null; }

function normalize(raw) {
  const item = Object.assign({}, raw || {});
  item.id = String(item.id == null ? "" : item.id);
  item.name = String(item.name || "活动");
  item.signed = !!item.signed || String(item.state || "") === "已报名" || String(item.state || "") === "signed";
  item.checkin_required = triState(item.checkin_required);
  item.checkout_required = triState(item.checkout_required);
  /* 详情补充的真实时间段：只认合法时间文本，缺失就是空（不要伪造） */
  item.checkin_start = String(item.checkin_start || "");
  item.checkin_end = String(item.checkin_end || "");
  item.checkout_start = String(item.checkout_start || "");
  item.checkout_end = String(item.checkout_end || "");
  const state = String(item.attendance_state || "");
  item.attendance_state = ["ready", "pending", "unknown"].indexOf(state) >= 0 ? state
    : (item.checkin_required !== null || item.checkout_required !== null ? "ready" : "unknown");
  item.attendance_source = String(item.attendance_source || "none");
  return item;
}

function triState(value) {
  if (value === true || value === false) return value;
  if (typeof value === "number" && (value === 0 || value === 1)) return value === 1;
  const text = String(value == null ? "" : value).trim().toLowerCase();
  if (["1", "true", "yes", "y", "是", "需要", "有"].indexOf(text) >= 0) return true;
  if (["0", "false", "no", "n", "否", "无需", "无"].indexOf(text) >= 0) return false;
  return null;
}

function matchesFlag(value, filter) {
  /* 需要和无需都排除未知（null）；全部保留未知 */
  if (filter === "all") return true;
  if (value === null) return false;
  return filter === "yes" ? value === true : value === false;
}

function requirementFlags(item) {
  /* 真实字段来自详情接口：签到/签退要求 + 时间段。
   * 未知（null）分两种：详情还没回来 →「同步中」；详情已确认没有数据 →「待确认」。
   * 只有明确 false 才写「无需」，绝不把未知说成无需。 */
  const pending = item.attendance_state === "pending";
  const timeRange = (start, end) => {
    const startTs = toTs(start);
    if (!startTs) return "";
    const endTs = toTs(end);
    return " " + hm(startTs * 1000) + (endTs && endTs > startTs ? "–" + hm(endTs * 1000) : "");
  };
  const flags = [];
  const add = (text, unknown) => flags.push('<span class="activity-flag' + (unknown ? " is-unknown" : "") + '">' + esc(text) + "</span>");
  if (item.checkin_required === false) add("无需签到", false);
  else if (item.checkin_required === true) add("签到" + timeRange(item.checkin_start, item.checkin_end), false);
  else if (pending) add("签到信息同步中", true);
  else add("签到时间待确认", true);
  if (item.checkout_required === false) add("无需签退", false);
  else if (item.checkout_required === true) add("签退" + timeRange(item.checkout_start, item.checkout_end), false);
  else if (pending) add("签退信息同步中", true);
  else add("签退时间待确认", true);
  return flags.length ? '<span class="activity-flags">' + flags.join("") + "</span>" : "";
}

function selectedItems() {
  return Array.from(S.selected).map((id) => findItem(id)).filter(Boolean);
}

/* 筛选只影响可见行：全局下一场、冲突检查、计划都用 S.items，不用这里的输出 */
function filtered() {
  const keyword = S.filter.q.trim().toLowerCase();
  const now = bnowS();   // 未校准时为 null：today/week 筛选不生效（不误筛空）
  const rows = S.items.filter((item) => {
    if (keyword && (String(item.name) + " " + String(item.place || "")).toLowerCase().indexOf(keyword) < 0) return false;
    /* 可报名 = 未报名且未截止；已截止的只剩在「全部」里可见 */
    if (S.filter.status === "free" && (item.signed || stateOf(item) === "closed")) return false;
    if (S.filter.status === "signed" && !item.signed) return false;
    if (S.filter.time !== "all") {
      const stamp = toTs(item.sign_start);
      if (!stamp) return false;
      if (now != null && S.filter.time === "today" && startOfDay(stamp * 1000) !== startOfDay(now * 1000)) return false;
      if (now != null && S.filter.time === "week" && (stamp < now - 7 * 86400 || stamp > now + 7 * 86400)) return false;
    }
    if (!matchesFlag(item.checkin_required, S.filter.checkin) || !matchesFlag(item.checkout_required, S.filter.checkout)) return false;
    return true;
  });
  rows.sort((a, b) => {
    if (S.filter.sort === "name") return String(a.name).localeCompare(String(b.name), "zh");
    const ta = toTs(a.sign_start);
    const tb = toTs(b.sign_start);
    if (ta === null && tb !== null) return 1;
    if (ta !== null && tb === null) return -1;
    if (ta !== tb) return (ta || 0) - (tb || 0);
    return String(a.name).localeCompare(String(b.name), "zh");
  });
  return rows;
}

/* 可加入执行计划的统一判据：未报名且未截止（已截止只能查看）。
 * 列表计数、芯片计数、大屏指标、下一场与复选框门禁共用这一份。 */
function isEligibleForPlan(item) {
  return !!item && !item.signed && modeOf(item) !== "closed";
}

/* 全局下一场：按全部活动算，不受筛选影响。
 * 返回 openId（已开放未截止）与 futureId（最近的未来场次）：
 * 存在开放活动时大屏与快捷选择优先 openId，只有没有开放活动才轮到
 * 未来场次；已截止活动不参与下一场。 */
function nextOpen() {
  const now = bnowS();
  if (now == null) return { best: null, openNow: false, bestId: null,
                           openId: null, futureId: null };  // 未校准：完整空目标，不让 undefined 冒充有效值
  let best = null;
  let openNow = false;
  let openId = null;
  let openLeft = Infinity;
  let futureId = null;
  let futureLeft = Infinity;
  S.items.forEach((item) => {
    if (!isEligibleForPlan(item)) return;
    const left = leftOf(item);
    if (left === null) return;
    if (left <= 0) {
      openNow = true;
      /* 多个开放场次取最早开放的（left 最小）：最紧急的一场 */
      if (left < openLeft) { openLeft = left; openId = item.id; }
    } else if (left < futureLeft) { futureLeft = left; futureId = item.id; }
  });
  if (futureId !== null) best = futureLeft;
  /* 统一目标：只要当前有已开放场次，倒计时就属于它——已开放没有倒计时，
   * best 必须是 null，不能把另一场未来活动的倒计时段配给这个标题（R12）。
   * 只有没有任何已开放场次时，才轮到最近未来场。 */
  if (openNow) best = null;
  const targetId = openNow ? openId : futureId;
  return { best: best, openNow: openNow, bestId: targetId,
           openId: openId, futureId: futureId };
}

function groupSelected(items) {
  const groups = new Map();
  items.slice().sort((a, b) => (toTs(a.sign_start) || Infinity) - (toTs(b.sign_start) || Infinity)).forEach((item) => {
    const key = item.sign_start || "";
    if (!groups.has(key)) groups.set(key, { key: key, items: [] });
    groups.get(key).items.push(item);
  });
  return Array.from(groups.values());
}

function validateSelection() {
  const errors = [];
  const conflicts = [];
  const items = selectedItems();
  /* 已报名活动是外部占用：只按举办时段 act_start/act_end 比较，
   * 报名开放时间不参与（同一时刻开放的活动可以并行）。 */
  const signedItems = S.items.filter((item) => item.signed);
  items.forEach((item) => {
    if (item.signed) errors.push({ title: "此活动已报名", text: "「" + item.name + "」已报名，请从本次计划中移除。" });
    /* 报名截止是可执行门禁：已截止活动即使被勾上（时序差）也开始不了 */
    if (modeOf(item) === "closed") errors.push({ title: "报名已截止", text: "「" + item.name + "」的报名已截止（" + (item.sign_end || "时间未公布") + "），不能加入计划。" });
    if (!toTs(item.sign_start)) errors.push({ title: "报名开放时间待确认", text: "「" + item.name + "」没有可用的报名开始时间。" });
    const start = toTs(item.act_start);
    const end = toTs(item.act_end);
    if (!start || !end) errors.push({ title: "活动时间待确认", text: "「" + item.name + "」缺少完整的活动起止时间，不能静默加入可执行计划。" });
    else if (end <= start) errors.push({ title: "活动时间无效", text: "「" + item.name + "」的结束时间不晚于开始时间。" });
    if (!start || !end || end <= start || item.signed) return;
    const clash = signedItems.find((other) => {
      if (String(other.id) === String(item.id)) return false;
      const otherStart = toTs(other.act_start);
      const otherEnd = toTs(other.act_end);
      return otherStart && otherEnd && otherEnd > otherStart && start < otherEnd && otherStart < end;
    });
    if (clash) {
      errors.push({ title: "与已报名活动重叠",
                    text: "「" + item.name + "」与已报名的「" + clash.name + "」举办时间重叠（"
                      + activityTimeText(clash) + "），同一时段只能去一场。" });
    }
  });
  /* 已报名活动缺少举办时段时无法判定冲突：保守阻止，不静默放行 */
  if (items.length) {
    const unknown = signedItems.filter((item) => !toTs(item.act_start) || !toTs(item.act_end));
    if (unknown.length) {
      const names = unknown.slice(0, 3).map((item) => "「" + item.name + "」").join("、");
      errors.push({ title: "已报名活动时间待确认",
                    text: names + (unknown.length > 3 ? " 等 " + unknown.length + " 场" : "")
                      + "缺少举办时间，无法确认是否与本次选择冲突。" });
    }
  }
  for (let i = 0; i < items.length; i += 1) {
    const leftStart = toTs(items[i].act_start);
    const leftEnd = toTs(items[i].act_end);
    if (!leftStart || !leftEnd || leftEnd <= leftStart) continue;
    for (let j = i + 1; j < items.length; j += 1) {
      const rightStart = toTs(items[j].act_start);
      const rightEnd = toTs(items[j].act_end);
      if (rightStart && rightEnd && rightEnd > rightStart && leftStart < rightEnd && rightStart < leftEnd) conflicts.push({ left: items[i], right: items[j] });
    }
  }
  return { errors: uniqueErrors(errors), conflicts: conflicts };
}

function conflictIds() {
  const ids = new Set();
  validateSelection().conflicts.forEach((conflict) => { ids.add(String(conflict.left.id)); ids.add(String(conflict.right.id)); });
  return ids;
}

/* 计划里正在提交的那一场：行内黑竖条 + 提交中标签 */
function armedTaskByActivity(id) {
  if (!S.plan.tasks.size) return null;
  let found = null;
  S.plan.tasks.forEach((task) => {
    if (String(task.activity_id) === String(id) && String(task.status) === "submitting") found = task;
  });
  return found;
}

function uniqueErrors(errors) {
  const seen = new Set();
  return errors.filter((error) => {
    const key = error.title + "\n" + error.text;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function toggleSelection(id, checked) {
  if (S.busy) return;
  const item = findItem(id);
  /* 已报名和已截止都不能进选择集合（含整行点击路径，复选框已禁用） */
  if (!item || !isEligibleForPlan(item)) return;
  if (checked) {
    const existing = selectedItems();
    const candidateStart = toTs(item.act_start);
    const candidateEnd = toTs(item.act_end);
    /* 已报名活动先挡：它是学校确认的外部占用，不是本次选择内部冲突 */
    const signedClash = S.items.find((other) => {
      if (!other.signed || String(other.id) === String(id)) return false;
      const start = toTs(other.act_start);
      const end = toTs(other.act_end);
      return candidateStart && candidateEnd && start && end
        && candidateStart < end && start < candidateEnd;
    });
    if (signedClash) {
      S.selectionNotice = "与已报名活动「" + signedClash.name + "」的举办时间重叠（"
        + activityTimeText(signedClash) + "），不能再加入计划。";
      render();
      return;
    }
    const conflict = existing.find((other) => {
      const start = toTs(other.act_start);
      const end = toTs(other.act_end);
      return candidateStart && candidateEnd && start && end && candidateStart < end && start < candidateEnd;
    });
    if (conflict) {
      S.selectionNotice = "与「" + conflict.name + "」的活动时间重叠，同一计划只能选择其中一场。已保留原选择。";
      render();
      return;
    }
    S.selected.add(String(id));
    S.selectionNotice = "";
  } else {
    S.selected.delete(String(id));
    S.selectionNotice = "";
  }
  setResult(null);
  render();
}

function clearSelection() {
  if (S.busy) return;
  S.selected.clear();
  S.selectionNotice = "";
  S.plan = { id: null, status: "idle", tasks: new Map() };
  setResult(null);
  render();
}

function paintChips() {
  /* 状态胶囊带计数（原型样式）：可报名/已报名/全部按全量数据算；
   * 可报名排除已截止（只能查看），已报名单独计，剩下是已截止 */
  const free = S.items.filter(isEligibleForPlan).length;
  const signed = S.items.filter((item) => item.signed).length;
  /* 没有可信现在时，可报名/已截止的划分无法确定：计数显示待确认而不是伪 0 */
  const counts = { free: bnowS() !== null || !S.items.length ? free : "待确认",
                   signed, all: S.items.length };
  document.querySelectorAll(".chip").forEach((chip) => {
    const on = chip.dataset.status === S.filter.status;
    chip.classList.toggle("is-on", on);
    chip.setAttribute("aria-pressed", on ? "true" : "false");
    let count = chip.querySelector(".ct");
    if (!count) {
      count = document.createElement("span");
      count.className = "ct";
      chip.appendChild(count);
    }
    count.textContent = counts[chip.dataset.status] || 0;
  });
}

function setFilter(key, value) {
  S.filter[key] = value;
  savePrefs({ filter: Object.assign({}, S.filter) });
  render();
}

/* 后端验证失败：errors/conflicts 逐条翻译成「哪一场、什么问题」，
 * 不折成一句泛化文案；同 sign_start 不是冲突依据，只报举办时段。 */
const PLAN_ERROR_TEXT = {
  missing_id: "有活动缺少 id，刷新列表后再选。",
  duplicate_id: "同一活动被选了两遍，去掉重复的那一条。",
  already_signed: "此活动已报名，请从本次计划中移除。",
  invalid_sign_start: "报名开放时间缺失或格式不对。",
  activity_time_unknown: "缺少完整的活动举办起止时间。",
  activity_time_invalid: "活动结束时间不晚于开始时间。",
  activity_overlap: "活动举办时段重叠。",
  sign_end_passed: "报名已截止，不能加入计划。",
};

function showPlanError(text, conflicts, errors) {
  const messages = [];
  (errors || []).forEach((error) => {
    const kind = error && error.kind;
    if (kind === "activity_overlap") return; /* 冲突单列，避免重复 */
    const label = PLAN_ERROR_TEXT[kind] || "这一项不能加入计划。";
    const name = error && (error.name || error.activity_id);
    messages.push("<div class=\"conflict-item\"><strong>" + esc(label) + "</strong><br>" + (name ? "「" + esc(name) + "」" : "") + "</div>");
  });
  (conflicts || []).forEach((conflict) => {
    const left = conflict.left || {};
    const right = conflict.right || {};
    messages.push("<div class=\"conflict-item\"><strong>活动时间重叠</strong><br>「" + esc(left.name) + "」与「" + esc(right.name) +
      "」举办时段重叠（" + esc(shortTime(left.act_start)) + " – " + esc(shortTime(left.act_end)) + "），只能选其中一场。</div>");
  });
  if (messages.length) {
    S.selectionNotice = "计划未能开始，先处理上面的提示再试。";
    const box = $("#plan-conflicts");
    box.innerHTML = messages.join("");
    box.hidden = false;
    render();
  }
  setResult({ ok: false, title: "计划没能开始", text: (messages.length ? "上面列出了每一场的问题。" : "") + (text || "先处理提示，再试一次。") });
}

/* ---------------------------------------------------------------- 状态与文案 */
/* 双模式口径（与后端 signup_plan.classify_signup_mode 一致）：
 * sign_end 已过 → closed；sign_start 在未来 → pre_register；其余 → instant_signup。
 * 前端只做展示与即时门禁，后端 start_plan 仍是最终权威；
 * 活动举办时间不参与模式判断——它只用于冲突检查。 */
function modeOf(item) {
  if (!item) return "unknown";
  const now = bnowS();
  if (now == null) return "unknown";   // 未校准：模式未知（后端仍是最终权威）
  const end = toTs(item.sign_end);
  if (end !== null && end <= now) return "closed";
  const start = toTs(item.sign_start);
  if (start !== null && start > now) return "pre_register";
  return "instant_signup";
}

function modeText(mode) {
  return { pre_register: "预报名", instant_signup: "立即报名", closed: "已截止" }[mode] || "";
}

function stateOf(item) {
  if (!item) return "unknown";
  if (item.signed) return "signed";
  const state = String(item.state || "");
  if (state.indexOf("已报名") >= 0 || state === "signed") return "signed";
  if (state.indexOf("已截止") >= 0 || state === "closed") return "closed";
  const bn = bnowS();
  const end = toTs(item.sign_end);
  if (bn === null) {
    /* 未校准：只认学校给的静态「已报名/已截止/开放中」文本，
     * 不拿本机时间判开放——列表不得被错误 OS 日期误筛空 */
    if (state.indexOf("开放中") >= 0 || state === "open") return "open";
    return "unknown";
  }
  if (end !== null && end < bn) return "closed";
  /* 开放时间已知时随业务时钟换组，不能一直沿用上次拉列表时的「未开放」文本 */
  const open = toTs(item.sign_start);
  if (open !== null) return open > bn ? "waiting" : "open";
  if (state.indexOf("开放中") >= 0 || state === "open") return "open";
  if (state.indexOf("未开放") >= 0 || state === "waiting") return "waiting";
  const left = leftOf(item);
  if (left === null) return "unknown";
  return left > 0 ? "waiting" : "open";
}

function taskLabel(state) {
  return { queued: "排队", waiting: "等待开放", submitting: "提交中", success: "报名成功", already_signed: "已报名", failed: "报名未成功", canceled: "已取消" }[state] || "处理中";
}

function taskClass(state, task) {
  if (state === "success" || state === "already_signed") return "is-ok";
  if (state === "failed") {
    /* 未知结论用中性色：不能给用户一个「确定失败」的红标 */
    const stop = String(task && task.stop_reason || "");
    if (stop === "unconfirmed" || stop === "auth_unavailable") return "is-warn";
    return "is-bad";
  }
  if (state === "canceled") return "is-warn";
  if (state === "submitting" || state === "waiting" || state === "queued") return "is-busy";
  return "";
}

function leftOf(item) {
  if (!item) return null;
  const stamp = toTs(item.sign_start);
  const left = bnLeftSec(stamp);
  if (left !== null) return Math.floor(left);
  return typeof item.seconds_to_open === "number" ? item.seconds_to_open : null;
}

/* 展示模型：区分「学校没公布开放时间」与「本机暂时没有可信现在」。
 * 只有前者才说「待公布」；后者保留已知绝对时间并标「待校准」。 */
function signStartKnown(item) {
  return toTs(item && item.sign_start) !== null;
}

function leftTextOf(item) {
  const left = leftOf(item);
  if (left === null) return signStartKnown(item) ? "待校准" : "待公布";
  return left <= 0 ? "已开放" : durText(left);
}

function validActivityTime(item) {
  const start = toTs(item && item.act_start);
  const end = toTs(item && item.act_end);
  return start !== null && end !== null && end > start;
}

function activityTimeText(item) {
  if (!validActivityTime(item)) return "活动时间待确认";
  return "活动 " + whenText(toTs(item.act_start) * 1000) + " – " + hm(toTs(item.act_end) * 1000);
}

/* ---------------------------------------------------------------- 每秒 */
let lastBeat = _monoMs();

function tick() {
  /* 心跳间隔用同源单调读数：系统墙钟跳变不得伪造/吞掉睡眠提示 */
  const monoMs = _monoMs();
  const gap = monoMs - lastBeat;
  lastBeat = monoMs;
  if (gap > SLEEP_GAP_MS && !S.sleepGap) {
    S.sleepGap = gap;
    paintBanners();
  }
  /* 顶栏时间只认业务北京时间：未知显示待校准，绝不拿系统时间顶上 */
  paintClockStatus();
  const popTime = $("#clock-pop-time");
  if (popTime && clockPopOpenNow()) {
    const bn = businessNowMs();
    popTime.textContent = (bn === null ? "--:--:--" : clockParts(bn).time);
  }
  /* 登录剩余按绝对锚点走字：胶囊「约剩」与打开中的时钟浮层每秒重算显示 */
  paintLogin();
  paintClockPopLeft();
  /* 某一行跨过边界（开放了、截止了）才整表重画；平时只改倒计时文字 */
  let dirty = false;
  S.rowStates.forEach((state, id) => {
    const item = findItem(id);
    if (item && stateOf(item) !== state) dirty = true;
  });
  if (dirty) {
    paintList();
  } else {
    S.rowEls.forEach((els, id) => {
      const item = findItem(id);
      if (!item) return;
      const left = leftOf(item);
      if (els.left) {
        const text = leftTextOf(item);
        if (els.left.textContent !== text) els.left.textContent = text;
        els.left.classList.toggle("is-soon", left !== null && left > 0 && left <= 600);
      }
      if (els.ago && left !== null && left <= 0 && !item.signed) els.ago.textContent = "已开放 " + durText(-left);
    });
  }
  const nowChip = document.querySelector("#rows [data-now]");
  if (nowChip) {
    /* 「现在」线只认业务北京时间：Date.now 是本机墙钟，绝不顶上（决策 012） */
    const bnForChip = businessNowMs();
    nowChip.textContent = bnForChip === null ? "待校准" : hm(bnForChip);
  }
  /* 阶段在秒与秒之间会变（开放那一刻、第一次真实提交）：变了才重画摘要，
   * 让标题与批次说法跟 LED/任务卡同步，不必每秒重建整块标题区。 */
  const runPhase = S.busy ? executionView().phase : null;
  if (runPhase !== S.lastRunPhase) {
    S.lastRunPhase = runPhase;
    paintSelected();
  }
  paintRail();
  paintCountdown();
  paintLed();
  paintRoute();
  /* 未终态任务行：每秒按同一份阶段解释走字。
   * 开放那一刻 preopen→preparing 时，标签与说明必须跟着改，
   * 不能初次渲染正确、下一秒又被原始 submitting 文案盖回去。 */
  if (S.plan.tasks.size) {
    const now = bnowS();   // 未校准传 null：任务卡显示等待/待确认，绝不猜已开放
    S.plan.tasks.forEach((task) => {
      if (RUNNING_STATUSES.indexOf(String(task.status)) < 0) return;
      const row = document.querySelector('.task-row[data-task-id="' + cssEscape(String(task.task_id)) + '"]');
      if (!row) return;
      const detail = row.querySelector(".task-detail");
      if (detail) detail.textContent = taskModePrefix(task) + taskDisplayDetail(task, now);
      const label = row.querySelector(".task-state");
      if (label) label.textContent = taskDisplayLabel(task);
    });
  }
}

function doCheck() {
  api("check").catch((err) => addLog("[X] 检查登录状态失败：" + msg(err)));
}

function stripLabel(label) { return String(label || "").replace(/^等待并抢报名[：:]\s*/, "").trim(); }

function cleanReason(reason) {
  let value = String(reason == null ? "" : reason).trim();
  for (let i = 0; i < 3; i += 1) {
    const next = value.replace(/^\[X\]\s*/, "").replace(/^没抢到[：:]\s*/, "").trim();
    if (next === value) break;
    value = next;
  }
  return value || "原因不明";
}

function grabHint(reason) {
  if (/名额|已满|满了|抢完/.test(String(reason))) return "名额被抢完了。等下一场，或换一个活动再试。";
  if (/还没|未开放|没放开|页面/.test(String(reason))) return "页面还没放开。点「刷新」确认开放时间，再试一次。";
  if (/登录|登陆|session|401|403|token/i.test(String(reason))) return "登录状态已失效。先点「登录」重新登录，再点「刷新」。";
  if (/网络|连接|超时|timeout|连不上|dns|ssl/i.test(String(reason))) return "连不上学校站点。检查校园网或代理，恢复后点「刷新」。";
  return "先点「刷新」看看列表，再重新选一次活动。";
}

function failHint(text) {
  const raw = String(text || "");
  if (S.login.present === false || S.login.result === "none") return "完成登录后点「刷新」加载活动。";
  if (S.login.result === "expired") return "点「重新登录」后刷新。";
  if (S.login.result === "unknown") return "恢复网络后点「刷新」重试。";
  if (needLogin(raw)) return "点「重新登录」后刷新。";
  if (/网络|连接|超时|timeout|连不上|dns|ssl|代理/i.test(raw)) return "连不上学校站点。检查校园网或代理，恢复后点「重试」。";
  return "程序组件没能正常启动。先点「刷新」；还不行就看下面的运行记录。";
}

function listErrorCopy(raw) {
  /* 组件问题优先：缺组件时列表失败不许被翻译成「先登录/重新登录」 */
  if (S.runtime.error) {
    const copy = runtimeErrorCopy();
    return { title: copy.title, text: copy.hint };
  }
  const text = failHint(raw);
  if (S.login.present === false || S.login.result === "none") return { title: "还没登录", text };
  if (S.login.result === "expired") return { title: "登录已过期", text };
  if (S.login.result === "unknown") return { title: "暂时无法加载", text };
  return { title: "活动暂时没加载", text };
}

function needLogin(text) { return /登录状态|登录状态|未登录|请先登录|session|401|403|token/i.test(String(text || "")); }
function leftText(hours) { return hours >= 1 ? Math.round(hours) + " 小时" : Math.max(1, Math.round(hours * 60)) + " 分钟"; }
function numOrNull(value) { return typeof value === "number" && isFinite(value) ? value : null; }
function msg(error) { return String((error && error.message) || error || "未知错误"); }

/* ---------------------------------------------------------------- 时间工具 */
/* 学校时间恒为 UTC+8：不跟随本机时区，也不因夏令时变化。
 * 界面上的「学校时刻」一律用这套格式化；顶栏那颗时钟仍显示本机时间，
 * 因为它是给人看「这台电脑现在几点」的。 */
const SCHOOL_TZ = "Asia/Shanghai";
const SCHOOL_OFFSET_S = 8 * 3600;
const SCHOOL_FMT = new Intl.DateTimeFormat("zh-CN", {
  timeZone: SCHOOL_TZ, year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
});

/* 学校时区的年月日时分秒（整数字段，避免 toLocaleString 的本地化文本） */
function schoolParts(ms) {
  const out = {};
  SCHOOL_FMT.formatToParts(new Date(ms)).forEach((part) => {
    if (part.type !== "literal") out[part.type] = part.value;
  });
  return { y: Number(out.year), mo: Number(out.month), d: Number(out.day),
           h: Number(out.hour), mi: Number(out.minute), s: Number(out.second) };
}

/* 学校时间文本 → epoch 秒；非法返回 null，绝不滚成另一个日期（R14）。
 * new Date(y, m, d, ...) 会把 2026-02-30 悄悄变成 3 月 2 日——那等于替用户
 * 编了一个不存在的报名时间。这里用 Date.UTC 算，再逐字段回验。 */
function toTs(value) {
  if (!value) return null;
  const match = String(value).match(/^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})$/);
  if (!match) return null;
  const [y, mo, d, h, mi, s] = match.slice(1).map(Number);
  if (mo < 1 || mo > 12 || d < 1 || d > 31 || h > 23 || mi > 59 || s > 59) return null;
  const utc = Date.UTC(y, mo - 1, d, h, mi, s);
  const check = new Date(utc);
  if (check.getUTCFullYear() !== y || check.getUTCMonth() !== mo - 1 ||
      check.getUTCDate() !== d || check.getUTCHours() !== h ||
      check.getUTCMinutes() !== mi || check.getUTCSeconds() !== s) return null;
  return utc / 1000 - SCHOOL_OFFSET_S;
}

function fmt(seconds) {
  let value = Math.max(0, Math.floor(seconds));
  const days = Math.floor(value / 86400);
  const hours = Math.floor((value % 86400) / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const secs = value % 60;
  const pad = (number) => String(number).padStart(2, "0");
  if (days > 0) return days + "天 " + pad(hours) + ":" + pad(minutes);
  return pad(hours) + ":" + pad(minutes) + ":" + pad(secs);
}

/* 行内距开放：7 分 16 秒 / 2 时 53 分 / 1 天 3 时 */
function durText(seconds) {
  const value = Math.max(0, Math.floor(seconds));
  const days = Math.floor(value / 86400);
  const hours = Math.floor((value % 86400) / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const secs = value % 60;
  if (days) return days + " 天 " + hours + " 时";
  if (hours) return hours + " 时 " + String(minutes).padStart(2, "0") + " 分";
  if (minutes) return minutes + " 分 " + String(secs).padStart(2, "0") + " 秒";
  return secs + " 秒";
}

function shortTime(value) {
  const match = String(value || "").match(/^\d{4}-(\d{2})-(\d{2}) (\d{2}):(\d{2})/);
  return match ? match[1] + "-" + match[2] + " " + match[3] + ":" + match[4] : "";
}

function hm(ms) {
  const p = schoolParts(ms);
  const pad = (n) => String(n).padStart(2, "0");
  return pad(p.h) + ":" + pad(p.mi);
}

/* 学校口径的「当天零点」：只用来算跨天差，不做本地时区换算 */
function startOfDay(ms) {
  const p = schoolParts(ms);
  return Date.UTC(p.y, p.mo - 1, p.d);
}

function relDayText(ms) {
  const bnMs = businessNowMs();
  const diff = bnMs == null ? NaN : Math.round((startOfDay(ms) - startOfDay(bnMs)) / 86400000);
  if (diff === 0) return "今天";
  if (diff === 1) return "明天";
  if (diff === 2) return "后天";
  if (diff === -1) return "昨天";
  const p = schoolParts(ms);
  const weekday = WEEK[new Date(Date.UTC(p.y, p.mo - 1, p.d)).getUTCDay()];
  return p.mo + "月" + p.d + "日 " + weekday;
}

function dayLabel(ms) {
  return relDayText(ms);
}

/* 完整时刻：今天只写时分，别的天带日期星期（一律学校时区） */
function whenText(ms) {
  const bnMs = businessNowMs();
  const diff = bnMs == null ? NaN : Math.round((startOfDay(ms) - startOfDay(bnMs)) / 86400000);
  const p = schoolParts(ms);
  const pad = (n) => String(n).padStart(2, "0");
  const time = pad(p.h) + ":" + pad(p.mi) + (p.s ? ":" + pad(p.s) : "");
  if (diff === 0) return "今天 " + time;
  if (diff === 1) return "明天 " + time;
  if (diff === 2) return "后天 " + time;
  return p.mo + "月" + p.d + "日 " + time;
}

function esc(value) {
  return String(value == null ? "" : value).replace(/[&<>\"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;" }[character]));
}

function cssEscape(value) {
  return String(value).replace(/(["\\])/g, "\\$1");
}

/* ---------------------------------------------------------------- 六步可重播引导 */
/* 引导「可见」= 遮罩在场且没被计划运行暂停：只有可见时才约束键盘焦点 */
function tourVisible() {
  const box = $("#tour");
  return !!(S.tour.open && !S.tour.paused && box && !box.hidden);
}

function tourShouldAutoStart() {
  /* 只按内容版本判定：看过本版（tour_version >= TOUR_VERSION）就不再自动弹。
   * 完成/跳过时间戳只描述「当时那一版」，不得挡住更新的内容版本；
   * 软件版本（VERSION/1.1.0 等）变化与 TOUR_VERSION 无关。 */
  const seen = Number(S.prefs.tour_version);
  if (Number.isFinite(seen) && seen >= TOUR_VERSION) return false;
  return !S.busy;
}

function startTour(at) {
  if (S.busy) return;                 /* 计划运行中优先，引导不启动 */
  /* 记住引导开始前焦点在哪，关闭/跳过/完成后还回去（元素还在页面上才还） */
  const prev = document.activeElement;
  S.tour.restoreFocus = (prev && prev !== document.body) ? prev : null;
  S.tour.open = true;
  S.tour.paused = false;
  S.tour.step = Math.max(0, Math.min(Number(at) || 0, TOUR_STEPS.length - 1));
  $("#tour").hidden = false;
  paintTour();
  const nextBtn = $("#tour-next");
  if (nextBtn) nextBtn.focus();       /* 键盘焦点留在引导卡片里 */
}

/* 引导可见时的键盘约束：Tab/Shift+Tab 在卡片按钮间循环。
 * 返回 true = 已处理（调用方直接 return），按钮清单与 aria-modal 声明一致。 */
function tourTrapTab(event) {
  event.preventDefault();
  const buttons = ["#tour-skip", "#tour-prev", "#tour-next"]
    .map((sel) => $(sel)).filter((el) => el && !el.disabled);
  if (!buttons.length) return true;
  const idx = buttons.indexOf(document.activeElement);
  if (idx < 0) {
    buttons[event.shiftKey ? buttons.length - 1 : 0].focus();
    return true;
  }
  const dir = event.shiftKey ? -1 : 1;
  buttons[(idx + dir + buttons.length) % buttons.length].focus();
  return true;
}

function tourClose(finished) {
  S.tour.open = false;
  S.tour.paused = false;
  $("#tour").hidden = true;
  const back = S.tour.restoreFocus || null;
  S.tour.restoreFocus = null;
  if (back && document.body && document.body.contains(back)) {
    try { back.focus(); } catch (err) { /* 元素已不可聚焦就算了 */ }
  }
  if (finished) savePrefsNow({ tour_version: TOUR_VERSION, tour_completed_at: new Date().toISOString() });
}

function tourSkip() {
  /* 跳过=本版不再自动弹；帮助中的重看引导随时可回顾。 */
  savePrefsNow({ tour_version: TOUR_VERSION, tour_skipped_at: new Date().toISOString() });
  tourClose(false);
}

function tourPauseNow() {
  S.tour.paused = true;
  $("#tour").hidden = true;
}

function tourResumeNow() {
  if (!S.tour.open) return;
  S.tour.paused = false;
  $("#tour").hidden = false;
  paintTour();
}

function paintTour() {
  if (!S.tour.open || S.tour.paused) return;
  const box = $("#tour");
  const step = TOUR_STEPS[S.tour.step];
  const total = TOUR_STEPS.length;
  $("#tour-step").textContent = "第 " + (S.tour.step + 1) + "/" + total + " 步";
  $("#tour-title").textContent = step.title;
  $("#tour-text").textContent = step.text;
  $("#tour-prev").disabled = S.tour.step === 0;
  $("#tour-next").textContent = S.tour.step === total - 1 ? "完成" : "下一步";
  $("#tour-dots").innerHTML = TOUR_STEPS.map((s, i) =>
    '<i class="' + (i === S.tour.step ? "on" : "") + '"></i>').join("");
  /* 目标缺失/隐藏/窄窗（≤980）时不做高亮，退回底部静态说明条——
   * 不伪造定位，也不挡运行中的操作 */
  const target = document.querySelector('[data-tour-target="' + step.target + '"]');
  const usable = !!target && window.innerWidth > 980 &&
                 target.offsetWidth > 0 && target.getClientRects().length > 0;
  box.classList.toggle("is-bottom", !usable);
  const spot = $("#tour-spot");
  const card = $("#tour-card");
  card.style.top = "";
  card.style.left = "";
  if (!usable) {
    spot.style.top = spot.style.left = spot.style.width = spot.style.height = "";
    return;
  }
  const rect = target.getBoundingClientRect();
  const pad = 6;
  spot.style.top = Math.round(rect.top - pad) + "px";
  spot.style.left = Math.round(rect.left - pad) + "px";
  spot.style.width = Math.round(rect.width + pad * 2) + "px";
  spot.style.height = Math.round(rect.height + pad * 2) + "px";
  /* 卡片优先贴目标下方，放不下换上方，再放不下交回底部说明条 */
  const cw = card.offsetWidth || 340;
  const ch = card.offsetHeight || 200;
  const gap = 12;
  let top = rect.bottom + gap;
  if (top + ch > window.innerHeight - 12) top = Math.max(12, rect.top - ch - gap);
  if (top <= 12 && rect.top < ch + 12) {
    box.classList.add("is-bottom");
    return;
  }
  card.style.top = Math.round(top) + "px";
  card.style.left = Math.round(Math.min(Math.max(12, rect.left), window.innerWidth - cw - 12)) + "px";
}

/* ---------------------------------------------------------------- 事件绑定 */
window.onBackend = {
  log: (line) => addLog(line),
  check: (payload) => {
    const data = payload || {};
    /* 旧轮迟到：不能覆盖新一轮的检查结论（R11）。先有界暂存，
     * 等 skipped ack 接续回这个 owner 再重放一次。 */
    if (data.request_id && S.round.check && data.request_id !== S.round.check) {
      cacheRoundPayload("check", data.request_id, data);
      return;
    }
    if (data.request_id && !markRoundApplied("check", data.request_id)) return;
    if (data.request_id && data.request_id === S.round.check) {
      clearTimeout(S.round.checkTimer);
    }
    const hadRuntimeError = !!S.runtime.error;
    /* 真实校验回来了：解除「检查中」，记下四态结论（valid/none/expired/unknown） */
    S.login.checking = false;
    S.login.result = data.result || null;
    /* FB4：组件结论按刷新轮次处理——本轮 check 报组件故障立即挂横幅并计入
     * 故障累计（另一分支成功/普通 unknown 不得撤）；本轮 check 无故障只收口
     * 自己的分支，撤横幅必须等本轮两分支都完成（两到达顺序语义一致） */
    const code = data.runtime_error || null;
    if (code && RUNTIME_ERRORS[code]) {
      S.runtime.roundError = code;
      S.runtime.error = code;
      S.runtime.checkPending = false;
    } else if (S.runtime.checkPending) {
      S.runtime.checkPending = false;
      if (!S.runtime.listPending) S.runtime.error = S.runtime.roundError;
    }
    if (data.demo) S.demo = true;
    /* demo 的 session_present 是 null（没有真实会话）：不能 !! 成 false，
     * 否则会走「首次未登录」板面，把演示样例全遮住（C07） */
    setLogin(data.hours_left, !!data.ok, loginPresent(data, !!data.demo), data);
    if (data.result === "valid" && firstNotice("check", data.request_id, "valid")) {
      addLog("[OK] 已复用本机登录状态，学校接口校验通过");
    }
    /* 时钟只认结构化载荷：没有结论就落 unknown，绝不从文本猜「慢几秒」（R08） */
    applyClock(data.clock);
    if (!data.ok && firstNotice("check", data.request_id, "fail")) {
      const reason = String(data.reason || data.text || "登录状态检查没通过");
      addLog(reason.startsWith("[X]") ? reason : "[X] " + reason);
    }
    paintBanners();     /* 组件横幅随 check 结论出现/撤销 */
    updateControls();
    paintChecks();
    if (!!S.runtime.error !== hadRuntimeError) {
      /* 组件结论出现/解除：列表空态与大屏短状态跟着切换（解除即恢复原状态） */
      render();
    }
  },
  activities: (payload) => applyActivities(payload, false),
  done: (payload) => handleDone(payload),
  business_clock: (payload) => {
    /* 事件只作「重取通知」：先落地推送内容，再向本机取一次带往返测量的权威快照 */
    ingestBusinessClock(payload);
    pollClockNow();
  },
  plan: (payload) => handlePlan(payload),
  task: (payload) => handleTask(payload),
  plan_done: (payload) => handlePlanDone(payload),
  update: (payload) => receiveUpdate(payload),
};

bindPlaceToggle();

$("#q").addEventListener("input", (event) => setFilter("q", event.target.value));
$("#chips").addEventListener("click", (event) => {
  const chip = event.target.closest(".chip");
  if (chip) { setFilter("status", chip.dataset.status); paintChips(); }
});
$("#time").addEventListener("change", (event) => setFilter("time", event.target.value));
$("#sort").addEventListener("change", (event) => setFilter("sort", event.target.value));
$("#checkin").addEventListener("change", (event) => setFilter("checkin", event.target.value));
$("#checkout").addEventListener("change", (event) => setFilter("checkout", event.target.value));
$("#rows").addEventListener("change", (event) => {
  const check = event.target.closest(".row-check");
  if (check) toggleSelection(check.dataset.id, check.checked);
});
$("#rows").addEventListener("click", (event) => {
  if (event.target.closest(".row-check")) return;
  /* 地点是独立可聚焦控件：点它只展开/收起完整文本，不改变选择 */
  const place = event.target.closest(".c-place");
  if (place) { togglePlace(place); return; }
  const row = event.target.closest(".row");
  if (!row) return;
  const check = row.querySelector(".row-check");
  if (check && !check.disabled) { check.checked = !check.checked; toggleSelection(check.dataset.id, check.checked); }
});
/* 键盘展开地点：Enter / Space 只翻转 aria-expanded，不触发开始计划或勾选 */
$("#rows").addEventListener("keydown", (event) => {
  const place = event.target.closest(".c-place");
  if (!place) return;
  if (event.key !== "Enter" && event.key !== " " && event.key !== "Spacebar") return;
  event.preventDefault();
  togglePlace(place);
});
$("#btn-refresh").addEventListener("click", () => refresh(false));
$("#btn-login").addEventListener("click", doLogin);
$("#btn-help").addEventListener("click", showHelp);
$("#clock").addEventListener("click", (event) => { event.stopPropagation(); toggleClockPop(); });
document.addEventListener("mousedown", (event) => {
  if (!event.target.closest("#clock-pop") && !event.target.closest("#clock")) closeClockPop(false);
});
$("#btn-log-folder").addEventListener("click", () => api("open_data").catch((err) => addLog("[X] 打不开数据目录：" + msg(err))));
$("#btn-log-copy").addEventListener("click", copyLog);
$("#btn-plan").addEventListener("click", startPlan);
$("#btn-stop").addEventListener("click", stopPlan);
$("#btn-board-login").addEventListener("click", doLogin);
$("#btn-board-pick").addEventListener("click", () => {
  const id = nextOpen().bestId;
  if (id) toggleSelection(id, true);
});
/* 大屏「时间待校准」的唯一恢复入口：打开现有时间详情，不新增第二个校准入口 */
$("#board-text").addEventListener("click", (event) => {
  const target = event.target;
  if (target && target.closest && target.closest("[data-open-clock]")) toggleClockPop();
});
const stopDialog = $("#dlg-stop");
if (stopDialog) stopDialog.addEventListener("close", () => {
  /* 只有「停止计划」按钮回 yes 才真正停止；Esc/继续等待都是 no */
  if (stopDialog.returnValue === "yes") doStopPlan();
  stopDialog.returnValue = "";
});
$("#btn-clear-plan").addEventListener("click", clearSelection);
$("#plan-tasks").addEventListener("click", (event) => {
  const button = event.target.closest(".task-cancel");
  if (button) cancelTask(button.dataset.taskId);
});
$("#banners").addEventListener("click", (event) => {
  if (event.target.closest('[data-act="sleep-ok"]')) {
    S.sleepGap = 0;
    paintBanners();
  }
  if (event.target.closest('[data-act="runtime-refresh"]')) {
    /* 横幅上的「重新检查」= 一次完整刷新：check 事件带回组件结论，恢复即撤横幅 */
    refresh(false);
  }
});
$("#headed").addEventListener("change", (event) => savePrefs({ headed: event.target.checked }));
/* 计划级重试策略：改完即存偏好，下一个计划生效（运行中的计划不受影响） */
function setRetryPolicy(patch) {
  Object.assign(S.retryPolicy, patch);
  savePrefs({ retry_policy: Object.assign({}, S.retryPolicy) });
}
$("#retry-for").addEventListener("change", (event) => {
  const value = Number(event.target.value);
  if (["120", "600", "1800", "3600", "0"].indexOf(event.target.value) >= 0) {
    setRetryPolicy({ retry_for_s: value });
  } else {
    event.target.value = String(S.retryPolicy.retry_for_s);
  }
});
$("#max-attempts").addEventListener("change", (event) => {
  const value = Math.floor(Number(event.target.value));
  if (Number.isFinite(value) && value >= 0 && String(value).length <= 4) {
    setRetryPolicy({ max_attempts: value });
  } else {
    event.target.value = String(S.retryPolicy.max_attempts);
  }
});
$("#btn-log-toggle").addEventListener("click", () => setLogOpen(!S.logOpen));
$("#btn-log-clear").addEventListener("click", () => { $("#log").textContent = ""; $("#log-last").textContent = "还没有记录"; });
/* 六步引导：跳过/上一步/下一步；说明页重播入口；GitHub 最新版本外链（点击才打开） */
$("#tour-skip").addEventListener("click", tourSkip);
$("#tour-prev").addEventListener("click", () => {
  if (S.tour.step > 0) { S.tour.step -= 1; paintTour(); }
});
$("#tour-next").addEventListener("click", () => {
  if (S.tour.step >= TOUR_STEPS.length - 1) { tourClose(true); return; }
  S.tour.step += 1;
  paintTour();
});
/* 单帮助入口：重播仍走 replayTourFrom 的关闭模态与焦点恢复路径；
 * 计划运行中禁用重播（不打断计划），弹窗本身任何状态都能打开。 */
$("#btn-help-tour").addEventListener("click", () => replayTourFrom("#help", "#btn-help"));
$("#btn-update-check").addEventListener("click", checkUpdate);
$("#btn-help-latest").addEventListener("click", () => {
  /* 用户点击才打开外链：后端用系统默认浏览器打开固定的 releases/latest 地址；
   * 打不开时把地址摆成可选文本（局部反馈），不在 WebView 里跳转 */
  helpStatus("");
  api("open_latest_release").then((res) => {
    if (!res || res.ok === false) {
      helpStatus("发布页没能打开（系统浏览器可能被拦）。地址：" + RELEASES_LATEST_URL, "warn");
    }
  }).catch((err) => {
    helpStatus("发布页没能打开：" + msg(err) + "。地址：" + RELEASES_LATEST_URL, "warn");
  });
});
$("#btn-help-data").addEventListener("click", () => {
  helpStatus("");
  api("open_data").catch((err) => {
    helpStatus("打不开数据目录：" + msg(err) + "。可以先再试一次，或在系统里手动打开本机数据目录。", "bad");
  });
});
/* 窗口尺寸或内部滚动变化时重新定位高亮与卡片 */
window.addEventListener("resize", () => { if (S.tour.open && !S.tour.paused) paintTour(); });
document.addEventListener("scroll", () => { if (S.tour.open && !S.tour.paused) paintTour(); }, true);
$("#empty-action").addEventListener("click", () => {
  /* 只按 paintEmpty 写下的动作意图分派：
   * 「有缓存 + 列表失败」的空态写的是 refresh，不能被 items.length 猜成清筛选。 */
  if ($("#empty-action").dataset.action === "clear-filters") clearFilters();
  else refresh(false);
});

document.addEventListener("keydown", (event) => {
  if (event.key === "F5") { event.preventDefault(); refresh(false); return; }
  const typing = event.target.closest && event.target.closest("input, select, textarea");
  /* 引导可见时键盘约束在卡片内：Esc=跳过、Tab 在卡片按钮间循环，
   * 背景 Ctrl+F / `/` / Enter 不移焦点、不触发开始计划；
   * 计划运行把引导暂停（tourVisible=false）时全部放行——停止操作不受影响。 */
  if (tourVisible()) {
    if (event.key === "Escape") { event.preventDefault(); tourSkip(); return; }
    if (event.key === "Tab") { tourTrapTab(event); return; }
    if ((event.ctrlKey && (event.key === "f" || event.key === "F")) ||
        (event.key === "/" && !typing)) { event.preventDefault(); return; }
    if (event.key === "Enter" && !typing) {
      const inCard = event.target.closest && event.target.closest("#tour-card");
      if (!inCard) { event.preventDefault(); return; }   /* 背景回车不开始计划 */
      return;   /* 卡片内回车：交给按钮原生激活（click 监听照常触发） */
    }
  }
  /* 帮助/停止确认打开时：Enter/Esc 只属于弹窗——
   * 不穿透到背景开始计划、不清空选择、不误跳引导；停止确认弹窗的
   * Esc/「继续等待」=不停止 的原生语义不变 */
  if ((event.key === "Enter" || event.key === "Escape") &&
      ["#help", "#dlg-stop"].some((sel) => { const d = $(sel); return !!d && d.open; })) {
    return;
  }
  /* Ctrl+F 或 / 跳到搜索框 */
  if ((event.ctrlKey && (event.key === "f" || event.key === "F")) || (event.key === "/" && !typing)) {
    event.preventDefault();
    $("#q").focus();
    $("#q").select();
    return;
  }
  if (event.key === "Escape") {
    /* 依次：关引导 → 关浮层 → 退输入 → 收起结论 → 清空选择；弹窗由浏览器自己收 */
    if (tourVisible()) { tourSkip(); return; }
    if (clockPopOpenNow()) { closeClockPop(true); return; }
    if (typing) { event.target.blur(); return; }
    if (!$("#result").hidden) { setResult(null); return; }
    if (S.selected.size && !S.busy) { clearSelection(); return; }
    return;
  }
  if (event.key !== "Enter") return;
  const active = document.activeElement;
  const tagName = active && active.tagName ? active.tagName : "BODY";
  if (["INPUT", "SELECT", "TEXTAREA", "BUTTON", "A"].indexOf(tagName) >= 0 || $("#help").open) return;
  startPlan();
});

window.addEventListener("online", () => { S.online = true; paintBanners(); paintChecks(); });
window.addEventListener("offline", () => { S.online = false; paintBanners(); paintChecks(); });

document.addEventListener("visibilitychange", () => {
  if (document.hidden) return;
  tick();
  if (!hasBridge()) return;
  api("bootstrap").then((info) => {
    if (!info) return;
    ingestBootstrapPlan(info);
    if (!!info.busy !== S.busy) setBusy(!!info.busy);
    if (!info.busy) refresh(true);
  }).catch(() => {});
});

setInterval(tick, 1000);
boot().catch((error) => { addLog("[X] 界面初始化失败：" + msg(error)); render(); });
