/* 条款门禁用例：状态编排、确认/拒绝/复查与桥接效果（DOM 呈现见 views/terms.js）。 */
function termsAccepted() {
  return !!S.terms.version && S.terms.acceptedVersion === S.terms.version;
}

/* 学校动作是否被条款拦下（决策 013 返修）：
 * - 离线演示（demo）不做学校网络动作，不受限；
 * - unknown（bootstrap 失败、返回 null、缺 terms、版本为空或结构非法）一律拦，
 *   没有任何依据可以放行学校动作；
 * - 有版本但没同意当前版本 → 拦；
 * - 还没拿到任何条款状态（version 为空且未标 unknown，例如测试桩或桥接未
 *   就绪）→ 前端没有依据，交给后端门禁兜底，不在此处冒充「已确认未同意」。 */
function termsLocked() {
  if (S.demo) return false;
  if (S.terms.unknown) return true;
  if (!S.terms.version) return false;
  return !termsAccepted();
}

/* 后端明确回绝时的界面收敛：后端是最终防线，前端要把状态跟上——
 * 有版本就按「未同意当前版本」，没有版本就按未知，两者都不放行学校动作。 */
function noteTermsRequired(res) {
  if (res && res.terms && String(res.terms.version || "")) {
    S.terms.version = String(res.terms.version);
    S.terms.acceptedVersion = String(res.terms.accepted_version || "");
    S.terms.unknown = false;
  } else if (!S.terms.version) {
    S.terms.unknown = true;
  }
  paintTerms();
  paintBanners();
  updateControls();
  if (!S.terms.declined && !S.terms.unknown) openTerms();
}

/* 条款状态未知：拦住学校动作并挂可重试横幅；没有版本时不强弹条款，
 * 只保留帮助与横幅里的显式入口（用户自己点才会打开）。 */
function markTermsUnknown() {
  S.terms.version = "";
  S.terms.acceptedVersion = "";
  S.terms.acceptedAt = "";
  S.terms.unknown = true;
  S.terms.declined = false;
  S.terms.prompted = false;
  paintTerms();
  paintBanners();
  updateControls();
}

/* bootstrap.terms 的唯一入口：版本、已同意版本与同意时间进会话状态；
 * 需要强弹的条件 = 未同意 && 本会话没弹过 && 本机没拒绝过这一版
 * && 真实形态（离线演示不做学校网络动作，不弹条款，决策 013）。 */
function ingestTerms(terms) {
  const data = (terms && typeof terms === "object") ? terms : {};
  const version = String(data.version || "");
  if (!version) {
    /* 后端没给条款版本：可能是 bootstrap 失败、terms 缺失、版本为空或结构
     * 非法。此时不能算「未同意」也不能放行——按未知处理，fail closed。 */
    markTermsUnknown();
    return;
  }
  S.terms.version = version;
  S.terms.acceptedVersion = String(data.accepted_version || "");
  S.terms.acceptedAt = String(data.accepted_at || "");
  S.terms.unknown = false;
  /* 重启后把「本版本拒绝过」恢复成会话状态：否则 refresh、F5 和可见性恢复
   * 会因为 declined=false 又把条款弹一遍（返修缺陷 1）。 */
  const declinedSaved = String(S.prefs.terms_declined_version || "");
  if (!termsAccepted() && declinedSaved === version) S.terms.declined = true;
  paintTerms();
  if (!termsAccepted() && !S.demo && !S.terms.prompted && !S.terms.declined) {
    S.terms.prompted = true;
    openTerms();
  }
  paintBanners();
  updateControls();
}

/* 条款弹窗：从「要点 + 查看完整条款」开始；帮助里的「查看条款」与横幅
 * 「查看条款并同意」都走这一个入口，也是拒绝后唯一的再同意通道。 */
