/* 仅测试环境加载的 pywebview 替身（QA 服务注入，生产页面原样加载）。
 *
 * 约束：
 * - 方法名与载荷字段先对照生产 Api（local/app.py）与页面消费点；
 * - 未实现的方法直接抛错，绝不提供「万能成功桩」；
 * - 不发任何学校 / GitHub 请求，不保存真实会话，只在内存里记录调用；
 * - QA 通过 window.__QA_STATE 在场景中途切换行为，通过 window.__QA_EMIT 推事件。
 *
 * @author LiXinwen
 */
(function () {
  "use strict";

  const scenario = String(window.__QA_SCENARIO || "valid");
  const calls = (window.__QA_CALLS = []);
  const state = (window.__QA_STATE = window.__QA_STATE || {});

  const DEFAULTS = {
    valid: { check: "valid", list: "ok", start: "accept" },
    unlogged: { check: "none", list: "empty", start: "reject" },
    checking: { check: "slow", list: "slow", start: "accept" },
    unknown: { check: "unknown", list: "fail", start: "reject" },
    empty: { check: "valid", list: "empty", start: "accept" },
    list_fail: { check: "valid", list: "fail", start: "accept" },
    component: { check: "valid", list: "component", start: "reject" },
    demo: { check: "valid", list: "ok", start: "accept", demo: true },
    skipped: { check: "valid", list: "ok", start: "accept" },
    reconcile_hang: { check: "valid", list: "ok", start: "hang" },
    reconcile_lost: { check: "valid", list: "ok", start: "pending" },
    legacy_session: { check: "valid_no_estimate", list: "ok", start: "accept" },
    login_ok: { check: "valid", list: "ok", start: "accept", loginDone: true, loginOk: true },
  }[scenario] || { check: "valid", list: "ok", start: "accept" };

  ["check", "list", "start", "demo", "planId", "busy"].forEach((key) => {
    if (state[key] === undefined && DEFAULTS[key] !== undefined) state[key] = DEFAULTS[key];
  });
  if (state.planId === undefined) state.planId = "qa-plan";
  if (state.busy === undefined) state.busy = false;
  /* 计划接纳代次（P1）：生产 Api 每次最终接纳新计划递增；qa-plan 视为第二次
   * 接纳（代次 2），旧计划 qa-plan-old 携带代次 1。 */
  if (state.planGen === undefined) state.planGen = 2;

  /* 独立北京时间（决策 012）：固定权威 epoch + 页面单调经过时长。
   * 字段形状与生产 BusinessClock.snapshot() 相同：epoch_s 只对应 tick_s，
   * sampled_tick_s 是快照生成时刻，age_s = sampled - tick。
   * 绝不每次用 Date.now 造新 epoch——那会把「旧采样/旧代次/过期」都伪装成
   * 新鲜授时，掩盖真实缺陷。
   * 可注入项：state.clockEpochS（权威基准）、state.clockAgeS（授时年龄）、
   * state.clockUncertaintyS（采样时误差）、state.clockUnavailable（失去授时）、
    * state.clockInitialUnavailable（首校时未完成）、clockSyncStatus（同步阶段）。
   * age>900 由桥按生产规则收成 unavailable。 */
  const QA_EPOCH_S = 1793016000;          /* 2026-10-26 20:00:00 +08:00 */
  const perfNowMs = () => (typeof performance !== "undefined"
    && typeof performance.now === "function")
    ? performance.now() : Date.now();
  const QA_TICK_BASE_S = 1000;                        /* 桥装载时单调 tick 基准 */
  if (state.clockGen === undefined) state.clockGen = 1;
  if (state.clockRound === undefined) state.clockRound = 1;

  function qaTickS() {
    /* 单调读数：tick = 1000 + performance.now()/1000，与生产 GetTickCount64
     * 语义同形（只前进，不随墙钟跳变）。 */
    return 1000 + perfNowMs() / 1000;
  }

  function clockEpochS() {
    const value = Number(state.clockEpochS);
    return Number.isFinite(value) ? value : QA_EPOCH_S;
  }

  /* 业务当前时刻：固定基准 + 桥装载后的单调经过时长。 */
  function clockNowS() {
    return clockEpochS() + (qaTickS() - QA_TICK_BASE_S);
  }

  function clockAgeS() {
    const injected = Number(state.clockAgeS);
    if (Number.isFinite(injected)) return Math.max(0, injected);
    return Math.max(0, qaTickS() - QA_TICK_BASE_S);
  }

  function clockSnapshot(generation) {
    const gen = Number(generation || state.clockGen || 1);
    const sampleTick = qaTickS();
    const age = clockAgeS();
    const fail = state.clockUnavailable === true || state.clockInitialUnavailable === true;
    const syncStatus = state.clockSyncStatus || (state.clockInitialUnavailable === true
      ? "syncing" : (fail ? "failed" : "succeeded"));
    if (fail || age > 900) {
      return { schema: 1, owner_id: "qa-clock", generation: gen,
               state: "unavailable", sync_status: syncStatus, sync_round: state.clockRound,
               epoch_s: null, tick_s: null, sampled_tick_s: null,
               uncertainty_s: 0.0,
               source: state.demo ? "演示时间（离线，非真实授时）" : "qa-ntp",
               age_s: fail ? null : age,
               reason: state.clockInitialUnavailable ? "正在获取北京时间（QA 注入）"
                 : fail ? "暂时无法获取北京时间（QA 注入）"
                 : "锚点超过 900 秒未重校" };
    }
    const stateName = age > 300 ? "holdover" : "ready";
    /* 采样时误差可注入：生产快照的 uncertainty_s 已包含到采样点的漂移，
     * 夹具不得再用「不含 age 的弱化数值」掩盖 UI 重复累计。 */
    const injectedUncertainty = Number(state.clockUncertaintyS);
    const uncertainty = Number.isFinite(injectedUncertainty)
      ? Math.max(0, injectedUncertainty)
      : (0.2 + 100e-6 * age);
    return { schema: 1, owner_id: "qa-clock", generation: gen,
             state: stateName, sync_status: syncStatus, sync_round: state.clockRound,
             /* epoch 与 tick 是同一授时锚点；sampled 只表示文档生成时刻 */
             epoch_s: clockNowS() - age, tick_s: sampleTick - age,
             sampled_tick_s: sampleTick,
             uncertainty_s: uncertainty,
             source: state.demo ? "演示时间（离线，非真实授时）" : "qa-ntp",
             age_s: age,
             reason: stateName === "holdover"
               ? "沿用上次校时（" + Math.round(age) + " 秒未重校）"
               : "北京时间已同步" };
  }

  function planAnnotated(payload) {
    /* 生产 Api 的内部桥接事件统一携带接纳代次：桥必须复刻，否则会掩盖
     * 「旧计划事件不得改写当前计划」的真实归属行为。 */
    return Object.assign({}, payload, { plan_generation: Number(state.planGen) || 1 });
  }

  function record(name, args) {
    calls.push({ name: name, args: args || [], at: Date.now() });
  }

  function pad(n) {
    return String(n).padStart(2, "0");
  }

  /* 学校时间固定按 UTC+8 文本生成：页面按同一口径解析，换本机时区也不漂。
   * 活动时刻基于桥的业务时钟（固定权威 epoch），不随沙箱 Date.now 位移——
   * OS 墙钟注入 ±8h/±30 天时，活动列表仍锚定同一份北京时间。 */
  function schoolText(offsetSec) {
    const d = new Date((clockNowS() + (offsetSec || 0)) * 1000 + 8 * 3600 * 1000);
    return d.getUTCFullYear() + "-" + pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate())
      + " " + pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes()) + ":" + pad(d.getUTCSeconds());
  }

  function item(id, name, openOffset, endOffset, place, extra) {
    return Object.assign({
      id: id,
      name: name,
      sign_start: schoolText(openOffset),
      sign_end: schoolText(endOffset),
      act_start: schoolText(openOffset + 86400),
      act_end: schoolText(openOffset + 86400 + 7200),
      place: place,
      signed: false,
      checkin_required: true,
      checkout_required: false,
    }, extra || {});
  }

  const LONG_PLACE = "天津市西青区宾水西道399号天津市大学软件学院教学实训楼A区四层软件工程实训中心A401多功能报告厅（入场请从东门刷校园卡经连廊上四层，报告厅在最里侧）";

  function items() {
    return [
      item("a-future", "人工智能前沿讲座", 5400, 9000, "学术报告厅 A101"),
      item("a-open", "校园歌手大赛决赛", -120, 7200, LONG_PLACE, { checkin_required: false }),
      item("a-signed", "志愿服务分享会", -3600, 7200, "青年之家", { signed: true }),
      item("a-closed", "已截止的历史活动", -86400, -7200, "老图书馆"),
      /* 学校确实没给开放时间：与「本机暂时没有可信现在」是两种不同情况 */
      { id: "a-nosign", name: "开放时间未公布", sign_start: "", sign_end: "",
        act_start: schoolText(3 * 86400), act_end: schoolText(3 * 86400 + 7200),
        place: "学生活动中心", signed: false,
        checkin_required: true, checkout_required: false },
      item("a-long", "2026年秋季学期大学生创新创业训练计划项目立项申报宣讲暨优秀项目经验分享交流会（第二场）",
           86400, 90000, "线上会议"),
      item("a-same-1", "同名活动", 7200, 10800, "体育馆"),
      item("a-same-2", "同名活动", 10800, 14400, "体育场"),
    ];
  }

  function loginInfo() {
    if (state.demo) {
      /* 生产 demo 的 session_present 是 null：桥必须复刻，否则会掩盖真实缺陷 */
      return { hours_left: null, session_present: null, estimated_expires_at: null,
               saved_at: null };
    }
    const mode = state.check;
    if (mode === "none") {
      return { hours_left: null, session_present: false, estimated_expires_at: null, saved_at: null };
    }
    if (mode === "expired") {
      return { hours_left: 0, session_present: true, estimated_expires_at: null, saved_at: null };
    }
    if (mode === "unknown") {
      return { hours_left: null, session_present: true, estimated_expires_at: null, saved_at: null };
    }
    if (mode === "valid_no_estimate") {
      /* 旧 saved_at 时代登录：学校校验有效但没有任何估算锚点——
       * 界面必须显示有效且不编造「约剩 N 小时」。 */
      return { hours_left: null, session_present: true, estimated_expires_at: null, saved_at: null };
    }
    return { hours_left: 5.5, session_present: true, estimated_expires_at: null, saved_at: null };
  }

  function clockPayload() {
    if (state.check === "unknown") {
      return { source: "http_date", state: "unknown", offset_s: null,
               uncertainty_s: null, rtt_s: null, checked_at: Math.floor(Date.now() / 1000) };
    }
    return { source: "http_date", state: "ok", offset_s: 0, uncertainty_s: 1.2,
             rtt_s: 0.2, checked_at: Math.floor(Date.now() / 1000) };
  }

  function delay(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function checkPayload(rid, mode) {
    if (mode === "none") {
      return { ok: false, result: "none", session_present: false, hours_left: null,
               request_id: rid, text: "本机还没有登录" };
    }
    if (mode === "expired") {
      return { ok: false, result: "expired", session_present: true, hours_left: 0,
               request_id: rid, text: "登录态已过期（4001）" };
    }
    if (mode === "unknown") {
      return { ok: false, result: "unknown", session_present: true, hours_left: null,
               request_id: rid, text: "接口异常：URLError" };
    }
    return { ok: true, result: "valid", session_present: true,
             hours_left: mode === "valid_no_estimate" ? null : 5.5,
             estimated_expires_at: null, saved_at: null,
             request_id: rid, clock: clockPayload() };
  }

  /* 生产 Api.check 只负责排队并立即回执，真实结论走 window.onBackend.check。
   * 桥必须同样分两步，否则界面会把「回执」当成终态而永远停在检查中。 */
  function check(rid) {
    record("check", [rid]);
    if (state.check === "hang") {
      return Promise.resolve({ ok: true, queued: true, request_id: rid });
    }
    if (state.nextCheckSkipped === true) {
      /* 已推过终态但后端仍在 finally 的 owner：真实 Api.check 会回
       * skipped + 原 request_id，若原操作刚好结束还会把终态一起带回来。 */
      state.nextCheckSkipped = false;
      const answer = { ok: true, queued: true, skipped: "已在检查登录态",
                       request_id: state.skipCheckId || "qa-old-check" };
      if (state.skipCheckResult) answer.result = state.skipCheckResult;
      return Promise.resolve(answer);
    }
    if (state.delayCheckMs) {
      /* 旧轮 ack 延迟回来：用于证明它不会收口后来的新一轮 */
      const wait = state.delayCheckMs;
      state.delayCheckMs = 0;
      return delay(wait).then(() => ({ ok: true, queued: true, request_id: rid }));
    }
    const mode = state.check;
    setTimeout(() => {
      window.onBackend.check(checkPayload(rid, mode));
    }, mode === "slow" ? 1200 : 60);
    return Promise.resolve({ ok: true, queued: true, request_id: rid });
  }

  function listPayload(rid, mode) {
    if (mode === "empty") return { ok: true, request_id: rid, items: [] };
    if (mode === "fail") {
      return { ok: false, request_id: rid, text: "接口异常：URLError", runtime_error: null };
    }
    if (mode === "component") {
      return { ok: false, request_id: rid, text: "程序组件缺失",
               runtime_error: "worker_missing" };
    }
    return { ok: true, request_id: rid, items: items() };
  }

  function listActivities(rid) {
    record("list_activities", [rid]);
    if (state.list === "hang") {
      return Promise.resolve({ ok: true, queued: true, request_id: rid });
    }
    if (state.nextListSkipped === true) {
      state.nextListSkipped = false;
      const answer = { ok: true, queued: true, skipped: "已经在拉活动列表",
                       request_id: state.skipListId || "qa-old-list" };
      if (state.skipListResult) answer.result = state.skipListResult;
      return Promise.resolve(answer);
    }
    if (state.delayListMs) {
      const wait = state.delayListMs;
      state.delayListMs = 0;
      return delay(wait).then(() => ({ ok: true, queued: true, request_id: rid }));
    }
    const mode = state.list;
    setTimeout(() => {
      window.onBackend.activities(listPayload(rid, mode));
    }, mode === "slow" ? 1200 : 60);
    return Promise.resolve({ ok: true, queued: true, request_id: rid });
  }

  function startPlan(payload, headed, policy, rid) {
    record("start_plan", [payload, headed, policy, rid]);
    const mode = state.start;
    if (mode === "hang") return new Promise(() => {});
    if (mode === "pending") {
      return Promise.resolve({ ok: true, queued: true, pending: true, request_id: rid });
    }
    if (mode === "reject") {
      return Promise.resolve({ ok: false, text: "已经有一个计划在运行，先停止或等它结束",
                               request_id: rid });
    }
    const ack = { ok: true, queued: true, plan_id: state.planId,
                  plan_generation: Number(state.planGen) || 1, request_id: rid };
    if (state.earlyTerminalBeforeAck) {
      /* 终态早于 ack：先推 plan/task/plan_done（已完成），延迟再回执 ack——
       * 用于证明 startPlan 的迟到 ack 不得把已结束计划重新置忙（P1）。 */
      state.earlyTerminalBeforeAck = false;
      setTimeout(() => {
        window.onBackend.plan(planAnnotated({ plan_id: state.planId,
                                              status: "running", tasks: [] }));
        window.onBackend.task(planAnnotated({
          plan_id: state.planId, task_id: "t1", activity_id: "a-future",
          name: "人工智能前沿讲座", status: "success", stop_reason: "success",
          attempts: 1 }));
        window.onBackend.plan_done(planAnnotated({ plan_id: state.planId,
                                                   status: "completed",
                                                   summary: { total: 1,
                                                              counts: { success: 1 } } }));
      }, 30);
      return delay(state.earlyAckDelayMs || 500).then(() => ack);
    }
    if (state.earlyEventsBeforeAck) {
      /* 合法新计划的事件可能早于 start_plan 的 ack（协调器在另一线程 emit）：
       * 先推计划/任务事件，延迟再回执 ack，用于证明 ack 不清已收任务。 */
      state.earlyEventsBeforeAck = false;
      setTimeout(() => {
        window.onBackend.plan(planAnnotated({ plan_id: state.planId, status: "running",
                                              tasks: [] }));
        window.onBackend.task(planAnnotated({
          plan_id: state.planId, task_id: "t1", activity_id: "a-future",
          name: "人工智能前沿讲座", status: "submitting", attempts: 2 }));
      }, 30);
      return delay(state.earlyAckDelayMs || 400).then(() => ack);
    }
    setTimeout(() => {
      window.onBackend.plan(planAnnotated({ plan_id: state.planId,
                                            status: "running", tasks: [] }));
    }, 60);
    return Promise.resolve(ack);
  }

  function startRequestState(rid) {
    record("start_request_state", [rid]);
    const mode = state.start;
    if (mode === "hang" || state.queryHang === true) return new Promise(() => {});
    if (state.queryState === "rejected") {
      return Promise.resolve({ ok: false, state: "rejected", request_id: rid,
                               text: "本机拒绝：已有计划在运行" });
    }
    if (state.queryState === "finished") {
      return Promise.resolve({
        ok: true, state: "accepted", plan_id: state.planId, request_id: rid,
        plan_generation: Number(state.planGen) || 1,
        running: false, finished: true,
        snapshot: { status: "completed", summary: { total: 1, counts: { success: 1 } },
                    tasks: [{ task_id: "t1", activity_id: "a-future", name: "人工智能前沿讲座",
                              status: "success", stop_reason: "success", attempts: 1 }] },
      });
    }
    if (mode === "pending" || state.queryState === "pending") {
      return Promise.resolve({ ok: true, state: "pending", request_id: rid });
    }
    return Promise.resolve({ ok: true, state: "accepted", plan_id: state.planId,
                             plan_generation: Number(state.planGen) || 1,
                             running: true, request_id: rid });
  }

  function stopPlan(planId) {
    record("stop_plan", [planId]);
    state.busy = false;
    setTimeout(() => {
      window.onBackend.plan_done(planAnnotated({ plan_id: planId, status: "stopped",
                                                 summary: { total: 0, counts: { canceled: 0 } } }));
    }, 40);
    return Promise.resolve({ ok: true, text: "已停止计划" });
  }

  /* 旧计划 qa-plan-old（代次 1）迟到的三类事件：生产 Api 不会转发它们，
   * 这里直接注入 onBackend 是在检验 UI 层的归属路由——B（代次 2）运行中或
   * 已结束时都不得被改写。request_id 来自真实调用参数（qa-plan-old 的对账）。 */
  function injectStalePlanEvents() {
    const old = "qa-plan-old";
    window.onBackend.plan({ plan_id: old, plan_generation: 1, request_id: "rid-old",
                            status: "running", tasks: [] });
    window.onBackend.task({ plan_id: old, plan_generation: 1, request_id: "rid-old",
                            task_id: "t-old", activity_id: "a-signed",
                            name: "志愿服务分享会", status: "success",
                            stop_reason: "success", attempts: 1 });
    window.onBackend.plan_done({ plan_id: old, plan_generation: 1, request_id: "rid-old",
                                 status: "completed",
                                 summary: { total: 1, counts: { success: 1 } } });
  }

  function savePrefs(prefs) {
    record("save_prefs", [prefs]);
    if (state.prefsBusy === true) {
      return Promise.resolve({ ok: false, error: "prefs_busy", text: "设置暂未保存，另一个窗口正在保存数据。" });
    }
    state.savedPrefs = Object.assign({}, state.savedPrefs || {}, prefs);
    return Promise.resolve({ ok: true });
  }

  const methods = {
    front_ready: () => { record("front_ready", []); return Promise.resolve({ ok: true }); },
    bootstrap: () => {
      record("bootstrap", []);
      const info = {
        version: "1.1.1",
        prefs: { welcome_seen: true, tour_version: 2,
                 retry_policy: { retry_for_s: 120, max_attempts: 0 } },
        login: loginInfo(),
        lifecycle: null,
        busy: !!state.busy,
        demo: !!state.demo,
        update_check: null,
        business_clock: clockSnapshot(1),
      };
      if (window.__QA_HELP) info.help = window.__QA_HELP;
      /* 数据准备注入点：给 F5 恢复场景提供计划快照，仍然走真实 boot() 路径 */
      if (window.__QA_PLAN) {
        info.plan = window.__QA_PLAN;
        info.busy = ["ready", "running"].indexOf(String(window.__QA_PLAN.status || "")) >= 0;
      }
      return Promise.resolve(info);
    },
    check: check,
    list_activities: listActivities,
    start_plan: startPlan,
    start_request_state: startRequestState,
    stop_plan: stopPlan,
    cancel_task: (taskId) => { record("cancel_task", [taskId]); return Promise.resolve({ ok: true }); },
    save_prefs: savePrefs,
    login: () => {
      record("login", []);
      if (state.loginDone === true) {
        /* 生产 Api.login 只回执「已排队」；真实结论走 done 事件（kind=login）。
         * state.loginOk/loginReason 控制成功或失败，loginDelayMs 控制耗时。 */
        const ok = state.loginOk !== false;
        const reason = String(state.loginReason || "");
        setTimeout(() => {
          window.onBackend.done({ kind: "login", ok: ok, reason: reason });
        }, Number(state.loginDelayMs) > 0 ? Number(state.loginDelayMs) : 80);
      }
      return Promise.resolve({ ok: true, queued: true });
    },
    open_data: () => { record("open_data", []); return Promise.resolve({ ok: true }); },
    /* 本机快照读取（生产 Api.clock_snapshot 同名）：UI 每 10 秒轮询、
     * visibility 恢复与 business_clock 事件后都会调它；不实现会被
     * pollClockNow 的 catch 判成「暂时无法读取时间状态」。 */
    clock_snapshot: () => {
      record("clock_snapshot", []);
      return Promise.resolve(clockSnapshot(state.clockGen));
    },
    sync_clock: (rid) => {
      record("sync_clock", [rid]);
      state.clockRound += 1;
      state.clockSyncStatus = "syncing";
      return delay(120).then(() => {
        const failed = state.clockSyncFails || state.clockUnavailable || state.clockInitialUnavailable;
        const gen = Number(state.clockGen || 1) + (failed ? 0 : 1);
        state.clockGen = gen;
        state.clockSyncStatus = failed ? "failed" : "succeeded";
        if (window.onBackend && window.onBackend.business_clock) {
          window.onBackend.business_clock(clockSnapshot(gen));
        }
        return { ok: true, queued: true, request_id: rid };
      });
    },
    open_latest_release: () => { record("open_latest_release", []); return Promise.resolve({ ok: true }); },
    check_update: (rid) => {
      record("check_update", [rid]);
      setTimeout(() => {
        window.onBackend.update({ request_id: rid, state: "current",
                                  current: "1.1.1", latest: "1.1.1", url: "",
                                  checked_at: Math.floor(Date.now() / 1000) });
      }, 120);
      return Promise.resolve({ ok: true, queued: true, request_id: rid });
    },
  };

  const api = new Proxy(methods, {
    get(target, prop) {
      if (typeof prop !== "string") return target[prop];
      if (prop in target) return target[prop];
      throw new Error("runtime_bridge: 未实现的方法 " + prop);
    },
  });

  window.pywebview = { api: api };
  window.__QA_EMIT = (kind, payload) => window.onBackend[kind](payload);
  window.__QA_EMIT_STALE = injectStalePlanEvents;
  /* 权威业务时刻（秒）：QA 断言用它对照 DOM 时钟，不硬编码具体日期。
   * Date.now 被注入 ±8h/±30 天时它不受影响（只走 performance 单调外推）。 */
  window.__QA_CLOCK_NOW = clockNowS;
  window.__QA_CLOCK_STATE = () => clockSnapshot(state.clockGen);
  window.__QA_CALLS = calls;
  window.__QA_STATE = state;
  document.dispatchEvent(new Event("pywebviewready"));
})();
