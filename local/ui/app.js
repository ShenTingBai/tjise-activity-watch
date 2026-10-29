"use strict";

/* 天软活动预报名工具：界面逻辑。
 * 只按 local/app.py 的真实契约说话：
 *   调用  window.pywebview.api.bootstrap / check / list_activities / login / start_plan /
 *         cancel_task / stop_plan / open_data / save_prefs / front_ready
 *   事件  window.onBackend.log / check / activities / done / plan / task / plan_done
 * 多活动计划由 signup_plan.py 协调；视觉语言参照 design/prototype（候车厅 + 时刻表 + LED 大屏），
 * 但状态机以多活动业务为准，不回退单活动模型。 */

async function boot() {
  const bridgeReady = await waitForBridge();
  const bootStarted = _monoMs();
  const info = bridgeReady ? await api("bootstrap").catch(() => null) : null;
  const bootRtt = _monoMs() - bootStarted;
  fillHelp((info && info.help) || DEFAULT_HELP);
  /* 当前版本唯一显示位，来自后端；打开帮助不代表已查询GitHub。 */
  $("#help-version").textContent = info && info.version ? String(info.version) : "—";
  restoreUpdate(info && info.update_check);
  if (info) {
    /* demo 标记由后端 bootstrap 带来：演示模式绕过「登录校验 valid」的开始门禁 */
    S.demo = !!info.demo;
    /* 生命周期快照（上次关闭时长/质量、登录估算）：只进时钟浮层当提示，
     * 不参与登录四态判定，也不影响开始门禁 */
    S.lifecycle = info.lifecycle || null;
    /* 三种重试模式的权威选项与默认值：桌面/手机共用 bootstrap 口径，
     * 覆盖 store.js 的离线兜底副本（两者由用例对照，不各写一套） */
    if (info.retry_options && info.retry_options.choices) S.retryOptions = info.retry_options;
    applyPrefs(info.prefs || {});
    /* 条款快照（bootstrap.terms）：未同意时拦下后面的首轮自动加载；
     * 首次（且本版本从未拒绝过）弹一次条款，之后只留横幅入口不强弹 */
    ingestTerms(info.terms);
    /* 独立北京时间：bootstrap 权威快照建立锚点（决策 012），
     * 桥接往返计入误差；随后由后台轮询接管后续快照 */
    ingestBusinessClock(info.business_clock, { authoritative: true, rttMs: bootRtt });
    S.businessClock.attempted = true;
    setLogin(info.login && info.login.hours_left, null,
             loginPresent(info.login, !!info.demo), info.login);
    setBusy(!!info.busy);
    ingestBootstrapPlan(info);
    /* 首次使用：六步引导代替一次性静态弹窗——但条款决定更早：未同意/未知
     * 时欢迎与引导都延后到同意后只继续一次；「暂不使用」过（declined）本
     * 会话不再自动开引导；绝不写 tour_version（看过与否只由引导自己落盘）。 */
    const tourWillAuto = tourShouldAutoStart();
    const firstRun = !S.prefs.welcome_seen;
    if (firstRun) savePrefs({ welcome_seen: true });
    if (termsLocked()) {
      S.terms.deferredIntro = {
        tour: !S.terms.declined && tourWillAuto,
        welcome: firstRun && !tourWillAuto,
      };
    } else {
      if (firstRun && !tourWillAuto) showHelp();
      if (tourWillAuto) startTour(0);
    }
  } else {
    /* 拿不到 bootstrap：条款状态也无从而知，按未知处理（fail closed），
     * 学校动作交给后端门禁 + 前端横幅双重拦住 */
    markTermsUnknown();
    setLogin(null, null);
    S.listState = "idle";
    render();
  }
  if (hasBridge()) {
    api("front_ready").catch(() => {});
    startClockPolling();
    pollClockNow();
    refresh(true);
  }
}


