/* 列表渲染：render/paintList/行与分组/空态/统计条。 */
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
