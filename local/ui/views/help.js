/* 帮助弹窗与重看引导入口。 */
function fillHelp(help) {
  const data = help || DEFAULT_HELP;
  $("#help-title").textContent = data.title || DEFAULT_HELP.title;
  $("#help-sub").textContent = data.subtitle || "";
  $("#help-note").textContent = data.note || "";
  $("#help-steps").innerHTML = (data.steps || []).map((step) => "<li><span class=\"no\">" + esc(step.no) + "</span><div><div class=\"t\">" + esc(step.title) + "</div><div class=\"d\">" + esc(step.text) + "</div></div></li>").join("");
}

function showHelp() {
  const dialog = $("#help");
  helpStatus("");
  paintUpdate();
  if (!dialog.open) dialog.showModal();
  savePrefs({ welcome_seen: true });
}

/* 辅助动作的局部反馈：只在帮助里说下一步，不去复读到大屏或结果框。
 * 长地址/错误原话做成可选文本，不在 WebView 里导航。 */
function helpStatus(text, tone) {
  const box = $("#help-status");
  if (!box) return;
  if (!text) {
    box.hidden = true;
    box.textContent = "";
    return;
  }
  box.hidden = false;
  box.dataset.tone = tone || "warn";
  box.textContent = text;
  revealHelpStatus(box);
}

/* 矮窗里反馈可能刚好落在折叠处：用户触发的结果滚入正文可见区，头尾不动。 */
function revealHelpStatus(box) {
  if (box && $("#help").open && typeof box.scrollIntoView === "function") {
    box.scrollIntoView({ block: "nearest" });
  }
}

/* 帮助中的重播先确认非忙，再关模态、把焦点还给顶栏，最后进第1步。
 * 不复制教程长文，也不在弹窗还开着时叠一层遮罩。 */
function replayTourFrom(dialogSelector, openerSelector) {
  if (S.busy) {
    addLog("[i] 计划运行中：计划结束后再重看引导");
    return false;
  }
  const dialog = $(dialogSelector);
  if (dialog && dialog.open) dialog.close();
  const opener = $(openerSelector);
  if (opener) {
    try { opener.focus(); } catch (err) { /* 元素不可聚焦就算了，不挡住重播 */ }
  }
  startTour(0);
  return true;
}

/* 忙时只禁重播；查版本、打开目录不打断报名计划。 */
function paintReplayButtons() {
  [["#btn-help-tour", "重看引导"]].forEach((pair) => {
    const btn = $(pair[0]);
    if (!btn) return;
    btn.disabled = !!S.busy;
    btn.textContent = S.busy ? "计划结束后可重看引导" : pair[1];
    btn.title = S.busy ? "计划结束后可重看引导" : "重新看一遍六步引导";
  });
}
