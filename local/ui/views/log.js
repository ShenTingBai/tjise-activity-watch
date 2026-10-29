/* 运行日志展开/复制。 */
function setLogOpen(open, silent) {
  S.logOpen = !!open;
  const card = document.querySelector(".log-card");
  if (card) card.dataset.open = String(S.logOpen);
  $("#btn-log-toggle").textContent = S.logOpen ? "收起" : "展开";
  $("#btn-log-toggle").setAttribute("aria-expanded", String(S.logOpen));
  if (!silent) savePrefs({ log_open: S.logOpen });
}

function copyLog() {
  /* 日志正文是逐行小条：复制时按行拼回带换行的纯文本 */
  const lines = Array.from($("#log").children).map((row) => row.textContent);
  const text = lines.join("\n");
  const done = () => addLog("[i] 运行记录已复制");
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, () => addLog("[X] 复制没成功"));
  else {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); done(); } catch (err) { addLog("[X] 复制没成功"); }
    ta.remove();
  }
}

/* ---------------------------------------------------------------- 条款 */
/* 决策 013：条款门禁只拦「学校网络动作」——登录、真实校验、活动列表、报名
 * 计划。帮助、设置、条款本身与离线演示（demo，不碰学校系统）不受限。 */