function openTerms(source, focusBack) {
  /* 已打开时不重置任何状态：后台 refresh→noteTermsRequired 会在保存中/未确认
   * 期间再次调进来，重置会打断在途保存（modal 打开时用户入口本来也点不到） */
  if (!termsDialogAvailable()) return;
  /* 每次打开复位交互态：上一轮的保存/未确认/程序性关闭标记都不带进来；
   * 请求代号 +1 作废在途回执（重新核对期间关窗再开的迟到结果不许覆盖）；
   * 来源与焦点返回目标按入口记录（自动弹没有入口，关闭时落回 #btn-help）。 */
  S.terms.saving = false;
  S.terms.unconfirmed = false;
  S.terms.closeHandled = false;
  /* 用例会整体替换 S.terms 为缺字段的普通对象：代号一律从 0 起算，
   * 避免 undefined+1=NaN 让回执永远对不上号 */
  S.terms.requestSeq = (S.terms.requestSeq || 0) + 1;
  S.terms.source = source || "auto";
  S.terms.focusBack = focusBack || null;
  /* 任何时刻都不叠两个 open dialog：帮助入口先关帮助（关闭后按来源重开） */
  closeHelpIfOpen();
  resetTermsFullView();
  /* 只读查看：已同意同版只显示「关闭」——不写时间、不写 declined、不刷新 */
  S.terms.viewOnly = termsAccepted() && !S.terms.unknown;
  applyTermsFooter();
  /* 条款状态未知时给出明确说明：弹窗里的全文仍可阅读，同意会按程序当前
   * 条款版本记录；不让用户在「版本 ——」的情况下猜自己同意了什么。 */
  if (S.terms.viewOnly) {
    termsStatus("已同意本版（" + (S.terms.acceptedAt || "同意时间未记录") +
      "）；条款更新后会再次展示并要求重新同意。", "info");
  } else {
    termsStatus(S.terms.unknown
      ? "本机没读到条款状态，登录、列表和报名计划已停用。下面的完整条款仍可阅读；"
        + "点「我已阅读并同意」会按程序的当前条款版本记录。"
      : "");
  }
  /* 已有引导遇条款暂时让位：只暂停不跳步，关闭后回到原步骤 */
  if (tourVisible()) {
    S.terms.tourYield = true;
    tourPauseNow();
  }
  showTermsDialog();
}

/* 关闭后按来源恢复焦点：状态清理在用例层，焦点落点的呈现细节在 views/terms.js。 */
function restoreTermsFocus() {
  const source = S.terms.source;
  const back = S.terms.focusBack;
  S.terms.source = "";
  S.terms.focusBack = null;
  restoreTermsFocusTo(source, back);
}

/* 打开条款时让位的引导：关闭后回到原步骤（step 没被改动）；计划仍在暂停
 * 引导时不抢着恢复，交给计划结束时的既有恢复路径。 */
function resumeTermsTour() {
  if (!S.terms.tourYield) return;
  S.terms.tourYield = false;
  if (S.tour.open && S.tour.paused && !S.busy) tourResumeNow();
}

/* boot 时被条款推迟的首次欢迎/引导：同意后只继续一次，引导适用性再判定
 * 一次；绝不写 tour_version（不伪造「已看过」）。 */
function runDeferredIntro() {
  const deferred = S.terms.deferredIntro;
  S.terms.deferredIntro = null;
  if (!deferred || termsLocked()) return;
  if (deferred.tour && tourShouldAutoStart()) {
    startTour(0);
    return;
  }
  if (deferred.welcome) showHelp();
}

/* 同意：后端 accept_terms 经 ui.json 原子事务落盘；成功才关弹窗解锁，
 * 并补上启动时被条款拦下的首轮加载。失败留在弹窗里给出重试说明。
 * 回执必须自证：version / accepted_version / accepted_at 三项齐全、
 * accepted_version 与 version 一致，且 version 与你实际读到的内嵌全文同版；
 * 任何一项不满足都只提示、不改状态、不清 unknown、不关弹窗、不刷新——
 * 未知态（bootstrap 失败、缺条款字段）绝不允许本地乐观解锁，也不能用过期
 * 的条款副本把学校动作放出去（fail closed）。 */