window.onBackend = {
  log: (line) => addLog(line),
  check: (payload) => {
    const data = payload || {};
    /* 旧轮迟到：不能覆盖新一轮的检查结论（R11）。先有界暂存，
     * 等 skipped ack 接续回这个 owner 再重放一次。 */
    if (data.request_id && S.round.check && data.request_id !== S.round.check) {
      cacheRoundPayload("check", data.request_id, data);
      return;
    }
    if (data.request_id && !markRoundApplied("check", data.request_id)) return;
    if (data.request_id && data.request_id === S.round.check) {
      clearTimeout(S.round.checkTimer);
    }
    /* 真实校验回来了：解除「检查中」，记下四态结论（valid/none/expired/unknown） */
    S.login.checking = false;
    S.login.result = data.result || null;
    /* FB4：组件结论按刷新轮次处理——本轮 check 报组件故障立即挂横幅并计入
     * 故障累计（另一分支成功/普通 unknown 不得撤）；本轮 check 无故障只收口
     * 自己的分支，撤横幅必须等本轮两分支都完成（两到达顺序语义一致） */
    const code = data.runtime_error || null;
    if (code && RUNTIME_ERRORS[code]) {
      S.runtime.roundError = code;
      S.runtime.error = code;
      S.runtime.checkPending = false;
    } else if (S.runtime.checkPending) {
      S.runtime.checkPending = false;
      if (!S.runtime.listPending) S.runtime.error = S.runtime.roundError;
    }
    if (data.demo) S.demo = true;
    /* demo 的 session_present 是 null（没有真实会话）：不能 !! 成 false，
     * 否则会走「首次未登录」板面，把演示样例全遮住（C07） */
    setLogin(data.hours_left, !!data.ok, loginPresent(data, !!data.demo), data);
    if (data.result === "valid" && firstNotice("check", data.request_id, "valid")) {
      addLog("[OK] 已复用本机登录状态，学校接口校验通过");
    }
    /* 时钟只认结构化载荷：没有结论就落 unknown，绝不从文本猜「慢几秒」（R08） */
    applyClock(data.clock);
    if (!data.ok && firstNotice("check", data.request_id, "fail")) {
      const reason = String(data.reason || data.text || "登录状态检查没通过");
      addLog(reason.startsWith("[X]") ? reason : "[X] " + reason);
    }
    paintBanners();     /* 组件横幅随 check 结论出现/撤销 */
    paintChecks();
    /* 同一轮校验结论统一收口：胶囊、时钟、大屏、路线、列表空态与各倒计时
     * 一起刷新，不留「胶囊更新了、倒计时还是旧的」混合态（上面的 request_id
     * 代次校验已保证只有本轮结果能走到这里）。 */
    syncLoginDependentView();
  },
  activities: (payload) => applyActivities(payload, false),
  done: (payload) => handleDone(payload),
  business_clock: (payload) => {
    /* 事件只作「重取通知」：先落地推送内容，再向本机取一次带往返测量的权威快照 */
    ingestBusinessClock(payload);
    pollClockNow();
  },
  plan: (payload) => handlePlan(payload),
  task: (payload) => handleTask(payload),
  plan_done: (payload) => handlePlanDone(payload),
  update: (payload) => receiveUpdate(payload),
};

bindPlaceToggle();

$("#q").addEventListener("input", (event) => setFilter("q", event.target.value));
$("#chips").addEventListener("click", (event) => {
  const chip = event.target.closest(".chip");
  if (chip) { setFilter("status", chip.dataset.status); paintChips(); }
});
$("#time").addEventListener("change", (event) => setFilter("time", event.target.value));
$("#sort").addEventListener("change", (event) => setFilter("sort", event.target.value));
$("#checkin").addEventListener("change", (event) => setFilter("checkin", event.target.value));
$("#checkout").addEventListener("change", (event) => setFilter("checkout", event.target.value));
$("#rows").addEventListener("change", (event) => {
  const check = event.target.closest(".row-check");
  if (check) toggleSelection(check.dataset.id, check.checked);
});
$("#rows").addEventListener("click", (event) => {
  if (event.target.closest(".row-check")) return;
  /* 地点是独立可聚焦控件：点它只展开/收起完整文本，不改变选择 */
  const place = event.target.closest(".c-place");
  if (place) { togglePlace(place); return; }
  const row = event.target.closest(".row");
  if (!row) return;
  const check = row.querySelector(".row-check");
  if (check && !check.disabled) { check.checked = !check.checked; toggleSelection(check.dataset.id, check.checked); }
});
/* 键盘展开地点：Enter / Space 只翻转 aria-expanded，不触发开始计划或勾选 */
$("#rows").addEventListener("keydown", (event) => {
  const place = event.target.closest(".c-place");
  if (!place) return;
  if (event.key !== "Enter" && event.key !== " " && event.key !== "Spacebar") return;
  event.preventDefault();
  togglePlace(place);
});
$("#btn-refresh").addEventListener("click", () => refresh(false));
$("#btn-login").addEventListener("click", doLogin);
$("#btn-help").addEventListener("click", showHelp);
/* 条款弹窗：展开完整条款 / 同意 / 暂不使用 / 只读关闭 / 重新确认。未同意时
 * Esc、右上角 X、暂不使用都算「暂不使用」（close 监听统一收口）；保存中 Esc
 * 被 cancel 明确拒绝；同意成功由 acceptTerms 自己走程序性关闭，不会到 decline。 */
