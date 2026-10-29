"use strict";

/* 手机页逻辑：只做配对、8 项操作与恢复对账。
 * 纯规则集中在 PhoneModel（node vm 可直接执行），DOM 层只负责取值与渲染。
 * 全列表一律 textContent，不拼 HTML；session cookie 不进 localStorage。 */

const PhoneModel = {
  /* 请求 ID：crypto.getRandomValues 在 HTTP 页面上也可用，不假定 randomUUID */
  newRequestId() {
    const buf = new Uint8Array(12);
    (globalThis.crypto || {}).getRandomValues
      ? globalThis.crypto.getRandomValues(buf)
      : buf.forEach((_, i) => { buf[i] = Math.floor(Math.random() * 256); });
    return Array.from(buf, (b) => b.toString(16).padStart(2, "0")).join("");
  },

  /* 筛选：关键词 + 报名状态 + 开放时间（今天 / 未来 7 天） */
  filterItems(items, filter, nowS) {
    const q = String((filter && filter.q) || "").trim().toLowerCase();
    const status = (filter && filter.status) || "all";
    const time = (filter && filter.time) || "all";
    return (items || []).filter((item) => {
      if (q) {
        const hay = String(item.name || "") + " " + String(item.place || item.location || "");
        if (hay.toLowerCase().indexOf(q) < 0) return false;
      }
      const signed = !!item.signed;
      if (status === "free" && signed) return false;
      if (status === "signed" && !signed) return false;
      if (time !== "all") {
        const open = Number(item.open_ts || 0);
        if (!(open > 0)) return false;
        const day = 86400;
        if (time === "today" && !(open <= nowS + day)) return false;
        if (time === "week" && !(open <= nowS + 7 * day)) return false;
      }
      return true;
    });
  },

  /* 断线退避：指数增长，上限 15 秒 */
  nextBackoff(currentMs) {
    const cur = Number(currentMs) || 1000;
    return Math.min(15000, Math.max(1000, cur * 2));
  },

  /* 状态过时：5 秒没有新快照就显示离线/未知 */
  staleAfterMs: 5000,
  isStale(lastOkMs, nowMs) {
    if (!lastOkMs) return true;
    return (Number(nowMs) - Number(lastOkMs)) > PhoneModel.staleAfterMs;
  },

  /* 过时/忙/空选都禁开始；停止始终可点 */
  canStart(model) {
    if (!model || PhoneModel.isStale(model.lastOkAt, model.nowMs)) return false;
    if (model.busy) return false;
    return (model.selected || []).length > 0;
  },
  canSubmitRelogin(model, challenge) {
    if (!model || PhoneModel.isStale(model.lastOkAt, model.nowMs)) return false;
    if (!challenge || !challenge.challenge_id) return false;
    return !PhoneModel.challengeExpired(challenge, model.nowMs);
  },

  /* 验证码代次：只有当前 flow/challenge 才能渲染与提交；到期禁提交 */
  challengeExpired(challenge, nowMs) {
    if (!challenge) return true;
    const got = Number(challenge.got_at || 0);
    const left = Number(challenge.expires_in || 0);
    return (Number(nowMs) - got) > left * 1000;
  },
  matchesChallenge(state, flowId, challengeId) {
    return !!state && String(state.flow_id || "") === String(flowId || "")
      && String(state.challenge_id || "") === String(challengeId || "");
  },

  /* 倒计时：优先后端已接纳 deadline；-1 初始 120，0 直达 sign_end（缺则 2 分钟） */
  RETRY_FALLBACK_S: 120,
  taskDeadline(task, nowS) {
    const telemetry = Number(task && task.retry_deadline);
    if (Number.isFinite(telemetry) && telemetry > 0) return telemetry;
    const raw = Number(task && task.retry_for_s);
    const retryFor = raw === -1 ? PhoneModel.RETRY_FALLBACK_S
      : (Number.isFinite(raw) ? raw : PhoneModel.RETRY_FALLBACK_S);
    const signEnd = Number(task && task.sign_end_ts || 0) || null;
    const anchor = Number(task && task.retry_started_at || 0) || null;
    if (retryFor === 0 && signEnd !== null) return signEnd;
    if (anchor === null) return null;
    let deadline = retryFor > 0 ? anchor + retryFor : anchor + PhoneModel.RETRY_FALLBACK_S;
    if (signEnd !== null) deadline = Math.min(deadline, signEnd);
    return deadline;
  },

  /* 0 模式缺截止：老实提示 2 分钟兜底 */
  retryHint(mode, hasSignEnd) {
    if (Number(mode) === 0 && !hasSignEnd) return "没有报名截止时间：本次最多 2 分钟";
    if (Number(mode) === -1) return "自动延长，最多 25 分钟；报名截止仍是硬上限";
    return "";
  },

  /* 未确认 start：同 request_id 只读对账；15 秒内不重复，超时也不清 pending */
  startPending(pending, nowMs) {
    if (!pending) return false;
    return (Number(nowMs) - Number(pending.at || 0)) < 15000 || !pending.done;
  },

  /* 呈现：安全 textContent 文本，不拼标签 */
  itemTitle(item) {
    return String(item && item.name || "未命名活动");
  },
  itemMeta(item) {
    const bits = [];
    if (item && item.sign_start) bits.push("开放 " + item.sign_start);
    if (item && item.sign_end) bits.push("截止 " + item.sign_end);
    if (item && item.signed) bits.push("已报名");
    return bits.join(" · ");
  },
  actionable(item) {
    if (!item || item.signed) return false;
    if (!item.open_ts && !item.sign_start) return false;      // 时间未知不可执行
    if (item.closed) return false;                            // 已截止不可执行
    return true;
  },
  connText(model) {
    if (!model || PhoneModel.isStale(model.lastOkAt, model.nowMs)) return "离线/未知";
    return "已连接";
  },
};

