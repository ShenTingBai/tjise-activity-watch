/* 首次引导渲染与键盘循环。 */
function tourVisible() {
  const box = $("#tour");
  return !!(S.tour.open && !S.tour.paused && box && !box.hidden);
}

function tourShouldAutoStart() {
  /* 只按内容版本判定：看过本版（tour_version >= TOUR_VERSION）就不再自动弹。
   * 完成/跳过时间戳只描述「当时那一版」，不得挡住更新的内容版本；
   * 软件版本（VERSION/1.1.0 等）变化与 TOUR_VERSION 无关。 */
  const seen = Number(S.prefs.tour_version);
  if (Number.isFinite(seen) && seen >= TOUR_VERSION) return false;
  return !S.busy;
}

function startTour(at) {
  if (S.busy) return;                 /* 计划运行中优先，引导不启动 */
  /* 记住引导开始前焦点在哪，关闭/跳过/完成后还回去（元素还在页面上才还） */
  const prev = document.activeElement;
  S.tour.restoreFocus = (prev && prev !== document.body) ? prev : null;
  S.tour.open = true;
  S.tour.paused = false;
  S.tour.step = Math.max(0, Math.min(Number(at) || 0, TOUR_STEPS.length - 1));
  $("#tour").hidden = false;
  paintTour();
  const nextBtn = $("#tour-next");
  if (nextBtn) nextBtn.focus();       /* 键盘焦点留在引导卡片里 */
}

/* 引导可见时的键盘约束：Tab/Shift+Tab 在卡片按钮间循环。
 * 返回 true = 已处理（调用方直接 return），按钮清单与 aria-modal 声明一致。 */
function tourTrapTab(event) {
  event.preventDefault();
  const buttons = ["#tour-skip", "#tour-prev", "#tour-next"]
    .map((sel) => $(sel)).filter((el) => el && !el.disabled);
  if (!buttons.length) return true;
  const idx = buttons.indexOf(document.activeElement);
  if (idx < 0) {
    buttons[event.shiftKey ? buttons.length - 1 : 0].focus();
    return true;
  }
  const dir = event.shiftKey ? -1 : 1;
  buttons[(idx + dir + buttons.length) % buttons.length].focus();
  return true;
}

function tourClose(finished) {
  S.tour.open = false;
  S.tour.paused = false;
  $("#tour").hidden = true;
  const back = S.tour.restoreFocus || null;
  S.tour.restoreFocus = null;
  if (back && document.body && document.body.contains(back)) {
    try { back.focus(); } catch (err) { /* 元素已不可聚焦就算了 */ }
  }
  if (finished) savePrefsNow({ tour_version: TOUR_VERSION, tour_completed_at: new Date().toISOString() });
}

function tourSkip() {
  /* 跳过=本版不再自动弹；帮助中的重看引导随时可回顾。 */
  savePrefsNow({ tour_version: TOUR_VERSION, tour_skipped_at: new Date().toISOString() });
  tourClose(false);
}

function tourPauseNow() {
  S.tour.paused = true;
  $("#tour").hidden = true;
}

function tourResumeNow() {
  if (!S.tour.open) return;
  S.tour.paused = false;
  $("#tour").hidden = false;
  paintTour();
}

function paintTour() {
  if (!S.tour.open || S.tour.paused) return;
  const box = $("#tour");
  const step = TOUR_STEPS[S.tour.step];
  const total = TOUR_STEPS.length;
  $("#tour-step").textContent = "第 " + (S.tour.step + 1) + "/" + total + " 步";
  $("#tour-title").textContent = step.title;
  $("#tour-text").textContent = step.text;
  $("#tour-prev").disabled = S.tour.step === 0;
  $("#tour-next").textContent = S.tour.step === total - 1 ? "完成" : "下一步";
  $("#tour-dots").innerHTML = TOUR_STEPS.map((s, i) =>
    '<i class="' + (i === S.tour.step ? "on" : "") + '"></i>').join("");
  /* 目标缺失/隐藏/窄窗（≤980）时不做高亮，退回底部静态说明条——
   * 不伪造定位，也不挡运行中的操作 */
  const target = document.querySelector('[data-tour-target="' + step.target + '"]');
  const usable = !!target && window.innerWidth > 980 &&
                 target.offsetWidth > 0 && target.getClientRects().length > 0;
  box.classList.toggle("is-bottom", !usable);
  const spot = $("#tour-spot");
  const card = $("#tour-card");
  card.style.top = "";
  card.style.left = "";
  if (!usable) {
    spot.style.top = spot.style.left = spot.style.width = spot.style.height = "";
    return;
  }
  const rect = target.getBoundingClientRect();
  const pad = 6;
  spot.style.top = Math.round(rect.top - pad) + "px";
  spot.style.left = Math.round(rect.left - pad) + "px";
  spot.style.width = Math.round(rect.width + pad * 2) + "px";
  spot.style.height = Math.round(rect.height + pad * 2) + "px";
  /* 卡片优先贴目标下方，放不下换上方，再放不下交回底部说明条 */
  const cw = card.offsetWidth || 340;
  const ch = card.offsetHeight || 200;
  const gap = 12;
  let top = rect.bottom + gap;
  if (top + ch > window.innerHeight - 12) top = Math.max(12, rect.top - ch - gap);
  if (top <= 12 && rect.top < ch + 12) {
    box.classList.add("is-bottom");
    return;
  }
  card.style.top = Math.round(top) + "px";
  card.style.left = Math.round(Math.min(Math.max(12, rect.left), window.innerWidth - cw - 12)) + "px";
}

/* ---------------------------------------------------------------- 事件绑定 */
