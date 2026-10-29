/* 刷新用例：偏好落地、计划快照摄入、check/list 双分支收口与列表合并。 */
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
  /* 重试策略偏好：只认白名单值，脏数据回落默认（120 秒 / 不限）。
   * 白名单与默认值来自 bootstrap 的权威选项（桌面/手机同源），不各写一套。 */
  const policy = S.prefs.retry_policy || {};
  const allowedFor = (S.retryOptions.choices || []).map((item) => String(item.value));
  const fallbackFor = Number(S.retryOptions.default) || 120;
  S.retryPolicy = {
    retry_for_s: allowedFor.indexOf(String(policy.retry_for_s)) >= 0 ? Number(policy.retry_for_s) : fallbackFor,
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
  /* 条款未同意：真实校验与活动列表都是学校网络动作，这一轮不发起；
   * 还没拒绝过就先弹条款，拒绝过或条款状态未知时安静返回——未知状态由
   * 横幅给重试入口，不在每次自动刷新时反复弹窗（按钮本就禁用，此处兜底） */
  if (termsLocked()) {
    if (!S.terms.declined && !S.terms.unknown) openTerms();
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
    syncLoginDependentView();
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
    /* 结论变了：胶囊、时钟、空态与所有倒计时一起收口，不能只改胶囊 */
    syncLoginDependentView();
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
        syncLoginDependentView();
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
  /* 后端条款回绝（terms_required）：按钮可能被脚本、快捷键或旧界面绕过，
   * 这条路径必须把界面拉回条款门禁，而不是只把报错丢进结果框。 */
  if (res.error === "terms_required") {
    if (branch === "check") {
      clearTimeout(S.round.checkTimer);
      S.login.checking = false;
      S.login.result = "unknown";
      S.login.ok = false;
      syncLoginDependentView();
      settleRefreshBranch("check");
    } else {
      clearTimeout(S.round.listTimer);
      S.listState = "error";
      S.listError = (res && res.text) || "活动列表没能开始加载";
      render();
      settleRefreshBranch("list");
    }
    noteTermsRequired(res);
    return;
  }
  if (branch === "check") {
    clearTimeout(S.round.checkTimer);
    S.login.checking = false;
    S.login.result = "unknown";
    S.login.ok = false;
    syncLoginDependentView();
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