$("#btn-terms-full").addEventListener("click", toggleTermsFull);
$("#btn-terms-accept").addEventListener("click", acceptTerms);
$("#btn-terms-decline").addEventListener("click", declineTerms);
$("#btn-help-terms").addEventListener("click", () => openTerms("help"));
$("#btn-terms-recheck").addEventListener("click", recheckTerms);
$("#btn-terms-close").addEventListener("click", () => {
  /* 只读查看的「关闭」：走原生关闭，由下面的 close 监听恢复来源焦点 */
  const dialog = $("#dlg-terms");
  if (dialog && dialog.open) dialog.close();
});
const termsDialog = $("#dlg-terms");
if (termsDialog) {
  /* 保存中的 Esc：cancel 明确拒绝（同意/暂不使用/X 已禁用）；其余按模式关闭 */
  termsDialog.addEventListener("cancel", (event) => {
    if (S.terms.saving) { event.preventDefault(); return; }
  });
  termsDialog.addEventListener("close", () => {
    /* 程序性关闭（同意成功/重新确认成功）已在各自路径处理焦点与引导 */
    if (S.terms.closeHandled) { S.terms.closeHandled = false; return; }
    /* 防御：保存中的关闭不该发生（cancel 已拦）；发生了也不记拒绝 */
    if (S.terms.saving) return;
    if (S.terms.unconfirmed) {
      /* 超时后关闭 = 离开待确认界面：不记成「同意未发生」，下次按 bootstrap 事实处理 */
      S.terms.unconfirmed = false;
      applyTermsFooter();
    } else if (!termsAccepted() && !S.terms.declined) {
      declineTerms(); /* 未同意关闭仍按原拒绝语义（Esc / X / 暂不使用按钮） */
    }
    restoreTermsFocus();
    resumeTermsTour();
  });
}
$("#clock").addEventListener("click", (event) => { event.stopPropagation(); toggleClockPop(); });
document.addEventListener("mousedown", (event) => {
  if (!event.target.closest("#clock-pop") && !event.target.closest("#clock")) closeClockPop(false);
});
$("#btn-log-folder").addEventListener("click", () => api("open_data")
  .then((res) => addLog("数据目录：" + ((res && res.path) || "已打开")))
  .catch((err) => addLog("[X] 打不开数据目录：" + msg(err))));
