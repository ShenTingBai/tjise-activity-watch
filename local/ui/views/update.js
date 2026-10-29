/* 更新通道呈现：状态文案、检查时间、进度行与动作区显隐（只读 S.update，不发桥接）。 */
function fmtBytes(n) {
  const bytes = Number(n) || 0;
  if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + " MB";
  if (bytes >= 1024) return (bytes / 1024).toFixed(0) + " KB";
  return bytes + " B";
}

/* 更新七态（检查/可用/确认/下载/校验/安装/失败）+ 无安装器退回发布页：
   每个状态只留该状态唯一可做的动作，检查从不自动下载。 */
function paintUpdate() {
  const box = $("#update-status");
  const button = $("#btn-update-check");
  const state = S.update.state;
  const result = S.update.result || {};
  box.dataset.state = state;
  const inFlight = ["checking", "downloading", "verifying", "launching"].indexOf(state) >= 0;
  button.disabled = inFlight;
  button.textContent = state === "checking" ? "检查中…" : (state === "error" || state === "failed") ? "重试检查" : "检查更新";
  const copy = { idle: "尚未检查", checking: "正在检查…", current: "当前为最新正式版",
    available: "发现新版本 " + (result.latest_version || ""),
    no_installer: result.text || "发现新版本，请前往下载页手动下载。",
    preview: "当前为预览版本，最新正式版 " + (result.latest_version || ""),
    unavailable: "暂无正式发布",
    confirm: "确认下载并安装 " + (result.latest_version || "") + "？",
    downloading: "正在下载安装包…",
    verifying: "正在校验安装包…",
    ready_to_install: "安装包已校验通过，可以启动安装程序。",
    launching: result.text || "正在启动安装程序…" };
  box.textContent = state === "error" ? "暂时无法检查更新。" + (result.text || "请重试或前往下载页。")
    : state === "failed" ? (result.text || "更新没能完成，请重试。")
    : copy[state] || copy.idle;
  /* 手动检查的结论带发生时间，避免把很久以前的结果误当成实时状态。 */
  const checked = $("#update-checked-at");
  const at = result && Date.parse(result.checked_at);
  checked.hidden = state === "checking" || !Number.isFinite(at);
  checked.textContent = checked.hidden ? "" : "检查时间 " + new Date(at).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  /* 进度行只在下载/校验/就绪阶段出现，数字一律来自后端回推，不用估计值。 */
  const progress = $("#update-progress");
  let progressText = "";
  if (state === "downloading") {
    const done = Number(result.downloaded) || 0;
    const total = Number(result.total_bytes) || 0;
    progressText = "已下载 " + fmtBytes(done) + " / " + fmtBytes(total) + "（" + (total > 0 ? Math.floor(done / total * 100) : 0) + "%）";
  } else if (state === "verifying") {
    progressText = "正在比对大小与 SHA-256…";
  } else if (state === "ready_to_install") {
    progressText = "已通过大小与 SHA-256 校验";
  }
  progress.hidden = !progressText;
  progress.textContent = progressText;
  const install = $("#btn-update-install");
  const confirm = $("#btn-update-confirm");
  const launch = $("#btn-update-launch");
  const cancel = $("#btn-update-cancel");
  const hasInstaller = !!(result.installer && result.installer.url);
  const showInstall = (state === "available" || state === "failed") && hasInstaller;
  const showConfirm = state === "confirm";
  const showLaunch = state === "ready_to_install";
  const showCancel = state === "confirm" || state === "downloading";
  install.hidden = !showInstall;
  confirm.hidden = !showConfirm;
  launch.hidden = !showLaunch;
  cancel.hidden = !showCancel;
  $("#update-flow").hidden = !(showInstall || showConfirm || showLaunch || showCancel);
}

/* 帮助面板里的更新状态单元格：新结论到达时展开（呈现入口由更新用例与 help 共用）。 */
function revealUpdateStatus() {
  revealHelpStatus($("#update-status").parentElement);
}
