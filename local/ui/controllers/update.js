/* 更新通道用例：检查/下载/取消/启动与在途收口（DOM 呈现见 views/update.js）。 */

function watchUpdate(requestId, ms) {
  clearTimeout(S.update.timer);
  /* 后端请求有8秒网络超时；桥接丢事件也不能让按钮永久停在进行中。 */
  S.update.timer = setTimeout(() => {
    if (S.update.requestId !== requestId) return;
    const stalled = S.update.state;
    if (["checking", "downloading", "verifying"].indexOf(stalled) < 0) return;
    receiveUpdate({ request_id: requestId, state: stalled === "checking" ? "error" : "failed",
      text: "没有收到更新结果，请重试。", error_code: "timeout" });
  }, ms || 15000);
}

function receiveUpdate(payload) {
  const data = payload || {};
  if (!data.request_id || data.request_id !== S.update.requestId) return;
  clearTimeout(S.update.timer);
  /* 合并而非替换：下载进度事件不一定带安装器信息，要保住检查阶段选好的那份。 */
  S.update.result = Object.assign({}, S.update.result || {}, data);
  const known = ["available", "no_installer", "current", "preview", "unavailable", "error",
    "confirm", "downloading", "verifying", "ready_to_install", "launching", "failed"];
  S.update.state = known.indexOf(data.state) >= 0 ? data.state : "error";
  paintUpdate();
  /* 下载/校验是长动作，每次进度都续上看门狗；终态不再计时。 */
  if (S.update.state === "downloading" || S.update.state === "verifying") {
    watchUpdate(S.update.requestId, 60000);
  }
  revealUpdateStatus();
}

function restoreUpdate(snapshot) {
  if (!snapshot || !snapshot.request_id) { paintUpdate(); return; }
  S.update.requestId = String(snapshot.request_id);
  /* checking 表示「任一更新动作在途」（检查或下载）；具体阶段只认 result.state。 */
  if (snapshot.checking) {
    const prior = snapshot.result || {};
    S.update.result = prior;
    S.update.state = ["checking", "downloading", "verifying", "launching"].indexOf(prior.state) >= 0 ? prior.state : "checking";
    watchUpdate(S.update.requestId, S.update.state === "checking" ? 15000 : 60000);
  } else if (snapshot.result) receiveUpdate(snapshot.result);
  paintUpdate();
}

function checkUpdate() {
  if (["checking", "downloading", "verifying", "launching"].indexOf(S.update.state) >= 0) return;
  const requestId = "update-" + Date.now() + "-" + Math.random().toString(36).slice(2, 8);
  S.update.requestId = requestId;
  S.update.state = "checking";
  watchUpdate(requestId);
  paintUpdate();
  api("check_update", requestId).then((res) => {
    /* 结果可以先于ack到达：完成后不再被ack改回检查中。 */
    if (S.update.requestId !== requestId || S.update.state !== "checking") return;
    if (!res || res.ok === false || res.queued === false) {
      receiveUpdate({ request_id: requestId, state: "error", text: res && res.text || "检查没能启动，请重试。" });
    } else if (res.skipped && res.request_id) {
      S.update.requestId = String(res.request_id);
      watchUpdate(S.update.requestId);
      /* 重载时可能接上已完成的在途请求；读取本地快照，不额外发网络检查。 */
      const attachedId = S.update.requestId;
      api("bootstrap").then((info) => {
        /* 快照晚到时用户可能已开新查询；只有仍在接管同一请求才允许恢复。 */
        if (S.update.requestId === attachedId) restoreUpdate(info && info.update_check);
      }).catch(() => {});
    }
  }).catch((err) => {
    if (S.update.requestId === requestId && S.update.state === "checking") {
      receiveUpdate({ request_id: requestId, state: "error", text: msg(err) });
    }
  });
}

/* 「下载并安装」先进确认态：下载必须是用户显式确认后的动作，检查从不自动下载。 */
function startDownload() {
  const result = S.update.result;
  if (S.update.state !== "available" && S.update.state !== "failed") return;
  if (!result || !result.installer || !result.installer.url) return;
  S.update.state = "confirm";
  paintUpdate();
}

function confirmDownload() {
  if (S.update.state !== "confirm") return;
  const result = S.update.result || {};
  const installer = result.installer;
  if (!installer || !installer.url) {
    receiveUpdate({ request_id: S.update.requestId, state: "failed",
      text: "没有可下载的安装包，请前往下载页。", error_code: "no_installer" });
    return;
  }
  const requestId = "dl-" + Date.now() + "-" + Math.random().toString(36).slice(2, 8);
  S.update.requestId = requestId;
  S.update.state = "downloading";
  watchUpdate(requestId, 60000);
  paintUpdate();
  api("update_download", requestId).then((res) => {
    if (S.update.requestId !== requestId || S.update.state !== "downloading") return;
    if (!res || res.ok === false || res.queued === false) {
      receiveUpdate({ request_id: requestId, state: "failed",
        text: res && res.text || "下载没能启动，请重试。", error_code: "start_failed" });
    }
  }).catch((err) => {
    if (S.update.requestId === requestId && S.update.state === "downloading") {
      receiveUpdate({ request_id: requestId, state: "failed", text: msg(err), error_code: "start_failed" });
    }
  });
}

/* 取消只停本次下载：后端清理半成品并回推可用态，不动旧程序和用户数据。 */
function cancelDownload() {
  if (S.update.state === "confirm") {
    S.update.state = (S.update.result && S.update.result.installer && S.update.result.installer.url) ? "available" : "no_installer";
    paintUpdate();
    return;
  }
  if (S.update.state !== "downloading") return;
  api("update_cancel").then(() => {}).catch(() => {});
}

/* 启动安装器是最后一个用户确认动作；关闭主程序由用户自己控制，不自动退出或重启。 */
function launchInstaller() {
  if (S.update.state !== "ready_to_install") return;
  const requestId = "launch-" + Date.now() + "-" + Math.random().toString(36).slice(2, 8);
  S.update.requestId = requestId;
  S.update.state = "launching";
  paintUpdate();
  api("update_launch", requestId).then((res) => {
    if (S.update.requestId !== requestId) return;
    if (!res || res.ok === false) {
      receiveUpdate({ request_id: requestId, state: "failed",
        text: res && res.text || "安装程序没能启动，请重试。", error_code: "launch_failed" });
    }
  }).catch((err) => {
    if (S.update.requestId === requestId) {
      receiveUpdate({ request_id: requestId, state: "failed", text: msg(err), error_code: "launch_failed" });
    }
  });
}

/* 偏好串行保存：重试旧请求时，合并后的新值必须优先，不能把较新设置写回旧值。 */
