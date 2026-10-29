/* 计划控制器：开始/对账/接纳/停止、登录触发与 done 结果收口。 */
function startPlan() {
  if (S.busy || S.starting) return;
  /* 条款未同意：报名计划是学校网络动作，拒绝后按钮已禁用，这里只兜底不强弹 */
  if (termsLocked()) {
    if (!S.terms.declined && !S.terms.unknown) openTerms();
    return;
  }
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
      /* 后端条款回绝：不按普通业务失败弹结果框，回到条款门禁 */
      if (res && res.error === "terms_required") {
        noteTermsRequired(res);
        return;
      }
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
  /* 条款未同意：登录是学校网络动作，拒绝后按钮已禁用，这里只兜底不强弹 */
  if (termsLocked()) {
    if (!S.terms.declined && !S.terms.unknown) openTerms();
    return;
  }
  /* 登录是异步尝试：先把当前登录结论整份存下来。取消/超时/窗口关闭/失败
   * 都整份回滚，只显示这次尝试的错误；只有真实成功才作废快照并等新锚点
   * （见 handleDone），不能把仍有效的旧凭证画成「登录已失效」。 */
  S.login.preLoginSnapshot = loginSnapshotState();
  S.login.loginPending = true;
  setResult(null);
  setBusy(true);
  /* setBusy 会把 S.busy 置真；大屏据此会误判「报名计划在跑」，
   * 立刻重画一次，让登录尝试显示成「登录中」而不是「正在准备报名」。 */
  paintSelected();
  addLog("正在打开登录窗口，在弹出的浏览器里登完再回来…");
  api("login").then((res) => {
    if (res && res.demo) {
      /* 演示模式不打开学校登录页：如实说明，不当成登录失败（R20） */
      S.login.loginPending = false;
      S.login.preLoginSnapshot = null;
      setBusy(false);
      setResult({ ok: "partial", title: "演示模式不登录", text: res.text || "演示模式不联网。" });
      return;
    }
    if (!res || res.ok === false) {
      S.login.loginPending = false;
      /* 登录窗口没起来 / 被忙碌或条款拦下：回滚到尝试前的结论，只留错误提示 */
      restoreLoginSnapshotState(S.login.preLoginSnapshot);
      S.login.preLoginSnapshot = null;
      if (res && res.error && RUNTIME_ERRORS[res.error]) {
        /* FB4：登录的同步组件失败只挂横幅与唯一恢复入口，
         * 不再生成「登录没能开始」同义结果框；同时计入本轮刷新故障累计——
         * 刷新尚有分支未返回时，迟到的成功 check/list 不得撤掉这个新横幅 */
        noteRoundError(res.error);
        setBusy(false);
        syncLoginDependentView();
        return;
      }
      setBusy(false);
      syncLoginDependentView();
      setResult({ ok: false, title: "登录没能开始", text: (res && res.text) || "再点一次「登录」试试。" });
    }
  }).catch((err) => {
    S.login.loginPending = false;
    restoreLoginSnapshotState(S.login.preLoginSnapshot);
    S.login.preLoginSnapshot = null;
    setBusy(false);
    syncLoginDependentView();
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
      /* 登录成功=新会话已保存：作废尝试前快照，旧锚点立即清掉；紧接着的校验
       * 事件会带新锚点回来，这两秒里宁可显示未知，也不能拿旧凭证的剩余时间充数。 */
      S.login.preLoginSnapshot = null;
      setLogin(null, true, true, { estimated_expires_at: null, saved_at: null });
      syncLoginDependentView();
      setResult({ ok: true, title: "登录成功", text: "正在加载活动列表。登录剩余时间以学校校验为准。", autoClose: Date.now() + 6000 });
      doCheck();
      refresh(true);
    } else {
      /* 取消 / 超时 / 窗口关闭 / 登录失败：整份回滚到尝试前——旧会话仍然有效，
       * 不能因为这次没登成把胶囊画成「登录已失效」或丢掉剩余时间；
       * 只显示这次尝试的失败说明（present 也随快照回滚，不再单独保留）。 */
      restoreLoginSnapshotState(S.login.preLoginSnapshot);
      S.login.preLoginSnapshot = null;
      syncLoginDependentView();
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