$("#btn-log-copy").addEventListener("click", copyLog);
$("#btn-plan").addEventListener("click", startPlan);
$("#btn-stop").addEventListener("click", stopPlan);
$("#btn-board-login").addEventListener("click", doLogin);
$("#btn-board-pick").addEventListener("click", () => {
  const id = nextOpen().bestId;
  if (id) toggleSelection(id, true);
});
/* 大屏「时间待校准」的唯一恢复入口：打开现有时间详情，不新增第二个校准入口 */
$("#board-text").addEventListener("click", (event) => {
  const target = event.target;
  if (target && target.closest && target.closest("[data-open-clock]")) toggleClockPop();
});
const stopDialog = $("#dlg-stop");
if (stopDialog) stopDialog.addEventListener("close", () => {
  /* 只有「停止计划」按钮回 yes 才真正停止；Esc/继续等待都是 no */
  if (stopDialog.returnValue === "yes") doStopPlan();
  stopDialog.returnValue = "";
});
$("#btn-clear-plan").addEventListener("click", clearSelection);
$("#plan-tasks").addEventListener("click", (event) => {
  const button = event.target.closest(".task-cancel");
  if (button) cancelTask(button.dataset.taskId);
});
$("#banners").addEventListener("click", (event) => {
  if (event.target.closest('[data-act="sleep-ok"]')) {
    S.sleepGap = 0;
    paintBanners();
  }
  if (event.target.closest('[data-act="runtime-refresh"]')) {
    /* 横幅上的「重新检查」= 一次完整刷新：check 事件带回组件结论，恢复即撤横幅 */
    refresh(false);
  }
  const termsBtn = event.target.closest('[data-act="terms-open"]');
  if (termsBtn) {
    /* 条款横幅的「查看条款」：拒绝后的再同意入口，也是唯一入口；记住被点
     * 的按钮，关闭后焦点回到它（入口被重画消失就落回 #btn-help） */
    openTerms("banner", termsBtn);
  }
});
$("#headed").addEventListener("change", (event) => savePrefs({ headed: event.target.checked }));
/* 计划级重试策略：改完即存偏好，下一个计划生效（运行中的计划不受影响） */
function setRetryPolicy(patch) {
  Object.assign(S.retryPolicy, patch);
  savePrefs({ retry_policy: Object.assign({}, S.retryPolicy) });
}
$("#retry-for").addEventListener("change", (event) => {
  const value = Number(event.target.value);
  if (["120", "600", "1800", "3600", "0"].indexOf(event.target.value) >= 0) {
    setRetryPolicy({ retry_for_s: value });
  } else {
    event.target.value = String(S.retryPolicy.retry_for_s);
  }
});
$("#max-attempts").addEventListener("change", (event) => {
  const value = Math.floor(Number(event.target.value));
  if (Number.isFinite(value) && value >= 0 && String(value).length <= 4) {
    setRetryPolicy({ max_attempts: value });
  } else {
    event.target.value = String(S.retryPolicy.max_attempts);
  }
});
$("#btn-log-toggle").addEventListener("click", () => setLogOpen(!S.logOpen));
$("#btn-log-clear").addEventListener("click", () => { $("#log").textContent = ""; $("#log-last").textContent = "还没有记录"; });
/* 六步引导：跳过/上一步/下一步；说明页重播入口；GitHub 最新版本外链（点击才打开） */
$("#tour-skip").addEventListener("click", tourSkip);
$("#tour-prev").addEventListener("click", () => {
  if (S.tour.step > 0) { S.tour.step -= 1; paintTour(); }
});
$("#tour-next").addEventListener("click", () => {
  if (S.tour.step >= TOUR_STEPS.length - 1) { tourClose(true); return; }
  S.tour.step += 1;
  paintTour();
});
/* 单帮助入口：重播仍走 replayTourFrom 的关闭模态与焦点恢复路径；
 * 计划运行中禁用重播（不打断计划），弹窗本身任何状态都能打开。 */
$("#btn-help-tour").addEventListener("click", () => replayTourFrom("#help", "#btn-help"));
$("#btn-update-check").addEventListener("click", checkUpdate);
$("#btn-update-install").addEventListener("click", startDownload);
$("#btn-update-confirm").addEventListener("click", confirmDownload);
$("#btn-update-launch").addEventListener("click", launchInstaller);
$("#btn-update-cancel").addEventListener("click", cancelDownload);
$("#btn-help-latest").addEventListener("click", () => {
  /* 用户点击才打开外链：后端用系统默认浏览器打开固定的 releases/latest 地址；
   * 打不开时把地址摆成可选文本（局部反馈），不在 WebView 里跳转 */
  helpStatus("");
  api("open_latest_release").then((res) => {
    if (!res || res.ok === false) {
      helpStatus("发布页没能打开（系统浏览器可能被拦）。地址：" + RELEASES_LATEST_URL, "warn");
    }
  }).catch((err) => {
    helpStatus("发布页没能打开：" + msg(err) + "。地址：" + RELEASES_LATEST_URL, "warn");
  });
});
$("#btn-help-data").addEventListener("click", () => {
  helpStatus("");
  api("open_data")
    .then((res) => {
      /* 成功时**不进 help-status**：那块是错误/警告位，塞成功信息会把「出错」的
         语义冲淡（既有契约也明确要求成功时保持隐藏）。路径写回说明正文里，
         用户既能看见又能选中复制，下次也不用再点一遍。 */
      const line = $("#help-data-path");
      if (line && res && res.path) line.textContent = "数据目录：" + res.path;
    })
    .catch((err) => {
      helpStatus("打不开数据目录：" + msg(err) + "。可以先再试一次，或在系统里手动打开本机数据目录。", "bad");
    });
});
/* 窗口尺寸或内部滚动变化时重新定位高亮与卡片 */
window.addEventListener("resize", () => { if (S.tour.open && !S.tour.paused) paintTour(); });
document.addEventListener("scroll", () => { if (S.tour.open && !S.tour.paused) paintTour(); }, true);
$("#empty-action").addEventListener("click", () => {
  /* 只按 paintEmpty 写下的动作意图分派：
   * 「有缓存 + 列表失败」的空态写的是 refresh，不能被 items.length 猜成清筛选。 */
  if ($("#empty-action").dataset.action === "clear-filters") clearFilters();
  else refresh(false);
});

