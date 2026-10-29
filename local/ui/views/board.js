/* 计划板与任务呈现：选中面板、路线/倒计时/LED/横幅、任务卡。 */
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
  if (S.login.loginPending) {
    /* 登录尝试期间 S.busy 也为真，但这不是报名计划：
     * 不能把大屏说成「正在准备报名」，否则用户以为点登录触发了抢报名。 */
    setBoardTag("登录中", "amber", true);
    box.textContent = "正在打开登录窗口";
    boardText.textContent = "在浏览器里登完再回来；中途关掉窗口会保留原来的登录状态。";
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
  /* 缺组件时不给快捷登录入口（点了只会拿到结构化失败）；「刷新」可复查恢复。
   * 条款未同意时同样不给快捷登录（登录是学校网络动作，决策 013）。 */
  const needLogin = S.login.present === false && !S.runtime.error && !termsLocked();
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
  /* 「直到报名截止」但选中场次没给报名截止：老实提示 2 分钟兜底，不假装能一直试 */
  if (S.retryPolicy.retry_for_s === 0) {
    const missing = items.some((item) => !toTs(item.sign_end));
    if (missing) checks.push({ tone: "warn", text: "有场次没提供报名截止：本次最多 2 分钟" });
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
    const rawFor = Number(S.retryPolicy.retry_for_s);
    /* -1 自动延长的初始预算就是 120 秒；0 才是「直到报名截止」；非法值回默认 */
    const retryFor = rawFor === -1 ? RETRY_WINDOW_S
      : (Number.isFinite(rawFor) ? rawFor : RETRY_WINDOW_S);
    const signEnd = toTs(item.sign_end);
    const base = open > now ? open : now;
    let end = Number.isFinite(retryFor) && retryFor > 0 ? base + retryFor
      : (retryFor === 0 && signEnd !== null ? signEnd : base + RETRY_WINDOW_S);
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
  /* 倒计时优先后端已接纳的 deadline：合法扩窗后的权威截止也从这里接管 */
  if (telemetry !== null) return telemetry;
  const now = Number.isFinite(fallbackNow) ? fallbackNow : bnowS();
  const rawFor = task && Number.isFinite(Number(task.retry_for_s)) ? Number(task.retry_for_s) : RETRY_WINDOW_S;
  /* -1 自动延长：初始预算按 120 秒算，扩窗后由上面的 telemetry 接管；
   * 它不是「直到报名截止」——只有 0 才直接取 sign_end */
  const retryFor = rawFor === -1 ? RETRY_WINDOW_S : rawFor;
  const signEnd = task ? toTs(task.sign_end) : null;
  /* 绝对截止不依赖任何起点：先把这条独立路径处理掉，别被 anchor 判空挡住 */
  if (retryFor === 0 && signEnd !== null) return signEnd;
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
  /* 条款门禁（决策 013）：未同意时登录/刷新/开始全部禁用；离线演示不受限 */
  const termsGate = termsLocked();
  /* 缺组件：登录/开始都禁用（点了也是结构化失败）；刷新保持可用——复查组件并恢复的唯一通道 */
  $("#btn-login").disabled = S.busy || componentDown || termsGate;
  $("#btn-refresh").disabled = S.listState === "loading" || termsGate;
  /* 帮助任何状态都可打开；运行中只禁重看引导，原因写在按钮上。 */
  paintReplayButtons();
  const validation = validateSelection();
  /* 开始门禁：真实计划只在学校接口确认登录有效（valid）后可点；
   * checking/unknown/none/expired 一律禁用（demo 模式例外，供视觉演示）。
   * checking 必须显式排除：新一轮检查开始时不清旧 result，
   * 不排除的话上次 valid 会放行尚未确认的检查期。 */
  const loginReady = !!S.demo || (!S.login.checking && S.login.result === "valid");
  const canStart = !S.busy && !S.starting && !componentDown && !termsGate && loginReady && S.selected.size > 0
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
  if (S.terms.unknown) {
    /* 条款状态没读到（bootstrap 失败/缺 terms/版本非法）：停用学校动作并给
     * 唯一的重试入口；不自动弹窗——用户点开的才是可确认的同意动作 */
    parts.push('<div class="banner" data-tone="warn" role="alert"><span class="grow"><b>条款状态没能确认。</b>' +
      "登录、活动列表和报名计划暂时停用；点「查看条款」打开全文并重新确认，确认成功才会恢复。</span>" +
      '<button class="btn btn-ghost" type="button" data-act="terms-open">查看条款</button></div>');
  } else if (termsLocked()) {
    /* 条款未同意：一条横幅说明停用范围，并提供唯一的再同意入口；不强弹 */
    parts.push('<div class="banner" data-tone="warn" role="alert"><span class="grow"><b>还没有同意使用条款。</b>' +
      (S.terms.declined ? "登录、活动列表和报名计划保持停用。" : "同意后才能登录、查看活动列表与开始报名计划。") +
      "</span>" +
      '<button class="btn btn-ghost" type="button" data-act="terms-open">查看条款</button></div>');
  }
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
