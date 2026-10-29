/* 列表归一化、筛选/三态、选择与校验纯规则。 */
function findItem(id) { return S.byId.get(String(id)) || null; }

function normalize(raw) {
  const item = Object.assign({}, raw || {});
  item.id = String(item.id == null ? "" : item.id);
  item.name = String(item.name || "活动");
  item.signed = !!item.signed || String(item.state || "") === "已报名" || String(item.state || "") === "signed";
  item.checkin_required = triState(item.checkin_required);
  item.checkout_required = triState(item.checkout_required);
  /* 详情补充的真实时间段：只认合法时间文本，缺失就是空（不要伪造） */
  item.checkin_start = String(item.checkin_start || "");
  item.checkin_end = String(item.checkin_end || "");
  item.checkout_start = String(item.checkout_start || "");
  item.checkout_end = String(item.checkout_end || "");
  const state = String(item.attendance_state || "");
  item.attendance_state = ["ready", "pending", "unknown"].indexOf(state) >= 0 ? state
    : (item.checkin_required !== null || item.checkout_required !== null ? "ready" : "unknown");
  item.attendance_source = String(item.attendance_source || "none");
  return item;
}

function triState(value) {
  if (value === true || value === false) return value;
  if (typeof value === "number" && (value === 0 || value === 1)) return value === 1;
  const text = String(value == null ? "" : value).trim().toLowerCase();
  if (["1", "true", "yes", "y", "是", "需要", "有"].indexOf(text) >= 0) return true;
  if (["0", "false", "no", "n", "否", "无需", "无"].indexOf(text) >= 0) return false;
  return null;
}

function matchesFlag(value, filter) {
  /* 需要和无需都排除未知（null）；全部保留未知 */
  if (filter === "all") return true;
  if (value === null) return false;
  return filter === "yes" ? value === true : value === false;
}

function requirementFlags(item) {
  /* 真实字段来自详情接口：签到/签退要求 + 时间段。
   * 未知（null）分两种：详情还没回来 →「同步中」；详情已确认没有数据 →「待确认」。
   * 只有明确 false 才写「无需」，绝不把未知说成无需。 */
  const pending = item.attendance_state === "pending";
  const timeRange = (start, end) => {
    const startTs = toTs(start);
    if (!startTs) return "";
    const endTs = toTs(end);
    return " " + hm(startTs * 1000) + (endTs && endTs > startTs ? "–" + hm(endTs * 1000) : "");
  };
  const flags = [];
  const add = (text, unknown) => flags.push('<span class="activity-flag' + (unknown ? " is-unknown" : "") + '">' + esc(text) + "</span>");
  if (item.checkin_required === false) add("无需签到", false);
  else if (item.checkin_required === true) add("签到" + timeRange(item.checkin_start, item.checkin_end), false);
  else if (pending) add("签到信息同步中", true);
  else add("签到时间待确认", true);
  if (item.checkout_required === false) add("无需签退", false);
  else if (item.checkout_required === true) add("签退" + timeRange(item.checkout_start, item.checkout_end), false);
  else if (pending) add("签退信息同步中", true);
  else add("签退时间待确认", true);
  return flags.length ? '<span class="activity-flags">' + flags.join("") + "</span>" : "";
}

function selectedItems() {
  return Array.from(S.selected).map((id) => findItem(id)).filter(Boolean);
}

/* 筛选只影响可见行：全局下一场、冲突检查、计划都用 S.items，不用这里的输出 */
function filtered() {
  const keyword = S.filter.q.trim().toLowerCase();
  const now = bnowS();   // 未校准时为 null：today/week 筛选不生效（不误筛空）
  const rows = S.items.filter((item) => {
    if (keyword && (String(item.name) + " " + String(item.place || "")).toLowerCase().indexOf(keyword) < 0) return false;
    /* 可报名 = 未报名且未截止；已截止的只剩在「全部」里可见 */
    if (S.filter.status === "free" && (item.signed || stateOf(item) === "closed")) return false;
    if (S.filter.status === "signed" && !item.signed) return false;
    if (S.filter.time !== "all") {
      const stamp = toTs(item.sign_start);
      if (!stamp) return false;
      if (now != null && S.filter.time === "today" && startOfDay(stamp * 1000) !== startOfDay(now * 1000)) return false;
      if (now != null && S.filter.time === "week" && (stamp < now - 7 * 86400 || stamp > now + 7 * 86400)) return false;
    }
    if (!matchesFlag(item.checkin_required, S.filter.checkin) || !matchesFlag(item.checkout_required, S.filter.checkout)) return false;
    return true;
  });
  rows.sort((a, b) => {
    if (S.filter.sort === "name") return String(a.name).localeCompare(String(b.name), "zh");
    const ta = toTs(a.sign_start);
    const tb = toTs(b.sign_start);
    if (ta === null && tb !== null) return 1;
    if (ta !== null && tb === null) return -1;
    if (ta !== tb) return (ta || 0) - (tb || 0);
    return String(a.name).localeCompare(String(b.name), "zh");
  });
  return rows;
}

