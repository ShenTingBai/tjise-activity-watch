/* 展示文案纯规则：模式/状态/剩余时间/活动时间文本。 */
/* ---------------------------------------------------------------- 状态与文案 */
/* 双模式口径（与后端 signup_plan.classify_signup_mode 一致）：
 * sign_end 已过 → closed；sign_start 在未来 → pre_register；其余 → instant_signup。
 * 前端只做展示与即时门禁，后端 start_plan 仍是最终权威；
 * 活动举办时间不参与模式判断——它只用于冲突检查。 */
function modeOf(item) {
  if (!item) return "unknown";
  const now = bnowS();
  if (now == null) return "unknown";   // 未校准：模式未知（后端仍是最终权威）
  const end = toTs(item.sign_end);
  if (end !== null && end <= now) return "closed";
  const start = toTs(item.sign_start);
  if (start !== null && start > now) return "pre_register";
  return "instant_signup";
}

function modeText(mode) {
  return { pre_register: "预报名", instant_signup: "立即报名", closed: "已截止" }[mode] || "";
}

function stateOf(item) {
  if (!item) return "unknown";
  if (item.signed) return "signed";
  const state = String(item.state || "");
  if (state.indexOf("已报名") >= 0 || state === "signed") return "signed";
  if (state.indexOf("已截止") >= 0 || state === "closed") return "closed";
  const bn = bnowS();
  const end = toTs(item.sign_end);
  if (bn === null) {
    /* 未校准：只认学校给的静态「已报名/已截止/开放中」文本，
     * 不拿本机时间判开放——列表不得被错误 OS 日期误筛空 */
    if (state.indexOf("开放中") >= 0 || state === "open") return "open";
    return "unknown";
  }
  if (end !== null && end < bn) return "closed";
  /* 开放时间已知时随业务时钟换组，不能一直沿用上次拉列表时的「未开放」文本 */
  const open = toTs(item.sign_start);
  if (open !== null) return open > bn ? "waiting" : "open";
  if (state.indexOf("开放中") >= 0 || state === "open") return "open";
  if (state.indexOf("未开放") >= 0 || state === "waiting") return "waiting";
  const left = leftOf(item);
  if (left === null) return "unknown";
  return left > 0 ? "waiting" : "open";
}

function taskLabel(state) {
  return { queued: "排队", waiting: "等待开放", submitting: "提交中", success: "报名成功", already_signed: "已报名", failed: "报名未成功", canceled: "已取消" }[state] || "处理中";
}

function taskClass(state, task) {
  if (state === "success" || state === "already_signed") return "is-ok";
  if (state === "failed") {
    /* 未知结论用中性色：不能给用户一个「确定失败」的红标 */
    const stop = String(task && task.stop_reason || "");
    if (stop === "unconfirmed" || stop === "auth_unavailable") return "is-warn";
    return "is-bad";
  }
  if (state === "canceled") return "is-warn";
  if (state === "submitting" || state === "waiting" || state === "queued") return "is-busy";
  return "";
}

function leftOf(item) {
  if (!item) return null;
  const stamp = toTs(item.sign_start);
  const left = bnLeftSec(stamp);
  if (left !== null) return Math.floor(left);
  return typeof item.seconds_to_open === "number" ? item.seconds_to_open : null;
}

/* 展示模型：区分「学校没公布开放时间」与「本机暂时没有可信现在」。
 * 只有前者才说「待公布」；后者保留已知绝对时间并标「待校准」。 */
function signStartKnown(item) {
  return toTs(item && item.sign_start) !== null;
}

function leftTextOf(item) {
  const left = leftOf(item);
  if (left === null) return signStartKnown(item) ? "待校准" : "待公布";
  return left <= 0 ? "已开放" : durText(left);
}

function validActivityTime(item) {
  const start = toTs(item && item.act_start);
  const end = toTs(item && item.act_end);
  return start !== null && end !== null && end > start;
}

function activityTimeText(item) {
  if (!validActivityTime(item)) return "活动时间待确认";
  return "活动 " + whenText(toTs(item.act_start) * 1000) + " – " + hm(toTs(item.act_end) * 1000);
}

/* ---------------------------------------------------------------- 每秒 */
