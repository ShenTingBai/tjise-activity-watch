/* 任务展示纯模型：状态归类、阶段/文案/细节投影（无 DOM）。 */
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
  const at = Number.isFinite(now) ? now : bnowS();
  const open = toTs(task.sign_start);
  const mode = String(task.signup_mode || "");
  /* 立即模式本来就不等开放：时间不可读时也不能说成「等待开放」 */
  if (at == null) return mode === "instant_signup" ? "preparing" : "preopen";
  /* 开放时间未知时只能「等」：不能因为算不出时间就说已经开放 */
  if (open === null) return mode === "instant_signup" ? "preparing" : "preopen";
  if (open > at) return "preopen";
  return "preparing";
}

/* 任务卡的阶段标签：raw「等待开放」在已开放时是假话，按显示阶段改写 */
function taskDisplayLabel(task) {
  const phase = taskDisplayPhase(task);
  if (phase === "preopen") return "等待开放";
  if (phase === "preparing") return "准备报名";
  if (phase === "retrying") return "提交中";
  const state = String(task && task.status || "queued");
  if (state === "failed") {
    /* 结果没确认 / 登录状态未知都不是「失败」：标签必须留出核对空间 */
    if (task.stop_reason === "unconfirmed") return "待核对";
    if (task.stop_reason === "auth_unavailable") return "未提交";
  }
  return taskLabel(state);
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
  const now = bnowS();
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
