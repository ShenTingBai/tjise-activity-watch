/* 计划/任务事件规约：归属、代次、合并与结论纯投影。 */
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

/* 计划级终态与单场终态集合：终态之后的同计划旧事件只补详情，不回退结论（P1） */
const PLAN_TERMINAL = ["completed", "partial", "failed", "stopped"];
const TASK_TERMINAL = ["success", "failed", "already_signed", "canceled"];

/* 已见过的最大计划接纳代次：屏上计划的代次与会话记录取大（P1） */
function maxPlanGen() {
  const a = Number(S.maxPlanGen);
  const b = Number(S.plan && S.plan.gen);
  return Math.max(Number.isFinite(a) && a > 0 ? a : 0,
                  Number.isFinite(b) && b > 0 ? b : 0);
}

/* 计划事件归属路由（P1）：事件只有属于当前屏上计划，或携带比已见过的更大的
 * plan_generation（后端最终接纳时赋值，新计划的事件可能早于 start_plan 的
 * ack），才允许改当前状态。旧计划迟到的 plan/task/plan_done 只能进历史
 * 对账，不得清运行中计划的忙态、任务、停止入口，也不得改写已完成结论。 */
function planEventOwns(data) {
  const id = String((data && data.plan_id) || "");
  if (!id) return false;
  if (S.plan.id === id) return true;          // 当前屏上计划：同计划按幂等合并
  const gen = Number(data.plan_generation);
  if (!Number.isFinite(gen)) return !S.plan.id;   // 旧格式载荷：只在空屏时接纳
  return gen > maxPlanGen();                  // 更新的接纳（含早于 ack 的新计划）
}

/* 接纳事件时更新会话代次记录，返回事件的代次（无代次返回 0） */
function admitPlanEvent(data) {
  const gen = Number(data && data.plan_generation);
  if (!Number.isFinite(gen)) return 0;
  S.maxPlanGen = Math.max(S.maxPlanGen || 0, gen);
  return gen;
}

/* 同计划状态推进：已定终态不回退，运行态照常更新（P1） */
function advancePlanStatus(incoming) {
  if (PLAN_TERMINAL.indexOf(S.plan.status) >= 0) return;
  S.plan.status = String(incoming || "ready");
  setBusy(["ready", "running"].indexOf(S.plan.status) >= 0);
}

function handlePlan(payload) {
  const data = payload || {};
  if (!planEventOwns(data)) return;      /* 旧计划迟到事件不替换当前计划（P1） */
  const gen = admitPlanEvent(data);
  if (!S.plan.id || S.plan.id !== String(data.plan_id)) {
    S.plan = { id: String(data.plan_id || ""), gen: gen, status: String(data.status || "ready"), tasks: new Map(), armedAt: Date.now() / 1000 };
    setBusy(["ready", "running"].indexOf(S.plan.status) >= 0);
  } else {
    if (gen > (Number(S.plan.gen) || 0)) S.plan.gen = gen;
    advancePlanStatus(data.status);
  }
  /* 晚到 plan 事件经 mergeTask 合并，不抹已收到的提交次数等 telemetry */
  (data.tasks || []).forEach((task) => {
    S.plan.tasks.set(String(task.task_id),
                     mergeTask(S.plan.tasks.get(String(task.task_id)) || null, task));
  });
  render();
}

function handleTask(payload) {
  const data = payload || {};
  if (!planEventOwns(data)) return;           // 旧计划的场次事件：不改当前计划（P1）
  const gen = admitPlanEvent(data);
  if (!S.plan.id || S.plan.id !== String(data.plan_id)) {
    S.plan = { id: String(data.plan_id || ""), gen: gen, status: "running", tasks: new Map(), armedAt: Date.now() / 1000 };
  } else if (gen > (Number(S.plan.gen) || 0)) {
    S.plan.gen = gen;
  }
  const prev = S.plan.tasks.get(String(data.task_id)) || null;
  const task = mergeTask(prev, data);
  /* 已结束的场次不得被旧进度拖回运行态：状态保持先到的事实（P1） */
  if (prev && TASK_TERMINAL.indexOf(String(prev.status)) >= 0
      && TASK_TERMINAL.indexOf(String(task.status)) < 0) {
    task.status = prev.status;
  }
  S.plan.tasks.set(String(data.task_id), task);
  if (["success", "failed", "already_signed", "canceled"].indexOf(task.status) >= 0) {
    /* 报名成功是已确认事实：任务卡、列表、计数、选择门禁和冲突基线同一时刻更新 */
    if (noteSignedTasks([task])) scheduleSignedRefresh();
    showTaskResult(task);
  }
  render();
}

/* 计划级结论分类：协调器的 counts.failed 把「结果未确认」也算进失败，
 * 但对用户这是两种不同的事实——这里按逐任务 stop_reason 单列，
 * 任务卡、总结、大屏和 LED 共用这一份口径。 */
function planOutcome() {
  const counts = { success: 0, already_signed: 0, failed: 0, canceled: 0,
                   unconfirmed: 0, auth_unavailable: 0, unknown: 0 };
  S.plan.tasks.forEach((task) => {
    const state = String(task.status || "");
    if (state === "success") counts.success += 1;
    else if (state === "already_signed") counts.already_signed += 1;
    else if (state === "canceled") counts.canceled += 1;
    else if (state === "failed") {
      if (task.stop_reason === "unconfirmed") counts.unconfirmed += 1;
      else if (task.stop_reason === "auth_unavailable") counts.auth_unavailable += 1;
      else counts.failed += 1;
    }
  });
  counts.unknown = counts.unconfirmed + counts.auth_unavailable;
  return counts;
}

/* 未确认类别的统一标签：任务数、总结标题、大屏与 LED 必须同一个数字口径。
 * 两种未知同时存在时说「需确认」，不把没提交的场次混进「待核对」。 */
function unknownLabel(outcome) {
  if (!outcome || !outcome.unknown) return "";
  if (outcome.unconfirmed && outcome.auth_unavailable) {
    return "有 " + outcome.unknown + " 场需确认";
  }
  if (outcome.unconfirmed) return "有 " + outcome.unconfirmed + " 场待核对";
  return "有 " + outcome.auth_unavailable + " 场未提交";
}

/* 计划终态的唯一呈现入口：plan_done 事件与 F5/bootstrap 恢复共用一份，
 * 这样「实时结束」和「重载后恢复」不会出现两套结论（FR05）。 */
