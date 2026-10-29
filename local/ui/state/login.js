/* 登录三态还原与剩余估算（只展示，不参与放行）。 */
function loginSnapshotState() {
  return {
    hours: S.login.hours, ok: S.login.ok, present: S.login.present,
    expireAt: S.login.expireAt, savedAt: S.login.savedAt, result: S.login.result,
  };
}

function restoreLoginSnapshotState(snap) {
  if (!snap) return;
  S.login.hours = snap.hours;
  S.login.ok = snap.ok;
  S.login.present = snap.present;
  S.login.expireAt = snap.expireAt;
  S.login.savedAt = snap.savedAt;
  S.login.result = snap.result;
}

function setLogin(hours, ok, present, anchor) {
  S.login.hours = numOrNull(hours);
  S.login.ok = ok;
  /* present 是后端 session_present 的回声：false=从没登录过，true=登录过。
   * 不传（undefined）表示这次事件没说，保留上一次的判断。 */
  if (present !== undefined) S.login.present = !!present;
  /* 估算锚点（绝对时刻）：bootstrap.login / check 事件 / 登录完成后都带同一来源的
   * estimated_expires_at（epoch 秒）。收到就整份替换（重新登录=新锚点），
   * 显式 null 且本机无会话时清掉旧锚点；事件没带（undefined）保持现状。
   * 绝不按「本次收到事件的时刻 + hours」重算——那会把每次刷新都变成续期。 */
  if (anchor && typeof anchor === "object" && "estimated_expires_at" in anchor) {
    const at = numOrNull(anchor.estimated_expires_at);
    S.login.expireAt = at === null ? null : at * 1000;   /* 统一存 epoch 毫秒 */
    if (at === null) S.login.savedAt = null;
    else if (anchor.saved_at) S.login.savedAt = anchor.saved_at;
  }
  paintLogin();
  if (document.body) {
    paintRail();
    paintSelected();
    paintBoardActions();
    /* 后到的校验结论要能把空态从「正在检查」改成真实状态 */
    refreshLoginEmptyState();
  }
}

/* 当前凭证剩余小时：只从绝对锚点按 Date.now 推；没有锚点返回事件带回的静态值，
 * 两者都没有返回 null（未知，不编 17 小时）。 */
function loginHoursLeft() {
  if (S.login.expireAt !== null && S.login.expireAt !== undefined) {
    const bnMs = businessNowMs();
    if (bnMs == null) return null;   // 未校准：剩余显示未知，不拿 OS 时间冒充
    return (S.login.expireAt - bnMs) / 3600000;
  }
  return S.login.hours;
}

function expiryText() {
  const at = S.login.expireAt;
  return at ? "约到 " + whenText(at) : "未知（以学校校验为准）";
}

/* 登录态关联视口的统一收口（P1）：胶囊、剩余时间、时钟、大屏、路线、列表
 * 倒计时与空态、按钮必须在同一轮结论里一起刷新，不能只改胶囊留下旧倒计时。
 * 只重画、不改结论；调用点都已在请求代次校验之后，旧轮迟到事件不得用它
 * 覆盖新轮。 */
function syncLoginDependentView() {
  paintLogin();
  paintClockStatus();
  refreshLoginEmptyState();
  render();
}