/* 纯状态导出：node vm 行为测试直接跑真实函数，不用搜字符串代替断言 */
if (typeof globalThis !== "undefined") globalThis.PhoneModel = PhoneModel;

/* ---------------- DOM 层 ---------------- */
if (typeof document !== "undefined") {
  const $ = (id) => document.getElementById(id);
  const model = {
    items: [], selected: [], filter: { q: "", status: "all", time: "all" },
    retry: { retry_for_s: 120, max_attempts: 0 }, busy: false,
    lastOkAt: 0, nowMs: 0, backoff: 1000, seq: 0,
    flow: null, challenge: null, pending: null, csrf: "", pollTimer: null,
  };

  function setBanner(text) {
    const el = $("banner");
    el.textContent = text || "";
    el.hidden = !text;
  }
  function canWrite() {
    model.nowMs = Date.now();
    return !PhoneModel.isStale(model.lastOkAt, model.nowMs);
  }
  function refreshControls() {
    model.nowMs = Date.now();
    const stale = PhoneModel.isStale(model.lastOkAt, model.nowMs);
    $("conn").textContent = PhoneModel.connText(model);
    $("conn").dataset.state = stale ? "off" : "ok";
    $("btn-start").disabled = !PhoneModel.canStart(model);
    $("btn-stop").hidden = !model.busy;
    $("btn-cancel-task").hidden = !model.busy;
    $("btn-relogin-submit").disabled = !PhoneModel.canSubmitRelogin(model, model.challenge);
    $("pending-note").hidden = !(model.pending && !model.pending.done);
    $("retry-hint").textContent = PhoneModel.retryHint(
      model.retry.retry_for_s, (model.items || []).some((i) => i.sign_end_ts));
  }

  function renderItems() {
    const list = $("items");
    const shown = PhoneModel.filterItems(model.items, model.filter, model.nowMs / 1000);
    list.textContent = "";
    shown.forEach((item) => {
      const li = document.createElement("li");
      const usable = PhoneModel.actionable(item);
      li.dataset.disabled = usable ? "0" : "1";
      const label = document.createElement("label");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = model.selected.indexOf(item.id) >= 0;
      box.disabled = !usable;
      box.addEventListener("change", () => {
        const at = model.selected.indexOf(item.id);
        if (box.checked && at < 0) model.selected.push(item.id);
        if (!box.checked && at >= 0) model.selected.splice(at, 1);
        refreshControls();
      });
      const span = document.createElement("span");
      const name = document.createElement("div");
      name.className = "name";
      name.textContent = PhoneModel.itemTitle(item);
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = PhoneModel.itemMeta(item);
      span.appendChild(name);
      span.appendChild(meta);
      label.appendChild(box);
      label.appendChild(span);
      li.appendChild(label);
      list.appendChild(li);
    });
    $("empty").hidden = shown.length > 0;
    refreshControls();
  }

  function renderPlan() {
    const plan = model.plan;
    if (!plan) {
      $("plan-line").textContent = "未开始";
      return;
    }
    const tasks = plan.tasks || [];
    const lines = tasks.map((task) => {
      const left = PhoneModel.taskDeadline(task, model.nowMs / 1000);
      return PhoneModel.itemTitle(task) + "：" + String(task.status || "");
    });
    $("plan-line").textContent = lines.join("；") || "计划进行中";
  }

  function addLog(text) {
    const li = document.createElement("li");
    li.textContent = String(text || "");
    $("logs").appendChild(li);
    while ($("logs").childElementCount > 40) $("logs").removeChild($("logs").firstChild);
  }

  /* ---- 接口 ---- */
  async function api(path, options) {
    const opts = Object.assign({ credentials: "same-origin" }, options || {});
    opts.headers = Object.assign({}, opts.headers || {});
    if (opts.body && typeof opts.body !== "string") {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    if (opts.method === "POST" && model.csrf) opts.headers["X-Tjise-Csrf"] = model.csrf;
    const resp = await fetch(path, opts);
    return { status: resp.status, body: await resp.json().catch(() => ({})) };
  }

  async function pair() {
    const code = String($("pair-code").value || "").trim();
    $("pair-error").textContent = "";
    if (!code) { $("pair-error").textContent = "请输入 6 位配对码"; return; }
    const { status, body } = await api("/pair", { method: "POST", body: { code } });
    if (status !== 200 || !body.ok) {
      $("pair-error").textContent = "配对失败：" + String(body.text || body.error || status);
      return;
    }
    model.csrf = String(body.csrf || "");
    $("pair").hidden = true;
    $("app").hidden = false;
    await poll(true);
  }

  /* ---- 轮询：活跃 1 秒 / 隐藏 5 秒，单飞 + 指数退避 ≤15 秒 ---- */
  async function poll(force) {
    if (model.flying) return;                     // 单飞：在途不重发
    model.flying = true;
    try {
      const { status, body } = await api("/api/state?after=" + (force ? 0 : model.seq));
      if (status === 401 || status === 403) { onNeedPair(); return; }
      if (status !== 200) { onPollFail(); return; }
      model.lastOkAt = Date.now();
      model.backoff = 1000;
      ingest(body || {});
    } catch (err) {
      onPollFail();
    } finally {
      model.flying = false;
      schedule();
      refreshControls();
      renderItems();
      renderPlan();
    }
  }
  function onPollFail() {
    model.backoff = PhoneModel.nextBackoff(model.backoff);
  }
  function onNeedPair() {
    $("app").hidden = true;
    $("pair").hidden = false;
  }
  function schedule() {
    if (model.pollTimer) clearTimeout(model.pollTimer);
    const hidden = typeof document !== "undefined" && document.hidden;
    const base = hidden ? 5000 : 1000;
    const wait = model.backoff > base ? model.backoff : base;
    model.pollTimer = setTimeout(() => poll(false), wait);
  }
  function ingest(payload) {
    if (payload.reset) model.seq = 0;
    const state = payload.state || {};
    if (Array.isArray(state.activities)) model.items = state.activities;
    if (state.plan) model.plan = state.plan;
    if (typeof state.busy === "boolean") model.busy = state.busy;
    if (state.retry_options && Array.isArray(state.retry_options.choices)) {
      model.retryOptions = state.retry_options;
      fillRetryOptions(state.retry_options);
    }
    (payload.events || []).forEach((event) => {
      model.seq = Math.max(model.seq, Number(event.seq || 0));
      if (event.kind === "log") addLog((event.payload || {}).text || (event.payload || {}).detail || "");
      if (event.kind === "phone_relogin") ingestRelogin(event.payload || {});
    });
    if (payload.seq) model.seq = Math.max(model.seq, Number(payload.seq));
    if (model.pending && !model.pending.done) void reconcile();
  }

  /* ---- 开始 / 停止 / 取消：未确认 start 用同一 request_id 只读对账 ---- */
  async function startPlan() {
    if (!canWrite()) { setBanner("状态已过时，先刷新再开始"); return; }
    const items = model.selected.map((id) => ({ id }));
    if (!items.length) return;
    if (model.pending && PhoneModel.startPending(model.pending, Date.now())) {
      setBanner("上一次开始还没核对完，请先等结果");
      return;
    }
    const requestId = model.pending && !model.pending.done
      ? model.pending.request_id : PhoneModel.newRequestId();
    model.pending = { request_id: requestId, at: Date.now(), done: false };
    const { body } = await api("/api/action", {
      method: "POST",
      body: { action: "start_plan", args: {
        items, headed: false,
        retry_policy: { retry_for_s: Number(model.retry.retry_for_s),
                        max_attempts: Number(model.retry.max_attempts || 0) },
        request_id: requestId } },
    });
    if (body && body.ok) model.pending.done = true;
    else if (body && body.error) setBanner(String(body.text || body.error));
    refreshControls();
    void poll(true);
  }
  async function reconcile() {
    const requestId = model.pending && model.pending.request_id;
    if (!requestId) return;
    const { body } = await api("/api/action", {
      method: "POST",
      body: { action: "start_request_state", args: { request_id: requestId } },
    });
    if (body && (body.state === "accepted" || body.state === "rejected"
                 || body.state === "canceled")) {
      model.pending.done = true;
      setBanner("");
    }
  }
  async function stopPlan() {
    if (!window.confirm("确定停止这次计划？")) return;
    const planId = model.plan && model.plan.plan_id;
    if (!planId) return;
    await api("/api/action", { method: "POST",
      body: { action: "stop_plan", args: { plan_id: planId } } });
    void poll(true);
  }
  async function cancelTask() {
    if (!window.confirm("确定取消这一场？")) return;
    const planId = model.plan && model.plan.plan_id;
    const task = (model.plan && model.plan.tasks || [])[0];
    if (!planId || !task) return;
    await api("/api/action", { method: "POST",
      body: { action: "cancel_task", args: { plan_id: planId, task_id: task.task_id } } });
    void poll(true);
  }
  async function saveRetry() {
    await api("/api/action", { method: "POST",
      body: { action: "save_retry_policy", args: { retry_policy: {
        retry_for_s: Number(model.retry.retry_for_s),
        max_attempts: Number(model.retry.max_attempts || 0) } } } });
  }
  function fillRetryOptions(options) {
    const sel = $("retry-for");
    sel.textContent = "";
    (options.choices || []).forEach((choice) => {
      const opt = document.createElement("option");
      opt.value = String(choice.value);
      opt.textContent = String(choice.label);
      sel.appendChild(opt);
    });
    sel.value = String(model.retry.retry_for_s);
  }

  /* ---- 重登 ---- */
  function ingestRelogin(info) {
    model.flow = info;
    if (info.state === "waiting_code" && info.challenge_id) {
      model.challenge = {
        flow_id: info.flow_id, challenge_id: info.challenge_id,
        expires_in: info.expires_in, got_at: Date.now(),
      };
      const img = $("captcha");
      if (String(info.image || "").indexOf("data:image/png;base64,") === 0) {
        img.src = info.image;
        img.hidden = false;
      } else {
        img.hidden = true;
        img.removeAttribute("src");
      }
      $("code").value = "";
      $("btn-relogin-submit").hidden = false;
      $("btn-relogin-refresh").hidden = false;
      $("btn-relogin-cancel").hidden = false;
      $("btn-relogin").hidden = true;
    } else if (info.state === "success") {
      $("relogin-note").textContent = "重登成功，正在刷新列表";
      model.challenge = null;
      void poll(true);
    } else if (info.state && info.state !== "waiting_code") {
      $("relogin-note").textContent = "重登结束：" + String(info.safe_error || info.state);
      model.challenge = null;
    }
    refreshControls();
  }
  async function beginRelogin() {
    if (!canWrite()) { setBanner("状态已过时，先刷新再重登"); return; }
    const { body } = await api("/api/action", { method: "POST",
      body: { action: "phone_relogin_begin", args: {} } });
    if (!body || !body.ok) {
      $("relogin-note").textContent = "还不能开始：" + String(
        (body && body.text) || (body && body.error) || "请到电脑上准备账号");
      return;
    }
    $("relogin-note").textContent = "等待验证码…";
    void poll(true);
  }
  async function submitRelogin() {
    const code = String($("code").value || "").trim();
    if (!model.flow || !model.challenge) return;
    if (!PhoneModel.matchesChallenge(model.flow, model.challenge.flow_id,
                                     model.challenge.challenge_id)) {
      $("relogin-note").textContent = "验证码已换代，请看新图";
      $("code").value = "";
      return;
    }
    await api("/api/action", { method: "POST",
      body: { action: "phone_relogin_submit", args: {
        flow_id: model.challenge.flow_id,
        challenge_id: model.challenge.challenge_id, code } } });
    $("code").value = "";
    void poll(true);
  }
  async function cancelRelogin() {
    const flowId = model.flow && model.flow.flow_id;
    await api("/api/action", { method: "POST",
      body: { action: "phone_relogin_cancel", args: { flow_id: flowId || "" } } });
    model.challenge = null;
    $("captcha").hidden = true;
    void poll(true);
  }

  function wire() {
    $("btn-pair").addEventListener("click", () => void pair());
    $("btn-refresh").addEventListener("click", () => void poll(true));
    $("btn-start").addEventListener("click", () => void startPlan());
    $("btn-stop").addEventListener("click", () => void stopPlan());
    $("btn-cancel-task").addEventListener("click", () => void cancelTask());
    $("btn-relogin").addEventListener("click", () => void beginRelogin());
    $("btn-relogin-submit").addEventListener("click", () => void submitRelogin());
    $("btn-relogin-cancel").addEventListener("click", () => void cancelRelogin());
    $("btn-relogin-refresh").addEventListener("click", () => void cancelRelogin());
    $("q").addEventListener("input", () => { model.filter.q = $("q").value; renderItems(); });
    document.querySelectorAll("[data-status]").forEach((btn) => {
      btn.addEventListener("click", () => {
        model.filter.status = btn.dataset.status;
        document.querySelectorAll("[data-status]").forEach((other) =>
          other.setAttribute("aria-pressed", other === btn ? "true" : "false"));
        renderItems();
      });
    });
    document.querySelectorAll("[data-time]").forEach((btn) => {
      btn.addEventListener("click", () => {
        model.filter.time = btn.dataset.time;
        document.querySelectorAll("[data-time]").forEach((other) =>
          other.setAttribute("aria-pressed", other === btn ? "true" : "false"));
        renderItems();
      });
    });
    $("retry-for").addEventListener("change", () => {
      model.retry.retry_for_s = Number($("retry-for").value);
      void saveRetry();
      refreshControls();
    });
    $("max-attempts").addEventListener("change", () => {
      model.retry.max_attempts = Number($("max-attempts").value || 0);
      void saveRetry();
    });
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) void poll(true);       // 回前台拉全量，不重放写动作
    });
    schedule();
    refreshControls();
  }

  globalThis.PhoneUI = { model, poll, wire, renderItems, refreshControls,
                         ingest, startPlan, submitRelogin, beginRelogin };
  if (document.readyState !== "loading") wire();
  else document.addEventListener("DOMContentLoaded", wire);
}
