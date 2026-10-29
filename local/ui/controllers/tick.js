/* 每秒心跳：走字/增量重绘/睡眠检测 + doCheck 与错误人话。 */
let lastBeat = _monoMs();

function tick() {
  /* 心跳间隔用同源单调读数：系统墙钟跳变不得伪造/吞掉睡眠提示 */
  const monoMs = _monoMs();
  const gap = monoMs - lastBeat;
  lastBeat = monoMs;
  if (gap > SLEEP_GAP_MS && !S.sleepGap) {
    S.sleepGap = gap;
    paintBanners();
  }
  /* 顶栏时间只认业务北京时间：未知显示待校准，绝不拿系统时间顶上 */
  paintClockStatus();
  const popTime = $("#clock-pop-time");
  if (popTime && clockPopOpenNow()) {
    const bn = businessNowMs();
    popTime.textContent = (bn === null ? "--:--:--" : clockParts(bn).time);
  }
  /* 登录剩余按绝对锚点走字：胶囊「约剩」与打开中的时钟浮层每秒重算显示 */
  paintLogin();
  paintClockPopLeft();
  /* 某一行跨过边界（开放了、截止了）才整表重画；平时只改倒计时文字 */
  let dirty = false;
  S.rowStates.forEach((state, id) => {
    const item = findItem(id);
    if (item && stateOf(item) !== state) dirty = true;
  });
  if (dirty) {
    paintList();
  } else {
    S.rowEls.forEach((els, id) => {
      const item = findItem(id);
      if (!item) return;
      const left = leftOf(item);
      if (els.left) {
        const text = leftTextOf(item);
        if (els.left.textContent !== text) els.left.textContent = text;
        els.left.classList.toggle("is-soon", left !== null && left > 0 && left <= 600);
      }
      if (els.ago && left !== null && left <= 0 && !item.signed) els.ago.textContent = "已开放 " + durText(-left);
    });
  }
  const nowChip = document.querySelector("#rows [data-now]");
  if (nowChip) {
    /* 「现在」线只认业务北京时间：Date.now 是本机墙钟，绝不顶上（决策 012） */
    const bnForChip = businessNowMs();
    nowChip.textContent = bnForChip === null ? "待校准" : hm(bnForChip);
  }
  /* 阶段在秒与秒之间会变（开放那一刻、第一次真实提交）：变了才重画摘要，
   * 让标题与批次说法跟 LED/任务卡同步，不必每秒重建整块标题区。 */
  const runPhase = S.busy ? executionView().phase : null;
  if (runPhase !== S.lastRunPhase) {
    S.lastRunPhase = runPhase;
    paintSelected();
  }
  paintRail();
  paintCountdown();
  paintLed();
  paintRoute();
  /* 未终态任务行：每秒按同一份阶段解释走字。
   * 开放那一刻 preopen→preparing 时，标签与说明必须跟着改，
   * 不能初次渲染正确、下一秒又被原始 submitting 文案盖回去。 */
  if (S.plan.tasks.size) {
    const now = bnowS();   // 未校准传 null：任务卡显示等待/待确认，绝不猜已开放
    S.plan.tasks.forEach((task) => {
      if (RUNNING_STATUSES.indexOf(String(task.status)) < 0) return;
      const row = document.querySelector('.task-row[data-task-id="' + cssEscape(String(task.task_id)) + '"]');
      if (!row) return;
      const detail = row.querySelector(".task-detail");
      if (detail) detail.textContent = taskModePrefix(task) + taskDisplayDetail(task, now);
      const label = row.querySelector(".task-state");
      if (label) label.textContent = taskDisplayLabel(task);
    });
  }
}

function doCheck() {
  api("check").catch((err) => addLog("[X] 检查登录状态失败：" + msg(err)));
}

function stripLabel(label) { return String(label || "").replace(/^等待并抢报名[：:]\s*/, "").trim(); }

function cleanReason(reason) {
  let value = String(reason == null ? "" : reason).trim();
  for (let i = 0; i < 3; i += 1) {
    const next = value.replace(/^\[X\]\s*/, "").replace(/^没抢到[：:]\s*/, "").trim();
    if (next === value) break;
    value = next;
  }
  return value || "原因不明";
}

function grabHint(reason) {
  if (/名额|已满|满了|抢完/.test(String(reason))) return "名额被抢完了。等下一场，或换一个活动再试。";
  if (/还没|未开放|没放开|页面/.test(String(reason))) return "页面还没放开。点「刷新」确认开放时间，再试一次。";
  if (/登录|登陆|session|401|403|token/i.test(String(reason))) return "登录状态已失效。先点「登录」重新登录，再点「刷新」。";
  if (/网络|连接|超时|timeout|连不上|dns|ssl/i.test(String(reason))) return "连不上学校站点。检查校园网或代理，恢复后点「刷新」。";
  return "先点「刷新」看看列表，再重新选一次活动。";
}

function failHint(text) {
  const raw = String(text || "");
  if (S.login.present === false || S.login.result === "none") return "完成登录后点「刷新」加载活动。";
  if (S.login.result === "expired") return "点「重新登录」后刷新。";
  if (S.login.result === "unknown") return "恢复网络后点「刷新」重试。";
  if (needLogin(raw)) return "点「重新登录」后刷新。";
  if (/网络|连接|超时|timeout|连不上|dns|ssl|代理/i.test(raw)) return "连不上学校站点。检查校园网或代理，恢复后点「重试」。";
  return "程序组件没能正常启动。先点「刷新」；还不行就看下面的运行记录。";
}

function listErrorCopy(raw) {
  /* 组件问题优先：缺组件时列表失败不许被翻译成「先登录/重新登录」 */
  if (S.runtime.error) {
    const copy = runtimeErrorCopy();
    return { title: copy.title, text: copy.hint };
  }
  const text = failHint(raw);
  if (S.login.present === false || S.login.result === "none") return { title: "还没登录", text };
  if (S.login.result === "expired") return { title: "登录已过期", text };
  if (S.login.result === "unknown") return { title: "暂时无法加载", text };
  return { title: "活动暂时没加载", text };
}

function needLogin(text) { return /登录状态|登录状态|未登录|请先登录|session|401|403|token/i.test(String(text || "")); }
function leftText(hours) { return hours >= 1 ? Math.round(hours) + " 小时" : Math.max(1, Math.round(hours * 60)) + " 分钟"; }
function numOrNull(value) { return typeof value === "number" && isFinite(value) ? value : null; }
function msg(error) { return String((error && error.message) || error || "未知错误"); }

/* ---------------------------------------------------------------- 时间工具 */
/* 学校时间恒为 UTC+8：不跟随本机时区，也不因夏令时变化。
 * 界面上的「学校时刻」一律用这套格式化；顶栏那颗时钟仍显示本机时间，
 * 因为它是给人看「这台电脑现在几点」的。 */