function acceptTerms() {
  /* 在途保存、超时未确认、只读查看期间禁止直接再次写同意：先验状态，
   * 零请求零写入——连续点击只会登记一次在途、发出一次请求。 */
  if (S.terms.saving || S.terms.unconfirmed || S.terms.viewOnly) return;
  /* 提交的必须是用户实际读到的那份全文版本（内嵌全文的版本节点），
   * 不是后端自报的版本——否则旧 HTML 也能给新后端「同意当前版」。
   * 副本没标版本、或与已知程序版本不一致时先在本地拒绝，零请求零写入；
   * 状态未知（尚无 S.terms.version）时仍把有效副本交给后端判定。 */
  const documentVersion = termsDocVersion();
  if (!documentVersion) {
    termsStatus("当前副本没有标明版本，无法确认你读的是哪一版；请重新下载完整版本后再确认。", "warn");
    return;
  }
  if (S.terms.version && documentVersion !== S.terms.version) {
    termsStatus("本份条款（" + documentVersion + "）与程序要求的版本（" +
      S.terms.version + "）不一致，没有解锁；请重新下载完整版本后再确认。", "warn");
    return;
  }
  /* 同步登记在途再发请求：按钮立即进入保存态（正在保存…、三控件禁用、
   * Esc/X 被拦）；10 秒没有可确认结果就转「保存结果未确认」——门禁保持、
   * 关闭权限恢复，原请求代号作废，迟到回执一律丢弃。 */
  const seq = (S.terms.requestSeq = (S.terms.requestSeq || 0) + 1);
  S.terms.saving = true;
  applyTermsFooter();
  termsStatus("正在保存…", "info");
  const timer = setTimeout(() => termsSaveTimeout(seq), 10000);
  const settle = () => {
    clearTimeout(timer);
    if (seq === S.terms.requestSeq && S.terms.saving) {
      S.terms.saving = false;
      applyTermsFooter();
    }
  };
  api("accept_terms", documentVersion).then((res) => {
    /* 迟到回执（已超时或新一轮请求已开始）：不关窗、不覆盖新状态、不重复刷新 */
    if (seq !== S.terms.requestSeq) return;
    /* 任何明确结果（成功/失败）都收口在途：按钮恢复、状态文案随后覆盖 */
    settle();
    if (!res || res.ok !== true) {
      if (res && res.error === "terms_version_mismatch") {
        /* 副本与程序不同源：保持门禁，只说本份与程序版本不一致、要重新
         * 下载完整版本——不建议「重新打开同一份」，那修不了版本差异。 */
        termsStatus("本份条款（" + (documentVersion || "未标明版本") +
          "）与程序要求的版本不一致，没有解锁；请重新下载完整版本后再确认。", "warn");
        return;
      }
      termsStatus((res && res.text) || "条款同意状态没有保存成功，请再点一次。", "warn");
      return;
    }
    const terms = (res.terms && typeof res.terms === "object") ? res.terms : {};
    const version = String(terms.version || "");
    const acceptedVersion = String(terms.accepted_version || "");
    const acceptedAt = String(terms.accepted_at || "");
    if (!version || !acceptedVersion || !acceptedAt || acceptedVersion !== version) {
      /* 回执不完整/自相矛盾：只能说「保存结果无法确认」——没有证据断言
       * 后端没写入，也不能在这里擅自解锁或撤销已发出的事务。 */
      termsStatus("保存结果无法确认（回执缺字段或自相矛盾，版本 " +
        (version || "缺失") + "）；请点「重新确认状态」核对本机记录。", "warn");
      return;
    }
    /* 用户实际读到的全文版本：取自 index.html 内嵌条款的版本字段。
     * 空值同样按不可确认处理——没有可比对的副本就不解锁。 */
    const docVersion = termsDocVersion();
    if (docVersion !== version) {
      termsStatus("当前条款副本不匹配（本份 " + (docVersion || "未标明版本") +
        "，程序 " + version + "），没有解锁；请重新下载完整版本后再确认。", "warn");
      return;
    }
    /* 显式同意成功收口：清未确认，状态、焦点、引导按成功路径各走一次 */
    S.terms.unconfirmed = false;
    S.terms.viewOnly = false;
    S.terms.version = version;
    S.terms.acceptedVersion = acceptedVersion;
    S.terms.acceptedAt = acceptedAt;
    S.terms.unknown = false;
    S.terms.declined = false;
    S.terms.prompted = true;
    /* 拒绝版本标记失效：立即落盘（不因关窗丢失），条款更新前不会再弹 */
    savePrefsNow({ terms_declined_version: "" });
    /* 程序性关闭：跳过 close 监听里的拒绝语义与焦点恢复，改走成功路径 */
    S.terms.closeHandled = true;
    closeTermsDialog();
    paintTerms();
    paintBanners();
    updateControls();
    if (hasBridge()) refresh(true); /* 完整成功最多补加载一次 */
    focusHelpButton();
    resumeTermsTour(); /* 打开时让位的引导回原步骤 */
    runDeferredIntro(); /* boot 被条款推迟的首次欢迎/引导只继续一次 */
  }).catch((err) => {
    if (seq !== S.terms.requestSeq) return;
    settle();
    termsStatus("条款同意状态没有保存成功：" + msg(err) + "；请再点一次。", "warn");
  });
}

/* 10 秒没拿到可确认结果：不是失败也不是取消——门禁保持、关闭权限恢复、
 * 禁止直接再次写同意，改走「重新确认状态」核对本机记录。原请求代号作废，
 * 迟到回执不许关窗或覆盖这个状态。 */