/* 可加入执行计划的统一判据：未报名且未截止（已截止只能查看）。
 * 列表计数、芯片计数、大屏指标、下一场与复选框门禁共用这一份。 */
function isEligibleForPlan(item) {
  return !!item && !item.signed && modeOf(item) !== "closed";
}

/* 全局下一场：按全部活动算，不受筛选影响。
 * 返回 openId（已开放未截止）与 futureId（最近的未来场次）：
 * 存在开放活动时大屏与快捷选择优先 openId，只有没有开放活动才轮到
 * 未来场次；已截止活动不参与下一场。 */
function nextOpen() {
  const now = bnowS();
  if (now == null) return { best: null, openNow: false, bestId: null,
                           openId: null, futureId: null };  // 未校准：完整空目标，不让 undefined 冒充有效值
  let best = null;
  let openNow = false;
  let openId = null;
  let openLeft = Infinity;
  let futureId = null;
  let futureLeft = Infinity;
  S.items.forEach((item) => {
    if (!isEligibleForPlan(item)) return;
    const left = leftOf(item);
    if (left === null) return;
    if (left <= 0) {
      openNow = true;
      /* 多个开放场次取最早开放的（left 最小）：最紧急的一场 */
      if (left < openLeft) { openLeft = left; openId = item.id; }
    } else if (left < futureLeft) { futureLeft = left; futureId = item.id; }
  });
  if (futureId !== null) best = futureLeft;
  /* 统一目标：只要当前有已开放场次，倒计时就属于它——已开放没有倒计时，
   * best 必须是 null，不能把另一场未来活动的倒计时段配给这个标题（R12）。
   * 只有没有任何已开放场次时，才轮到最近未来场。 */
  if (openNow) best = null;
  const targetId = openNow ? openId : futureId;
  return { best: best, openNow: openNow, bestId: targetId,
           openId: openId, futureId: futureId };
}

function groupSelected(items) {
  const groups = new Map();
  items.slice().sort((a, b) => (toTs(a.sign_start) || Infinity) - (toTs(b.sign_start) || Infinity)).forEach((item) => {
    const key = item.sign_start || "";
    if (!groups.has(key)) groups.set(key, { key: key, items: [] });
    groups.get(key).items.push(item);
  });
  return Array.from(groups.values());
}

