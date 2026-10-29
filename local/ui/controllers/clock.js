/* 校时用例：重新校时、后台轮询与快照摄入后的呈现刷新。 */
/* 重新校准：只调用本机时钟；反馈留在时间详情，不抢占业务结论。 */
function resyncClock() {
  const c = S.businessClock;
  if (c.syncState === "syncing") return;
  c.syncState = "syncing";
  c.syncError = "";
  const request = (c.syncRequest || 0) + 1;
  c.syncRequest = request;
  c.syncRequestRound = c.syncRound;
  c.syncFollowRound = c.syncStatus === "syncing" ? c.syncRound : null;
  const startedGen = c.gen;
  paintClockStatus();
  if (clockPopOpenNow()) renderClockPop();
  paintClockPopFeedback();
  api("sync_clock").then(() => {
    if (c.syncRequest !== request || c.syncState !== "syncing") return;
    /* 结果经 business_clock 事件/轮询回收；超时无回执才在详情里报失败 */
    c.syncTimer = setTimeout(() => {
      if (S.businessClock.syncRequest === request && S.businessClock.syncState === "syncing"
          && S.businessClock.gen === startedGen) {
        S.businessClock.syncState = "idle";
        S.businessClock.syncError = "暂时无法确认本次校准结果，请稍后重试。";
        paintClockStatus();
        if (clockPopOpenNow()) renderClockPop();
        paintClockPopFeedback();
      }
    }, 12000);
    pollClockNow();
  }).catch((err) => {
    if (c.syncRequest !== request || c.syncState !== "syncing") return;
    c.syncState = "idle";
    c.syncError = "重新校准没能开始：" + msg(err);
    paintClockStatus();
    if (clockPopOpenNow()) renderClockPop();
    paintClockPopFeedback();
  });
}

/* 本机快照轮询：不访问外部网络；首校时完成、F5/可见性恢复、失效后都能更新。 */
function pollClockNow() {
  if (!hasBridge() || S.demo) return Promise.resolve(null);
  const started = _monoMs();
  return api("clock_snapshot").then((snap) => {
    ingestBusinessClock(snap, { rttMs: _monoMs() - started });
    S.businessClock.attempted = true;
    return snap;
  }).catch(() => {
    S.businessClock.attempted = true;
    markClockUnavailable("暂时无法读取时间状态");
    return null;
  });
}

function startClockPolling() {
  if (S.clockTimer) return;
  S.clockTimer = setInterval(pollClockNow, CLOCK_POLL_MS);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) pollClockNow();
  });
}