function termsSaveTimeout(seq) {
  if (seq !== S.terms.requestSeq) return;
  S.terms.requestSeq = (S.terms.requestSeq || 0) + 1;
  S.terms.saving = false;
  S.terms.unconfirmed = true;
  applyTermsFooter();
  termsStatus("保存结果未确认：没有收到可确认的结果，本机是否已记录同意还不确定；" +
    "门禁保持关闭。点「重新确认状态」核对本机记录。", "warn");
}

/* 重新确认状态：只读一次 bootstrap 的条款快照（不调用 boot/refresh/check/
 * list），不写任何东西——按本机事实收口未确认态。 */
function recheckTerms() {
  if (S.terms.saving || !S.terms.unconfirmed) return;
  if (!termsDialogIsOpen()) return;
  const seq = (S.terms.requestSeq = (S.terms.requestSeq || 0) + 1);
  setRecheckBusy(true);
  const resetRecheck = () => setRecheckBusy(false);
  api("bootstrap").then((info) => {
    /* 关窗或新一轮操作后到达的读取结果一律丢弃：不覆盖、不重复收口 */
    if (seq !== S.terms.requestSeq || !termsDialogIsOpen()) { resetRecheck(); return; }
    const data = (info && info.terms && typeof info.terms === "object") ? info.terms : {};
    const version = String(data.version || "");
    const acceptedVersion = String(data.accepted_version || "");
    const acceptedAt = String(data.accepted_at || "");
    const docVersion = termsDocVersion();
    if (version && acceptedVersion === version && acceptedAt && docVersion === version) {
      /* 明确已同意同版、有时间、且匹配本份：按显式同意成功收口（不再发写请求） */
      S.terms.version = version;
      S.terms.acceptedVersion = acceptedVersion;
      S.terms.acceptedAt = acceptedAt;
      S.terms.unknown = false;
      S.terms.declined = false;
      S.terms.prompted = true;
      S.terms.unconfirmed = false;
      savePrefsNow({ terms_declined_version: "" });
      S.terms.closeHandled = true;
      closeTermsDialog();
      paintTerms();
      paintBanners();
      updateControls();
      if (hasBridge()) refresh(true);
      focusHelpButton();
      resumeTermsTour();
      runDeferredIntro();
      return;
    }
    if (version && (!acceptedVersion || acceptedVersion !== version)) {
      /* 明确未同意：恢复同意按钮，可以重新点一次（仍受本地副本校验） */
      S.terms.version = version;
      S.terms.acceptedVersion = acceptedVersion;
      S.terms.acceptedAt = acceptedAt;
      S.terms.unknown = false;
      S.terms.unconfirmed = false;
      applyTermsFooter();
      resetRecheck();
      termsStatus("本机尚未记录同意（要求版本 " + version +
        "）；可以重新点「我已阅读并同意」。", "warn");
      return;
    }
    /* 读不到版本、回执缺时间、本份不匹配：保留未确认，不改门禁不改状态 */
    resetRecheck();
    termsStatus("还是没能确认：本机记录与本份条款对不上或没读到版本；门禁保持关闭。", "warn");
  }).catch(() => {
    if (seq !== S.terms.requestSeq || !termsDialogIsOpen()) { resetRecheck(); return; }
    resetRecheck();
    termsStatus("没能读到条款状态，仍按未确认处理；门禁保持关闭。", "warn");
  });
}

/* 拒绝：不退出、本版本不再强弹；把拒绝版本落盘（跨重启生效，条款版本
 * 变化时会重新提示）。登录/列表/报名计划保持禁用，横幅与帮助保留入口。 */
function declineTerms() {
  /* 保存中/超时未确认/只读查看都不记拒绝：保存中的关闭由 cancel 拦，
   * 这里再防御一次，避免任何路径把在途保存写成「同意未发生」。 */
  if (S.terms.saving || S.terms.unconfirmed || S.terms.viewOnly) return;
  /* 重复拒绝是幂等操作：只在首次拒绝时置标记并落盘，但仍关窗和刷新界面 */
  if (!S.terms.declined) {
    S.terms.declined = true;
    /* 版本未知时没有可锚定的版本，不写空标记（写了也拦不住下一次成功读取） */
    if (S.terms.version) savePrefsNow({ terms_declined_version: S.terms.version });
  }
  closeTermsDialog();
  paintBanners();
  updateControls();
}
