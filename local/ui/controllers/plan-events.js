/* 计划事件用例：plan/task/plan_done 收口、终态呈现与重启停止计划。 */
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

/* 登录尝试前的完整快照：取消 / 超时 / 窗口关闭 / 失败时整份回滚。
 * 只回滚界面用的登录结论，不动 session.json——这次没登成不等于旧凭证失效。 */
