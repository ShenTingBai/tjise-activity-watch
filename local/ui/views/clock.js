/* 顶栏时钟呈现（走字不重建浮层）。 */
function paintClockStatus() {
  const btn = $("#clock");
  if (!btn) return;
  const ui = clockUiState();
  const copy = CLOCK_COPY[ui] || CLOCK_COPY.unavailable;
  btn.dataset.state = ui;
  const label = $("#clock-label");
  const value = $("#clock-time");
  const icon = $("#clock-icon");
  if (label) label.textContent = ui === "demo" ? "演示时间" : "北京时间";
  if (icon) icon.innerHTML = CLOCK_ICONS[ui] || CLOCK_ICONS.unavailable;
  const nowMs = businessNowMs();
  if (value) {
    if (ui === "syncing") value.textContent = "校准中…";
    else if (ui === "unavailable") value.textContent = "待校准";
    else value.textContent = clockParts(nowMs).time;
  }
  const title = (ui === "demo" ? "演示时间" : "北京时间") + "，" + copy.label + "，查看时间详情";
  btn.title = title;
  btn.setAttribute("aria-label", title);
  btn.setAttribute("aria-expanded", clockPopOpenNow() ? "true" : "false");
}
