/* 偏好保存队列（防抖 + flush）。 */
function flushPrefs() {
  if (S.saveBusy || !hasBridge() || !Object.keys(S.savePending).length) return Promise.resolve();
  const pending = S.savePending;
  S.savePending = {};
  S.saveBusy = true;
  return api("save_prefs", pending).then((res) => {
    if (!res || res.ok !== true) throw new Error((res && res.text) || "保存没有确认成功");
    S.saveFailures = 0;
    S.saveWarned = false;
  }).catch(() => {
    S.savePending = Object.assign({}, pending, S.savePending);
    S.saveFailures = (S.saveFailures || 0) + 1;
    if (S.saveFailures >= 2 && !S.saveWarned) {
      S.saveWarned = true;
      addLog("[!] 设置暂未保存，当前窗口仍使用这些设置。稍后修改设置可重试保存。");
    }
  }).finally(() => {
    S.saveBusy = false;
    if (!Object.keys(S.savePending).length) return;
    if (!S.saveFailures) flushPrefs();
    else if (S.saveFailures < 2) S.saveTimer = setTimeout(flushPrefs, 500);
  });
}

function savePrefs(patch) {
  Object.assign(S.prefs, patch || {});
  Object.assign(S.savePending, patch || {});
  S.saveFailures = 0;
  clearTimeout(S.saveTimer);
  S.saveTimer = setTimeout(flushPrefs, 250);
}

function savePrefsNow(patch) {
  /* 引导完成/跳过这类不能因关窗丢失的状态：立即落盘，不等 250ms 防抖 */
  Object.assign(S.prefs, patch || {});
  Object.assign(S.savePending, patch || {});
  S.saveFailures = 0;
  clearTimeout(S.saveTimer);
  return flushPrefs();
}
