/* 独立北京时间状态机（决策 012）：快照锚点、误差与业务时刻读数。 */
const TASK_RUNTIME_FIELDS = ["run_started_at", "worker_spawned_at", "page_opened_at", "watch_started_at",
  "retry_deadline", "retry_started_at", "attempts", "last_reason", "stop_reason"];

/* ---- 独立北京时间（决策 012）----
 * 快照锚点按真实字段推进：epoch_at_sample = epoch_s + sampled_tick_s - tick_s；
 * 页面用 performance.now 外推，桥接往返的一半计入误差与外推起点。
 * 旧代次/旧采样/其他实例的快照一律不落地；businessNowMs 每次校验状态、年龄、
 * 漂移与总误差，无效返回 null——调用方显示待校准，绝不强转 0。 */
const CLOCK_MAX_AGE_MS = 900000;        /* 锚点超过 900 秒不可用（与后端一致） */
const CLOCK_HOLDOVER_MS = 300000;       /* 超过 300 秒标「使用上次校准结果」 */
const CLOCK_MAX_UNCERTAINTY_MS = 500;   /* 总误差超过 0.5 秒不可用 */
const CLOCK_DRIFT_PPM = 100;            /* holdover 保守漂移 */
const CLOCK_POLL_MS = 10000;            /* 后台最多每 10 秒读一次本机快照 */

/* 同源单调读数：浏览器用 performance.now；node 测试桩缺 performance 时退回
 * Date.now——锚点与读数必须同一来源，外推才成立（仅测试环境影响）。 */
const _monoMs = (typeof performance !== "undefined" && typeof performance.now === "function")
  ? () => performance.now() : () => Date.now();

const CLOCK_ICONS = {
  ready: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.35"><circle cx="8" cy="8" r="6"/><path d="m5 8 2 2 4-4"/></svg>',
  holdover: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.35"><circle cx="8" cy="8" r="6"/><path d="M8 4v4l3 2"/></svg>',
  syncing: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.35"><path d="M13.5 7a5.6 5.6 0 1 0-1 4M13.5 2v5H9"/></svg>',
  unavailable: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.35"><path d="M8 2 15 14H1Z"/><path d="M8 6v4m0 2h.01"/></svg>',
  demo: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.35"><circle cx="8" cy="8" r="6"/><path d="m6.5 5 4 3-4 3Z"/></svg>',
};

const CLOCK_COPY = {
  ready: { label: "已校准", explain: "报名时间和倒计时均以北京时间为准。" },
  syncing: { label: "正在校准…", explain: "正在获取网络时间。报名计划需在校准完成后启动。" },
  holdover: { label: "使用上次校准结果", explain: "时间仍可用，工具会自动重试校准。" },
  unavailable: { label: "时间校准失败", explain: "报名计划暂不可启动。检查网络后重试。" },
  demo: { label: "演示时间", explain: "使用示例活动和时间，不会提交报名。" },
};
const CLOCK_SYNC_FAILURE = "本次校准未成功。";

/* 快照字段校验与换算：任何缺失/非数值都返回 null，不拼接半份快照。 */
function clockSnapshotFields(snap, rttMs) {
  const s = snap && typeof snap === "object" ? snap : {};
  const epoch = Number(s.epoch_s);
  const tick = Number(s.tick_s);
  const sampled = Number(s.sampled_tick_s);
  const unc = Number(s.uncertainty_s);
  const age = Number(s.age_s);
  if (![epoch, tick, sampled].every(Number.isFinite)) return null;
  if (!Number.isFinite(unc) || unc < 0) return null;
  const bridge = Number.isFinite(rttMs) && rttMs > 0 ? rttMs : 0;
  return {
    owner: String(s.owner_id || ""),
    gen: Number.isFinite(Number(s.generation)) ? Number(s.generation) : 0,
    state: String(s.state || "unavailable"),
    /* epoch_s 只对应 tick_s：采样时刻的业务 epoch 必须补上两者差 */
    epochAtSampleMs: (epoch + (sampled - tick)) * 1000,
    sampleAgeMs: Number.isFinite(age) ? Math.max(0, age * 1000)
      : Math.max(0, (sampled - tick) * 1000),
    uncertaintyMs: unc * 1000 + bridge / 2,
    bridgeMs: bridge,
    source: String(s.source || ""),
    reason: String(s.reason || ""),
    syncStatus: ["pending", "syncing", "succeeded", "failed"].includes(s.sync_status)
      ? s.sync_status : null,
    syncRound: Number.isSafeInteger(s.sync_round) && s.sync_round >= 0 ? s.sync_round : null,
  };
}

