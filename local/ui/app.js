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
  title: "天软活动预报名工具怎么用",
  subtitle: "确认登录，选活动并开始，再看每场结果。",
  steps: [
    { no: 1, title: "确认登录", text: "首次点「登录」，在浏览器里完成登录。重开会自动检查；仅在提示失效时重新登录，暂时无法确认就刷新。" },
    { no: 2, title: "选活动并开始", text: "勾选不冲突的活动，设置重试时长和最多次数，再点「开始报名计划」。未来场次等开放，已开放场次立即尝试。" },
    { no: 3, title: "看进度与结果", text: "电脑保持唤醒和联网，窗口可最小化。逐场看结果；停止会结束本机任务，不会撤销学校报名。" },
  ],
  note: "仅操作本人账号；选中活动还没有开始，报名结果以学校活动页为准。",
};

/* 与 grab.py / app.py 对齐的常量 */
const EXPIRING_H = 2;        // 登录剩余不到 2 小时转橙色提醒
const TOKEN_LIFETIME_H = 17.5; // app.py TOKEN_LIFETIME_H：登录态寿命估算
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
  { target: "login", title: "登录状态",
    text: "这里是登录状态和「登录/刷新」入口。第一次点「登录」会弹出 Chrome 窗口——那是系统窗口，引导高亮不了，请在里面输学号、密码和验证码完成登录；之后重开会自动检查，只有失效才需要重新登录。" },
  { target: "list", title: "活动与模式",
    text: "活动按车站时刻表分组。未来的场次是「预报名」，选中后点「开始报名计划」，再等它开放；已开放的是「立即报名/抢名额」，也要先选中、再点「开始报名计划」才执行；已截止的只能查看，不能勾选。" },
  { target: "filters", title: "筛选",
    text: "按关键词、报名状态、开放时间、排序，以及是否要求签到/签退筛选。学校还没说明要求的显示「待说明」，只在「全部」里保留，不会误判成无需。" },
  { target: "select", title: "选择与冲突",
    text: "勾选想报的场次。活动举办时间重叠时只能保留其中一场；同一开放时刻但不重叠的场次可以一起提交，黑色大屏会先给出检查条和冲突提示。" },
  { target: "start", title: "重试与开始",
    text: "这里设置重试时长和最多次数（默认 2 分钟、不限次数）。次数只算真正发出去的提交，没发出去的重试不计数；不管怎么设，报名截止都是硬上限。确认大屏检查条没有红灯后，点「开始报名计划」。" },
  { target: "run", title: "运行与结果",
    text: "计划运行时，LED 大屏、时间线、任务卡和底部运行记录逐场显示进度：任务卡上是这一场的结果，整块大屏的结论要等整个计划结束。停止只结束本机任务，不会撤销学校那边已经报上的。窗口可以最小化，关掉程序或让电脑睡眠会中断执行。路线图只显示真实发生的时间点，没发生的显示「待打开/待发生」。" },
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
  login: { hours: null, ok: null, expireAt: null, savedAt: null, present: null, checking: false, result: null },
  /* 程序组件自检结论（worker_missing/worker_permission/spawn_failed）：非空时组件
   * 问题优先于登录/网络文案，登录与开始禁用。check/list 两分支按「刷新轮次」
   * 协同恢复：任一分支报组件故障立即挂横幅，另一分支成功/普通 unknown 不得撤；
   * 本轮两分支都完成且无故障才撤旧横幅（FB4 的两到达顺序一致语义） */
  runtime: { error: null, checkPending: false, listPending: false, roundError: null },
  /* FB1 轻量「开始提交中」标志：成功/拒绝/reject 均释放，只做即时按钮反馈，
   * 后端最终接纳点的原子重检仍是最终权威 */
  starting: false,
  clock: { skew: 0, checked: false },
  online: navigator.onLine !== false,
  sleepGap: 0,
  busy: false,
  stopping: false,
  /* 后端 bootstrap 的 demo 标记：演示模式不要求登录校验 valid 即可开始计划 */
  demo: false,
  plan: { id: null, status: "idle", tasks: new Map() },
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
  recheckPending: false,
  savePending: {},
  saveTimer: null,
};

function hasBridge() {
  return !!(window.pywebview && window.pywebview.api);
}

function api(name, ...args) {
  if (!hasBridge()) return Promise.reject(new Error("页面没有连接本机脚本"));
  const fn = window.pywebview.api[name];
  if (typeof fn !== "function") return Promise.reject(new Error("本机脚本不支持：" + name));
  try {
    return Promise.resolve(fn.apply(window.pywebview.api, args));
  } catch (err) {
    return Promise.reject(err);
  }
}

