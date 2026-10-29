/* 学校粗测偏差与时钟详情浮层。 */
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