function markClockUnavailable(reason) {
  const cur = S.businessClock;
  cur.state = "unavailable";
  cur.reason = String(reason || "暂时无法获取北京时间");
  paintClockStatus();
  updateControls();
  render();
}

function ingestBusinessClock(snap, opts) {
  const options = opts || {};
  const cur = S.businessClock;
  const fields = clockSnapshotFields(snap, options.rttMs);
  if (!fields) {
    markClockUnavailable("暂时无法读取时间状态");
    return;
  }
  /* 归属：只接受同一实例；新 owner 只由权威 bootstrap 重建 */
  if (cur.owner && fields.owner && fields.owner !== cur.owner && !options.authoritative) return;
  if (fields.gen < cur.gen) return;                 /* 旧代次不覆盖新结论 */
  /* 是否完成首轮由后端报告：收到 bootstrap 并不意味着校准已经结束。 */
  const freshAttempt = fields.syncRound === null || cur.syncRound == null
    || fields.syncRound >= cur.syncRound;
  let attemptSettled = false;
  if (freshAttempt) {
    cur.syncStatus = fields.syncStatus;
    if (fields.syncRound !== null) cur.syncRound = fields.syncRound;
    /* 失败不增加授时 generation，必须按尝试编号核对，不能一直等成功代次。 */
    const ownsAttempt = fields.syncRound !== null && cur.syncRequestRound != null
      && (fields.syncRound > cur.syncRequestRound || cur.syncFollowRound === fields.syncRound);
    if (cur.syncState === "syncing" && ownsAttempt
        && (fields.syncStatus === "succeeded" || fields.syncStatus === "failed")) {
      clearTimeout(cur.syncTimer);
      cur.syncState = "idle";
      cur.syncError = fields.syncStatus === "failed" ? CLOCK_SYNC_FAILURE : "";
      attemptSettled = true;
    }
  }
  if (attemptSettled) {
    paintClockStatus();
    paintClockPopFeedback();
  }
  if (fields.gen === cur.gen && cur.anchorEpochMs
      && fields.epochAtSampleMs < cur.epochAtSampleMs) return;   /* 同代旧采样不倒退 */
  if (fields.state === "unavailable") {
    /* 同代「不可用」不得抹掉已建立的可读锚点：真正的过期由年龄/误差门禁判 */
    if (fields.gen === cur.gen && cur.anchorEpochMs
        && (cur.state === "ready" || cur.state === "holdover")) return;
    cur.state = "unavailable";
    cur.reason = fields.reason || "暂时无法获取北京时间";
    cur.gen = fields.gen;
    cur.owner = fields.owner || cur.owner;
    cur.syncState = "idle";
    paintClockStatus();
    updateControls();
    render();
    return;
  }
  const advanced = fields.gen > cur.gen;
  cur.owner = fields.owner || cur.owner;
  cur.gen = fields.gen;
  cur.state = fields.state === "holdover" ? "holdover" : "ready";
  cur.anchorEpochMs = fields.epochAtSampleMs + fields.bridgeMs / 2;
  cur.perfAt = _monoMs();
  cur.sampleAgeMs = fields.sampleAgeMs;
  cur.uncertaintyMs = fields.uncertaintyMs;
  cur.bridgeMs = fields.bridgeMs;
  cur.source = fields.source;
  cur.reason = fields.reason;
  if (advanced && !attemptSettled) {
    cur.syncState = "idle";
    cur.syncError = "";
  }
  paintClockStatus();
  updateControls();   /* 时钟门禁即时生效：不可用→开始禁用，恢复→解禁 */
  render();
}

/* 采样后经过时长（同一单调域）。快照 age_s 是采样时的锚点年龄；
 * 两者相加才是「距最近校准」的真实年龄（300/900 秒门禁用）。 */
function clockSampleElapsedMs() {
  const c = S.businessClock || {};
  const elapsed = _monoMs() - c.perfAt;
  return Number.isFinite(elapsed) && elapsed >= 0 ? elapsed : null;
}