function waitForBridge(timeoutMs) {
  const limit = typeof timeoutMs === "number" ? timeoutMs : 15000;
  return new Promise((resolve) => {
    const started = Date.now();
    const probe = () => {
      if (hasBridge()) {
        resolve(true);
        return;
      }
      if (Date.now() - started >= limit) {
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
  const info = bridgeReady ? await api("bootstrap").catch(() => null) : null;
  fillHelp((info && info.help) || DEFAULT_HELP);
  /* 版本唯一显示位在「更多」弹窗：只用 bootstrap 带来的真实版本号，
   * 前面「当前版本」是行内标签，这里不写死候选号、也不自己拼前缀 */
  $("#settings-version").textContent = info && info.version ? String(info.version) : "—";
  if (info) {
    /* demo 标记由后端 bootstrap 带来：演示模式绕过「登录校验 valid」的开始门禁 */
    S.demo = !!info.demo;
    /* 生命周期快照（上次关闭时长/质量、登录估算）：只进时钟浮层当提示，
     * 不参与登录四态判定，也不影响开始门禁 */
    S.lifecycle = info.lifecycle || null;
    applyPrefs(info.prefs || {});
    setLogin(info.login && info.login.hours_left, null,
             info.login && "session_present" in info.login ? !!info.login.session_present : undefined,
             info.login);
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

function ingestBootstrapPlan(info) {
  /* 页面重载后 plan/task 事件已丢：用 bootstrap 带回的计划快照恢复任务列表。
   * 只恢复显示，不重新提交——提交只发生在 start_plan，与恢复无关。 */
  const plan = info && info.plan;
  if (!plan || !plan.plan_id) return;
  if (S.plan.id === String(plan.plan_id)) return;
  S.plan = { id: String(plan.plan_id), status: String(plan.status || "running"), tasks: new Map(), armedAt: Date.now() / 1000 };
  (plan.tasks || []).forEach((task) => {
    /* 快照现在携带完整运行时 detail（checkpoint/模式/策略）：重载后
     * 时间线显示真实节点，而不是退回估算时间 */
    S.plan.tasks.set(String(task.task_id), mergeTask(null, task));
  });
  setBusy(["ready", "running"].indexOf(S.plan.status) >= 0);
  render();
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
    if (!quiet) setResult({ ok: false, title: "还没有连接本机脚本", text: "请从图形界面启动，再点「刷新」拉活动列表。" });
    return;
  }
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
  api("check").then((res) => {
    if (res && res.ok === false) {
      /* 同步拒绝（如正在关闭）没有终态事件：本分支按无故障收口，不让轮次悬挂 */
      settleRefreshBranch("check");
    }
  }).catch((err) => {
    /* 桥接/脚本异常也必须离开「检查中」：落 unknown 结论并刷新胶囊与按钮，
     * 不能把界面永远停在「正在检查登录态」；下一步是点「刷新」再试。
     * ok 必须同时置 false：旧的 valid/warn 状态不清理的话，
     * paintLogin 的 unknown 分支（要求 ok===false）接不住，顶栏会继续显示「登录有效」。 */
    S.login.checking = false;
    S.login.ok = false;
    S.login.result = "unknown";
    paintLogin();
    updateControls();
    /* 结论变了，空态要跟着从「正在检查」落到「暂时无法确认」 */
    refreshLoginEmptyState();
    addLog("[X] 检查登录态失败：" + msg(err) + "；点「刷新」再试一次");
    settleRefreshBranch("check");
  });
  api("list_activities").then((res) => {
    if (res && res.ok === false) {
      settleRefreshBranch("list");
    }
  }).catch((err) => {
    /* 桥接/脚本异常同 Worker 失败一个口径：完整说明归列表空态，
     * 不再为同一列表错误额外弹结果框 */
    S.listState = "error";
    S.listError = msg(err);
    render();
    settleRefreshBranch("list");
  });
}

/* 组件错误 → 一句话标题 + 恢复动作说明（后端 runtime_error 的唯一展示口径） */
const RUNTIME_ERRORS = {
  worker_missing: { title: "程序组件缺失", hint: "登录、拉活动和报名都依赖这个组件，它没跟着程序一起到位。请重新下载完整版本（单 EXE 或完整 ZIP 都内置组件），装好后点「重新检查」。" },
  worker_permission: { title: "程序组件无法读取", hint: "组件文件在，但被系统拦住或没有权限（刚下载的文件常见）。右键程序 → 属性 → 勾「解除锁定」，或重新解压后再点「重新检查」。" },
  spawn_failed: { title: "程序组件启动失败", hint: "组件在但没能启动。点「重新检查」再试一次；反复出现请重新下载完整版本。" },
};

function runtimeErrorCopy() {
  return RUNTIME_ERRORS[S.runtime.error] || RUNTIME_ERRORS.spawn_failed;
}

function applyActivities(payload, quiet) {
  const res = payload || {};
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
  if (nowChip) nowChip.textContent = hm(Date.now());
}

function skeletonHtml() {
  let html = '<div class="skel" aria-label="正在拉取活动列表">';
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
  const now = Date.now();
  rows.forEach((item) => {
    const st = stateOf(item);
    if (st === "open") open.push(item);
    else if (st === "waiting") {
      const key = String(startOfDay(toTs(item.sign_start) * 1000 || now));
      if (!days.has(key)) days.set(key, []);
      days.get(key).push(item);
    }
    else if (st === "unknown") unknown.push(item);
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
  return '<div class="nowline" aria-hidden="true"><span class="nowline-chip">现在 <b data-now>' + hm(Date.now()) + "</b></span></div>";
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
  const left = leftOf(item);
  const leftText = left === null ? "待公布" : (left <= 0 ? "已开放" : durText(left));
  /* 已截止活动保留在列表里查看，但复选框禁用；已报名同理 */
  const checkDisabled = S.busy || !isEligibleForPlan(item);
  /* 学校常把开放时间定在 00:59:59 这种点：有秒就把秒写成小号 */
  let timeHtml = '<span class="t">--:--</span>';
  if (toTs(item.sign_start) !== null) {
    const text = String(item.sign_start || "");
    const mm = text.match(/^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})(?::(\d{2}))?/);
    if (mm) {
      timeHtml = '<span class="t">' + esc(mm[4] + ":" + mm[5]) + "</span>" + (mm[6] && mm[6] !== "00" ? '<span class="s">:' + esc(mm[6]) + "</span>" : "");
      const dayDiffMs = startOfDay(toTs(item.sign_start) * 1000) - startOfDay(Date.now());
      if (state !== "waiting" && Math.round(dayDiffMs / 86400000) !== 0) timeHtml += '<span class="d">' + esc(relDayText(toTs(item.sign_start) * 1000)) + "</span>";
    }
  }
  const sub = [];
  if (state === "open" && toTs(item.sign_end) !== null) sub.push("<span>报名截止 " + esc(whenText(toTs(item.sign_end) * 1000)) + "</span>");
  /* 活动行显示自己的执行模式：预报名 / 立即报名（抢名额）；已截止由状态格标示 */
  if (state === "waiting" || state === "open") {
    sub.push('<span class="activity-flags"><span class="activity-flag">' + esc(modeText(modeOf(item))) + "</span></span>");
  }
  sub.push("<span" + (invalid ? ' class="is-unknown"' : "") + ">" + esc(activityTimeText(item)) + "</span>");
  const flags = requirementFlags(item);
  return "<div class=\"" + classes.join(" ") + "\" data-id=\"" + esc(id) + "\" aria-label=\"" + esc(item.name) + "\">" +
    "<div class=\"c-sel\"><input class=\"row-check\" type=\"checkbox\" data-id=\"" + esc(id) + "\"" + (selected ? " checked" : "") + (checkDisabled ? " disabled" : "") + " aria-label=\"选择" + esc(item.name) + "\"></div>" +
    "<div class=\"c-time\">" + timeHtml + "</div>" +
    "<div class=\"c-main\"><div class=\"name\" title=\"" + esc(item.name) + "\">" + esc(item.name) + "</div><div class=\"sub\">" + sub.join("") + flags + "</div></div>" +
    "<div class=\"c-place\" title=\"" + esc(item.place || "地点待定") + "\">" + esc(item.place || "地点待定") + "</div>" +
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
  return '<span class="state-tag state-unknown">待公布</span>';
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
    /* 还没成功拉过一次（idle，或首屏加载中）：未知占位，
     * 不能趁这时候说「学校没有活动」，也不能先让用户去登录 */
    title.textContent = "还没拉到活动列表";
    text.textContent = "点「刷新」拉一次；需要登录时，顶部会提示下一步。";
    action.textContent = "刷新";
    return;
  }
  /* 以下都是「学校成功返回了 0 场」：按登录状态给各自下一步 */
  const login = loginEmptyState();
  if (login === "checking") {
    /* 校验还没回来：不提前说有效，也不逼用户重登 */
    title.textContent = "正在检查登录态";
    text.textContent = "正在核对已保存的登录，稍等一下；这段时间先不用重新登录。";
    action.textContent = "刷新";
    return;
  }
  if (login === "none") {
    title.textContent = "登录后查看活动";
    text.textContent = "点右上角「登录」，完成后点「刷新」。";
    action.textContent = "刷新";
    return;
  }
  if (login === "expired") {
    title.textContent = "登录态失效，先重新登录";
    text.textContent = "点右上角「重新登录」，完成后点「刷新」。";
    action.textContent = "刷新";
    return;
  }
  if (login === "unknown") {
    title.textContent = "暂时无法确认登录态";
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
  $("#metric-total").textContent = countsKnown ? String(S.items.length) : "—";
  $("#metric-free").textContent = countsKnown ? String(free) : "—";
  /* metric-next（LED 标注）统一由 paintLedLabel 维护，这里不再抢写 */
  let text = "先选活动，系统会检查举办时间冲突。";
  if (!S.busy && S.login.present === false) text = "完成登录后选活动。";
  else if (S.busy && S.plan.id) {
    /* 大屏摘要与标题/LED/任务卡同一套阶段解释：等开放 / 准备提交 / 正在提交 */
    const view = executionView();
    text = view.phase === "submitting" ? "正在提交 " + view.count + " 场 · 每场任务独立显示结果。"
      : view.phase === "arming" ? "已开放，正在准备提交。"
      : view.phase === "waiting" ? "本批 " + view.count + " 场等开放 · 到点自动开始。"
      : "计划运行中 · 每场任务独立显示结果。";
  }
  else if (S.plan.status === "partial") text = "计划部分完成 · 失败的场次可以单独查看。";
  else if (S.plan.status === "completed") text = "计划已完成 · 建议去活动页确认一眼。";
  else if (S.plan.status === "stopped") text = "计划已停止 · 已提交的场次保留原状态。";
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
  const at = Number.isFinite(now) ? now : Date.now() / 1000;
  const open = toTs(task.sign_start);
  /* 开放时间未知时只能「等」：不能因为算不出时间就说已经开放 */
  if (open === null) return "preopen";
  if (open > at) return "preopen";
  return "preparing";
}

/* 任务卡的阶段标签：raw「等待开放」在已开放时是假话，按显示阶段改写 */
function taskDisplayLabel(task) {
  const phase = taskDisplayPhase(task);
  if (phase === "preopen") return "等待开放";
  if (phase === "preparing") return "准备报名";
  if (phase === "retrying") return "提交中";
  return taskLabel(String(task && task.status || "queued"));
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
  const now = Date.now() / 1000;
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
    boardText.textContent = "正在拉取活动，先等这一次加载结束。";
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
      boardText.textContent = view.phase === "submitting"
        ? "正在提交 " + view.count + " 场 · 每场任务独立显示结果。"
        : view.phase === "arming"
          ? "已开放，正在准备 " + view.count + " 场 · 每场任务独立显示结果。"
          : "本批 " + view.count + " 场待执行 · 每场任务独立显示结果。";
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
      boardText.textContent = openNow ? "这场已经开放，现在可以选中后开始报名计划。" : "在下面的时刻表里选一场，它就会到这块屏上。";
    } else if (S.listState === "ready" && !S.items.length) {
      /* 学校成功返回 0 场：大屏只留一句短状态，完整原因在列表空态，不在这里复读 */
      setBoardTag("暂时没有", "off", true);
      box.textContent = "学校这次没有返回活动";
      boardText.textContent = "稍后点「刷新」再看。";
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
  if (S.plan.status === "completed") setBoardTag("报名成功", "green", true);
  else if (S.plan.status === "partial") setBoardTag("部分完成", "amber", true);
  else if (S.plan.status === "stopped") setBoardTag("计划已停止", "off", true);
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
function paintBoardState() {
  const board = $("#board");
  if (!board) return;
  const terminal = ["completed", "failed", "partial", "stopped"].indexOf(String(S.plan.status)) >= 0;
  let state = "idle";
  if (S.busy) state = "running";
  else if (terminal && S.plan.doneSelection === selectionSignature()) state = String(S.plan.status);
  board.dataset.planState = state;
}

function boardMetaHtml(item) {
  if (!item) return "";
  const place = esc(item.place || "地点待定");
  const act = esc(activityTimeText(item));
  return '<span><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 21s-6-5.3-6-10a6 6 0 1 1 12 0c0 4.7-6 10-6 10z"/><circle cx="12" cy="11" r="2.2"/></svg>' + place + '</span>' +
    '<span><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="5" width="16" height="15" rx="2"/><path d="M4 10h16M9 3v4M15 3v4"/></svg>' + act + "</span>";
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
  const showPick = !needLogin && !selectedItems().length && !!next;
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
    if (expireAt < executionEnd * 1000) checks.push({ tone: "warn", text: "登录约 " + whenText(expireAt) + " 过期，赶不上这次报名窗口，建议先重新登录" });
    else checks.push({ tone: "ok", text: "登录撑得到这次报名窗口" });
  }
  const skew = S.clock.skew;
  if (Math.abs(skew) > 3) checks.push({ tone: "warn", text: skew > 0 ? "本机慢 " + skew + " 秒，到点会晚点" : "本机快 " + (-skew) + " 秒" });
  else if (S.clock.checked) checks.push({ tone: "ok", text: "时钟准" });
  const earliest = earliestSelectedOpen();
  if (earliest !== null && earliest - Date.now() / 1000 > 86400) checks.push({ tone: "warn", text: "开放在 24 小时以后" });
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
  const now = Date.now() / 1000;
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
  /* 同一帧用同一个 now：不能让 map 的下标冒充时间参数 */
  const now = Date.now() / 1000;
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
  const now = Date.now() / 1000;
  const open = toTs(target.item.sign_start);
  const task = target.task || {};
  /* 每个站点独立判断 done/now/todo（不再是单一线性 index）：
   * 本次执行链（启动→页面→盯按钮）只认真实 checkpoint——事件没发生就
   * 显示「待启动/待打开/待发生」，不得拿计划时刻或当前时间编造
   * 看起来像已发生的事件时间；原定开放与截止是时间参考节点——历史开放
   * 照样标 done，但不抢占当前执行节点。 */
  const runStarted = Number(task.run_started_at) || null;
  const pageOpened = Number(task.page_opened_at) || null;
  const watchStarted = Number(task.watch_started_at) || null;
  const deadline = routeDeadline(task, open, now);
  const terminal = ["success", "already_signed", "failed", "canceled"].indexOf(String(task.status)) >= 0;
  const deadlineKnown = deadline.time !== null;
  const stations = [
    { name: "本次启动", time: runStarted ? routeTime(runStarted) : "待启动", done: runStarted !== null },
    { name: "活动页已打开", time: pageOpened ? routeTime(pageOpened) : "待打开", done: pageOpened !== null },
    { name: "盯按钮", time: watchStarted ? routeTime(watchStarted) : "待发生", done: watchStarted !== null, hot: true },
    { name: "原定开放", time: routeTime(open), done: now >= open, hot: true },
    /* 截止未知时显示「待定」，不能因为 null 参与比较而被标成已发生 */
    { name: deadline.name, time: deadlineKnown ? routeTime(deadline.time) : "待定",
      done: terminal || (deadlineKnown && now >= deadline.time) },
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
  return { name: "本次重试到", time: effectiveRetryDeadline(task, now) };
}

function routeTime(ts) {
  const sameDay = startOfDay(ts * 1000) === startOfDay(Date.now());
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
        : fmt(Math.max(0, view.retryUntil - Date.now() / 1000)) + " · 正在提交 " + view.count + " 场";
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
        countDown.textContent = "时间待公布 · 本批 " + view.count + " 场待执行";
        return;
      }
      const left = Math.floor(view.openAt - Date.now() / 1000);
      countDown.classList.toggle("is-hot", left > 0 && left <= 10);
      countDown.textContent = (left <= 0 ? "现在开放 · " : fmt(left) + " · ") + "本批 " + view.count + " 场待执行";
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
    countDown.textContent = "时间待公布";
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
        paintLedLabel("正在提交 " + view.count + " 场，重试截止", "待定");
        return;
      }
      ledSet(fmtLed(Math.max(0, view.retryUntil - Date.now() / 1000)), "amber");
      paintLedLabel("正在提交 " + view.count + " 场，重试截止", fmtClock(view.retryUntil));
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
        paintLedLabel("本批 " + view.count + " 场待执行，开放", "待公布");
        return;
      }
      const left = Math.floor(view.openAt - Date.now() / 1000);
      ledSet(fmtLed(left), left <= 10 ? "red" : "amber");
      /* 直接格式化真实开放时刻：不再用 now + 已取整 left 重拼，避免 1 秒偏差 */
      paintLedLabel("本批 " + view.count + " 场待执行，开放", fmtClock(view.openAt));
      return;
    }
    /* 准备中/汇总中：屏上不给假倒计时，也不提前点亮成功 */
    ledSet("--:--:--", "off");
    paintLedLabel("计划状态", view.phase === "settling" ? "汇总中" : "准备中");
    return;
  }
  /* 终态结论（✓/✕/灭灯）留在屏上，直到选择集合发生变化；变化后回到预览倒计时 */
  const terminal = { completed: ["✓", "green"], failed: ["✕", "red"], stopped: ["--:--:--", "off"], partial: ["--:--:--", "off"] }[S.plan.status];
  if (terminal && S.plan.doneSelection === selectionSignature()) {
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
  if (next.openNow) { ledSet("00:00:00", "dim"); paintLedLabel("有活动开放中", null); return; }
  ledSet("--:--:--", "off");
  paintLedLabel("最近开放", "待公布");
}

/* 秒级时间戳 → HH:MM:SS（LED 标注与点阵同款数字口径） */
function fmtClock(ts) {
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
}

/* 终态的停止条件人话：让用户知道是「到点/到次数/到截止」里的哪一个 */
function stopReasonText(reason) {
  return {
    success: "报名成功",
    already_signed: "已经报过",
    login_invalid: "登录失效，先重新登录",
    max_attempts: "达到最大提交次数",
    retry_duration: "重试时长到了",
    sign_end: "报名已截止",
    user_stopped: "已停止",
    process_error: "脚本异常退出",
  }[reason] || "";
}

/* telemetry 的 retry_deadline 是 epoch 秒（后端结构化行）；空串/0 表示没有 */
function telemetryDeadline(task) {
  const epoch = Number(task && task.retry_deadline);
  return Number.isFinite(epoch) && epoch > 0 ? epoch : null;
}

/* 提交中任务行的说明文字：telemetry（第 N 次/最近原因/重试到）优先，
 * 没有 telemetry 才退回固定窗口估算。taskHtml 与每秒走字共用这一份口径。
 * 截止可能未知（已开放又没有任何启动参考点）：说「等待结果」，不编 0 秒。 */
function submittingDetail(task) {
  const parts = [];
  if (task.attempts) parts.push("第 " + task.attempts + " 次提交");
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
  parts.push("重试窗口约 " + fmt(Math.max(0, fallback - Date.now() / 1000)));
  return parts.join(" · ");
}

function taskHtml(task, now) {
  const state = String(task.status || "queued");
  /* 标签与说明都按显示阶段改写：raw submitting 可能只是提前开页的预热 */
  const label = taskDisplayLabel(task);
  const cls = taskClass(state);
  /* 混合计划每场显示自己的模式：快照带 signup_mode，旧载荷按时间现算 */
  const mode = String(task.signup_mode || modeOf(task));
  const modePrefix = taskModePrefix(task);
  let detail = task.reason || task.detail || "";
  if (RUNNING_STATUSES.indexOf(state) >= 0) {
    /* 等待 / 准备 / 提交共用一份解释；typing 的等待文案只在没有阶段说明时兜底 */
    detail = taskDisplayDetail(task, now)
      || (state === "waiting" ? (mode === "pre_register" ? "等待报名开放" : "报名已开放，马上开始") : detail);
  } else if (["failed", "canceled"].indexOf(state) >= 0) {
    const bits = [];
    if (task.attempts) bits.push("提交了 " + task.attempts + " 次");
    const stopNote = stopReasonText(task.stop_reason);
    if (stopNote && task.stop_reason !== "success" && task.stop_reason !== "already_signed") bits.push(stopNote);
    if (bits.length) detail = (detail ? detail + "（" : "") + bits.join("，") + (detail ? "）" : "");
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
  const now = Number.isFinite(fallbackNow) ? fallbackNow : Date.now() / 1000;
  const retryFor = task && Number.isFinite(Number(task.retry_for_s)) ? Number(task.retry_for_s) : RETRY_WINDOW_S;
  const signEnd = task ? toTs(task.sign_end) : null;
  /* 绝对截止不依赖任何起点：先把这条独立路径处理掉，别被 anchor 判空挡住 */
  if (!(retryFor > 0) && signEnd !== null) return signEnd;
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
  /* 「更多」按钮本身任何状态可用；只有两处「重看引导」在计划运行中禁用（按钮上有可见原因） */
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
  $("#btn-plan").disabled = !canStart;
  $("#btn-plan").textContent = S.busy ? "计划运行中"
    : (S.starting ? "正在提交…" : "开始报名计划");
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
    setResult({ ok: false, title: "计划还不能开始", text: "先处理上面的时间提示，再开始报名计划。" });
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
   * 最终接纳点的原子重检是最终权威），成功/拒绝/reject 均释放 */
  S.starting = true;
  updateControls();
  api("start_plan", items, $("#headed").checked,
      { retry_for_s: S.retryPolicy.retry_for_s, max_attempts: S.retryPolicy.max_attempts }).then((res) => {
    S.starting = false;
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
     * 这里只在计划 id 变了时才重置，避免把已收到的任务表清空。 */
    if (!S.plan.id || S.plan.id !== String(res.plan_id || "")) {
      S.plan = { id: String(res.plan_id || ""), status: "ready", tasks: new Map(), armedAt: Date.now() / 1000 };
    }
    S.stopping = false;
    setBusy(true);
    /* 已开放场次会立即执行，不能写成「等待各自开放时间」：
     * 按两种正式模式分别说明（预报名等开放 / 立即报名抢名额）。 */
    const futureCount = items.filter((item) => item.signup_mode === "pre_register").length;
    const instantCount = items.filter((item) => item.signup_mode === "instant_signup").length;
    const parts = [];
    if (futureCount) parts.push(futureCount + " 场预报名等开放");
    if (instantCount) parts.push(instantCount + " 场立即报名/抢名额马上执行");
    addLog("已安排 " + items.length + " 场活动：" + (parts.join("，") || "按各场状态执行") + "。");
    render();
  }).catch((err) => {
    S.starting = false;
    setBusy(false);
    updateControls();
    setResult({ ok: false, title: "计划没能开始", text: msg(err) });
  });
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
  setResult(null);
  setBusy(true);
  addLog("正在打开登录窗口，在弹出的 Chrome 里登完再回来…");
  api("login").then((res) => {
    if (!res || res.ok === false) {
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
    setBusy(false);
    setResult({ ok: false, title: "登录没能开始", text: msg(err) });
  });
}

/* ---------------------------------------------------------------- 事件 */
function handleDone(payload) {
  const data = payload || {};
  if (data.kind === "login") {
    setBusy(false);
    if (data.ok) {
      /* 登录成功=新会话已保存：旧锚点立即作废（清掉），紧接着的校验事件会带
       * 新锚点回来；这两秒里宁可显示未知，也不能拿旧凭证的剩余时间充数。 */
      setLogin(null, true, true, { estimated_expires_at: null, saved_at: null });
      setResult({ ok: true, title: "登录好了", text: "剩余时间正在重新核对（以学校校验为准），活动列表正在刷新。", autoClose: Date.now() + 6000 });
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
    else setResult({ ok: false, title: "没抢到：" + cleanReason(data.reason), text: grabHint(data.reason) });
  }
}

/* 登录失败：按 grab.py login 真会打印的句子归类，回答发生了什么+现在能做什么 */
function loginFailCopy(reason) {
  const raw = String(reason || "");
  if (/窗口被关掉/.test(raw)) return { title: "登录窗口被关掉了", text: "登录还没完成，窗口就关了。点「再试一次」重新打开。" };
  if (/仍未检测到登录成功/.test(raw)) return { title: "等了 10 分钟还没登录成功", text: "Chrome 窗口里一直没完成登录，超时了。点「再试一次」。" };
  if (/打不开登录页/.test(raw)) return { title: "打不开学校的登录页", text: "检查网络、校园网或代理，然后再试一次。" };
  if (/接口校验失败/.test(raw)) return { title: "登录后学校那边没校验通过", text: "登录态存下来了，但学校不认。再登录一次试试。" };
  if (/没找到可用的浏览器/.test(raw)) return { title: "没找到能用的浏览器", text: "这台电脑上要有 Chrome 或 Edge，装好以后再试。" };
  return { title: "登录没完成", text: cleanReason(raw) || "原因不明。可以再试一次，或者看看运行记录。" };
}

/* 运行时 checkpoint 字段：晚到的 plan 快照或恢复载荷缺这些字段时，
 * 不得用空值抹掉已经收到的真实事件（页面重载恢复与晚到 plan 事件共用）。 */
const TASK_RUNTIME_FIELDS = ["run_started_at", "page_opened_at", "watch_started_at",
  "retry_deadline", "attempts", "last_reason", "stop_reason"];

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

function handlePlan(payload) {
  const data = payload || {};
  if (!S.plan.id || S.plan.id !== String(data.plan_id)) S.plan = { id: String(data.plan_id || ""), status: data.status || "ready", tasks: new Map(), armedAt: Date.now() / 1000 };
  S.plan.status = String(data.status || "ready");
  /* 与已有任务合并：快照缺运行时字段时不覆盖实时 checkpoint（晚到 plan
   * 事件不能抹掉已收到的第 N 次提交/页面打开等 telemetry） */
  (data.tasks || []).forEach((task) => {
    S.plan.tasks.set(String(task.task_id),
                     mergeTask(S.plan.tasks.get(String(task.task_id)) || null, task));
  });
  setBusy(["ready", "running"].indexOf(S.plan.status) >= 0);
  render();
}

function handleTask(payload) {
  const data = payload || {};
  if (!S.plan.id || S.plan.id !== String(data.plan_id)) S.plan = { id: String(data.plan_id || ""), status: "running", tasks: new Map(), armedAt: Date.now() / 1000 };
  const prev = S.plan.tasks.get(String(data.task_id)) || null;
  const task = mergeTask(prev, data);
  S.plan.tasks.set(String(data.task_id), task);
  if (["success", "failed", "already_signed", "canceled"].indexOf(task.status) >= 0) showTaskResult(task);
  render();
}

function handlePlanDone(payload) {
  const data = payload || {};
  S.plan.id = String(data.plan_id || S.plan.id || "");
  S.plan.status = String(data.status || "failed");
  /* 计划收尾：解除「正在停止」并解锁 busy（canceled/终态都已落定） */
  S.stopping = false;
  /* 记下终态时的选择集合：集合不变时结论留在 LED 上，变化后回到预览 */
  S.plan.doneSelection = selectionSignature();
  setBusy(false);
  const summary = data.summary || {};
  const counts = summary.counts || {};
  /* 计划级结论也带上失败场次到达的停止条件（次数/时长/截止），去重后一句话说清 */
  const stopNotes = [];
  S.plan.tasks.forEach((task) => {
    if (String(task.status) !== "failed") return;
    const note = stopReasonText(task.stop_reason);
    if (note && task.stop_reason !== "process_error" && stopNotes.indexOf(note) < 0) stopNotes.push(note);
  });
  const noteText = stopNotes.length ? "（" + stopNotes.join("、") + "）" : "";
  if (S.plan.status === "completed") setResult({ ok: true, title: "报名计划完成", text: "成功 " + ((counts.success || 0) + (counts.already_signed || 0)) + " 场，建议去活动页确认一眼。" });
  else if (S.plan.status === "partial") setResult({ ok: "partial", title: "报名计划部分完成", text: "成功 " + (counts.success || 0) + " 场，失败或取消 " + ((counts.failed || 0) + (counts.canceled || 0)) + " 场" + noteText + "。" });
  else if (S.plan.status === "stopped") setResult({ ok: "partial", title: "报名计划已停止", text: "已提交的场次保留原状态，尚未提交的场次已取消。", action: { label: "重新开始等", run: restartStoppedPlan } });
  else setResult({ ok: false, title: "报名计划没有成功", text: (counts.failed || 0) + " 场都没抢到" + noteText + "。逐场原因已列在上面任务状态里。", action: { label: "刷新列表", run: () => refresh(false) } });
  render();
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

function showTaskResult(task) {
  const name = task.name || "这一场活动";
  /* 计划还在跑时，单场终态只更新任务卡与运行记录：结果框和整屏留给
   * handlePlanDone 的计划级结论（单场成功不得抢写全局成功，UX-05）。 */
  const planLive = !!S.busy;
  if (task.status === "success") {
    addLog("[OK] 报名成功：" + name);
    if (!planLive) setResult({ ok: true, title: "报名成功：" + name, text: "建议去活动页确认一眼。" });
  } else if (task.status === "already_signed") {
    addLog("[i] 已经报过：" + name);
    if (!planLive) setResult({ ok: "partial", title: "已经报过：" + name, text: "这场保留为已报名，其他场次继续。" });
  } else if (task.status === "canceled") {
    addLog("[i] 已取消：" + name);
    if (!planLive) setResult({ ok: "partial", title: "已取消：" + name, text: "其他场次继续按计划处理。" });
  } else {
    /* 终态文案 = 归因建议 + 学校原话 + 到达的停止条件（时长/次数/截止） */
    const bits = [grabHint(task.reason), "原话：" + cleanReason(task.reason)];
    const stopNote = stopReasonText(task.stop_reason);
    if (stopNote && task.stop_reason !== "process_error") bits.push(stopNote);
    addLog("[X] 没抢到：" + name + " · " + cleanReason(task.reason));
    if (!planLive) setResult({ ok: false, title: "没抢到：" + name, text: bits.join(" ") });
    /* 失败后顺手复查一次登录态和列表：掉了登录的页面只会看到按钮不出现，
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
    return (S.login.expireAt - Date.now()) / 3600000;
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
  if (S.busy) {
    cls += " is-busy";
    text = S.stopping ? "正在停止" : "正在处理";
    note = S.stopping ? "正在结束还在重试的场次，很快就好" : "计划正在运行，窗口别关";
    state = "busy";
  } else if (S.login.checking || (ok === null && S.login.present === true && h !== null)) {
    /* 正在真实校验：本机有会话也不提前宣布「登录有效」，等学校接口结论 */
    cls += " is-busy";
    text = "正在检查登录态";
    note = "正在核对已保存的登录，马上就好";
    state = "checking";
  } else if (S.login.present === false) {
    /* 从没登录过（session.json 缺失/解析不了/没有 token）：引导首次登录，
     * 不算失效。check 失败也是「还没登录」，不是「登录态坏了」。 */
    cls += " is-none";
    note = "点右上角「登录」开始使用";
  } else if (S.login.result === "unknown" && ok === false) {
    /* 学校接口没应答/网络异常：不能误报成「重新登录」，会话原样保留 */
    cls += " is-none";
    text = "暂时无法确认登录态";
    note = "网络或学校接口没应答；点「刷新」再试一次，暂时不用重新登录";
    state = "unknown";
  } else if (S.login.result === "valid" && typeof h === "number" && h <= 0) {
    /* 学校校验有效但估算已到点：仍显示有效，估算只作提醒，不得显示成失效 */
    cls += " is-ok";
    text = "登录有效";
    sub = "估算已到点，以学校校验为准";
    note = "已复用本机登录态；估算时间只是提醒，不代表失效";
    state = "ok";
  } else if ((ok === false || (typeof h === "number" && h <= 0))
             && S.login.present !== false) {
    /* 登录过且确认失败/过期，才叫登录态失效；hours_left=0 单独出现
     * 不代表首次未登录。present 未知（老载荷）时维持保守判失效。 */
    cls += " is-bad";
    text = "登录态失效";
    note = "点右上角「重新登录」重登一次，再刷新列表";
    state = "bad";
  } else if (typeof h === "number") {
    if (h <= EXPIRING_H) {
      cls += " is-warn";
      text = "登录快过期";
      sub = "约剩 " + leftText(h);
      note = "不到 2 小时，建议现在先重登一次";
      state = "warn";
    } else {
      cls += " is-ok";
      text = "登录有效";
      sub = "约剩 " + leftText(h);
      note = S.login.result === "valid" ? "已复用本机登录态，够用一阵子" : "登录态够用一阵子，过期后重登一次就行";
      state = "ok";
    }
  } else if (ok === true) {
    cls += " is-ok";
    text = "登录态有效";
    note = "刚核对过，可以拉列表";
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
  const stamp = new Date().toTimeString().slice(0, 8);
  /* 每行一层小条：按脚本前缀着色（[X] 红 / [!] 橙 / [OK] 绿），原话不改 */
  const row = document.createElement("div");
  row.className = "lg";
  const kind = text.indexOf("[X]") === 0 ? "bad"
    : text.indexOf("[!]") === 0 ? "warn"
    : (text.indexOf("[OK]") === 0 || text.indexOf("▶") === 0) ? "ok" : "";
  if (kind) row.dataset.k = kind;
  row.textContent = stamp + "  " + text;
  box.appendChild(row);
  while (box.childElementCount > 500) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
  /* 收起时顶栏显示最新一句 */
  const last = $("#log-last");
  if (last && text.trim()) last.textContent = stamp + "  " + text.trim();
  /* 抓取日志里的时钟偏差原话：check 只回最后一行，这里补一条通道 */
  const hit = text.match(/本机时钟与服务器相差\s*([+-]?\d+)\s*秒/);
  if (hit) {
    S.clock.skew = parseInt(hit[1], 10);
    S.clock.checked = true;
    paintClockSkew();
  }
  requestAnimationFrame(() => { box.scrollTop = box.scrollHeight; });
}

function paintClockSkew() {
  const badge = $("#clock-skew");
  if (!badge) return;
  const skew = S.clock.skew;
  if (Math.abs(skew) > 3) {
    badge.hidden = false;
    badge.textContent = skew > 0 ? "本机慢 " + skew + " 秒" : "本机快 " + (-skew) + " 秒";
    $("#clock").title = "本机时钟与学校相差 " + skew + " 秒，点开看怎么同步";
  } else {
    badge.hidden = true;
    $("#clock").title = "";
  }
}

/* ---------------------------------------------------------------- 时钟浮层 */
function closeClockPop() {
  const pop = $("#clock-pop");
  if (pop && !pop.hidden) pop.hidden = true;
}

function toggleClockPop() {
  const pop = $("#clock-pop");
  if (!pop) return;
  if (!pop.hidden) { pop.hidden = true; return; }
  const skew = S.clock.skew;
  let html;
  if (Math.abs(skew) > 3) {
    html = "<h4>" + (skew > 0 ? "本机时钟慢 " + skew + " 秒" : "本机时钟快 " + (-skew) + " 秒") + "</h4>" +
      "<p>" + (skew > 0
        ? "学校服务器比这台电脑快 " + skew + " 秒，到点会晚 " + skew + " 秒才点。"
        : "这台电脑比学校服务器快 " + (-skew) + " 秒，可能提早开始刷新页面。") + "</p>" +
      "<p>到 Windows「设置 › 时间和语言 › 日期和时间」点「立即同步」，然后点「刷新」重新对一次。</p>";
  } else if (S.clock.checked) {
    html = "<h4>本机时钟准</h4><p>和学校服务器相差不到 3 秒。每次「刷新」都会重新对一次。</p>";
  } else {
    html = "<h4>还没和学校对过时间</h4><p>登录有效时，「刷新」会顺带和学校服务器对一次时间。</p>";
  }
  /* 生命周期提示（bootstrap.lifecycle）：上一周期结束到本次启动的固定离线间隔。
   * abnormal 只有存活参考点可依，必须带「约」；未知不编时间。这些只是提示，
   * 登录主状态仍按四态口径渲染。 */
  const life = S.lifecycle || {};
  const lifeRows = [];
  if (typeof life.closed_for_s === "number") {
    const approx = life.close_quality === "abnormal" ? "约 " : "";
    lifeRows.push(["上次关闭后离线", approx + durText(life.closed_for_s) +
                   "（" + (life.close_quality === "abnormal" ? "按最后存活时刻估算，上次未正常退出" : "从上次正常关闭到本次启动") + "）"]);
  } else if (life.close_quality === "unknown" && life.last_heartbeat_at) {
    lifeRows.push(["上次关闭后离线", "未知（上次没正常退出，时间对不上）"]);
  }
  /* 当前凭证的预计剩余与胶囊同源（S.login 锚点）：重新登录后这里跟着换新值，
   * 不再读 bootstrap 时冻结的生命周期快照。锚点缺失显示未知，不编 17 小时。 */
  if (S.login.present !== false) {
    const anchor = S.login.expireAt;
    lifeRows.push(["登录预计还剩", anchor
      ? '<span id="clock-pop-left"></span>'
      : "未知（以学校校验为准）"]);
  }
  if (lifeRows.length) {
    /* 行值由代码生成（含倒计时 span 占位），不含用户输入，不再整体转义 */
    html += '<dl class="kv">' + lifeRows.map((row) =>
      "<dt>" + esc(row[0]) + "</dt><dd>" + row[1] + "</dd>").join("") + "</dl>";
  }
  html += '<div class="acts"><button class="btn btn-line" type="button" id="clock-pop-close">关闭</button></div>';
  pop.innerHTML = html;
  pop.hidden = false;
  paintClockPopLeft();
  /* 浮层贴在顶栏时钟下方，不出视口 */
  const rect = $("#clock").getBoundingClientRect();
  pop.style.top = Math.round(rect.bottom + 8) + "px";
  pop.style.left = Math.round(Math.max(12, Math.min(window.innerWidth - pop.offsetWidth - 12, rect.right - pop.offsetWidth))) + "px";
  const closeBtn = pop.querySelector("#clock-pop-close");
  if (closeBtn) closeBtn.addEventListener("click", closeClockPop);
}

/* 浮层里的「登录预计还剩」随每秒走字递减：打开时与 tick() 都会重写这一个 dd。 */
function paintClockPopLeft() {
  const cell = document.querySelector("#clock-pop-left");
  if (!cell) return;
  const anchor = S.login.expireAt;
  if (!anchor) { cell.textContent = "未知（以学校校验为准）"; return; }
  const leftS = (anchor - Date.now()) / 1000;
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
  const more = $("#settings");
  /* 两个弹窗职责不同，但都是原生模态：不能叠着开 */
  if (more && more.open) more.close();
  if (!dialog.open) dialog.showModal();
  savePrefs({ welcome_seen: true });
}

function showSettings() {
  /* 「更多」入口任何状态都可用：未登录、出错、计划运行中都要能打开（只读动作不受忙限制） */
  const dialog = $("#settings");
  const help = $("#help");
  if (help && help.open) help.close();
  settingsStatus("");          /* 每次打开都从干净状态开始，不带上一轮的错误 */
  if (dialog && !dialog.open) dialog.showModal();
}

/* 「更多」的局部反馈：只在这个弹窗里说下一步，不去复读到大屏或结果框。
 * 长地址/错误原话做成可选文本，不在 WebView 里导航。 */
function settingsStatus(text, tone) {
  const box = $("#settings-status");
  if (!box) return;
  if (!text) {
    box.hidden = true;
    box.textContent = "";
    return;
  }
  box.hidden = false;
  box.dataset.tone = tone || "warn";
  box.textContent = text;
}

/* 说明与「更多」两处重播共用这一条路径：先确认非忙，再关当前弹窗、
 * 把焦点还给对应顶栏入口（退出引导时正好回到这里），最后进第 1 步。
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

/* 忙时两处重播都禁用，并把原因写在按钮上——不只靠 title 提示 */
function paintReplayButtons() {
  [["#btn-settings-tour", "重看引导"], ["#btn-help-tour", "重看引导"]].forEach((pair) => {
    const btn = $(pair[0]);
    if (!btn) return;
    btn.disabled = !!S.busy;
    btn.textContent = S.busy ? "计划结束后可重看引导" : pair[1];
    btn.title = S.busy ? "计划结束后可重看引导" : "重新看一遍六步引导";
  });
}

function savePrefs(patch) {
  Object.assign(S.prefs, patch || {});
  Object.assign(S.savePending, patch || {});
  clearTimeout(S.saveTimer);
  S.saveTimer = setTimeout(() => {
    const pending = S.savePending;
    S.savePending = {};
    if (hasBridge()) api("save_prefs", pending).catch(() => {});
  }, 250);
}

function savePrefsNow(patch) {
  /* 引导完成/跳过这类不能因关窗丢失的状态：立即落盘，不等 250ms 防抖 */
  Object.assign(S.prefs, patch || {});
  Object.assign(S.savePending, patch || {});
  clearTimeout(S.saveTimer);
  const pending = S.savePending;
  S.savePending = {};
  if (hasBridge()) api("save_prefs", pending).catch(() => {});
}

function findItem(id) { return S.byId.get(String(id)) || null; }

function normalize(raw) {
  const item = Object.assign({}, raw || {});
  item.id = String(item.id == null ? "" : item.id);
  item.name = String(item.name || "活动");
  item.signed = !!item.signed || String(item.state || "") === "已报名" || String(item.state || "") === "signed";
  item.checkin_required = triState(item.checkin_required);
  item.checkout_required = triState(item.checkout_required);
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
  const flags = [];
  if (item.checkin_required === null && item.checkout_required === null) {
    return '<span class="activity-flags"><span class="activity-flag is-unknown">签到/签退待说明</span></span>';
  }
  if (item.checkin_required === true) flags.push('<span class="activity-flag">签到</span>');
  else if (item.checkin_required === null) flags.push('<span class="activity-flag is-unknown">签到待说明</span>');
  if (item.checkout_required === true) flags.push('<span class="activity-flag">签退</span>');
  else if (item.checkout_required === null) flags.push('<span class="activity-flag is-unknown">签退待说明</span>');
  return flags.length ? '<span class="activity-flags">' + flags.join("") + "</span>" : "";
}

function selectedItems() {
  return Array.from(S.selected).map((id) => findItem(id)).filter(Boolean);
}

/* 筛选只影响可见行：全局下一场、冲突检查、计划都用 S.items，不用这里的输出 */
function filtered() {
  const keyword = S.filter.q.trim().toLowerCase();
  const now = Date.now() / 1000;
  const rows = S.items.filter((item) => {
    if (keyword && (String(item.name) + " " + String(item.place || "")).toLowerCase().indexOf(keyword) < 0) return false;
    /* 可报名 = 未报名且未截止；已截止的只剩在「全部」里可见 */
    if (S.filter.status === "free" && (item.signed || stateOf(item) === "closed")) return false;
    if (S.filter.status === "signed" && !item.signed) return false;
    if (S.filter.time !== "all") {
      const stamp = toTs(item.sign_start);
      if (!stamp) return false;
      if (S.filter.time === "today" && new Date(stamp * 1000).toDateString() !== new Date(now * 1000).toDateString()) return false;
      if (S.filter.time === "week" && (stamp < now - 7 * 86400 || stamp > now + 7 * 86400)) return false;
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
  const now = Date.now() / 1000;
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
  return { best: best, openNow: openNow, bestId: openId !== null ? openId : futureId,
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
  items.forEach((item) => {
    if (item.signed) errors.push({ title: "已经报过一场", text: "「" + item.name + "」已报名，先取消它再开始计划。" });
    /* 报名截止是可执行门禁：已截止活动即使被勾上（时序差）也开始不了 */
    if (modeOf(item) === "closed") errors.push({ title: "报名已截止", text: "「" + item.name + "」的报名已截止（" + (item.sign_end || "时间未公布") + "），不能加入计划。" });
    if (!toTs(item.sign_start)) errors.push({ title: "报名开放时间待确认", text: "「" + item.name + "」没有可用的报名开始时间。" });
    const start = toTs(item.act_start);
    const end = toTs(item.act_end);
    if (!start || !end) errors.push({ title: "活动时间待确认", text: "「" + item.name + "」缺少完整的活动起止时间，不能静默加入可执行计划。" });
    else if (end <= start) errors.push({ title: "活动时间无效", text: "「" + item.name + "」的结束时间不晚于开始时间。" });
  });
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
  const counts = { free, signed, all: S.items.length };
  document.querySelectorAll(".chip").forEach((chip) => {
    const on = chip.dataset.status === S.filter.status;
    chip.classList.toggle("is-on", on);
    chip.setAttribute("aria-selected", on ? "true" : "false");
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
  already_signed: "已经报过这一场，先取消选择再开始。",
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
    S.selectionNotice = "后端校验没通过，处理上面的提示后再试。";
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
  const now = Date.now() / 1000;
  const end = toTs(item.sign_end);
  if (end !== null && end <= now) return "closed";
  const start = toTs(item.sign_start);
  if (start !== null && start > now) return "pre_register";
  return "instant_signup";
}

function modeText(mode) {
  return { pre_register: "预报名", instant_signup: "立即报名/抢名额", closed: "已截止" }[mode] || "";
}

function stateOf(item) {
  if (!item) return "unknown";
  if (item.signed) return "signed";
  const state = String(item.state || "");
  if (state.indexOf("已报名") >= 0 || state === "signed") return "signed";
  if (state.indexOf("已截止") >= 0 || state === "closed") return "closed";
  const end = toTs(item.sign_end);
  if (end !== null && end < Date.now() / 1000) return "closed";
  /* 开放时间已知时随墙钟换组，不能一直沿用上次拉列表时的「未开放」文本 */
  const open = toTs(item.sign_start);
  if (open !== null) return open > Date.now() / 1000 ? "waiting" : "open";
  if (state.indexOf("开放中") >= 0 || state === "open") return "open";
  if (state.indexOf("未开放") >= 0 || state === "waiting") return "waiting";
  const left = leftOf(item);
  if (left === null) return "unknown";
  return left > 0 ? "waiting" : "open";
}

function taskLabel(state) {
  return { queued: "排队", waiting: "等待开放", submitting: "提交中", success: "成功", already_signed: "已报名", failed: "失败", canceled: "已取消" }[state] || "处理中";
}

function taskClass(state) {
  if (state === "success" || state === "already_signed") return "is-ok";
  if (state === "failed") return "is-bad";
  if (state === "canceled") return "is-warn";
  if (state === "submitting" || state === "waiting" || state === "queued") return "is-busy";
  return "";
}

function leftOf(item) {
  if (!item) return null;
  const stamp = toTs(item.sign_start);
  if (stamp !== null) return Math.floor(stamp - Date.now() / 1000);
  return typeof item.seconds_to_open === "number" ? item.seconds_to_open : null;
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
let lastBeat = Date.now();

function tick() {
  const nowMs = Date.now();
  /* 心跳间隔异常：最小化时浏览器放慢计时，阈值放宽到 2 分钟 */
  const gap = nowMs - lastBeat;
  lastBeat = nowMs;
  if (gap > SLEEP_GAP_MS && !S.sleepGap) {
    S.sleepGap = gap;
    paintBanners();
  }
  const clock = $("#clock-time");
  if (clock) clock.textContent = new Date().toTimeString().slice(0, 8);
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
        const text = left === null ? "待公布" : (left <= 0 ? "已开放" : durText(left));
        if (els.left.textContent !== text) els.left.textContent = text;
        els.left.classList.toggle("is-soon", left !== null && left > 0 && left <= 600);
      }
      if (els.ago && left !== null && left <= 0 && !item.signed) els.ago.textContent = "已开放 " + durText(-left);
    });
  }
  const nowChip = document.querySelector("#rows [data-now]");
  if (nowChip) nowChip.textContent = hm(nowMs);
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
    const now = Date.now() / 1000;
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
  api("check").catch((err) => addLog("[X] 检查登录态失败：" + msg(err)));
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
  if (/登录|登陆|session|401|403|token/i.test(String(reason))) return "登录态失效了。先点「登录」重登，再点「刷新」。";
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
  return "脚本没跑通。先点「重试」；还不行就看下面的运行记录。";
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

function needLogin(text) { return /登录态|未登录|请先登录|session|401|403|token/i.test(String(text || "")); }
function leftText(hours) { return hours >= 1 ? Math.round(hours) + " 小时" : Math.max(1, Math.round(hours * 60)) + " 分钟"; }
function numOrNull(value) { return typeof value === "number" && isFinite(value) ? value : null; }
function msg(error) { return String((error && error.message) || error || "未知错误"); }

/* ---------------------------------------------------------------- 时间工具 */
function toTs(value) {
  if (!value) return null;
  const match = String(value).match(/^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})$/);
  if (!match) return null;
  const date = new Date(+match[1], +match[2] - 1, +match[3], +match[4], +match[5], +match[6]);
  return isNaN(date.getTime()) ? null : date.getTime() / 1000;
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
  const d = new Date(ms);
  const pad = (n) => String(n).padStart(2, "0");
  return pad(d.getHours()) + ":" + pad(d.getMinutes());
}

function startOfDay(ms) {
  const d = new Date(ms);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

function relDayText(ms) {
  const diff = Math.round((startOfDay(ms) - startOfDay(Date.now())) / 86400000);
  if (diff === 0) return "今天";
  if (diff === 1) return "明天";
  if (diff === 2) return "后天";
  if (diff === -1) return "昨天";
  const d = new Date(ms);
  return (d.getMonth() + 1) + "月" + d.getDate() + "日 " + WEEK[d.getDay()];
}

function dayLabel(ms) {
  return relDayText(ms);
}

/* 完整时刻：今天只写时分，别的天带日期星期 */
function whenText(ms) {
  const diff = Math.round((startOfDay(ms) - startOfDay(Date.now())) / 86400000);
  const d = new Date(ms);
  const pad = (n) => String(n).padStart(2, "0");
  const time = pad(d.getHours()) + ":" + pad(d.getMinutes()) + (d.getSeconds() ? ":" + pad(d.getSeconds()) : "");
  if (diff === 0) return "今天 " + time;
  if (diff === 1) return "明天 " + time;
  if (diff === 2) return "后天 " + time;
  return (d.getMonth() + 1) + "月" + d.getDate() + "日 " + time;
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
  /* 跳过=本版不再自动弹；说明页的「重新看一遍引导」随时可以重播 */
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
    setLogin(data.hours_left, !!data.ok,
             "session_present" in data ? !!data.session_present : undefined,
             data);
    if (data.result === "valid") addLog("[OK] 已复用本机登录态，学校接口校验通过");
    /* check 的 text 是 grab.py cmd_check 的最后一行：有效时恰好是时钟偏差提醒 */
    const hit = String(data.text || "").match(/相差\s*([+-]?\d+)\s*秒/);
    if (hit) {
      S.clock.skew = parseInt(hit[1], 10);
      S.clock.checked = true;
      paintClockSkew();
    }
    if (!data.ok) {
      const reason = String(data.reason || data.text || "登录态检查没通过");
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
  plan: (payload) => handlePlan(payload),
  task: (payload) => handleTask(payload),
  plan_done: (payload) => handlePlanDone(payload),
};

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
  const row = event.target.closest(".row");
  if (!row) return;
  const check = row.querySelector(".row-check");
  if (check && !check.disabled) { check.checked = !check.checked; toggleSelection(check.dataset.id, check.checked); }
});
$("#btn-refresh").addEventListener("click", () => refresh(false));
$("#btn-login").addEventListener("click", doLogin);
$("#btn-help").addEventListener("click", showHelp);
$("#clock").addEventListener("click", (event) => { event.stopPropagation(); toggleClockPop(); });
document.addEventListener("mousedown", (event) => {
  if (!event.target.closest("#clock-pop") && !event.target.closest("#clock")) closeClockPop();
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
/* 辅助入口：说明与「更多」各自有重播按钮，共用 replayTourFrom 一条路径；
 * 计划运行中禁用重播（不打断计划），弹窗本身任何状态都能打开。 */
$("#btn-settings").addEventListener("click", showSettings);
$("#btn-settings-tour").addEventListener("click", () => replayTourFrom("#settings", "#btn-settings"));
$("#btn-help-tour").addEventListener("click", () => replayTourFrom("#help", "#btn-help"));
$("#btn-settings-latest").addEventListener("click", () => {
  /* 用户点击才打开外链：后端用系统默认浏览器打开固定的 releases/latest 地址；
   * 打不开时把地址摆成可选文本（局部反馈），不在 WebView 里跳转 */
  settingsStatus("");
  api("open_latest_release").then((res) => {
    if (!res || res.ok === false) {
      settingsStatus("发布页没能打开（系统浏览器可能被拦）。地址：" + RELEASES_LATEST_URL, "warn");
    }
  }).catch((err) => {
    settingsStatus("发布页没能打开：" + msg(err) + "。地址：" + RELEASES_LATEST_URL, "warn");
  });
});
$("#btn-settings-data").addEventListener("click", () => {
  settingsStatus("");
  api("open_data").catch((err) => {
    settingsStatus("打不开数据目录：" + msg(err) + "。可以先再试一次，或在系统里手动打开本机数据目录。", "bad");
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
  /* 任意弹窗（说明/设置/停止确认）打开时：Enter/Esc 只属于弹窗——
   * 不穿透到背景开始计划、不清空选择、不误跳引导；停止确认弹窗的
   * Esc/「继续等待」=不停止 的原生语义不变 */
  if ((event.key === "Enter" || event.key === "Escape") &&
      ["#help", "#settings", "#dlg-stop"].some((sel) => { const d = $(sel); return !!d && d.open; })) {
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
    const pop = $("#clock-pop");
    if (pop && !pop.hidden) { pop.hidden = true; return; }
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