function validateSelection() {
  const errors = [];
  const conflicts = [];
  const items = selectedItems();
  /* 已报名活动是外部占用：只按举办时段 act_start/act_end 比较，
   * 报名开放时间不参与（同一时刻开放的活动可以并行）。 */
  const signedItems = S.items.filter((item) => item.signed);
  items.forEach((item) => {
    if (item.signed) errors.push({ title: "此活动已报名", text: "「" + item.name + "」已报名，请从本次计划中移除。" });
    /* 报名截止是可执行门禁：已截止活动即使被勾上（时序差）也开始不了 */
    if (modeOf(item) === "closed") errors.push({ title: "报名已截止", text: "「" + item.name + "」的报名已截止（" + (item.sign_end || "时间未公布") + "），不能加入计划。" });
    if (!toTs(item.sign_start)) errors.push({ title: "报名开放时间待确认", text: "「" + item.name + "」没有可用的报名开始时间。" });
    const start = toTs(item.act_start);
    const end = toTs(item.act_end);
    if (!start || !end) errors.push({ title: "活动时间待确认", text: "「" + item.name + "」缺少完整的活动起止时间，不能静默加入可执行计划。" });
    else if (end <= start) errors.push({ title: "活动时间无效", text: "「" + item.name + "」的结束时间不晚于开始时间。" });
    if (!start || !end || end <= start || item.signed) return;
    const clash = signedItems.find((other) => {
      if (String(other.id) === String(item.id)) return false;
      const otherStart = toTs(other.act_start);
      const otherEnd = toTs(other.act_end);
      return otherStart && otherEnd && otherEnd > otherStart && start < otherEnd && otherStart < end;
    });
    if (clash) {
      errors.push({ title: "与已报名活动重叠",
                    text: "「" + item.name + "」与已报名的「" + clash.name + "」举办时间重叠（"
                      + activityTimeText(clash) + "），同一时段只能去一场。" });
    }
  });
  /* 已报名活动缺少举办时段时无法判定冲突：保守阻止，不静默放行 */
  if (items.length) {
    const unknown = signedItems.filter((item) => !toTs(item.act_start) || !toTs(item.act_end));
    if (unknown.length) {
      const names = unknown.slice(0, 3).map((item) => "「" + item.name + "」").join("、");
      errors.push({ title: "已报名活动时间待确认",
                    text: names + (unknown.length > 3 ? " 等 " + unknown.length + " 场" : "")
                      + "缺少举办时间，无法确认是否与本次选择冲突。" });
    }
  }
  for (let i = 0; i < items.length; i += 1) {
    const leftStart = toTs(items[i].act_start);
    const leftEnd = toTs(items[i].act_end);
    if (!leftStart || !leftEnd || leftEnd <= leftStart) continue;
    for (let j = i + 1; j < items.length; j += 1) {
      const rightStart = toTs(items[j].act_start);
      const rightEnd = toTs(items[j].act_end);
      if (rightStart && rightEnd && rightEnd > rightStart && leftStart < rightEnd && rightStart < leftEnd) conflicts.push({ left: items[i], right: items[j] });
    }
  }
  return { errors: uniqueErrors(errors), conflicts: conflicts };
}

function conflictIds() {
  const ids = new Set();
  validateSelection().conflicts.forEach((conflict) => { ids.add(String(conflict.left.id)); ids.add(String(conflict.right.id)); });
  return ids;
}

/* 计划里正在提交的那一场：行内黑竖条 + 提交中标签 */
function armedTaskByActivity(id) {
  if (!S.plan.tasks.size) return null;
  let found = null;
  S.plan.tasks.forEach((task) => {
    if (String(task.activity_id) === String(id) && String(task.status) === "submitting") found = task;
  });
  return found;
}

function uniqueErrors(errors) {
  const seen = new Set();
  return errors.filter((error) => {
    const key = error.title + "\n" + error.text;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function toggleSelection(id, checked) {
  if (S.busy) return;
  const item = findItem(id);
  /* 已报名和已截止都不能进选择集合（含整行点击路径，复选框已禁用） */
  if (!item || !isEligibleForPlan(item)) return;
  if (checked) {
    const existing = selectedItems();
    const candidateStart = toTs(item.act_start);
    const candidateEnd = toTs(item.act_end);
    /* 已报名活动先挡：它是学校确认的外部占用，不是本次选择内部冲突 */
    const signedClash = S.items.find((other) => {
      if (!other.signed || String(other.id) === String(id)) return false;
      const start = toTs(other.act_start);
      const end = toTs(other.act_end);
      return candidateStart && candidateEnd && start && end
        && candidateStart < end && start < candidateEnd;
    });
    if (signedClash) {
      S.selectionNotice = "与已报名活动「" + signedClash.name + "」的举办时间重叠（"
        + activityTimeText(signedClash) + "），不能再加入计划。";
      render();
      return;
    }
    const conflict = existing.find((other) => {
      const start = toTs(other.act_start);
      const end = toTs(other.act_end);
      return candidateStart && candidateEnd && start && end && candidateStart < end && start < candidateEnd;
    });
    if (conflict) {
      S.selectionNotice = "与「" + conflict.name + "」的活动时间重叠，同一计划只能选择其中一场。已保留原选择。";
      render();
      return;
    }
    S.selected.add(String(id));
    S.selectionNotice = "";
  } else {
    S.selected.delete(String(id));
    S.selectionNotice = "";
  }
  setResult(null);
  render();
}

function clearSelection() {
  if (S.busy) return;
  S.selected.clear();
  S.selectionNotice = "";
  S.plan = { id: null, status: "idle", tasks: new Map() };
  setResult(null);
  render();
}
