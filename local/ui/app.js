"use strict";

const $ = (selector) => document.querySelector(selector);

const DEFAULT_HELP = {
  title: "天软活动报名",
  subtitle: "选好多场活动，开放时按批次自动提交",
  steps: [
    { no: 1, title: "先登录", text: "点右上角「登录」，在弹出的 Chrome 里登录一次。登录态过期后点同一个按钮重登。" },
    { no: 2, title: "选活动", text: "把想报的场次加入计划。活动时间重叠时只能保留其中一场；同刻开放但不重叠的场次可以一起提交。" },
    { no: 3, title: "保持在线", text: "电脑别睡眠、别断网；窗口可以最小化，但不要关掉。每场活动都会单独显示结果。" },
  ],
  note: "只给本人账号使用，不要借别人的账号；同一计划由本机统一协调。",
};

const S = {
  items: [],
  byId: new Map(),
  rowEls: new Map(),
  selected: new Set(),
  selectionNotice: "",
  filter: { q: "", status: "all", time: "all", sort: "open", checkin: "all", checkout: "all" },
  listState: "idle",
  login: { hours: null, ok: null },
  busy: false,
  plan: { id: null, status: "idle", tasks: new Map() },
  prefs: {},
  logOpen: false,
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
  if (info) {
    applyPrefs(info.prefs || {});
    setLogin(info.login && info.login.hours_left, null);
    setBusy(!!info.busy);
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
  $("#q").value = S.filter.q;
  $("#time").value = S.filter.time;
  $("#sort").value = S.filter.sort;
  $("#checkin").value = S.filter.checkin;
  $("#checkout").value = S.filter.checkout;
  $("#headed").checked = !!S.prefs.headed;
  paintChips();
  setLogOpen(S.logOpen, true);
}

function refresh(quiet) {
  if (!hasBridge()) {
    if (!quiet) setResult({ ok: false, title: "还没有连接本机脚本", text: "请从图形界面启动，再点「刷新」拉活动列表。" });
    return;
  }
  S.listState = "loading";
  render();
  api("check").catch((err) => addLog("[X] 检查登录态失败：" + msg(err)));
  api("list_activities").catch((err) => {
    S.listState = "error";
    render();
    if (!quiet) setResult({ ok: false, title: "活动列表没拉下来", text: failHint(msg(err)) });
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
    if (!quiet) setResult({ ok: false, title: "活动列表没拉下来", text: failHint(res.text || "未知原因") });
    return;
  }
  S.items = (res.items || []).map(normalize);
  S.byId = new Map(S.items.map((item) => [String(item.id), item]));
  Array.from(S.selected).forEach((id) => { if (!S.byId.has(String(id))) S.selected.delete(id); });
  S.listState = "ready";
  render();
}

function render() {
  const rows = filtered();
  paintRows(rows);
  paintEmpty(rows);
  paintCount(rows);
  paintRail();
  paintSelected();
  paintPlan();
  updateControls();
  tick();
}

function paintCount(rows) {
  $("#count").textContent = S.listState === "loading" ? "正在刷新…" : (rows.length + " / " + S.items.length + " 场");
}

function paintRail() {
  const free = S.items.filter((item) => !item.signed).length;
  $("#metric-total").textContent = S.items.length ? String(S.items.length) : "—";
  $("#metric-free").textContent = S.items.length ? String(free) : "—";
  const next = nextOpen();
  $("#metric-next").textContent = next.openNow ? "已开放" : (next.best === null ? "待公布" : fmt(next.best));
  let text = "先选活动，系统会检查举办时间冲突。";
  if (S.busy && S.plan.id) text = "计划运行中 · 每场任务独立显示结果。";
  else if (S.plan.status === "partial") text = "计划部分完成 · 失败的场次可以单独查看。";
  else if (S.plan.status === "completed") text = "计划已完成 · 建议去活动页确认一眼。";
  else if (S.plan.status === "stopped") text = "计划已停止 · 已提交的场次保留原状态。";
  else if (S.selected.size) text = "已选 " + S.selected.size + " 场 · 先看右侧冲突提示。";
  $("#rail-message").textContent = text;
}

function paintRows(rows) {
  const body = $("#rows");
  S.rowEls = new Map();
  body.innerHTML = rows.map(rowHtml).join("");
  rows.forEach((item) => {
    const id = String(item.id);
    const tr = body.querySelector('tr[data-id="' + cssEscape(id) + '"]');
    if (!tr) return;
    S.rowEls.set(id, {
      row: tr,
      check: tr.querySelector(".row-check"),
      left: tr.querySelector(".state-left"),
      state: tr.querySelector(".state-cell"),
    });
  });
}

function rowHtml(item) {
  const id = String(item.id);
  const state = stateOf(item);
  const selected = S.selected.has(id);
  const invalid = !validActivityTime(item);
  const conflict = conflictIds().has(id);
  const flags = requirementFlags(item);
  const classes = ["activity-row"];
  if (selected) classes.push("is-selected");
  if (item.signed) classes.push("is-signed");
  if (invalid) classes.push("is-unknown");
  if (conflict) classes.push("is-conflict");
  const left = leftOf(item);
  const leftText = left === null ? "时间待公布" : (left <= 0 ? "已开放" : fmt(left));
  const actText = activityTimeText(item);
  const checkDisabled = item.signed || S.busy;
  return "<tr class=\"" + classes.join(" ") + "\" data-id=\"" + esc(id) + "\">" +
    "<td class=\"c-select\"><input class=\"row-check\" type=\"checkbox\" data-id=\"" + esc(id) + "\"" + (selected ? " checked" : "") + (checkDisabled ? " disabled" : "") + " aria-label=\"选择" + esc(item.name) + "\"></td>" +
    "<td class=\"c-left state-left\" title=\"" + esc(item.sign_start || "时间待公布") + "\">" + esc(leftText) + "</td>" +
    "<td class=\"c-time\" title=\"" + esc(item.sign_start || "时间待公布") + "\">" + esc(shortTime(item.sign_start) || "待公布") + "</td>" +
    "<td class=\"c-state state-cell\">" + tag(state) + "</td>" +
    "<td class=\"c-name\"><strong class=\"activity-main\" title=\"" + esc(item.name) + "\">" + esc(item.name) + "</strong><small class=\"activity-time" + (invalid ? " is-unknown" : "") + "\">" + esc(actText) + flags + "</small></td>" +
    "<td class=\"c-place\" title=\"" + esc(item.place || "地点待公布") + "\">" + esc(item.place || "地点待公布") + "</td>" +
    "</tr>";
}

function paintEmpty(rows) {
  const empty = $("#empty");
  const title = $("#empty-title");
  const text = $("#empty-text");
  const action = $("#empty-action");
  if (rows.length || S.listState === "loading") {
    empty.hidden = true;
    return;
  }
  empty.hidden = false;
  action.hidden = false;
  if (S.listState === "error") {
    title.textContent = "活动列表没拉下来";
    text.textContent = "先检查登录态或网络，再点「重试」。";
    action.textContent = "重试";
    return;
  }
  if (S.items.length) {
    title.textContent = "没有符合条件的活动";
    text.textContent = "换个关键词，或清除筛选看完整列表。";
    action.textContent = "清除筛选";
    return;
  }
  title.textContent = "这里还没有活动";
  text.textContent = "点「刷新」拉一次；提示登录态失效就先点「登录」。";
  action.textContent = "刷新";
}

function paintSelected() {
  const box = $("#selected");
  const items = selectedItems();
  if (!items.length) {
    box.className = "selected-empty";
    box.innerHTML = "还没选活动<span>在左侧勾选一场或多场</span>";
    return;
  }
  const groups = groupSelected(items);
  const summary = groups.map((group) => shortTime(group.key) + " 同时提交 " + group.items.length + " 场").join(" · ");
  const list = items.slice(0, 4).map((item) => "<span>" + esc(item.name) + "</span>").join("");
  const extra = items.length > 4 ? "<span>还有 " + (items.length - 4) + " 场…</span>" : "";
  box.className = "selected-set";
  box.innerHTML = "已选 " + items.length + " 场<span>" + esc(summary) + "</span><div class=\"selected-list\">" + list + extra + "</div>";
}

function paintPlan() {
  const conflictBox = $("#plan-conflicts");
  const validation = validateSelection();
  const messages = [];
  if (S.selectionNotice) messages.push("<div class=\"conflict-item\"><strong>选择提醒</strong><br>" + esc(S.selectionNotice) + "</div>");
  validation.errors.forEach((error) => messages.push("<div class=\"conflict-item\"><strong>" + esc(error.title) + "</strong><br>" + esc(error.text) + "</div>"));
  validation.conflicts.forEach((conflict) => messages.push("<div class=\"conflict-item\"><strong>活动时间重叠</strong><br>「" + esc(conflict.left.name) + "」与「" + esc(conflict.right.name) + "」只能选其中一场。</div>"));
  conflictBox.innerHTML = messages.join("");
  conflictBox.hidden = messages.length === 0;

  const tasksBox = $("#plan-tasks");
  const tasks = Array.from(S.plan.tasks.values());
  tasksBox.innerHTML = tasks.length ? tasks.map(taskHtml).join("") : "";
  const countDown = $("#countdown");
  const selected = selectedItems();
  if (!selected.length) countDown.textContent = "待选";
  else {
    const groups = groupSelected(selected);
    const first = groups[0];
    const left = leftOf(first.items[0]);
    countDown.textContent = left === null ? "时间待公布" : (left <= 0 ? "现在开放 · " : fmt(left) + " · ") + "同时提交 " + first.items.length + " 场";
  }
}

function taskHtml(task) {
  const state = String(task.status || "queued");
  const label = taskLabel(state);
  const cls = taskClass(state);
  const detail = task.reason || task.detail || (state === "waiting" ? "等待报名开放" : "");
  const canCancel = ["queued", "waiting"].indexOf(state) >= 0 && S.busy;
  return "<div class=\"task-row\" data-task-id=\"" + esc(task.task_id) + "\"><div><strong class=\"task-name\" title=\"" + esc(task.name) + "\">" + esc(task.name) + "</strong><span class=\"task-detail\">" + esc(detail) + "</span></div><div class=\"task-actions\"><span class=\"task-state " + cls + "\">" + esc(label) + "</span>" + (canCancel ? "<button class=\"task-cancel\" type=\"button\" data-task-id=\"" + esc(task.task_id) + "\">取消</button>" : "") + "</div></div>";
}

function updateControls() {
  $("#btn-login").disabled = S.busy;
  $("#btn-refresh").disabled = S.listState === "loading";
  const validation = validateSelection();
  const canStart = !S.busy && S.selected.size > 0 && validation.errors.length === 0 && validation.conflicts.length === 0 && S.listState === "ready";
  $("#btn-plan").disabled = !canStart;
  $("#btn-plan").textContent = S.busy ? "计划运行中" : "开始报名计划";
  $("#btn-stop").disabled = !S.busy || !S.plan.id;
  $("#btn-clear-plan").disabled = S.busy || (!S.selected.size && !S.plan.tasks.size);
  document.querySelectorAll(".row-check").forEach((check) => {
    const item = findItem(check.dataset.id);
    check.disabled = S.busy || !!(item && item.signed);
  });
}

function startPlan() {
  if (S.busy) return;
  const validation = validateSelection();
  if (!S.selected.size) {
    setResult({ ok: false, title: "还没选活动", text: "在左侧勾选至少一场未报名活动。" });
    return;
  }
  if (validation.errors.length || validation.conflicts.length) {
    paintPlan();
    setResult({ ok: false, title: "计划还不能开始", text: "先处理右侧的时间提示，再开始报名计划。" });
    return;
  }
  const items = selectedItems().map((item) => ({
    id: item.id,
    name: item.name,
    sign_start: item.sign_start || "",
    act_start: item.act_start || "",
    act_end: item.act_end || "",
    signed: !!item.signed,
  }));
  setResult(null);
  api("start_plan", items, $("#headed").checked).then((res) => {
    if (!res || res.ok === false) {
      setBusy(false);
      showPlanError(res && res.text, res && res.conflicts);
      return;
    }
    S.plan = { id: String(res.plan_id || ""), status: "ready", tasks: new Map() };
    setBusy(true);
    addLog("已安排 " + items.length + " 场活动，等待各自开放时间。");
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
  api("stop_plan", S.plan.id).then((res) => {
    if (res && res.ok === false) addLog(res.text || "当前没有这个计划");
  }).catch((err) => addLog("[X] 停止计划失败：" + msg(err)));
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

function handleDone(payload) {
  const data = payload || {};
  if (data.kind === "login") {
    setBusy(false);
    if (data.ok) {
      setLogin(null, true);
      setResult({ ok: true, title: "登录成功", text: "正在刷新活动列表。" });
      doCheck();
      refresh(true);
    } else {
      setLogin(null, false);
      setResult({ ok: false, title: "登录没成功", text: failHint(data.reason || "登录窗口没有完成") });
    }
    return;
  }
  if (data.kind === "grab") {
    setBusy(false);
    if (data.ok) setResult({ ok: true, title: "报名成功：" + stripLabel(data.label), text: "建议去活动页确认一眼。" });
    else setResult({ ok: false, title: "没抢到：" + cleanReason(data.reason), text: grabHint(data.reason) });
  }
}

function handlePlan(payload) {
  const data = payload || {};
  if (!S.plan.id || S.plan.id !== String(data.plan_id)) S.plan = { id: String(data.plan_id || ""), status: data.status || "ready", tasks: new Map() };
  S.plan.status = String(data.status || "ready");
  (data.tasks || []).forEach((task) => S.plan.tasks.set(String(task.task_id), Object.assign({}, task)));
  setBusy(["ready", "running"].indexOf(S.plan.status) >= 0);
  render();
}

function handleTask(payload) {
  const data = payload || {};
  if (!S.plan.id || S.plan.id !== String(data.plan_id)) S.plan = { id: String(data.plan_id || ""), status: "running", tasks: new Map() };
  const task = Object.assign({}, S.plan.tasks.get(String(data.task_id)) || {}, data);
  S.plan.tasks.set(String(data.task_id), task);
  if (["success", "failed", "already_signed", "canceled"].indexOf(task.status) >= 0) showTaskResult(task);
  render();
}

function handlePlanDone(payload) {
  const data = payload || {};
  S.plan.id = String(data.plan_id || S.plan.id || "");
  S.plan.status = String(data.status || "failed");
  setBusy(false);
  const summary = data.summary || {};
  const counts = summary.counts || {};
  if (S.plan.status === "completed") setResult({ ok: true, title: "报名计划完成", text: "成功 " + ((counts.success || 0) + (counts.already_signed || 0)) + " 场，建议去活动页确认一眼。" });
  else if (S.plan.status === "partial") setResult({ ok: "partial", title: "报名计划部分完成", text: "成功 " + (counts.success || 0) + " 场，失败或取消 " + ((counts.failed || 0) + (counts.canceled || 0)) + " 场。" });
  else if (S.plan.status === "stopped") setResult({ ok: "partial", title: "报名计划已停止", text: "已提交的场次保留原状态，尚未提交的场次已取消。" });
  else setResult({ ok: false, title: "报名计划没有成功", text: "逐场原因已列在右侧任务状态里。" });
  render();
}

function showTaskResult(task) {
  const name = task.name || "这一场活动";
  if (task.status === "success") setResult({ ok: true, title: "报名成功：" + name, text: "建议去活动页确认一眼。" });
  else if (task.status === "already_signed") setResult({ ok: "partial", title: "已经报过：" + name, text: "这场保留为已报名，其他场次继续。" });
  else if (task.status === "canceled") setResult({ ok: "partial", title: "已取消：" + name, text: "其他场次继续按计划处理。" });
  else setResult({ ok: false, title: "没抢到：" + cleanReason(task.reason), text: grabHint(task.reason) });
}

function setLogin(hours, ok) {
  S.login.hours = numOrNull(hours);
  S.login.ok = ok;
  paintLogin();
}

function paintLogin() {
  const pill = $("#pill");
  const detail = $("#status-detail");
  const h = S.login.hours;
  const ok = S.login.ok;
  let cls = "pill";
  let text = "未登录";
  let note = "点右上角「登录」，在弹出的 Chrome 里登一次";
  let state = "none";
  if (S.busy) {
    cls += " is-busy";
    text = "正在处理";
    note = "计划正在运行，窗口别关";
    state = "busy";
  } else if (ok === false || (typeof h === "number" && h <= 0)) {
    cls += " is-bad";
    text = "登录态失效";
    note = "点右上角「登录」重登一次，再刷新列表";
    state = "bad";
  } else if (typeof h === "number") {
    if (h <= 2) {
      cls += " is-warn";
      text = "即将过期 · 约剩 " + leftText(h);
      note = "不到 2 小时，建议现在先重登一次";
      state = "warn";
    } else {
      cls += " is-ok";
      text = "有效 · 约剩 " + leftText(h);
      note = "登录态够用一阵子，过期后重登一次就行";
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
  pill.innerHTML = "<i aria-hidden=\"true\"></i>" + esc(text);
  detail.textContent = note;
}

function setBusy(busy) {
  S.busy = !!busy;
  paintLogin();
  updateControls();
}

function setResult(result) {
  const el = $("#result");
  if (!result) {
    el.hidden = true;
    el.textContent = "";
    el.className = "result";
    return;
  }
  el.hidden = false;
  const cls = result.ok === true ? "is-ok" : (result.ok === "partial" ? "is-partial" : "is-bad");
  el.className = "result " + cls;
  el.innerHTML = "<strong>" + esc(result.title || "") + "</strong><span>" + esc(result.text || "") + "</span>";
}

function addLog(line) {
  const box = $("#log");
  const stamp = new Date().toTimeString().slice(0, 8);
  box.textContent += stamp + "  " + String(line == null ? "" : line) + "\n";
  if (box.textContent.length > 40000) box.textContent = box.textContent.slice(-30000);
  box.scrollTop = box.scrollHeight;
  requestAnimationFrame(() => { box.scrollTop = box.scrollHeight; });
}

function setLogOpen(open, silent) {
  S.logOpen = !!open;
  const card = document.querySelector(".log-card");
  if (card) card.classList.toggle("is-open", S.logOpen);
  $("#btn-log-toggle").textContent = S.logOpen ? "收起" : "展开";
  if (!silent) savePrefs({ log_open: S.logOpen });
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

function filtered() {
  const keyword = S.filter.q.trim().toLowerCase();
  const now = Date.now() / 1000;
  const rows = S.items.filter((item) => {
    if (keyword && (String(item.name) + " " + String(item.place || "")).toLowerCase().indexOf(keyword) < 0) return false;
    if (S.filter.status === "free" && item.signed) return false;
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

function nextOpen() {
  let best = null;
  let openNow = false;
  S.items.forEach((item) => {
    if (item.signed) return;
    const left = leftOf(item);
    if (left === null) return;
    if (left <= 0) openNow = true;
    else if (best === null || left < best) best = left;
  });
  return { best: best, openNow: openNow };
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
  if (!item || item.signed) return;
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

function selectAllVisible() {
  filtered().forEach((item) => {
    if (!item.signed && !S.selected.has(String(item.id))) toggleSelection(item.id, true);
  });
}

function paintChips() {
  document.querySelectorAll(".chip").forEach((chip) => {
    const on = chip.dataset.status === S.filter.status;
    chip.classList.toggle("is-on", on);
    chip.setAttribute("aria-selected", on ? "true" : "false");
  });
}

function setFilter(key, value) {
  S.filter[key] = value;
  savePrefs({ filter: Object.assign({}, S.filter) });
  render();
}

function showPlanError(text, conflicts) {
  if (conflicts && conflicts.length) {
    S.selectionNotice = "后端也发现了活动时段重叠，请取消其中一场后再试。";
    render();
  }
  setResult({ ok: false, title: "计划没能开始", text: text || "先处理右侧提示，再试一次。" });
}

function stateOf(item) {
  if (!item) return "unknown";
  if (item.signed) return "signed";
  const state = String(item.state || "");
  if (state.indexOf("已报名") >= 0 || state === "signed") return "signed";
  if (state.indexOf("开放中") >= 0 || state === "open") return "open";
  if (state.indexOf("未开放") >= 0 || state === "waiting") return "waiting";
  const left = leftOf(item);
  if (left === null) return "unknown";
  return left > 0 ? "waiting" : "open";
}

function tag(state) {
  if (state === "signed") return '<span class="state-tag state-signed">已报名</span>';
  if (state === "open") return '<span class="state-tag state-open">开放中</span>';
  if (state === "waiting") return '<span class="state-tag state-wait">未开放</span>';
  return '<span class="state-tag state-unknown">待公布</span>';
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
  return shortTime(item.act_start) + " – " + shortTime(item.act_end);
}

function tick() {
  S.rowEls.forEach((els, id) => {
    const item = findItem(id);
    if (!item || !els.left) return;
    const left = leftOf(item);
    const text = left === null ? "时间待公布" : (left <= 0 ? "已开放" : fmt(left));
    if (els.left.textContent !== text) els.left.textContent = text;
    els.left.classList.toggle("is-soon", left !== null && left > 0 && left <= 600);
    if (els.state && left !== null && !item.signed) els.state.innerHTML = tag(left <= 0 ? "open" : "waiting");
  });
  paintRail();
  const selected = selectedItems();
  const groups = selected.length ? groupSelected(selected) : [];
  const countdown = $("#countdown");
  if (!groups.length) countdown.textContent = "待选";
  else {
    const left = leftOf(groups[0].items[0]);
    countdown.textContent = left === null ? "时间待公布" : (left <= 0 ? "现在开放 · " : fmt(left) + " · ") + "同时提交 " + groups[0].items.length + " 场";
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
  if (needLogin(raw)) return "登录态失效或还没登录。点「登录」重登一次，再点「刷新」。";
  if (/网络|连接|超时|timeout|连不上|dns|ssl|代理/i.test(raw)) return "连不上学校站点。检查校园网或代理，恢复后点「重试」。";
  return "脚本没跑通。先点「重试」；还不行就看下面的运行日志。";
}

function needLogin(text) { return /登录态|未登录|请先登录|session|401|403|token/i.test(String(text || "")); }
function leftText(hours) { return hours >= 1 ? Math.round(hours) + " 小时" : Math.max(1, Math.round(hours * 60)) + " 分钟"; }
function numOrNull(value) { return typeof value === "number" && isFinite(value) ? value : null; }
function msg(error) { return String((error && error.message) || error || "未知错误"); }

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

function shortTime(value) {
  const match = String(value || "").match(/^\d{4}-(\d{2})-(\d{2}) (\d{2}):(\d{2})/);
  return match ? match[1] + "-" + match[2] + " " + match[3] + ":" + match[4] : "";
}

function esc(value) {
  return String(value == null ? "" : value).replace(/[&<>\"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;" }[character]));
}

function cssEscape(value) {
  return String(value).replace(/(["\\])/g, "\\$1");
}

window.onBackend = {
  log: (line) => addLog(line),
  check: (payload) => {
    const data = payload || {};
    setLogin(data.hours_left, !!data.ok);
    if (!data.ok && data.text) addLog("[X] " + data.text);
    updateControls();
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
  const row = event.target.closest("tr[data-id]");
  if (!row) return;
  const check = row.querySelector(".row-check");
  if (check && !check.disabled) { check.checked = !check.checked; toggleSelection(check.dataset.id, check.checked); }
});
$("#btn-refresh").addEventListener("click", () => refresh(false));
$("#btn-login").addEventListener("click", doLogin);
$("#btn-help").addEventListener("click", showHelp);
$("#btn-open").addEventListener("click", () => api("open_data").catch((err) => addLog("[X] 打不开数据目录：" + msg(err))));
$("#btn-plan").addEventListener("click", startPlan);
$("#btn-stop").addEventListener("click", stopPlan);
$("#btn-clear-plan").addEventListener("click", clearSelection);
$("#plan-tasks").addEventListener("click", (event) => {
  const button = event.target.closest(".task-cancel");
  if (button) cancelTask(button.dataset.taskId);
});
$("#headed").addEventListener("change", (event) => savePrefs({ headed: event.target.checked }));
$("#btn-log-toggle").addEventListener("click", () => setLogOpen(!S.logOpen));
$("#btn-log-clear").addEventListener("click", () => { $("#log").textContent = ""; });
$("#empty-action").addEventListener("click", () => {
  if (S.items.length) { S.filter = { q: "", status: "all", time: "all", sort: "open", checkin: "all", checkout: "all" }; applyPrefs({ filter: S.filter, log_open: S.logOpen, headed: $("#headed").checked }); savePrefs({ filter: S.filter }); render(); }
  else refresh(false);
});

document.addEventListener("keydown", (event) => {
  if (event.key === "F5") { event.preventDefault(); refresh(false); return; }
  if (event.key !== "Enter") return;
  const active = document.activeElement;
  const tagName = active && active.tagName ? active.tagName : "BODY";
  if (["INPUT", "SELECT", "TEXTAREA", "BUTTON", "A"].indexOf(tagName) >= 0 || $("#help").open) return;
  startPlan();
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) return;
  tick();
  if (!hasBridge()) return;
  api("bootstrap").then((info) => {
    if (!info) return;
    if (!!info.busy !== S.busy) setBusy(!!info.busy);
    if (!info.busy) refresh(true);
  }).catch(() => {});
});

setInterval(tick, 1000);
boot().catch((error) => { addLog("[X] 界面初始化失败：" + msg(error)); render(); });
