/* 条款弹窗呈现：版本/状态/底部操作区/全文切换/焦点恢复（只读 S.terms，不发桥接）。 */

function paintTerms() {
  /* 标题里的程序要求版本：状态未知时写「尚未确认」，不给一个看似确定的
   * 版本号让用户以为自己在读某一版（设计稿状态矩阵）。 */
  const version = S.terms.unknown ? "尚未确认" : (S.terms.version || "—");
  const inDialog = $("#dlg-terms-version");
  if (inDialog) inDialog.textContent = version;
  const inHelp = $("#help-terms-version");
  if (inHelp) inHelp.textContent = version;
}

/* 底部操作区按模式收敛：只读只显示关闭；保存中禁三控件并显示「正在保存…」；
 * 超时未确认禁同意与暂不使用、亮出「重新确认状态」，关闭权限恢复。 */
function applyTermsFooter() {
  const dialog = $("#dlg-terms");
  if (!dialog) return;
  const accept = $("#btn-terms-accept");
  const decline = $("#btn-terms-decline");
  const close = $("#btn-terms-close");
  const recheck = $("#btn-terms-recheck");
  const x = dialog.querySelector(".dlg-x");
  const esc = dialog.querySelector(".help-esc");
  const saving = !!S.terms.saving;
  const unconfirmed = !!S.terms.unconfirmed;
  const viewOnly = !!S.terms.viewOnly;
  if (close) close.hidden = !viewOnly;
  if (recheck) {
    recheck.hidden = !unconfirmed;
    if (!unconfirmed) {
      recheck.disabled = false;
      recheck.textContent = "重新确认状态";
    }
  }
  if (decline) {
    decline.hidden = viewOnly;
    decline.disabled = saving || unconfirmed;
  }
  if (accept) {
    accept.hidden = viewOnly;
    accept.disabled = viewOnly || saving || unconfirmed;
    accept.textContent = saving ? "正在保存…" : "我已阅读并同意";
  }
  if (x) x.disabled = saving;
  if (esc) {
    esc.textContent = saving ? "正在保存…"
      : (viewOnly || unconfirmed) ? "Esc 关闭" : "Esc 暂不使用";
  }
}

function toggleTermsFull() {
  const full = $("#dlg-terms-full-text");
  const btn = $("#btn-terms-full");
  if (!full || !btn) return;
  full.hidden = !full.hidden;
  /* 要点⇄全文在同一阅读区切换：按钮文案、aria-expanded、工具行标题三处
   * 同步；阅读区滚回顶部，焦点留在触发按钮上（切换不抢焦点）。 */
  btn.textContent = full.hidden ? "查看完整条款" : "返回条款要点";
  btn.setAttribute("aria-expanded", full.hidden ? "false" : "true");
  const title = $("#dlg-terms-view-title");
  if (title) title.textContent = full.hidden ? "条款要点" : "完整条款";
  const body = $(".terms-body");
  if (body) body.scrollTop = 0;
}

/* 条款弹窗里的局部反馈：保存失败只在这里说下一步，不刷大屏 */
function termsStatus(text, tone) {
  const box = $("#dlg-terms-status");
  if (!box) return;
  if (!text) {
    box.hidden = true;
    box.textContent = "";
    return;
  }
  box.hidden = false;
  box.dataset.tone = tone || "warn";
  box.textContent = text;
  /* 反馈与阅读区同在唯一滚动区内：失败/未知/重新确认等非空消息置顶立即可见；
   * 只归零阅读区自身 scrollTop，不碰焦点、不滚整页，清空分支的提前 return 不受影响。 */
  const body = $(".terms-body");
  if (body) body.scrollTop = 0;
}

/* ---- 弹窗/焦点等 DOM 细节的窄接口：条款用例（controller）只按名调用 ---- */

function termsDialogAvailable() {
  /* 可打开 = 弹窗存在且当前没开（已打开时不重置任何状态）。 */
  const dialog = $("#dlg-terms");
  return !!dialog && !dialog.open;
}

function termsDialogIsOpen() {
  const dialog = $("#dlg-terms");
  return !!(dialog && dialog.open);
}

function showTermsDialog() {
  const dialog = $("#dlg-terms");
  if (!dialog || dialog.open) return;
  dialog.showModal();
  /* 首次焦点放在可聚焦标题：Tab 从标题开始遍历整段条款（设计稿键盘条） */
  const heading = $("#dlg-terms-title");
  if (heading) { try { heading.focus(); } catch (err) { /* 标题不可聚焦就算了 */ } }
}

function closeTermsDialog() {
  const dialog = $("#dlg-terms");
  if (dialog && dialog.open) dialog.close();
}

function closeHelpIfOpen() {
  /* 任何时刻都不叠两个 open dialog：帮助入口先关帮助（关闭后按来源重开） */
  const help = $("#help");
  if (help && help.open) help.close();
}

function resetTermsFullView() {
  const full = $("#dlg-terms-full-text");
  if (full) {
    full.hidden = true;
    const btn = $("#btn-terms-full");
    if (btn) {
      btn.textContent = "查看完整条款";
      btn.setAttribute("aria-expanded", "false");
    }
    /* 每次打开都回到「要点」视图：工具行标题与正文视图同步复位 */
    const title = $("#dlg-terms-view-title");
    if (title) title.textContent = "条款要点";
  }
}

function termsDocVersion() {
  /* 内嵌全文的版本字段：同意/复查都只认这一份（不是后端自报版本）。 */
  const node = $("#dlg-terms-document-version");
  return String((node && node.textContent) || "").trim();
}

function setRecheckBusy(busy) {
  const button = $("#btn-terms-recheck");
  if (!button) return;
  button.disabled = !!busy;
  button.textContent = busy ? "正在核对…" : "重新确认状态";
}

function focusHelpButton() {
  const helpBtn = $("#btn-help");
  if (helpBtn) { try { helpBtn.focus(); } catch (err) { /* 焦点落不下去就算了 */ } }
}

/* 关闭后按来源恢复焦点：帮助入口重开帮助并聚焦「查看条款」；横幅入口回
 * 被点的按钮（入口已消失就落回 #btn-help）；自动弹与同意成功回 #btn-help。 */
function restoreTermsFocusTo(source, back) {
  if (source === "help") {
    const help = $("#help");
    if (help && !help.open) help.showModal();
    const entry = $("#btn-help-terms");
    if (entry) { try { entry.focus(); } catch (err) { /* 入口不可聚焦就算了 */ } return; }
  }
  if (back && document.body && document.body.contains(back)) {
    try { back.focus(); } catch (err) { /* 落到 #btn-help */ }
    if (document.activeElement === back) return;
  }
  const fallback = $("#btn-help");
  if (fallback) { try { fallback.focus(); } catch (err) {} }
}