function clockAgeMs() {
  const c = S.businessClock || {};
  if (!c.anchorEpochMs) return null;
  const elapsed = clockSampleElapsedMs();
  if (elapsed === null) return null;
  return (c.sampleAgeMs || 0) + elapsed;
}

/* 同一份只读推导：采样时误差 + 采样后漂移。
 * uncertainty_s 已经包含锚点到采样的漂移，不能再按整段年龄叠加一次；
 * 桥接往返只计一次采样误差，也不参与后续漂移。 */
function clockReading() {
  const c = S.businessClock || {};
  if (!c.anchorEpochMs) return null;
  if (c.state !== "ready" && c.state !== "holdover") return null;
  const elapsed = clockSampleElapsedMs();
  if (elapsed === null) return null;
  const age = (c.sampleAgeMs || 0) + elapsed;
  const drift = CLOCK_DRIFT_PPM * 1e-6 * elapsed;
  const uncertainty = (c.uncertaintyMs || 0) + drift;
  const valid = age <= CLOCK_MAX_AGE_MS && Number.isFinite(uncertainty)
    && uncertainty <= CLOCK_MAX_UNCERTAINTY_MS;
  return {
    nowMs: c.anchorEpochMs + elapsed,
    ageMs: age,
    uncertaintyMs: uncertainty,
    driftMs: drift,
    valid: valid,
  };
}

/* 业务当前时刻：门禁、顶栏、详情与倒计时共用同一份推导；无效返回 null。 */
function businessNowMs() {
  const reading = clockReading();
  return reading && reading.valid ? reading.nowMs : null;
}

function bnowS() {
  const m = businessNowMs();
  return m == null ? null : m / 1000;
}

function bnLeftSec(targetSec) {
  const n = bnowS();
  return (n == null || !Number.isFinite(targetSec)) ? null : targetSec - n;
}

/* 五个界面状态：演示 / 校准中 / 已校准 / 使用上次结果 / 待校准 */
function clockUiState() {
  if (S.demo) return "demo";
  const c = S.businessClock || {};
  const reading = clockReading();
  if (!reading || !reading.valid) {
    if (!c.anchorEpochMs && (c.syncStatus === "pending" || c.syncStatus === "syncing"
        || c.attempted !== true)) return "syncing";
    return "unavailable";
  }
  return reading.ageMs > CLOCK_HOLDOVER_MS ? "holdover" : "ready";
}

function clockParts(ms) {
  if (!Number.isFinite(ms)) return { time: "--:--:--", date: "日期待确认" };
  const moment = new Date(ms);
  try {
    return {
      time: new Intl.DateTimeFormat("zh-CN", {
        timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit",
        second: "2-digit", hour12: false,
      }).format(moment),
      date: new Intl.DateTimeFormat("zh-CN", {
        timeZone: "Asia/Shanghai", year: "numeric", month: "long", day: "numeric",
        weekday: "long",
      }).format(moment),
    };
  } catch (err) {
    return { time: moment.toTimeString().slice(0, 8), date: "" };
  }
}

function clockUncertaintyText(ms) {
  if (!Number.isFinite(ms) || ms <= 0) return "无法确认";
  const seconds = ms / 1000;
  if (seconds >= 1) return "约 ±" + seconds.toFixed(1) + " 秒";
  if (seconds >= 0.01) return "约 ±" + seconds.toFixed(2) + " 秒";
  return "约 ±" + seconds.toFixed(3) + " 秒";   /* 非零绝不显示成 ±0.00 */
}

function clockLastSyncText() {
  /* 最近校准 = 权威 age_s + 接入后的单调经过时长。
   * F5/bootstrap 重新接入旧锚点不会把「10 分钟前」刷新成「刚刚」。 */
  const ageMs = clockAgeMs();
  if (ageMs === null) return "尚未完成";
  const ago = Math.max(0, ageMs / 1000);
  if (ago < 60) return "刚刚";
  if (ago < 3600) return Math.floor(ago / 60) + " 分钟前";
  return Math.floor(ago / 3600) + " 小时前";
}

/* 顶栏唯一时间入口：正常只显示北京时间 + 时分秒 + 小状态图形。 */
