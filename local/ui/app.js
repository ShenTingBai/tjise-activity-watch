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
  title: "天软活动预报名工具",
  subtitle: "选好多场活动，开放时按批次自动提交",
  steps: [
    { no: 1, title: "先登录", text: "点「登录」完成首次登录。之后重开会自动检查，只有失效才重登。" },
    { no: 2, title: "选活动", text: "把想报的场次加入计划。活动时间重叠时只能保留其中一场；同刻开放但不重叠的场次可以一起提交。" },
    { no: 3, title: "保持在线", text: "电脑别睡眠、别断网；窗口可以最小化，但不要关掉。每场活动都会单独显示结果。" },
  ],
  note: "只给本人账号使用，不要借别人的账号；同一计划由本机统一协调。",
};

/* 与 grab.py / app.py 对齐的常量 */
const EXPIRING_H = 2;        // 登录剩余不到 2 小时转橙色提醒
const TOKEN_LIFETIME_H = 17.5; // app.py TOKEN_LIFETIME_H：登录态寿命估算
const RETRY_WINDOW_S = 120;  // grab.py --window：开放后最多再试 120 秒
const SLEEP_GAP_MS = 120e3;  // 两次心跳间隔超过 2 分钟：多半是电脑睡过去了
const WEEK = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

const S = {
  items: [],
  byId: new Map(),
  rowEls: new Map(),
  rowStates: new Map(),
  selected: new Set(),
  selectionNotice: "",
  filter: { q: "", status: "all", time: "all", sort: "open", checkin: "all", checkout: "all" },
  listState: "idle",
  login: { hours: null, ok: null, expireAt: null, present: null, checking: false, result: null },
  clock: { skew: 0, checked: false },
  online: navigator.onLine !== false,
  sleepGap: 0,
  busy: false,
  stopping: false,
  /* 后端 bootstrap 的 demo 标记：演示模式不要求登录校验 valid 即可开始计划 */
  demo: false,
  plan: { id: null, status: "idle", tasks: new Map() },
  prefs: {},
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
  $("#help-version").textContent = info && info.version ? "版本 " + info.version : "";
  if (info) {
    /* demo 标记由后端 bootstrap 带来：演示模式绕过「登录校验 valid」的开始门禁 */
    S.demo = !!info.demo;
    applyPrefs(info.prefs || {});
    setLogin(info.login && info.login.hours_left, null,
             info.login && "session_present" in info.login ? !!info.login.session_present : undefined);
    setBusy(!!info.busy);
    ingestBootstrapPlan(info);
    /* 第一次打开自动弹一次说明；之后随时从顶栏「说明」打开 */
    if (!S.prefs.welcome_seen) showHelp();
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

function refresh(quiet) {
  if (!hasBridge()) {
    if (!quiet) setResult({ ok: false, title: "还没有连接本机脚本", text: "请从图形界面启动，再点「刷新」拉活动列表。" });
    return;
  }
  S.listState = "loading";
  /* 先进「检查中」：本机有会话也不提前说「登录有效」，等真实校验回来 */
  S.login.checking = true;
  paintLogin();
  render();
  api("check").catch((err) => {
    /* 桥接/脚本异常也必须离开「检查中」：落 unknown 结论并刷新胶囊与按钮，
     * 不能把界面永远停在「正在检查登录态」；下一步是点「刷新」再试。
     * ok 必须同时置 false：旧的 valid/warn 状态不清理的话，
     * paintLogin 的 unknown 分支（要求 ok===false）接不住，顶栏会继续显示「登录有效」。 */
    S.login.checking = false;
    S.login.ok = false;
    S.login.result = "unknown";
    paintLogin();
    updateControls();
    addLog("[X] 检查登录态失败：" + msg(err) + "；点「刷新」再试一次");
  });
  api("list_activities").catch((err) => {
    S.listState = "error";
    render();
    const copy = listErrorCopy(msg(err));
    if (!quiet) setResult({ ok: false, title: copy.title, text: copy.text });
  });
}

function applyActivities(payload, quiet) {
  const res = payload || {};
  if (!res.ok) {
    S.items = [];
    S.byId = new Map();
    S.selected.clear();
    S.listState = "error";
    render();
    const copy = listErrorCopy(res.text || "未知原因");
    if (!quiet) setResult({ ok: false, title: copy.title, text: copy.text });
    return;
  }
  S.items = (res.items || []).map(normalize);
  S.byId = new Map(S.items.map((item) => [String(item.id), item]));
  Array.from(S.selected).forEach((id) => { if (!S.byId.has(String(id))) S.selected.delete(id); });
  S.listState = "ready";
  render();
}

function render() {
  paintList();
  /* 活动列表异步回填后同步更新状态胶囊计数，避免真实数据已到但仍显示初始化的 0。 */
  paintChips();
  paintRail();
  paintSelected();
  paintChecks();
  paintPlan();
  paintRoute();
  paintBanners();
  updateControls();
  tick();
  paintLed();
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
  if (armed) return '<span class="state-tag state-armed">' + esc(taskLabel(String(armed.status || "submitting"))) + "</span>";
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
  if (S.login.present === false || S.login.result === "none") {
    title.textContent = "登录后查看活动";
    text.textContent = "点右上角「登录」，完成后点「刷新」。";
    action.textContent = "刷新";
    return;
  }
  if (S.listState === "error") {
    const copy = listErrorCopy("");
    title.textContent = copy.title;
    text.textContent = copy.text;
    action.textContent = "重试";
    return;
  }
  if (S.items.length) {
    title.textContent = "没有符合筛选的活动";
    text.textContent = "一共 " + S.items.length + " 场，换个关键词，或清除筛选看完整列表。";
    action.textContent = "清除筛选";
    return;
  }
  title.textContent = "这里还没有活动";
  text.textContent = "点「刷新」拉一次；还没登录过就先点「登录」，提示登录态失效就先「重新登录」。";
  action.textContent = "刷新";
}

function paintCount(rows) {
  $("#count").textContent = S.listState === "loading" ? "正在刷新…" : (rows.length + " / " + S.items.length + " 场");
}

/* ---------------------------------------------------------------- 大屏 */
function paintRail() {
  /* 可报名指标排除已截止：与筛选、开始门禁同一份 isEligibleForPlan 口径 */
  const free = S.items.filter(isEligibleForPlan).length;
  $("#metric-total").textContent = S.items.length ? String(S.items.length) : "—";
  $("#metric-free").textContent = S.items.length ? String(free) : "—";
  /* metric-next（LED 标注）统一由 paintLedLabel 维护，这里不再抢写 */
  let text = "先选活动，系统会检查举办时间冲突。";
  if (!S.busy && S.login.present === false) text = "完成登录后选活动。";
  else if (S.busy && S.plan.id) text = "计划运行中 · 每场任务独立显示结果。";
  else if (S.plan.status === "partial") text = "计划部分完成 · 失败的场次可以单独查看。";
  else if (S.plan.status === "completed") text = "计划已完成 · 建议去活动页确认一眼。";
  else if (S.plan.status === "stopped") text = "计划已停止 · 已提交的场次保留原状态。";
  else if (S.selected.size) text = "已选 " + S.selected.size + " 场 · 先看右侧冲突提示。";
  $("#rail-message").textContent = text;
}

function paintSelected() {
  const box = $("#selected");
  const meta = $("#selected-meta");
  const boardText = $("#board-text");
  const items = selectedItems();
  const next = boardTargetItem();
  const activeTask = Array.from(S.plan.tasks.values()).find((task) => ["waiting", "queued", "submitting"].indexOf(String(task.status)) >= 0);
  const activeItem = activeTask ? (findItem(activeTask.activity_id) || items[0] || null) : null;
  box.className = "b-title";
  meta.innerHTML = "";
  boardText.textContent = "";
  if (!S.busy && S.login.present === false) {
    setBoardTag("首次使用", "off", true);
    box.textContent = "登录后开始";
    boardText.textContent = "活动会在登录完成后自动加载。";
    return;
  }
  if (!S.busy && S.listState === "error" && !S.items.length) {
    setBoardTag("需重试", "red", true);
    box.textContent = "活动暂时没加载";
    boardText.textContent = failHint("");
    return;
  }
  if (S.busy && activeItem) {
    setBoardTag(String(activeTask.status) === "submitting" ? "正在提交" : "等开放", String(activeTask.status) === "submitting" ? "red" : "amber", true);
    box.textContent = activeItem.name;
    meta.innerHTML = boardMetaHtml(activeItem);
    boardText.textContent = "计划运行中 · 每场任务独立显示结果。";
    return;
  }
  if (!items.length) {
    if (next) {
      const openNow = stateOf(next) === "open";
      setBoardTag(openNow ? "开放中" : "下一场", "amber", !openNow);
      box.textContent = next.name;
      meta.innerHTML = boardMetaHtml(next);
      boardText.textContent = openNow ? "这场已经开放，现在可以选中后开始报名计划。" : "在下面的时刻表里选一场，它就会到这块屏上。";
    } else {
      setBoardTag("暂时没有", "off", true);
      box.textContent = "近期没有要开放的活动";
      boardText.textContent = "可能还没公布报名时间，也可能都报过了。过一会儿点「刷新」看看。";
    }
    return;
  }
  const activeTasks = Array.from(S.plan.tasks.values()).filter((task) => ["waiting", "queued", "submitting"].indexOf(String(task.status)) >= 0);
  if (S.busy && activeTasks.some((task) => String(task.status) === "submitting")) setBoardTag("正在提交", "red", true);
  else if (S.busy) setBoardTag("等开放", "amber", true);
  else if (S.plan.status === "completed") setBoardTag("报名成功", "green", true);
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
    boardText.textContent = S.busy ? "计划运行中 · 每场任务独立显示结果。" : "在下面的时刻表里确认活动，再开始报名计划。";
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
  const needLogin = S.login.present === false;
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
  tasksBox.innerHTML = tasks.length ? tasks.map(taskHtml).join("") : "";
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
  const stations = [
    { name: "本次启动", time: runStarted ? routeTime(runStarted) : "待启动", done: runStarted !== null },
    { name: "活动页已打开", time: pageOpened ? routeTime(pageOpened) : "待打开", done: pageOpened !== null },
    { name: "盯按钮", time: watchStarted ? routeTime(watchStarted) : "待发生", done: watchStarted !== null, hot: true },
    { name: "原定开放", time: routeTime(open), done: now >= open, hot: true },
    { name: deadline.name, time: routeTime(deadline.time), done: terminal || now >= deadline.time },
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
  const tasks = Array.from(S.plan.tasks.values());
  const active = tasks.find((task) => ["waiting", "submitting", "queued"].indexOf(String(task.status)) >= 0);
  if (active) {
    const item = findItem(active.activity_id) || selectedItems()[0];
    if (item) return { item, task: active };
  }
  return null;
}

function paintCountdown() {
  const countDown = $("#countdown");
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
  if (S.busy && S.plan.id) {
    const tasks = Array.from(S.plan.tasks.values());
    const submitting = tasks.filter((task) => String(task.status) === "submitting");
    if (submitting.length) {
      const end = retryDeadline(submitting[0]);
      ledSet(fmtLed(Math.max(0, end - Date.now() / 1000)), "amber");
      paintLedLabel("重试窗口到", fmtClock(end));
      return;
    }
    const selected = selectedItems();
    if (selected.length) {
      const groups = groupSelected(selected);
      const left = leftOf(groups[0].items[0]);
      if (left !== null && left > 0) {
        ledSet(fmtLed(left), left <= 10 ? "red" : "amber");
        paintLedLabel("同时提交 " + groups[0].items.length + " 场，开放", fmtClock(Date.now() / 1000 + left));
        return;
      }
      ledSet("00:00:00", "amber");
      paintLedLabel("现在开放", null);
      return;
    }
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
      paintLedLabel("最近所选开放", fmtClock(Date.now() / 1000 + left));
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
    paintLedLabel("最近开放", fmtClock(Date.now() / 1000 + next.best));
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
 * 没有 telemetry 才退回固定窗口估算。taskHtml 与每秒走字共用这一份口径。 */
function submittingDetail(task) {
  const parts = [];
  if (task.attempts) parts.push("第 " + task.attempts + " 次提交");
  if (task.last_reason) parts.push(task.last_reason);
  const deadline = telemetryDeadline(task);
  if (deadline) parts.push("重试到 " + hm(deadline * 1000));
  return parts.length ? parts.join(" · ") : "重试窗口约 " + fmt(Math.max(0, retryDeadline(task) - Date.now() / 1000));
}

function taskHtml(task) {
  const state = String(task.status || "queued");
  const label = taskLabel(state);
  const cls = taskClass(state);
  /* 混合计划每场显示自己的模式：快照带 signup_mode，旧载荷按时间现算 */
  const mode = String(task.signup_mode || modeOf(task));
  const modePrefix = ["queued", "waiting", "submitting"].indexOf(state) >= 0 && modeText(mode)
    ? modeText(mode) + " · " : "";
  let detail = task.reason || task.detail || (state === "waiting" ? (mode === "pre_register" ? "等待报名开放" : "报名已开放，马上开始") : "");
  if (state === "submitting") {
    /* 有 telemetry 就显示第 N 次提交与最近原因，而不是虚构的固定窗口 */
    detail = submittingDetail(task);
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
 * 优先 worker telemetry 的真实截止（epoch 秒）；没有再按任务推导——
 * 未开放场次从 sign_start 起算，已开放场次从 run_started_at/submitSeen 起算，
 * 用任务携带的 retry_for_s（0=直到报名截止，缺 sign_end 回退 120 秒），
 * sign_end 永远截断。不再固定回退「+120 秒」冒充长策略。 */
function effectiveRetryDeadline(task, fallbackNow) {
  const telemetry = telemetryDeadline(task);
  if (telemetry !== null) return telemetry;
  const now = Number.isFinite(fallbackNow) ? fallbackNow : Date.now() / 1000;
  const retryFor = task && Number.isFinite(Number(task.retry_for_s)) ? Number(task.retry_for_s) : RETRY_WINDOW_S;
  const signEnd = task ? toTs(task.sign_end) : null;
  const started = Number(task && task.run_started_at) || Number(task && task.submitSeen) / 1000 || null;
  const open = toTs(task && task.sign_start);
  const retryStart = open !== null && open > now ? open : (started || now);
  let deadline = retryFor > 0 ? retryStart + retryFor : (signEnd !== null ? signEnd : retryStart + RETRY_WINDOW_S);
  if (signEnd !== null) deadline = Math.min(deadline, signEnd);
  return deadline;
}

/* 提交中的重试截止（任务卡与 LED 的旧入口）：已并入统一推导 */
function retryDeadline(task) {
  return effectiveRetryDeadline(task);
}

function updateControls() {
  $("#btn-login").disabled = S.busy;
  $("#btn-refresh").disabled = S.listState === "loading";
  const validation = validateSelection();
  /* 开始门禁：真实计划只在学校接口确认登录有效（valid）后可点；
   * checking/unknown/none/expired 一律禁用（demo 模式例外，供视觉演示）。
   * checking 必须显式排除：新一轮检查开始时不清旧 result，
   * 不排除的话上次 valid 会放行尚未确认的检查期。 */
  const loginReady = !!S.demo || (!S.login.checking && S.login.result === "valid");
  const canStart = !S.busy && loginReady && S.selected.size > 0 && validation.errors.length === 0 && validation.conflicts.length === 0 && S.listState === "ready";
  $("#btn-plan").disabled = !canStart;
  $("#btn-plan").textContent = S.busy ? "计划运行中" : "开始报名计划";
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
  if (S.busy) return;
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
  api("start_plan", items, $("#headed").checked,
      { retry_for_s: S.retryPolicy.retry_for_s, max_attempts: S.retryPolicy.max_attempts }).then((res) => {
    if (!res || res.ok === false) {
      setBusy(false);
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
    setBusy(false);
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
      setBusy(false);
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
      setLogin(null, true, true);
      /* 登录只看退出码；成功后界面自己刷新列表（契约） */
      setResult({ ok: true, title: "登录好了", text: "大约 17 小时内有效，约到 " + expiryText() + "。活动列表正在刷新。", autoClose: Date.now() + 6000 });
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
  if (task.status === "success") setResult({ ok: true, title: "报名成功：" + name, text: "建议去活动页确认一眼。" });
  else if (task.status === "already_signed") setResult({ ok: "partial", title: "已经报过：" + name, text: "这场保留为已报名，其他场次继续。" });
  else if (task.status === "canceled") setResult({ ok: "partial", title: "已取消：" + name, text: "其他场次继续按计划处理。" });
  else {
    /* 终态文案 = 归因建议 + 学校原话 + 到达的停止条件（时长/次数/截止） */
    const bits = [grabHint(task.reason), "原话：" + cleanReason(task.reason)];
    const stopNote = stopReasonText(task.stop_reason);
    if (stopNote && task.stop_reason !== "process_error") bits.push(stopNote);
    setResult({ ok: false, title: "没抢到：" + name, text: bits.join(" ") });
    /* 失败后顺手复查一次登录态和列表：掉了登录的页面只会看到按钮不出现，
     * 复查能把这个真原因翻到登录胶囊上。只查状态，不改写已出的结论。 */
    if (!S.recheckPending) {
      S.recheckPending = true;
      setTimeout(() => { S.recheckPending = false; refresh(true); }, 400);
    }
  }
}

function setLogin(hours, ok, present) {
  S.login.hours = numOrNull(hours);
  S.login.ok = ok;
  /* present 是后端 session_present 的回声：false=从没登录过，true=登录过。
   * 不传（undefined）表示这次事件没说，保留上一次的判断。 */
  if (present !== undefined) S.login.present = !!present;
  /* 按当前时刻估算到期时间，只用于提醒，不参与判断 */
  S.login.expireAt = S.login.hours === null ? null : Date.now() + S.login.hours * 3600000;
  paintLogin();
  if (document.body) {
    paintRail();
    paintSelected();
    paintBoardActions();
  }
}

function expiryText() {
  const at = S.login.expireAt;
  return at === null ? "约 17 小时后" : "约到 " + whenText(at);
}

function paintLogin() {
  const pill = $("#pill");
  const detail = $("#status-detail");
  const h = S.login.hours;
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
  const expireAt = S.login.expireAt;
  if (expireAt !== null) {
    html += '<dl class="kv"><dt>登录约到</dt><dd>' + esc(whenText(expireAt)) + "</dd></dl>";
  }
  html += '<div class="acts"><button class="btn btn-line" type="button" id="clock-pop-close">关闭</button></div>';
  pop.innerHTML = html;
  pop.hidden = false;
  /* 浮层贴在顶栏时钟下方，不出视口 */
  const rect = $("#clock").getBoundingClientRect();
  pop.style.top = Math.round(rect.bottom + 8) + "px";
  pop.style.left = Math.round(Math.max(12, Math.min(window.innerWidth - pop.offsetWidth - 12, rect.right - pop.offsetWidth))) + "px";
  const closeBtn = pop.querySelector("#clock-pop-close");
  if (closeBtn) closeBtn.addEventListener("click", closeClockPop);
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
  if (!dialog.open) dialog.showModal();
  savePrefs({ welcome_seen: true });
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
  paintRail();
  paintCountdown();
  paintLed();
  paintRoute();
  /* 提交中的任务行：每秒走字（与 taskHtml 同一份 telemetry 口径） */
  if (S.plan.tasks.size) {
    S.plan.tasks.forEach((task) => {
      if (String(task.status) !== "submitting") return;
      const row = document.querySelector('.task-row[data-task-id="' + cssEscape(String(task.task_id)) + '"] .task-detail');
      if (row) row.textContent = submittingDetail(task);
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

/* ---------------------------------------------------------------- 事件绑定 */
window.onBackend = {
  log: (line) => addLog(line),
  check: (payload) => {
    const data = payload || {};
    /* 真实校验回来了：解除「检查中」，记下四态结论（valid/none/expired/unknown） */
    S.login.checking = false;
    S.login.result = data.result || null;
    setLogin(data.hours_left, !!data.ok,
             "session_present" in data ? !!data.session_present : undefined);
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
    updateControls();
    paintChecks();
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
$("#empty-action").addEventListener("click", () => {
  if (S.items.length) { S.filter = { q: "", status: "all", time: "all", sort: "open", checkin: "all", checkout: "all" }; applyPrefs({ filter: S.filter, log_open: S.logOpen, headed: $("#headed").checked }); savePrefs({ filter: S.filter }); render(); }
  else refresh(false);
});

document.addEventListener("keydown", (event) => {
  if (event.key === "F5") { event.preventDefault(); refresh(false); return; }
  /* Ctrl+F 或 / 跳到搜索框 */
  const typing = event.target.closest && event.target.closest("input, select, textarea");
  if ((event.ctrlKey && (event.key === "f" || event.key === "F")) || (event.key === "/" && !typing)) {
    event.preventDefault();
    $("#q").focus();
    $("#q").select();
    return;
  }
  if (event.key === "Escape") {
    /* 依次：关浮层 → 退输入 → 收起结论 → 清空选择；弹窗由浏览器自己收 */
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