document.addEventListener("keydown", (event) => {
  /* 打开的条款弹窗独占键盘——第一道判断，在引导/搜索处理之前：Enter/Esc/
   * Ctrl+F/`/` 不穿透到背景业务（不开始计划、不清选择、不抢搜索框），Tab、
   * 按钮激活与正常重载交给浏览器原生；保存中的 Esc 由 dialog 的 cancel 拦。 */
  const termsDlg = $("#dlg-terms");
  if (termsDlg && termsDlg.open) return;
  if (event.key === "F5") { event.preventDefault(); refresh(false); return; }
  const typing = event.target.closest && event.target.closest("input, select, textarea");
  /* 引导可见时键盘约束在卡片内：Esc=跳过、Tab 在卡片按钮间循环，
   * 背景 Ctrl+F / `/` / Enter 不移焦点、不触发开始计划；
   * 计划运行把引导暂停（tourVisible=false）时全部放行——停止操作不受影响。 */
  if (tourVisible()) {
    if (event.key === "Escape") { event.preventDefault(); tourSkip(); return; }
    if (event.key === "Tab") { tourTrapTab(event); return; }
    if ((event.ctrlKey && (event.key === "f" || event.key === "F")) ||
        (event.key === "/" && !typing)) { event.preventDefault(); return; }
    if (event.key === "Enter" && !typing) {
      const inCard = event.target.closest && event.target.closest("#tour-card");
      if (!inCard) { event.preventDefault(); return; }   /* 背景回车不开始计划 */
      return;   /* 卡片内回车：交给按钮原生激活（click 监听照常触发） */
    }
  }
  /* 帮助/停止确认打开时：Enter/Esc 只属于弹窗——
   * 不穿透到背景开始计划、不清空选择、不误跳引导；停止确认弹窗的
   * Esc/「继续等待」=不停止 的原生语义不变 */
  if ((event.key === "Enter" || event.key === "Escape") &&
      ["#help", "#dlg-stop"].some((sel) => { const d = $(sel); return !!d && d.open; })) {
    return;
  }
  /* Ctrl+F 或 / 跳到搜索框 */
  if ((event.ctrlKey && (event.key === "f" || event.key === "F")) || (event.key === "/" && !typing)) {
    event.preventDefault();
    $("#q").focus();
    $("#q").select();
    return;
  }
  if (event.key === "Escape") {
    /* 依次：关引导 → 关浮层 → 退输入 → 收起结论 → 清空选择；弹窗由浏览器自己收 */
    if (tourVisible()) { tourSkip(); return; }
    if (clockPopOpenNow()) { closeClockPop(true); return; }
    if (typing) { event.target.blur(); return; }
    if (!$("#result").hidden) { setResult(null); return; }
    if (S.selected.size && !S.busy) { clearSelection(); return; }
    return;
  }
  if (event.key !== "Enter") return;
  const active = document.activeElement;
  const tagName = active && active.tagName ? active.tagName : "BODY";
  if (["INPUT", "SELECT", "TEXTAREA", "BUTTON", "A"].indexOf(tagName) >= 0 || $("#help").open) return;
  startPlan();
});

window.addEventListener("online", () => { S.online = true; paintBanners(); paintChecks(); });
window.addEventListener("offline", () => { S.online = false; paintBanners(); paintChecks(); });

document.addEventListener("visibilitychange", () => {
  if (document.hidden) return;
  tick();
  if (!hasBridge()) return;
  api("bootstrap").then((info) => {
    if (!info) return;
    ingestBootstrapPlan(info);
    if (!!info.busy !== S.busy) setBusy(!!info.busy);
    if (!info.busy) refresh(true);
  }).catch(() => {});
});

setInterval(tick, 1000);
boot().catch((error) => {
  /* 启动链路整体失败同样没有条款依据：先按未知拦住学校动作再报错 */
  markTermsUnknown();
  addLog("[X] 界面初始化失败：" + msg(error));
  render();
});
