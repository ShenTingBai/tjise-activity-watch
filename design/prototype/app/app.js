/* 准点 · 天软活动报名 —— 界面逻辑
 *
 * 只按 local/app.py 的真实契约说话：
 *   调用  window.pywebview.api.bootstrap / check / list_activities / login / grab / stop / open_data / save_prefs / front_ready
 *   事件  window.onBackend.log / check / activities / done
 * 原型里由 proto/mock-backend.js 冒充后端；落地时删掉 app.html 里那一行 <script> 即可。
 *
 * 一个核心事实决定了整个界面：app.py 同一时间只跑一个子进程（_spawn 里有锁），
 * 登录和抢报名共用这一个位置。所以界面上只有一块「大屏」，谁占着位置就显示谁。
 */
(function () {
  "use strict";

  /* ------------------------------------------------ 与 grab.py / app.py 对齐的常量 */
  const LEAD_S = 100;       // grab.py --lead：开放前 100 秒打日志「开始等待开放」（页面其实一启动就打开了）
  const POLL_S = 5;         // grab.py POLL_LEAD：开放前 5 秒开始每 0.3 秒找「立即报名」
  const WINDOW_S = 120;     // grab.py --window：开放后最多再试 120 秒
  const TOKEN_H = 17.5;     // app.py TOKEN_LIFETIME_H：登录态寿命估算
  const EXPIRING_H = 2;     // 剩不到 2 小时就提醒
  const LOGIN_WAIT_S = 600; // grab.py login --timeout
  const SLEEP_GAP_MS = 120e3;

  const now = () => (window.ProtoClock ? window.ProtoClock.now() : Date.now());

  /* ------------------------------------------------ 小工具 */
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const pad = (n) => String(n).padStart(2, "0");
  const WEEK = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

  function parseTs(s) {
    const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?/.exec(String(s || ""));
    if (!m) return null;
    return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0)).getTime();
  }
  function fmtTs(ms) {
    const d = new Date(ms);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }
  const hms = (ms) => { const d = new Date(ms); return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };
  const hm = (ms) => { const d = new Date(ms); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
  /* 学校常把开放时间定在 00:59:59 这种点上：有秒就把秒带上 */
  const timeLabel = (ms) => (new Date(ms).getSeconds() ? hms(ms) : hm(ms));
  const startOfDay = (ms) => { const d = new Date(ms); d.setHours(0, 0, 0, 0); return d.getTime(); };
  const dayDiff = (ms, ref) => Math.round((startOfDay(ms) - startOfDay(ref)) / 86400e3);
  const dateLabel = (ms) => { const d = new Date(ms); return `${d.getMonth() + 1}月${d.getDate()}日`; };
  function relDay(ms, ref) {
    const k = dayDiff(ms, ref);
    return k === 0 ? "今天" : k === 1 ? "明天" : k === 2 ? "后天" : k === -1 ? "昨天" : "";
  }
  function whenLabel(ms, ref) {
    const r = relDay(ms, ref);
    return `${r || dateLabel(ms) + " " + WEEK[new Date(ms).getDay()]} ${timeLabel(ms)}`;
  }
  function clockAt(ms, ref) { return dayDiff(ms, ref) === 0 ? timeLabel(ms) : whenLabel(ms, ref); }
  function dur(sec) {
    sec = Math.max(0, Math.floor(sec));
    const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    if (d) return `${d} 天 ${h} 时`;
    if (h) return `${h} 时 ${pad(m)} 分`;
    if (m) return `${m} 分 ${pad(s)} 秒`;
    return `${s} 秒`;
  }
  function durCoarse(sec) {
    sec = Math.max(0, sec);
    if (sec >= 2 * 3600) return `${Math.floor(sec / 3600)} 小时`;
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
    return h ? `${h} 时 ${pad(m)} 分` : `${Math.max(1, m)} 分钟`;
  }
  function ago(sec) {
    sec = Math.max(0, Math.floor(sec));
    if (sec < 60) return "不到 1 分钟";
    if (sec < 3600) return `${Math.floor(sec / 60)} 分钟`;
    return `${Math.floor(sec / 3600)} 时 ${pad(Math.floor((sec % 3600) / 60))} 分`;
  }
  function ledTime(sec) {
    sec = Math.max(0, Math.floor(sec));
    return `${pad(Math.floor(sec / 3600))}:${pad(Math.floor((sec % 3600) / 60))}:${pad(sec % 60)}`;
  }

  const ICONS = {
    check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    cross: '<path d="M6 6l12 12M18 6L6 18"/>',
    alert: '<path d="M12 4l9 16H3z"/><path d="M12 10v4M12 17h.01"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
    pin: '<path d="M12 21s-6-5.3-6-10a6 6 0 1 1 12 0c0 4.7-6 10-6 10z"/><circle cx="12" cy="11" r="2.2"/>',
    cal: '<rect x="4" y="5" width="16" height="15" rx="2"/><path d="M4 10h16M9 3v4M15 3v4"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    login: '<path d="M14 4h4a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-4"/><path d="M10 16l4-4-4-4"/><path d="M14 12H4"/>',
    refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>',
    play: '<path d="M8 5.5v13l10-6.5z"/>',
    stop: '<rect x="6.5" y="6.5" width="11" height="11" rx="1.5"/>',
    swap: '<path d="M7 7h12l-3-3M17 17H5l3 3"/>',
    wifi: '<path d="M3 3l18 18"/><path d="M8.5 16.4a5 5 0 0 1 7 0M5 12.9a10 10 0 0 1 4.8-2.6M19 12.9a10 10 0 0 0-2.3-1.6M2 9.4a15 15 0 0 1 4.3-2.7M22 9.4A15 15 0 0 0 11 5.8"/><path d="M12 20h.01"/>',
    moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z"/>',
    log: '<path d="M5 5h14M5 10h14M5 15h9M5 20h6"/>',
  };
  const icon = (n) => `<svg class="i" viewBox="0 0 24 24" aria-hidden="true">${ICONS[n] || ""}</svg>`;

  /* ------------------------------------------------ 后端调用 */
  function api(name, ...args) {
    const a = window.pywebview && window.pywebview.api;
    const fn = a && a[name];
    if (typeof fn !== "function") return Promise.resolve({ ok: false, text: "后端还没准备好（" + name + "）" });
    try {
      return Promise.resolve(fn.apply(a, args)).catch((e) => ({ ok: false, text: String((e && e.message) || e) }));
    } catch (e) {
      return Promise.resolve({ ok: false, text: String((e && e.message) || e) });
    }
  }

  /* ------------------------------------------------ 状态 */
  const S = {
    booted: false,
    version: "",
    prefs: { welcome_seen: false, filter: { q: "", status: "free", time: "all" }, log_open: false, headed: false, sound: false },
    login: { state: "unknown", expiresAt: null, checking: false },   // unknown | pending | valid | expired | none | unreachable
    clock: { skew: 0, checked: false },
    list: { state: "idle", loading: false, errorKind: null, errorText: "", loadedAt: null },
    items: [],
    byId: new Map(),
    justSigned: new Set(),
    nextId: null,
    selectedId: null,
    slot: null,        // 占着大屏的任务：{kind:"login"|"grab"|"unknown", ...}
    result: null,      // 任务结束后留在大屏上的结论，点「知道了」才收起
    stopRequested: false,
    pendingSwitch: null,
    flicker: false,
    online: navigator.onLine !== false,
    sleep: null,
    logs: [],
    rowStates: new Map(),
  };

  /* ------------------------------------------------ 活动 */
  function normalize(raw) {
    return {
      id: String(raw.id),
      name: String(raw.name || "（没有名字的活动）"),
      place: String(raw.place || ""),
      signStart: parseTs(raw.sign_start),
      signEnd: parseTs(raw.sign_end),
      actStart: parseTs(raw.act_start),
      signStartText: String(raw.sign_start || ""),
      signed: !!raw.signed,
    };
  }
  /* 状态按时间现算：列表是快照，开放、截止都会随时间变 */
  function stateOf(it, t) {
    if (it.signed || S.justSigned.has(it.id)) return "signed";
    if (!it.signStart) return "unknown";
    if (t < it.signStart) return "waiting";
    if (it.signEnd && t > it.signEnd) return "closed";
    return "open";
  }
  const selectedItem = () => (S.selectedId ? S.byId.get(S.selectedId) || null : null);
  function computeNext(t) {
    let best = null;
    for (const it of S.items) {
      if (stateOf(it, t) !== "waiting") continue;
      if (!best || it.signStart < best.signStart) best = it;
    }
    return best ? best.id : null;
  }
  function actLabel(it, t) { return it.actStart ? "活动 " + whenLabel(it.actStart, t) : "活动时间待定"; }

  /* ------------------------------------------------ 事件（后端推送） */
  window.onBackend = {
    log(line) { addLog(line); if (S.slot) feedSlot(String(line == null ? "" : line)); },
    check(p) { onCheck(p || {}); },
    activities(p) { onActivities(p || {}); },
    done(p) { onDone(p || {}); },
    state() { /* app.py 文档里提过，实际不发；留空兼容 */ },
  };

  function onCheck(p) {
    S.login.checking = false;
    S.login.expiresAt = typeof p.hours_left === "number" ? now() + p.hours_left * 3600e3 : null;
    // check 只回最后一行：时钟偏差的提醒正好是最后一行（grab.py cmd_check）
    const sk = /相差\s*([+-]?\d+)\s*秒/.exec(p.text || "");
    S.clock.skew = sk ? parseInt(sk[1], 10) : 0;
    S.clock.checked = !!p.ok;
    if (p.ok) {
      S.login.state = "valid";
    } else if (!S.list.loading && S.list.errorKind && S.list.errorKind !== "error") {
      S.login.state = S.list.errorKind;
    } else if (p.hours_left == null) {
      S.login.state = "none";
    } else {
      // 失败时 check 的 text 只剩一句「请运行 python grab.py login」，分不清是过期还是断网，
      // 等活动列表的报错来细分（它带完整的 [X] 原话）
      S.login.state = "pending";
      clearTimeout(S.pendingTimer);
      S.pendingTimer = setTimeout(() => { if (S.login.state === "pending") { S.login.state = "expired"; renderAll(); } }, 4000);
    }
    renderAll();
  }

  function classifyListError(text) {
    const t = String(text || "");
    if (/还没有登录过/.test(t)) return "none";
    if (/登录态已过期|登录态已失效|身份验证失败|4001/.test(t)) return "expired";
    if (/接口异常|拉取活动失败|执行超时|URLError|timed out|Timeout|Connection/i.test(t)) return "unreachable";
    return "error";
  }

  function onActivities(p) {
    S.list.loading = false;
    if (p.ok) {
      S.items = (p.items || []).map(normalize);
      S.byId = new Map(S.items.map((it) => [it.id, it]));
      for (const id of Array.from(S.justSigned)) { const it = S.byId.get(id); if (it && it.signed) S.justSigned.delete(id); }
      S.list.state = "ok"; S.list.errorKind = null; S.list.errorText = ""; S.list.loadedAt = now();
      if (!(S.slot && S.slot.kind === "login")) S.login.state = "valid";
      if (S.selectedId && !S.byId.has(S.selectedId)) S.selectedId = null;
      if (S.result && S.result.awaitingCheck) S.result.awaitingCheck = false;
    } else {
      const kind = classifyListError(p.text);
      S.list.state = "error"; S.list.errorKind = kind; S.list.errorText = String(p.text || "");
      if (kind !== "error" && !(S.slot && S.slot.kind === "login")) S.login.state = kind;
      // 没抢到之后顺手复查的结果：登录态失效往往才是真正原因
      if (S.result && S.result.awaitingCheck) {
        S.result.awaitingCheck = false;
        if (kind === "expired" || kind === "none") S.result.loginSuspect = true;
        if (kind === "unreachable") S.result.netSuspect = true;
      }
    }
    S.nextId = computeNext(now());
    renderAll();
  }

  /* 子进程原话 → 大屏上的进度。只认 grab.py 真会打印的句子 */
  function feedSlot(raw) {
    const sl = S.slot;
    const m = /^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (.*)$/.exec(raw);
    const at = m ? parseTs(m[1]) : now();
    const text = (m ? m[2] : raw).trim();
    if (!text) return;
    sl.tail.push(text);
    if (sl.tail.length > 60) sl.tail.shift();
    if (sl.kind !== "grab") return;
    let hit;
    if (/详情页已加载/.test(text)) sl.pageAt = at;
    else if ((hit = /第\s*(\d+)\s*次提交：(.*)$/.exec(text))) sl.attempts.push({ n: +hit[1], reason: hit[2].trim(), at });
    else if (/你已经报过这个活动了|该活动已是报名状态/.test(text)) sl.already = true;
    else if ((hit = /本机时钟与服务器相差\s*([+-]?\d+)\s*秒/.exec(text))) { S.clock.skew = parseInt(hit[1], 10); S.clock.checked = true; }
    renderBoard();
    renderTop();
  }

  function onDone(p) {
    const sl = S.slot;
    const stopped = S.stopRequested;
    S.slot = null;
    S.stopRequested = false;
    if (p.kind === "login" || (sl && sl.kind === "login")) finishLogin(p, sl, stopped);
    else finishGrab(p, sl, stopped);
    renderAll();
  }

  const cleanReason = (r) => String(r || "").replace(/^\[X\]\s*/, "").replace(/^没抢到[：:]\s*/, "").trim();

  function finishGrab(p, sl, stopped) {
    const name = (sl && sl.name) || String(p.label || "").replace(/^等待并抢报名[：:]/, "") || "这一场";
    if (S.pendingSwitch) {
      const id = S.pendingSwitch;
      S.pendingSwitch = null;
      S.result = null;
      S.selectedId = id;
      toast(`已停下「${name}」`);
      setTimeout(() => arm(true), 0);
      return;
    }
    const base = { kind: "grab", id: sl ? sl.id : null, name, at: now(), attempts: sl ? sl.attempts.length : 0, mode: sl ? sl.mode : "scheduled" };
    if (stopped) {
      S.result = Object.assign(base, { outcome: "stopped" });
      toast(`已停止，不会再替你报名「${name}」`);
      return;
    }
    if (p.ok) {
      S.result = Object.assign(base, { outcome: "success" });
      if (sl) S.justSigned.add(sl.id);
      Sound.play("ok");
      announce(`报名成功：${name}`);
      refresh({ quiet: true });
      return;
    }
    if (sl && sl.already) {
      S.result = Object.assign(base, { outcome: "already" });
      S.justSigned.add(sl.id);
      refresh({ quiet: true });
      return;
    }
    const c = classifyFail(p, sl);
    S.result = Object.assign(base, { outcome: "fail", awaitingCheck: true }, c);
    Sound.play("bad");
    announce(`没抢到：${c.short}`);
    refresh({ quiet: true });   // 顺手复查登录态和列表
  }

  function classifyFail(p, sl) {
    const tail = sl ? sl.tail : [];
    const reason = cleanReason(p.reason);
    const all = [reason].concat(tail).join("\n");
    const last = sl && sl.attempts.length ? sl.attempts[sl.attempts.length - 1].reason : "";
    if (/还没有登录过|登录态已过期|登录态已失效|身份验证失败|4001/.test(all)) return { cat: "login", short: "登录态失效了" };
    if (/没找到可用的浏览器/.test(all)) return { cat: "browser", short: "没找到能用的浏览器" };
    if (/ERR_INTERNET_DISCONNECTED|ERR_NAME_NOT_RESOLVED|ERR_CONNECTION|ERR_TIMED_OUT|ERR_NETWORK|接口异常|URLError|打不开/.test(all) || !S.online) return { cat: "network", short: "连不上学校站点" };
    if (/找不到报名开始时间/.test(all)) return { cat: "no-time", short: "报名时间还没公布" };
    const src = last || reason;
    if (/未收到 signUp 响应/.test(src)) return { cat: "no-response", short: "学校没回应" };
    const m = /^code:(\S*)\s*(.*)$/.exec(src);
    if (m) {
      const msg = m[2].trim();
      if (/满|名额|人数已/.test(msg)) return { cat: "full", short: "名额已满", msg };
      if (/未开始|未到|尚未|未开放/.test(msg)) return { cat: "not-open", short: "学校说还没到报名时间", msg };
      return { cat: "server", short: msg || "学校拒绝了这次报名", msg };
    }
    if (/名额已满|页面未放开/.test(reason)) return { cat: "full", short: "名额已满，或页面一直没放开" };
    return { cat: "unknown", short: reason || "原因不明" };
  }

  function finishLogin(p, sl, stopped) {
    if (stopped) {
      S.result = null;
      toast("已取消登录");
      refresh({ quiet: true });
      return;
    }
    if (p.ok) {
      S.login.state = "valid";
      S.login.expiresAt = now() + TOKEN_H * 3600e3;
      S.result = { kind: "login", outcome: "success", at: now(), autoClose: now() + 6000 };
      announce("登录好了");
      refresh({ quiet: true });   // 契约：登录成功后前端自己拉一次列表
      return;
    }
    const all = [p.reason].concat(sl ? sl.tail : []).join("\n");
    let cat = "other";
    if (/窗口被关掉/.test(all)) cat = "closed";
    else if (/仍未检测到登录成功/.test(all)) cat = "timeout";
    else if (/打不开登录页/.test(all)) cat = "unreachable";
    else if (/接口校验失败/.test(all)) cat = "verify";
    else if (/没找到可用的浏览器/.test(all)) cat = "browser";
    S.result = { kind: "login", outcome: "fail", cat, reason: cleanReason(p.reason), at: now() };
    refresh({ quiet: true });
  }

  /* ------------------------------------------------ 挂之前的检查 */
  function preflight(it, t) {
    const st = stateOf(it, t);
    const pf = { mode: st === "open" ? "now" : "scheduled", checks: [], block: null, loginFix: null, text: "", sig: "" };
    const L = S.login.state;
    if (st === "signed") pf.block = { html: "你已经报过这一场，不用再抢。" };
    else if (st === "closed") pf.block = { html: "报名已经截止了。" };
    else if (st === "unknown") pf.block = { html: "报名时间还没公布。公布以后点「刷新」再来。", action: "refresh" };
    else if (L === "none") pf.block = { html: "还没登录。登录以后才能到点报名。", action: "login" };
    else if (L === "expired") pf.block = { html: "登录态失效了，先重新登录。", action: "login" };
    else if (L === "unreachable") pf.block = { html: "连不上学校站点，现在点了也会失败。检查网络后点「刷新」。", action: "refresh" };
    else if (L !== "valid") pf.block = { html: "正在检查登录，稍等一下。" };
    if (pf.block) { pf.sig = "b:" + pf.block.html; return pf; }

    const openAt = pf.mode === "now" ? t : it.signStart;
    const needUntil = openAt + WINDOW_S * 1000;
    const exp = S.login.expiresAt;
    const notes = [];
    if (!S.online) {
      pf.checks.push({ tone: "bad", icon: "cross", text: "网络断开了" });
      notes.push("网络断开了，恢复之前点了也会失败。");
    }
    if (exp && exp < needUntil) {
      // 登录态在开放（含 2 分钟重试）之前就会过期：脚本到点时页面已经掉登录了
      const fresh = needUntil - t < TOKEN_H * 3600e3 - 15 * 60e3;
      pf.loginFix = fresh ? "relogin" : "later";
      pf.checks.push({ tone: "warn", icon: "alert", text: `登录约 ${clockAt(exp, t)} 过期` });
      notes.push(fresh
        ? `登录约 <b>${clockAt(exp, t)}</b> 过期，比开放还早，到点时页面已经掉登录了。现在重新登录，就能撑到开放。`
        : `开放还有 <b>${dur((openAt - t) / 1000)}</b>，比一次登录能撑的时间（约 17 小时）还长。开放前 17 小时内重新登录，再来点「到点自动报名」。`);
    } else {
      pf.checks.push({ tone: "ok", icon: "check", text: pf.mode === "now" ? "登录有效" : "登录撑得到开放" });
    }
    const sk = S.clock.skew;
    if (Math.abs(sk) > 3) {
      pf.checks.push({ tone: "warn", icon: "alert", text: sk > 0 ? `本机慢 ${sk} 秒` : `本机快 ${-sk} 秒` });
      notes.push(sk > 0
        ? `本机时钟比学校慢 ${sk} 秒，到点会晚 ${sk} 秒才点。到 Windows「设置 › 时间和语言」点「立即同步」，再点「刷新」。`
        : `本机时钟比学校快 ${-sk} 秒，会提早开始刷新页面，真开放时反而可能慢几秒发现。到 Windows「设置 › 时间和语言」点「立即同步」。`);
    } else if (S.clock.checked) {
      pf.checks.push({ tone: "ok", icon: "check", text: "时钟准" });
    }
    if (pf.mode === "scheduled" && it.signStart - t > 86400e3 && !pf.loginFix) {
      pf.checks.push({ tone: "warn", icon: "alert", text: "开放在 24 小时以后" });
    }
    if (!notes.length) {
      notes.push(pf.mode === "now"
        ? `已经开放 <b>${ago((t - it.signStart) / 1000)}</b> 了。现在就替你点「立即报名」，没成功会接着试 2 分钟。`
        : "点了以后马上在后台打开活动页，开放前 5 秒开始盯「立即报名」，没成功会接着试 2 分钟。");
    }
    pf.text = notes[0];
    pf.sig = pf.mode + "|" + pf.checks.map((c) => c.tone + c.text).join("|") + "|" + pf.loginFix;
    return pf;
  }

  /* ------------------------------------------------ 动作 */
  function refresh(opts) {
    opts = opts || {};
    if (S.list.loading && !opts.force) return;
    S.list.loading = true;
    S.login.checking = true;
    renderAll();
    api("check").then((r) => { if (r && r.ok === false) fail(r); });
    api("list_activities").then((r) => { if (r && r.ok === false) fail(r); });
    function fail(r) {
      S.list.loading = false;
      S.login.checking = false;
      toast(r.text || "后端没响应", "bad");
      renderAll();
    }
  }

  async function doLogin() {
    closePop();
    if (S.slot) {
      toast(S.slot.kind === "grab" ? "正在等开放，先停止才能重新登录。" : "已经在登录了。", "warn");
      return;
    }
    S.result = null;
    S.stopRequested = false;
    S.slot = { kind: "login", startedAt: now(), tail: [] };
    renderAll();
    const res = await api("login");
    if (!res || !res.ok) {
      if (S.slot && S.slot.kind === "login") S.slot = null;
      toast((res && res.text) || "没能打开登录窗口", "bad");
      renderAll();
    }
  }

  async function arm(force) {
    const it = selectedItem();
    if (!it) return;
    const t = now();
    if (S.slot) {
      if (S.slot.kind === "grab" && S.slot.id !== it.id) return askSwitch(it);
      if (S.slot.kind === "login") toast("正在登录，登录好了再点。", "warn");
      return;
    }
    const pf = preflight(it, t);
    if (pf.block) { toast(pf.block.html, "warn"); renderBoard(); return; }
    if (pf.loginFix === "relogin" && !force) { doLogin(); return; }
    // 已经开放的活动：at 传「现在」，重试窗口从现在起算。
    // 传原来的开放时间的话，开放超过 2 分钟的活动 deadline 早就过了，脚本一次都不会试（grab.py: deadline = at + window）
    const atMs = pf.mode === "now" ? Math.floor(t / 1000) * 1000 : it.signStart;
    const atText = pf.mode === "now" ? fmtTs(atMs) : it.signStartText || fmtTs(atMs);
    S.result = null;
    S.stopRequested = false;
    S.slot = {
      kind: "grab", id: it.id, name: it.name, place: it.place, actStart: it.actStart,
      at: atMs, mode: pf.mode, headed: !!S.prefs.headed, armedAt: t, pageAt: null,
      attempts: [], already: false, tail: [],
    };
    S.flicker = true;
    if (S.prefs.sound) Sound.prime();
    renderAll();
    const res = await api("grab", it.id, it.name, atText, !!S.prefs.headed);
    if (!res || !res.ok) {
      if (S.slot && S.slot.kind === "grab" && S.slot.id === it.id && !S.slot.tail.length) S.slot = null;
      toast((res && res.text) || "没能开始，再试一次。", "bad");
      renderAll();
      return;
    }
    announce(pf.mode === "now" ? `正在替你报名「${it.name}」` : `开始等「${it.name}」开放`);
  }

  async function askSwitch(it) {
    const cur = S.slot;
    const ok = await confirmDlg({
      title: "换成这一场？",
      body: `「${cur.name}」会停止等待，到点不会再替你报名。改成等「${it.name}」。`,
      yes: "换成这一场", no: "不换",
    });
    if (!ok || !S.slot || S.slot.kind !== "grab") return;
    S.pendingSwitch = it.id;
    S.stopRequested = true;
    const res = await api("stop");
    if (!res || !res.ok) {
      S.pendingSwitch = null;
      S.stopRequested = false;
      toast((res && res.text) || "没停下来", "warn");
    }
  }

  async function stopSlot(skipConfirm) {
    const sl = S.slot;
    if (!sl) return;
    if (sl.kind === "grab" && !skipConfirm) {
      const submitting = grabPhase(sl, now()) === "submit";
      const ok = await confirmDlg({
        title: "停止等待？",
        body: submitting
          ? `正在提交「${sl.name}」。现在停下，可能已经交上去一次了，记得去活动页确认。`
          : `停止以后，到点不会再替你报名「${sl.name}」。`,
        yes: "停止", no: "继续等",
      });
      if (!ok || S.slot !== sl) return;
    }
    S.stopRequested = true;
    const res = await api("stop");
    if (!res || !res.ok) {
      S.stopRequested = false;
      toast((res && res.text) || "没停下来", "warn");
    }
  }

  function select(id, opts) {
    opts = opts || {};
    if (S.selectedId === id && !opts.force) return;
    S.selectedId = id || null;
    if (S.result && !S.slot) S.result = null;   // 点了别的行，就当看过结论了
    renderList();
    renderBoard();
    const row = id ? document.getElementById(rowDomId(id)) : null;
    $("#list").setAttribute("aria-activedescendant", row ? row.id : "");
    if (row && opts.scroll) row.scrollIntoView({ block: "nearest" });
  }
  function moveSel(delta) {
    const rows = $$("#list .row");
    if (!rows.length) return;
    let i = rows.findIndex((r) => r.dataset.id === S.selectedId);
    i = i < 0 ? (delta > 0 ? 0 : rows.length - 1) : Math.min(rows.length - 1, Math.max(0, i + delta));
    select(rows[i].dataset.id, { scroll: true });
  }
  function dismissResult() { S.result = null; renderBoard(); renderList(); }
  function pickNext() { if (S.nextId) select(S.nextId, { scroll: true }); }
  function rearm() {
    const id = S.result && S.result.id;
    S.result = null;
    if (id && S.byId.has(id)) { S.selectedId = id; renderAll(); arm(false); } else renderAll();
  }
  function clearFilter() {
    S.prefs.filter = { q: "", status: "all", time: "all" };
    applyFilterUI();
    renderList();
    savePrefs({ filter: S.prefs.filter }, true);
  }
  function focusPrimary() { const b = $("#board [data-primary]"); if (b) b.focus(); }
  function primaryAction() { const b = $("#board [data-primary]"); if (b && !b.disabled) b.click(); }

  const ACTS = {
    login: doLogin,
    refresh: () => refresh(),
    arm: () => arm(false),
    "arm-anyway": () => arm(true),
    stop: () => stopSlot(false),
    "stop-login": () => stopSlot(true),
    dismiss: dismissResult,
    "pick-next": pickNext,
    rearm,
    "show-log": () => setLogOpen(true),
    "clear-filter": clearFilter,
    switch: () => { const it = selectedItem(); if (it) askSwitch(it); },
    "sleep-ok": () => { S.sleep = null; renderBanners(); },
  };
  function runAct(name) { closePop(); (ACTS[name] || (() => {}))(); }

  /* ------------------------------------------------ 顶栏 */
  function loginView(t) {
    const L = S.login;
    if (S.slot && S.slot.kind === "login") return { st: "busy", text: "正在登录", sub: "" };
    if (L.state === "valid") {
      if (!L.expiresAt) return { st: "ok", text: "登录有效", sub: "" };
      const left = (L.expiresAt - t) / 1000;
      if (left <= 0) return { st: "warn", text: "登录可能过期了", sub: "" };
      if (left < EXPIRING_H * 3600) return { st: "warn", text: "登录快过期", sub: "约剩 " + durCoarse(left) };
      return { st: "ok", text: "登录有效", sub: "约剩 " + durCoarse(left) };
    }
    if (L.state === "expired") return { st: "bad", text: "登录已失效", sub: "" };
    if (L.state === "none") return { st: "off", text: "还没登录", sub: "" };
    if (L.state === "unreachable") return { st: "warn", text: "连不上学校站点", sub: "" };
    return { st: "busy", text: "正在检查登录", sub: "" };
  }
  function renderTop() {
    const t = now();
    $("#clock-time").textContent = hms(t);
    const sk = S.clock.skew;
    const skEl = $("#clock-skew");
    if (Math.abs(sk) > 3) { skEl.hidden = false; skEl.textContent = sk > 0 ? `本机慢 ${sk} 秒` : `本机快 ${-sk} 秒`; } else skEl.hidden = true;

    const v = loginView(t);
    const pill = $("#pill");
    if (pill.dataset.st !== v.st) pill.dataset.st = v.st;
    $("#pill-text").textContent = v.text;
    $("#pill-sub").textContent = v.sub;
    pill.setAttribute("aria-label", v.text + (v.sub ? "，" + v.sub : ""));

    const lb = $("#btn-login");
    $("#btn-login-text").textContent = S.slot && S.slot.kind === "login" ? "登录中…" : S.login.state === "none" ? "登录" : "重新登录";
    lb.disabled = !!S.slot;
    lb.title = S.slot && S.slot.kind === "grab" ? "正在等开放：先停止，才能重新登录（同一时间只能跑一个任务）" : "";
    lb.classList.toggle("btn-solid", S.login.state === "none" || S.login.state === "expired");
    lb.classList.toggle("btn-line", !(S.login.state === "none" || S.login.state === "expired"));

    const rb = $("#btn-refresh");
    rb.classList.toggle("is-busy", S.list.loading);
    rb.disabled = S.list.loading;
  }

  /* ------------------------------------------------ 横幅 */
  let bannerKey = "";
  function renderBanners() {
    const parts = [];
    if (!S.online) {
      parts.push({ tone: "bad", icon: "wifi", html: S.slot && S.slot.kind === "grab"
        ? "<b>网络断开了。</b>到点时还没恢复，这次报名会失败。恢复以后看一眼大屏。"
        : "<b>网络断开了。</b>恢复以后点「刷新」。" });
    }
    if (S.sleep) {
      parts.push({ tone: "warn", icon: "moon", act: "sleep-ok",
        html: `<b>刚才有 ${ago(S.sleep.gap / 1000)}界面没动静，电脑可能睡眠了。</b>要是睡过了开放时间，就赶不上了。看看大屏和运行记录。` });
    }
    const key = JSON.stringify(parts);
    if (key === bannerKey) return;
    bannerKey = key;
    $("#banners").innerHTML = parts.map((b) =>
      `<div class="banner" data-tone="${b.tone}" role="alert">${icon(b.icon)}<span class="grow">${b.html}</span>${b.act ? `<button class="btn btn-ghost" type="button" data-act="${b.act}">知道了</button>` : ""}</div>`).join("");
  }

  /* ------------------------------------------------ 大屏 */
  function frame(o) {
    const tags = (o.tags || []).map((g) =>
      `<span class="b-tag" data-tone="${g.tone || ""}"${g.pulse ? " data-pulse" : ""}>${g.dot === false ? "" : '<i class="dot"></i>'}${esc(g.text)}</span>`).join("");
    const sub = (o.sub || []).filter(Boolean).map((s) => `<span>${s.icon ? icon(s.icon) : ""}${esc(s.text)}</span>`).join("");
    return `<div class="b-main swap">
        <div class="b-tags">${tags}</div>
        <h2 class="b-title" title="${esc(o.tip || o.title)}">${esc(o.title)}</h2>
        ${sub ? `<div class="b-sub">${sub}</div>` : ""}
        <p class="b-text" data-k="text">${o.text || ""}</p>
      </div>
      <div class="b-led"><div class="led-wrap" data-led></div><div class="led-label" data-k="label"></div></div>
      ${o.foot ? `<div class="b-foot swap">${o.foot}</div>` : ""}`;
  }
  const bb = (act, label, kind, ic, primary) =>
    `<button class="bb bb-${kind || "ghost"}" type="button" data-act="${act}"${primary ? " data-primary" : ""}>${ic ? icon(ic) : ""}${label}</button>`;
  const clockLed = (t) => ({ text: hms(t), tone: "dim" });

  function boardModel(t) {
    if (S.slot && S.slot.kind === "login") return mLoginRun(t);
    if (S.slot && S.slot.kind === "grab") return mGrab(t);
    if (S.slot) return mBusy(t);
    if (S.result) return mResult(t);
    const it = selectedItem();
    if (it) return mPreview(it, t);
    const L = S.login.state;
    if (!S.items.length && (S.list.state === "idle" || (S.list.loading && L !== "none" && L !== "expired" && L !== "unreachable"))) return mLoading(t);
    if (L === "none" || L === "expired") return mNeedLogin(t, L);
    if (L === "unreachable") return mUnreachable(t);
    if (S.list.state === "error") return mListError(t);
    return mIdle(t);
  }

  function mLoading(t) {
    return {
      key: "loading", tone: "",
      html: frame({ tags: [{ text: "准备中", tone: "off", pulse: true }], title: "正在检查登录、拉活动列表", text: "第一次拉取要几秒钟。" }),
      led: clockLed(t), dyn: { label: "本机时间" },
    };
  }
  function mNeedLogin(t, kind) {
    const none = kind === "none";
    return {
      key: "need-login:" + kind, tone: none ? "" : "bad",
      html: frame({
        tags: [{ text: none ? "还没登录" : "登录已失效", tone: none ? "off" : "red" }],
        title: none ? "先登录一次" : "登录态失效了",
        text: none ? "点「登录」会弹出 Chrome，在里面输入学号、密码和验证码。登录一次大约能用 17 小时。"
                   : "登录一次大约能用 17 小时，过了就要重新登录。登录好了会自动刷新活动列表。",
        foot: `<span class="b-note grow">只给本人账号用，不借别人的账号。</span>${bb("login", none ? "登录" : "重新登录", "primary", "login", true)}`,
      }),
      led: clockLed(t), dyn: { label: "本机时间" },
    };
  }
  function mUnreachable(t) {
    return {
      key: "unreachable", tone: "",
      html: frame({
        tags: [{ text: "连不上", tone: "amber" }], title: "连不上学校站点",
        // 「接口异常：URLError / HTTPError」分不清是网络还是学校那边的错，所以不断言「不是登录的问题」
        text: "检查网络、校园网或代理，然后点「刷新」。一直这样的话，重新登录一次试试。",
        foot: `<span class="b-note grow">学校站点 xg.tjise.edu.cn 有时需要校园网或学校 VPN。</span>${bb("refresh", "刷新", "primary", "refresh", true)}`,
      }),
      led: clockLed(t), dyn: { label: "本机时间" },
    };
  }
  function mListError(t) {
    const first = (S.list.errorText.split("\n").find((l) => /\[X\]/.test(l)) || S.list.errorText.split("\n")[0] || "").replace(/^\[X\]\s*/, "");
    return {
      key: "list-error", tone: "",
      html: frame({ tags: [{ text: "列表没拉下来", tone: "red" }], title: "活动列表没拉下来", text: esc(first || "原因不明"),
        foot: `<span class="b-note grow">再拉一次试试；一直这样就看看运行记录。</span>${bb("refresh", "刷新", "primary", "refresh", true)}` }),
      led: clockLed(t), dyn: { label: "本机时间" },
    };
  }
  function mIdle(t) {
    const nx = S.nextId && S.byId.get(S.nextId);
    if (!nx) {
      return {
        key: "idle-none", tone: "",
        html: frame({ tags: [{ text: "暂时没有", tone: "off" }], title: "近期没有要开放的活动",
          text: "可能还没公布报名时间，也可能都报过了。过一会儿点「刷新」看看。",
          foot: `<span class="b-note grow">已经开放的场次在时刻表最上面，点一行就能马上报名。</span>${bb("refresh", "刷新", "ghost", "refresh", true)}` }),
        led: clockLed(t), dyn: { label: "本机时间" },
      };
    }
    return {
      key: "idle:" + nx.id, tone: "",
      html: frame({
        tags: [{ text: "下一场", tone: "amber", dot: false }], title: nx.name,
        sub: [{ icon: "pin", text: nx.place || "地点待定" }, { icon: "cal", text: actLabel(nx, t) }],
        foot: `<span class="b-note grow">在下面的时刻表里点一场，它就会到这块屏上。一次只等一场。</span>${bb("pick-next", "选这一场", "ghost", "", true)}`,
      }),
      led: { text: ledTime((nx.signStart - t) / 1000), tone: "dim" },
      dyn: { label: `<b>${whenLabel(nx.signStart, t)}</b> 开放`, text: `还有 ${dur((nx.signStart - t) / 1000)} 开放报名。` },
    };
  }

  function stateTag(st) {
    return {
      waiting: { text: "未开放", tone: "" },
      open: { text: "开放中", tone: "amber" },
      signed: { text: "已报名", tone: "green" },
      closed: { text: "已截止", tone: "off" },
      unknown: { text: "时间待公布", tone: "off" },
    }[st];
  }
  const chk = (c) => `<span class="chk" data-tone="${c.tone}">${icon(c.icon)}${esc(c.text)}</span>`;
  function toggles() {
    return `<label class="tg"><input type="checkbox" data-pref="headed"${S.prefs.headed ? " checked" : ""}><span class="box">${icon("check")}</span>看着它点<span class="lbl-long">（显示浏览器）</span></label>` +
           `<label class="tg"><input type="checkbox" data-pref="sound"${S.prefs.sound ? " checked" : ""}><span class="box">${icon("check")}</span>结束响一声</label>`;
  }

  function mPreview(it, t) {
    const st = stateOf(it, t);
    const pf = preflight(it, t);
    const tags = [stateTag(st)];
    if (S.nextId === it.id) tags.push({ text: "下一场", tone: "amber", dot: false });
    const sub = [{ icon: "pin", text: it.place || "地点待定" }, { icon: "cal", text: actLabel(it, t) }];
    if (st === "open" && it.signEnd) sub.push({ icon: "clock", text: "报名截止 " + whenLabel(it.signEnd, t) });
    let led, label;
    if (st === "waiting") { led = { text: ledTime((it.signStart - t) / 1000), tone: "dim" }; label = `<b>${whenLabel(it.signStart, t)}</b> 开放`; }
    else if (st === "open") {
      if (it.signEnd) { led = { text: ledTime((it.signEnd - t) / 1000), tone: "dim" }; label = "距报名截止"; }
      else { led = { text: ledTime((t - it.signStart) / 1000), tone: "dim" }; label = "已经开放"; }
    } else if (st === "signed") { led = { text: "✓", tone: "green" }; label = "已报名"; }
    else if (st === "closed") { led = { text: "00:00:00", tone: "off" }; label = "报名已截止"; }
    else { led = { text: "--:--:--", tone: "off" }; label = "报名时间待公布"; }

    let foot;
    if (pf.block) {
      const b = pf.block;
      const btn = b.action === "login" ? bb("login", S.login.state === "none" ? "登录" : "重新登录", "primary", "login", true)
                : b.action === "refresh" ? bb("refresh", "刷新", "ghost", "refresh", true) : "";
      foot = `<span class="b-note grow">${b.html}</span>${btn}`;
    } else {
      const armLabel = pf.mode === "now" ? "立即替我报名" : "到点自动报名";
      const checks = `<div class="checks grow">${pf.checks.map(chk).join("")}</div>`;
      if (pf.loginFix === "relogin") foot = `${checks}${toggles()}<button class="bb-link" type="button" data-act="arm-anyway">照样${pf.mode === "now" ? "报名" : "到点报名"}</button>${bb("login", "先重新登录", "primary", "login", true)}`;
      else if (pf.loginFix === "later") foot = `${checks}${toggles()}${bb("arm-anyway", "照样" + (pf.mode === "now" ? "报名" : "到点报名"), "ghost", "", true)}`;
      else foot = `${checks}${toggles()}${bb("arm", armLabel, "primary", "play", true)}`;
    }
    const text = pf.block ? (st === "waiting" ? `还有 ${dur((it.signStart - t) / 1000)} 开放报名。` : "") : pf.text;
    return {
      key: `preview:${it.id}:${st}:${pf.sig}:${S.nextId === it.id}`, tone: "",
      html: frame({ tags, title: it.name, sub, foot }),
      led, dyn: { label, text },
    };
  }

  function grabPhase(sl, t) {
    const end = sl.at + WINDOW_S * 1000;
    if (!sl.pageAt) return t > end + 5e3 ? "overtime" : "starting";
    if (sl.mode === "now") return t > end + 5e3 ? "overtime" : "submit";
    if (t < sl.at - POLL_S * 1000) return "waiting";
    if (t < sl.at) return "final";
    if (t <= end + 5e3) return "submit";
    return "overtime";
  }
  /* 线路图的每一站都是 grab.py 里真有动作的时刻：
   * 一启动就打开活动页（--lead 只决定「开始等待开放」那行日志什么时候打，不推迟开页），
   * 开放前 5 秒开始找按钮，开放后最多试到 at + 120 秒。 */
  function stations(sl) {
    if (sl.mode === "now") {
      return [
        { name: "开始", time: sl.armedAt },
        { name: "活动页已打开", time: sl.pageAt, hot: true },
        { name: "最晚到这里", time: sl.at + WINDOW_S * 1000 },
      ];
    }
    const base = sl.pageAt || sl.armedAt;
    return [
      { name: "开始等待", time: sl.armedAt },
      { name: "活动页已打开", time: sl.pageAt },
      { name: "盯按钮", time: Math.max(sl.at - POLL_S * 1000, base), hot: true },
      { name: "开放", time: sl.at, hot: true },
      { name: "最晚到这里", time: sl.at + WINDOW_S * 1000 },
    ];
  }
  function stationIndex(list, sl, t) {
    if (!sl.pageAt) return 0;
    let idx = 1;
    for (let i = 2; i < list.length; i++) if (list[i].time <= t) idx = i;
    return idx;
  }
  function routeHTML(list, idx) {
    return `<div class="route" style="--n:${list.length}" aria-label="这次报名的时间点">${list.map((s, i) => {
      const st = i < idx ? "done" : i === idx ? (s.hot ? "hot" : "now") : "todo";
      return `<div class="stn" data-s="${st}"><i class="stn-dot"></i><span class="stn-name">${s.name}</span><span class="stn-time">${s.time != null ? hms(s.time) : "马上"}</span></div>`;
    }).join("")}</div>`;
  }
  function humanAttempt(reason) {
    const m = /^code:(\S*)\s*(.*)$/.exec(reason || "");
    if (m) return m[2] ? `学校回「${m[2]}」` : `学校回了代码 ${m[1]}`;
    if (/未收到 signUp 响应/.test(reason)) return "点了，学校 12 秒内没回应";
    return reason || "没成功";
  }

  function mGrab(t) {
    const sl = S.slot;
    const ph = grabPhase(sl, t);
    const list = stations(sl);
    const idx = stationIndex(list, sl, t);
    const end = sl.at + WINDOW_S * 1000;
    const sub = [{ icon: "pin", text: sl.place || "地点待定" }, { icon: "cal", text: actLabel(sl, t) }];
    let tag, text, led, label, tone = "live";
    const hot = t >= sl.at - 10e3;
    if (ph === "starting") {
      tag = { text: "准备中", tone: "amber", pulse: true };
      text = sl.headed ? "正在打开浏览器和活动页…" : "正在后台打开活动页…";
      led = sl.mode === "now" ? { text: ledTime(Math.max(0, end - t) / 1000), tone: "amber" } : { text: ledTime((sl.at - t) / 1000), tone: hot ? "red" : "amber" };
      label = sl.mode === "now" ? `还能试到 <b>${hms(end)}</b>` : `<b>${whenLabel(sl.at, t)}</b> 开放`;
    } else if (ph === "waiting") {
      tag = { text: "等开放", tone: "amber", pulse: true };
      text = t >= sl.at - LEAD_S * 1000 ? "快了。开放前 5 秒开始盯「立即报名」，按钮一出现就点。" : "电脑别睡眠、别断网。窗口可以最小化，别关。";
      led = { text: ledTime((sl.at - t) / 1000), tone: hot ? "red" : "amber" };
      label = `<b>${whenLabel(sl.at, t)}</b> 开放`;
    } else if (ph === "final") {
      tag = { text: "马上开放", tone: "red", pulse: true }; tone = "hot";
      text = "正在盯「立即报名」，按钮一出现就点。";
      led = { text: ledTime(Math.ceil((sl.at - t) / 1000)), tone: "red" };
      label = `<b>${hms(sl.at)}</b> 开放`;
    } else if (ph === "submit") {
      tag = { text: "正在提交", tone: "red", pulse: true }; tone = "hot";
      const a = sl.attempts[sl.attempts.length - 1];
      text = a ? `第 ${a.n} 次提交：${esc(humanAttempt(a.reason))}。接着试。`
               : (sl.mode === "now" ? "已经开放了，正在找「立即报名」。" : "等「立即报名」出现。页面没放开时，每 2 到 8 秒刷新一次。");
      led = { text: ledTime((end - t) / 1000), tone: "amber" };
      label = `还能试到 <b>${hms(end)}</b>`;
    } else {
      tag = { text: "收尾中", tone: "off" }; tone = "";
      text = t - end > 60e3 ? "脚本好像卡住了。可以点「停止」，再看看运行记录。" : "重试时间到了，等脚本收尾…";
      led = { text: "00:00:00", tone: "off" };
      label = "重试时间已到";
    }
    const flick = S.flicker;
    S.flicker = false;
    return {
      key: `grab:${sl.id}:${ph}:${idx}:${sl.attempts.length}`, tone, flicker: flick,
      html: frame({
        tags: [tag], title: sl.name, sub,
        foot: `${routeHTML(list, idx)}${bb("stop", "停止", "ghost", "stop")}`,
      }),
      led, dyn: { label, text },
    };
  }

  function mLoginRun(t) {
    const sl = S.slot;
    const left = Math.max(0, sl.startedAt + LOGIN_WAIT_S * 1000 - t) / 1000;
    return {
      key: "login-run", tone: "live",
      html: frame({
        tags: [{ text: "正在登录", tone: "amber", pulse: true }], title: "去 Chrome 窗口里登录",
        text: "找不到窗口就看看任务栏。登录成功后窗口会自己关掉，这里接着刷新活动列表。",
        foot: `<ol class="lsteps grow"><li><b>1</b>输入学号和密码</li><li><b>2</b>填图形验证码，点「立即登录」</li><li><b>3</b>等窗口自己关掉</li></ol>${bb("stop-login", "取消登录", "ghost")}`,
      }),
      led: { text: ledTime(left).slice(3), tone: "amber" }, dyn: { label: "最多再等" },
    };
  }
  function mBusy(t) {
    return {
      key: "busy", tone: "live",
      html: frame({ tags: [{ text: "有任务在跑", tone: "amber", pulse: true }], title: "后台有一个任务在跑",
        text: "界面打开之前它就开始了。等它结束，或者点「停止」。", foot: `<span class="grow"></span>${bb("stop-login", "停止", "ghost", "stop")}` }),
      led: clockLed(t), dyn: { label: "本机时间" },
    };
  }

  function mResult(t) {
    const r = S.result;
    if (r.kind === "login") {
      if (r.outcome === "success") {
        const exp = S.login.expiresAt || r.at + TOKEN_H * 3600e3;
        return {
          key: "login-ok", tone: "ok",
          html: frame({ tags: [{ text: "登录好了", tone: "green", dot: false }], title: "登录好了",
            foot: `<span class="b-note grow">账号、密码不落盘，只存登录态。</span>${bb("dismiss", "知道了", "primary", "", true)}` }),
          led: { text: "✓", tone: "green" },
          dyn: { label: "登录成功", text: `大约 17 小时内有效，约到 <b>${clockAt(exp, t)}</b>。${S.list.loading ? "活动列表正在刷新。" : "活动列表已经刷新。"}` },
        };
      }
      const copy = {
        closed: ["登录窗口被关掉了", "登录还没完成，窗口就关了。点「再试一次」重新打开。"],
        timeout: ["等了 10 分钟还没登录成功", "Chrome 窗口里一直没完成登录，超时了。点「再试一次」。"],
        unreachable: ["打不开学校的登录页", "检查网络、校园网或代理，然后再试一次。"],
        verify: ["登录后学校那边没校验通过", "登录态存下来了，但学校不认。再登录一次试试。"],
        browser: ["没找到能用的浏览器", "这台电脑上要有 Chrome 或 Edge，装好以后再试。"],
        other: ["登录没完成", esc(r.reason || "原因不明") + "。可以再试一次，或者看看运行记录。"],
      }[r.cat] || ["登录没完成", ""];
      return {
        key: "login-fail:" + r.cat, tone: "bad",
        html: frame({ tags: [{ text: "登录没完成", tone: "red", dot: false }], title: copy[0], text: copy[1],
          foot: `<span class="grow"></span>${bb("dismiss", "知道了", "ghost")}${bb("login", "再试一次", "primary", "login", true)}` }),
        led: { text: "✕", tone: "red" }, dyn: { label: `<b>${hms(r.at)}</b> 结束` },
      };
    }

    const tries = r.attempts ? `，提交了 ${r.attempts} 次` : "";
    if (r.outcome === "success") {
      return {
        key: "ok:" + r.id, tone: "ok",
        html: frame({ tags: [{ text: "报名成功", tone: "green", dot: false }], title: `报名成功：${r.name}`,
          text: "去活动页确认一眼。不想去就尽快在活动页取消，别占名额。",
          foot: `<span class="b-note grow">结果已经记进运行记录；配了飞书机器人的话，群里也会收到一张卡片。</span>${bb("dismiss", "知道了", "primary", "", true)}` }),
        led: { text: "✓", tone: "green" },
        dyn: { label: `<b>${hms(r.at)}</b> 报上了${r.attempts ? "，第 " + r.attempts + " 次提交" : ""}` },
      };
    }
    if (r.outcome === "already") {
      return {
        key: "already:" + r.id, tone: "ok",
        html: frame({ tags: [{ text: "已报名", tone: "green", dot: false }], title: "你已经报过这一场",
          text: `「${esc(r.name)}」已经是报名状态，不用再抢。`,
          foot: `<span class="grow"></span>${bb("dismiss", "知道了", "primary", "", true)}` }),
        led: { text: "✓", tone: "green" }, dyn: { label: "已报名" },
      };
    }
    if (r.outcome === "stopped") {
      const it = r.id && S.byId.get(r.id);
      const can = it && (stateOf(it, t) === "waiting" || stateOf(it, t) === "open");
      return {
        key: "stopped:" + r.id + ":" + !!can, tone: "",
        html: frame({ tags: [{ text: "已停止", tone: "off" }], title: "已停止等待",
          text: `到点不会再替你报名「${esc(r.name)}」。`,
          foot: `<span class="grow"></span>${can ? bb("rearm", "重新开始等", "ghost", "play") : ""}${bb("dismiss", "知道了", "primary", "", true)}` }),
        led: { text: "--:--:--", tone: "off" }, dyn: { label: `<b>${hms(r.at)}</b> 停止` },
      };
    }

    const copy = failCopy(r);
    let title = copy.title;
    let text = copy.text;
    let actions = copy.actions;
    if (r.loginSuspect && r.cat !== "login") {
      // 脚本只看到「立即报名」没出现；掉了登录的页面也是这样，复查结果才是真原因
      title = "登录态失效了";
      text = "脚本只看到「立即报名」一直没出现。顺手查了一下，<b>登录态已经失效</b>，这多半才是原因。重新登录以后再试。";
      actions = bb("dismiss", "知道了", "ghost") + bb("login", "重新登录", "primary", "login", true);
    } else if (r.netSuspect && r.cat !== "network") {
      text += " 顺手查了一下：现在连不上学校站点。";
    }
    return {
      key: `fail:${r.id}:${r.cat}:${!!r.loginSuspect}:${!!r.netSuspect}:${!!r.awaitingCheck}`, tone: "bad",
      html: frame({ tags: [{ text: "没抢到", tone: "red", dot: false }], title: `没抢到：${title}`, text,
        foot: `<span class="b-note grow">${r.awaitingCheck ? "正在顺手复查登录态…" : "原话在运行记录里。"}</span>${actions}` }),
      led: { text: "✕", tone: "red" }, dyn: { label: `<b>${hms(r.at)}</b> 结束${tries}` },
    };
  }

  function failCopy(r) {
    const refreshList = bb("dismiss", "知道了", "ghost") + bb("refresh", "刷新列表", "primary", "refresh", true);
    const ok = bb("dismiss", "知道了", "primary", "", true);
    switch (r.cat) {
      case "full": return { title: r.msg ? "名额已满" : "名额已满，或页面一直没放开",
        text: r.msg ? `学校回「${esc(r.msg)}」。去活动页看看有没有人退出，或者挑别的场次。`
                    : "开放后 2 分钟里「立即报名」一直没出现。去活动页看看有没有人退出，或者在时刻表里挑别的场次。", actions: refreshList };
      case "not-open": return { title: "学校说还没到报名时间",
        text: `学校一直回「${esc(r.msg || "未开始")}」。可能是学校推迟了开放，或者本机时钟快了。刷新列表看看开放时间有没有改。`, actions: refreshList };
      case "server": return { title: r.short, text: "学校拒绝了这次报名。去活动页看看具体是什么情况。", actions: ok };
      case "login": return { title: "登录态失效了", text: "学校不认这次的登录。重新登录以后，再点一次「到点自动报名」。",
        actions: bb("dismiss", "知道了", "ghost") + bb("login", "重新登录", "primary", "login", true) };
      case "network": return { title: "连不上学校站点", text: "网络断了，或者校园网、代理拦住了。检查网络后点「刷新」，再试一次。",
        actions: bb("dismiss", "知道了", "ghost") + bb("refresh", "刷新", "primary", "refresh", true) };
      case "browser": return { title: "没找到能用的浏览器", text: "这台电脑上要有 Chrome 或 Edge。装好以后再试。", actions: ok };
      case "no-response": return { title: "学校没回应", text: "点了「确定」，12 秒内没收到学校回复。说不定其实报上了，去活动页确认一下。", actions: refreshList };
      case "no-time": return { title: "报名时间还没公布", text: "公布以后点「刷新」，再来点「到点自动报名」。", actions: refreshList };
      default: return { title: r.short, text: "看看运行记录，或者去活动页手动试一次。",
        actions: bb("show-log", "看运行记录", "ghost", "log") + ok };
    }
  }

  let boardKey = "";
  let boardLed = null;
  let boardDyn = {};
  function renderBoard() {
    const el = $("#board");
    const t = now();
    const m = boardModel(t);
    if (m.key !== boardKey) {
      const focusedAct = el.contains(document.activeElement) && document.activeElement.dataset ? document.activeElement.dataset.act : null;
      el.innerHTML = m.html;
      boardKey = m.key;
      boardDyn = {};
      const ledEl = el.querySelector("[data-led]");
      boardLed = ledEl ? LED.mount(ledEl) : null;
      if (boardLed && m.led) boardLed.set(m.led.text, m.led.tone);
      if (boardLed && m.flicker) boardLed.flicker();
      if (focusedAct) { const again = el.querySelector(`[data-act="${focusedAct}"]`); if (again) again.focus(); }
    }
    if ((el.dataset.tone || "") !== (m.tone || "")) el.dataset.tone = m.tone || "";
    if (boardLed && m.led) boardLed.set(m.led.text, m.led.tone);
    const dyn = m.dyn || {};
    for (const k of Object.keys(dyn)) {
      if (boardDyn[k] === dyn[k]) continue;
      boardDyn[k] = dyn[k];
      const node = el.querySelector(`[data-k="${k}"]`);
      if (node) node.innerHTML = dyn[k];
    }
  }

  /* ------------------------------------------------ 时刻表 */
  const rowDomId = (id) => "row-" + String(id).replace(/[^\w-]/g, "_");

  function visibleItems(t) {
    const f = S.prefs.filter;
    const q = String(f.q || "").trim().toLowerCase();
    const dayEnd = startOfDay(t) + 86400e3;
    return S.items.filter((it) => {
      const st = stateOf(it, t);
      if (f.status === "free" && (st === "signed" || st === "closed")) return false;
      if (f.status === "signed" && st !== "signed") return false;
      if (f.time !== "all" && st !== "open") {
        if (!it.signStart) return false;
        const horizon = f.time === "today" ? dayEnd : t + 7 * 86400e3;
        if (it.signStart >= horizon) return false;
        if (it.signStart < startOfDay(t)) return false;
      }
      if (q && it.name.toLowerCase().indexOf(q) < 0 && it.place.toLowerCase().indexOf(q) < 0) return false;
      return true;
    });
  }

  function groupItems(vis, t) {
    const open = [], unknown = [], closed = [], signed = [];
    const days = new Map();
    for (const it of vis) {
      const st = stateOf(it, t);
      if (st === "open") open.push(it);
      else if (st === "waiting") { const k = startOfDay(it.signStart); if (!days.has(k)) days.set(k, []); days.get(k).push(it); }
      else if (st === "unknown") unknown.push(it);
      else if (st === "closed") closed.push(it);
      else signed.push(it);
    }
    const byStart = (a, b) => (a.signStart || 0) - (b.signStart || 0) || a.name.localeCompare(b.name, "zh");
    const out = [];
    if (open.length) out.push({ kind: "open", label: "<b>正在开放</b><span>现在就能报</span>", items: open.sort(byStart) });
    if (open.length || days.size) out.push({ kind: "now" });
    Array.from(days.keys()).sort((a, b) => a - b).forEach((k) => {
      const r = relDay(k, t);
      const label = r ? `<b>${r}</b><span>${dateLabel(k)} ${WEEK[new Date(k).getDay()]}</span>` : `<b>${dateLabel(k)}</b><span>${WEEK[new Date(k).getDay()]}</span>`;
      out.push({ kind: "day", label, items: days.get(k).sort(byStart) });
    });
    if (unknown.length) out.push({ kind: "unknown", label: "<b>报名时间待公布</b><span>公布以后才能到点报名</span>", items: unknown.sort(byStart) });
    if (closed.length) out.push({ kind: "closed", label: "<b>刚截止</b><span>截止一小时内的还留着</span>", items: closed.sort(byStart) });
    if (signed.length) out.push({ kind: "signed", label: "<b>已报名</b>", items: signed.sort(byStart) });
    return out;
  }

  function stateCell(it, st, t) {
    const armed = S.slot && S.slot.kind === "grab" && S.slot.id === it.id;
    const armTag = armed ? `<span class="tag" data-k="armed"><i class="dot"></i>${t >= S.slot.at ? "提交中" : "等开放"}</span>` : "";
    switch (st) {
      case "waiting":
        return `<span class="left" data-left>${dur((it.signStart - t) / 1000)}</span>${armTag || (S.nextId === it.id ? '<span class="tag" data-k="next">下一场</span>' : "")}`;
      case "open":
        return `${armTag || '<span class="tag" data-k="open">开放中</span>'}<span class="small" data-ago>已开放 ${ago((t - it.signStart) / 1000)}</span>`;
      case "signed":
        return S.justSigned.has(it.id) && !it.signed ? `<span class="tag" data-k="ok">${icon("check")}刚报上</span>` : `<span class="tag" data-k="signed">${icon("check")}已报名</span>`;
      case "closed":
        return '<span class="tag" data-k="closed">已截止</span>';
      default:
        return '<span class="tag" data-k="unknown">待公布</span>';
    }
  }

  function rowHTML(it, t) {
    const st = stateOf(it, t);
    const sel = it.id === S.selectedId;
    const armed = S.slot && S.slot.kind === "grab" && S.slot.id === it.id;
    let tm = '<span class="t">--:--</span>';
    if (it.signStart) {
      const d = new Date(it.signStart);
      tm = `<span class="t">${hm(it.signStart)}</span>${d.getSeconds() ? `<span class="s">:${pad(d.getSeconds())}</span>` : ""}`;
      // 按天分组的行不用写日期；开放中、已截止、已报名几组跨好几天，不写日期就看不出先后
      if (st !== "waiting" && dayDiff(it.signStart, t) !== 0) tm += `<span class="d">${relDay(it.signStart, t) || dateLabel(it.signStart)}</span>`;
    }
    const sub = [];
    if (st === "open" && it.signEnd) sub.push(`<span>报名截止 ${esc(whenLabel(it.signEnd, t))}</span>`);
    sub.push(`<span>${esc(actLabel(it, t))}</span>`);
    let extra = "";
    if (sel && S.slot && S.slot.kind === "grab" && !armed && (st === "waiting" || st === "open")) {
      extra = `<div class="row-extra">${icon("info")}<span class="grow">大屏正在等「<b>${esc(S.slot.name)}</b>」。一次只能等一场。</span><button class="btn btn-line" type="button" data-act="switch">${icon("swap")}换成这一场</button></div>`;
    }
    const aria = `${esc(it.name)}，${it.signStart ? whenLabel(it.signStart, t) + " 开放" : "报名时间待公布"}`;
    return `<div class="row${armed ? " is-armed" : ""}" role="option" id="${rowDomId(it.id)}" data-id="${esc(it.id)}" data-st="${st}" aria-selected="${sel}" aria-label="${aria}">
      <div class="c-time">${tm}</div>
      <div class="c-main"><div class="name" title="${esc(it.name)}">${esc(it.name)}</div><div class="sub">${sub.join("")}</div></div>
      <div class="c-place" title="${esc(it.place)}">${it.place ? esc(it.place) : '<span class="muted">地点待定</span>'}</div>
      <div class="c-state">${stateCell(it, st, t)}</div>
      ${extra}
    </div>`;
  }

  const EMPTY_ART = '<svg class="empty-art" viewBox="0 0 76 52" aria-hidden="true"><rect x="2" y="2" width="72" height="36" rx="6" fill="none" stroke="currentColor" stroke-width="2"/><g fill="currentColor"><circle cx="16" cy="20" r="2.2"/><circle cx="23" cy="20" r="2.2"/><circle cx="34" cy="16" r="2"/><circle cx="34" cy="24" r="2"/><circle cx="45" cy="20" r="2.2"/><circle cx="52" cy="20" r="2.2"/><circle cx="60" cy="20" r="2.2"/></g><path d="M38 38v10M26 49h24" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';
  function emptyBox(title, text, act, label) {
    return `<div class="empty">${EMPTY_ART}<h3>${title}</h3><p>${text}</p>${act ? `<button class="btn btn-solid" type="button" data-act="${act}">${label}</button>` : ""}</div>`;
  }
  function skeleton() {
    return `<div class="skel" aria-hidden="true">${"<div class=\"skel-row\"><i></i><i></i><i></i><i></i></div>".repeat(6)}</div>`;
  }
  function emptyHTML() {
    const L = S.login.state;
    if (S.list.state === "idle" || (S.list.loading && S.list.state !== "error" && L !== "none" && L !== "expired")) return skeleton();
    if (S.slot && S.slot.kind === "login") return emptyBox("正在登录", "在 Chrome 窗口里登录好，这里会自动列出近期的活动。");
    if (L === "none") return emptyBox("先登录一次", "登录以后这里会列出近期的活动。点「登录」会弹出 Chrome，输入学号、密码和验证码。", "login", "登录");
    if (L === "expired") return emptyBox("登录态失效了", "重新登录以后，这里会自动刷新。", "login", "重新登录");
    if (L === "unreachable") return emptyBox("连不上学校站点", "检查网络、校园网或代理，然后点「刷新」。", "refresh", "刷新");
    if (S.list.state === "error") return emptyBox("活动列表没拉下来", esc((S.list.errorText.split("\n")[0] || "").replace(/^\[X\]\s*/, "")), "refresh", "刷新");
    return emptyBox("这里还没有活动", "点「刷新」拉一次；提示登录态失效就先点「登录」。", "refresh", "刷新");
  }

  function renderList() {
    const el = $("#list");
    const t = now();
    S.nextId = computeNext(t);
    el.classList.toggle("is-loading", S.list.loading && S.items.length > 0);
    renderCounts(t);
    if (!S.items.length) {
      el.innerHTML = emptyHTML();
      S.rowStates = new Map();
      S.rowEls = [];
      return;
    }
    const vis = visibleItems(t);
    if (!vis.length) {
      el.innerHTML = emptyBox("没有符合筛选的活动", `一共 ${S.items.length} 场，换个关键词，或者放宽筛选。`, "clear-filter", "清除筛选");
      S.rowStates = new Map();
      S.rowEls = [];
      return;
    }
    let html = '<div class="colhead" aria-hidden="true"><span>开放时间</span><span>活动</span><span class="h-place">地点</span><span>距开放</span></div>';
    for (const g of groupItems(vis, t)) {
      if (g.kind === "now") { html += `<div class="nowline" aria-hidden="true"><span class="nowline-chip">现在 <b data-now>${hm(t)}</b></span></div>`; continue; }
      html += `<section class="gsec" data-g="${g.kind}"><div class="grp">${g.label}<span class="count">${g.items.length} 场</span></div>`;
      for (const it of g.items) html += rowHTML(it, t);
      html += "</section>";
    }
    el.innerHTML = html;
    S.rowStates = new Map(vis.map((it) => [it.id, stateOf(it, t)]));
    S.rowEls = $$(".row", el).map((r) => ({ id: r.dataset.id, left: $("[data-left]", r), ago: $("[data-ago]", r) }));
    const row = S.selectedId ? document.getElementById(rowDomId(S.selectedId)) : null;
    el.setAttribute("aria-activedescendant", row ? row.id : "");
  }

  function renderCounts(t) {
    let free = 0, signed = 0;
    for (const it of S.items) {
      const st = stateOf(it, t);
      if (st === "signed") signed++;
      else if (st !== "closed") free++;
    }
    const set = (k, v) => { const n = $(`[data-count="${k}"]`); if (n) n.textContent = S.items.length ? " " + v : ""; };
    set("free", free); set("signed", signed); set("all", S.items.length);
    $("#list-meta").textContent = S.list.loadedAt ? `${hm(S.list.loadedAt)} 更新` : "";
  }

  /* 每秒只改倒计时那几格；只有状态跨界（开放了、截止了）才整表重画 */
  function tickList(t) {
    const nx = computeNext(t);
    let dirty = nx !== S.nextId;
    if (!dirty) {
      for (const [id, st] of S.rowStates) {
        const it = S.byId.get(id);
        if (!it || stateOf(it, t) !== st) { dirty = true; break; }
      }
    }
    if (dirty) { renderList(); return; }
    for (const r of S.rowEls || []) {
      const it = S.byId.get(r.id);
      if (!it) continue;
      if (r.left) r.left.textContent = dur((it.signStart - t) / 1000);
      if (r.ago) r.ago.textContent = "已开放 " + ago((t - it.signStart) / 1000);
    }
    const nowEl = $("#list [data-now]");
    if (nowEl) nowEl.textContent = hm(t);
  }

  function applyFilterUI() {
    const f = S.prefs.filter;
    $("#q").value = f.q || "";
    $$("[data-status]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.status === f.status)));
    $$("[data-time]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.time === f.time)));
  }

  /* ------------------------------------------------ 运行记录 */
  function parseLog(raw) {
    let text = String(raw == null ? "" : raw).replace(/\s+$/, "");
    let time = null;
    const m = /^\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2}) (.*)$/.exec(text);
    if (m) { time = m[1]; text = m[2]; }
    let kind = "plain";
    if (/^=+$/.test(text.trim())) kind = "rule";
    else if (/^\[OK\]/.test(text)) kind = "ok";
    else if (/^\[X\]/.test(text)) kind = "bad";
    else if (/^\[!\]/.test(text)) kind = "warn";
    else if (/^\[i\]/.test(text)) kind = "info";
    else if (/^▶/.test(text)) kind = "start";
    else if (/^(Traceback|\s+File |\w+(\.\w+)*Error\b|playwright\.)/.test(text)) kind = "trace";
    else if (/^\s{2,}/.test(text)) kind = "cont";
    return { time: time || hms(now()), text, kind, raw: String(raw == null ? "" : raw) };
  }
  function logLineHTML(L) { return `<div class="lg" data-k="${L.kind}"><span class="lt">${L.time}</span><span class="tx">${esc(L.text)}</span></div>`; }
  function addLog(raw) {
    const L = parseLog(raw);
    S.logs.push(L);
    if (S.logs.length > 500) S.logs.splice(0, S.logs.length - 500);
    if (L.kind !== "rule" && L.text.trim()) $("#log-last").textContent = `${L.time}  ${L.text.trim()}`;
    if (!S.prefs.log_open) return;
    const body = $("#log-body");
    const stick = body.scrollTop + body.clientHeight >= body.scrollHeight - 24;
    const empty = $(".log-empty", body);
    if (empty) empty.remove();
    body.insertAdjacentHTML("beforeend", logLineHTML(L));
    while (body.childElementCount > 500) body.firstElementChild.remove();
    if (stick) body.scrollTop = body.scrollHeight;
  }
  function renderLogBody() {
    const body = $("#log-body");
    body.innerHTML = S.logs.length ? S.logs.map(logLineHTML).join("") : '<div class="log-empty">还没有记录。登录、刷新、报名时脚本说的话都会出现在这里。</div>';
    body.scrollTop = body.scrollHeight;
  }
  function setLogOpen(open) {
    S.prefs.log_open = !!open;
    const bar = $("#logbar");
    bar.dataset.open = String(!!open);
    const tg = $("#log-toggle");
    tg.textContent = open ? "收起" : "展开";
    tg.setAttribute("aria-expanded", String(!!open));
    if (open) renderLogBody();
    savePrefs({ log_open: !!open });
  }
  function copyLog() {
    const text = S.logs.map((l) => l.raw).join("\n");
    const done = () => toast("运行记录已复制", "ok");
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, fallback);
    else fallback();
    function fallback() {
      const ta = document.createElement("textarea");
      ta.value = text; document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); done(); } catch (e) { toast("复制没成功", "bad"); }
      ta.remove();
    }
  }

  /* ------------------------------------------------ 提示、弹窗、浮层 */
  function toast(html, tone) {
    tone = tone || "info";
    const ic = { ok: "check", warn: "alert", bad: "cross", info: "info" }[tone] || "info";
    const el = document.createElement("div");
    el.className = "toast";
    el.dataset.tone = tone;
    el.innerHTML = `${icon(ic)}<span>${html}</span>`;
    $("#toasts").appendChild(el);
    setTimeout(() => el.remove(), 3400);
  }
  function announce(text) { const a = $("#announcer"); a.textContent = ""; setTimeout(() => { a.textContent = text; }, 30); }

  function confirmDlg(o) {
    const dlg = $("#dlg-confirm");
    $("#confirm-title").textContent = o.title;
    $("#confirm-body").textContent = o.body;
    $("#confirm-yes").textContent = o.yes;
    $("#confirm-no").textContent = o.no;
    return new Promise((resolve) => {
      const finish = (v) => { dlg.removeEventListener("close", onClose); if (dlg.open) dlg.close(); resolve(v); };
      const onClose = () => finish(dlg.returnValue === "yes");
      dlg.returnValue = "";
      dlg.addEventListener("close", onClose);
      $("#confirm-yes").onclick = () => { dlg.returnValue = "yes"; dlg.close(); };
      $("#confirm-no").onclick = () => { dlg.returnValue = "no"; dlg.close(); };
      dlg.showModal();
      $("#confirm-no").focus();
    });
  }

  function openHelp(welcome) {
    const dlg = $("#dlg-help");
    $("#help-version").textContent = S.version ? `版本 ${S.version}` : "";
    S.helpWelcome = !!welcome;
    if (!dlg.open) dlg.showModal();
    $("#help-ok").focus();
  }

  function openPop(anchor, html) {
    const pop = $("#pop");
    pop.innerHTML = html;
    pop.hidden = false;
    const r = anchor.getBoundingClientRect();
    const w = pop.offsetWidth;
    pop.style.top = `${Math.round(r.bottom + 8)}px`;
    pop.style.left = `${Math.round(Math.max(12, Math.min(window.innerWidth - w - 12, r.right - w)))}px`;
    S.popAnchor = anchor;
  }
  function closePop() { const pop = $("#pop"); if (!pop.hidden) { pop.hidden = true; S.popAnchor = null; } }
  function pillPop() {
    const t = now();
    const L = S.login;
    let html;
    if (S.slot && S.slot.kind === "login") html = "<h4>正在登录</h4><p>去弹出来的 Chrome 窗口里完成登录。</p>";
    else if (L.state === "valid") {
      const exp = L.expiresAt;
      html = `<h4>登录有效</h4>${exp ? `<dl class="kv"><dt>预计到期</dt><dd>${esc(clockAt(exp, t))}</dd><dt>还剩</dt><dd>约 ${esc(durCoarse((exp - t) / 1000))}</dd></dl>` : ""}
        <p>按登录时刻加 17.5 小时估算，以学校为准。到期前随时可以重新登录。</p>
        <div class="acts"><button class="btn btn-line" type="button" data-act="login"${S.slot ? " disabled" : ""}>重新登录</button></div>`;
    } else if (L.state === "expired") html = `<h4>登录态失效了</h4><p>重新登录一次，大约又能用 17 小时。</p><div class="acts"><button class="btn btn-solid" type="button" data-act="login">重新登录</button></div>`;
    else if (L.state === "none") html = `<h4>这台电脑还没登录过</h4><p>点「登录」会弹出 Chrome，输入学号、密码和验证码。</p><div class="acts"><button class="btn btn-solid" type="button" data-act="login">登录</button></div>`;
    else if (L.state === "unreachable") html = `<h4>连不上学校站点</h4><p>检查网络、校园网或代理，然后点「刷新」。一直这样的话，重新登录一次试试。</p><div class="acts"><button class="btn btn-line" type="button" data-act="refresh">刷新</button></div>`;
    else html = "<h4>正在检查登录</h4><p>马上就好。</p>";
    openPop($("#pill"), html);
  }
  function clockPop() {
    const sk = S.clock.skew;
    let html;
    if (Math.abs(sk) > 3) {
      html = `<h4>${sk > 0 ? `本机时钟慢 ${sk} 秒` : `本机时钟快 ${-sk} 秒`}</h4>
        <p>${sk > 0 ? `学校服务器比这台电脑快 ${sk} 秒，到点会晚 ${sk} 秒才点。` : `这台电脑比学校服务器快 ${-sk} 秒，会提早开始刷新页面，真开放时反而可能慢几秒发现。`}</p>
        <p>到 Windows「设置 › 时间和语言 › 日期和时间」点「立即同步」，然后点「刷新」重新对一次。</p>`;
    } else if (S.clock.checked) html = "<h4>本机时钟准</h4><p>和学校服务器相差不到 3 秒。每次「刷新」都会重新对一次。</p>";
    else html = "<h4>还没和学校对过时间</h4><p>登录有效时，「刷新」会顺带和学校服务器对一次时间。</p>";
    openPop($("#clock"), html);
  }

  /* ------------------------------------------------ 偏好 */
  function savePrefs(patch, lazy) {
    Object.assign(S.prefs, patch);
    if (lazy) {
      clearTimeout(S.prefTimer);
      S.prefTimer = setTimeout(() => api("save_prefs", { filter: S.prefs.filter }), 500);
    } else {
      api("save_prefs", patch);
    }
  }

  /* ------------------------------------------------ 结束时响一声（WebAudio，不带音频文件） */
  const Sound = {
    ctx: null,
    prime() {
      try {
        this.ctx = this.ctx || new AudioContext();
        if (this.ctx.state === "suspended") this.ctx.resume();
      } catch (e) { this.ctx = null; }
    },
    play(kind) {
      if (!S.prefs.sound || !this.ctx) return;
      const c = this.ctx;
      const notes = kind === "ok" ? [660, 880, 1320] : [520, 390];
      notes.forEach((f, i) => {
        const o = c.createOscillator();
        const g = c.createGain();
        const t0 = c.currentTime + i * 0.16;
        o.type = "sine"; o.frequency.value = f;
        g.gain.setValueAtTime(0.0001, t0);
        g.gain.exponentialRampToValueAtTime(0.16, t0 + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.3);
        o.connect(g); g.connect(c.destination);
        o.start(t0); o.stop(t0 + 0.32);
      });
    },
  };

  /* ------------------------------------------------ 渲染总入口与时钟 */
  function renderAll() {
    renderTop();
    renderBanners();
    renderBoard();
    renderList();
  }

  let lastReal = Date.now();
  let fakeGap = 0;
  function tick() {
    const real = Date.now();
    const gap = real - lastReal + fakeGap;
    fakeGap = 0;
    lastReal = real;
    // 两次心跳隔了两分钟以上：多半是电脑睡过去了（最小化时浏览器会放慢计时，所以门槛放宽）
    if (gap > SLEEP_GAP_MS && S.slot && S.slot.kind === "grab") { S.sleep = { gap }; renderBanners(); }
    const t = now();
    if (S.result && S.result.autoClose && t > S.result.autoClose && !S.slot) S.result = null;
    renderTop();
    tickList(t);
    renderBoard();
  }
  function startTicker() {
    const loop = () => {
      tick();
      const t = now();
      setTimeout(loop, 1000 - (t % 1000) + 8);   // 对齐到整秒，倒计时跳秒才准
    };
    loop();
  }
  /* 原型与自测用：模拟一段「电脑睡着了」 */
  window.__zd = { simulateSleep(ms) { fakeGap += ms; } };

  /* ------------------------------------------------ 绑定 */
  function bindUI() {
    $("#btn-help").addEventListener("click", () => openHelp(false));
    $("#btn-refresh").addEventListener("click", () => refresh());
    $("#btn-login").addEventListener("click", doLogin);
    $("#pill").addEventListener("click", (e) => { e.stopPropagation(); if (S.popAnchor === $("#pill")) closePop(); else pillPop(); });
    $("#clock").addEventListener("click", (e) => { e.stopPropagation(); if (S.popAnchor === $("#clock")) closePop(); else clockPop(); });
    $("#pop").addEventListener("click", (e) => { const b = e.target.closest("[data-act]"); if (b) runAct(b.dataset.act); });
    document.addEventListener("mousedown", (e) => { if (!e.target.closest("#pop") && !e.target.closest("#pill") && !e.target.closest("#clock")) closePop(); });

    $("#board").addEventListener("click", (e) => { const b = e.target.closest("[data-act]"); if (b && !b.disabled) runAct(b.dataset.act); });
    $("#board").addEventListener("change", (e) => {
      const inp = e.target.closest("input[data-pref]");
      if (!inp) return;
      savePrefs({ [inp.dataset.pref]: inp.checked });
      if (inp.dataset.pref === "sound" && inp.checked) { Sound.prime(); Sound.play("ok"); }
    });
    $("#banners").addEventListener("click", (e) => { const b = e.target.closest("[data-act]"); if (b) runAct(b.dataset.act); });

    const list = $("#list");
    list.addEventListener("click", (e) => {
      const b = e.target.closest("[data-act]");
      if (b) { e.stopPropagation(); runAct(b.dataset.act); return; }
      const row = e.target.closest(".row");
      if (row) select(row.dataset.id);
    });
    list.addEventListener("dblclick", (e) => {
      const row = e.target.closest(".row");
      if (row && !e.target.closest("[data-act]")) { select(row.dataset.id); focusPrimary(); }
    });

    let qTimer = null;
    $("#q").addEventListener("input", () => {
      S.prefs.filter.q = $("#q").value;
      clearTimeout(qTimer);
      qTimer = setTimeout(renderList, 120);
      savePrefs({ filter: S.prefs.filter }, true);
    });
    $$("[data-status]").forEach((b) => b.addEventListener("click", () => {
      S.prefs.filter.status = b.dataset.status; applyFilterUI(); renderList(); savePrefs({ filter: S.prefs.filter }, true);
    }));
    $$("[data-time]").forEach((b) => b.addEventListener("click", () => {
      S.prefs.filter.time = b.dataset.time; applyFilterUI(); renderList(); savePrefs({ filter: S.prefs.filter }, true);
    }));

    $("#log-toggle").addEventListener("click", () => setLogOpen(!S.prefs.log_open));
    $("#log-copy").addEventListener("click", copyLog);
    $("#log-clear").addEventListener("click", () => { S.logs = []; renderLogBody(); $("#log-last").textContent = "还没有记录"; });
    $("#log-folder").addEventListener("click", () => api("open_data").then((r) => { if (r && r.ok === false) toast(r.text || "没打开", "warn"); }));

    const help = $("#dlg-help");
    $("#help-ok").addEventListener("click", () => help.close());
    help.addEventListener("close", () => { if (S.helpWelcome) { S.helpWelcome = false; savePrefs({ welcome_seen: true }); } });

    window.addEventListener("online", () => { S.online = true; renderAll(); toast("网络恢复了", "ok"); });
    window.addEventListener("offline", () => { S.online = false; renderAll(); });
    document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });

    document.addEventListener("keydown", (e) => {
      if (e.key === "F5") { e.preventDefault(); refresh(); return; }
      if (document.querySelector("dialog[open]")) return;
      const typing = e.target.closest && e.target.closest("input, textarea");
      if ((e.ctrlKey && (e.key === "f" || e.key === "F")) || (e.key === "/" && !typing)) { e.preventDefault(); $("#q").focus(); $("#q").select(); return; }
      if (e.key === "Escape") {
        if (!$("#pop").hidden) closePop();
        else if (typing) e.target.blur();
        else if (S.result && !S.slot) dismissResult();
        else if (S.selectedId) select(null);
        return;
      }
      if (typing) { if (e.key === "ArrowDown") { e.preventDefault(); $("#list").focus(); moveSel(1); } return; }
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); moveSel(e.key === "ArrowDown" ? 1 : -1); return; }
      const onButton = e.target.closest && e.target.closest("button, label, a");
      if (e.key === "Enter" && !onButton && S.selectedId) { e.preventDefault(); primaryAction(); }
    });
  }

  /* ------------------------------------------------ 启动 */
  async function boot() {
    if (S.booted) return;
    S.booted = true;
    bindUI();
    const b = (await api("bootstrap")) || {};
    S.version = b.version || "";
    const p = b.prefs || {};
    S.prefs = Object.assign({}, S.prefs, p, { filter: Object.assign({}, S.prefs.filter, p.filter || {}) });
    if (!/^(free|signed|all)$/.test(S.prefs.filter.status)) S.prefs.filter.status = "free";
    if (!/^(today|week|all)$/.test(S.prefs.filter.time)) S.prefs.filter.time = "all";
    const hl = b.login && typeof b.login.hours_left === "number" ? b.login.hours_left : null;
    if (hl != null) S.login.expiresAt = now() + hl * 3600e3;
    if (b.busy) S.slot = { kind: "unknown", startedAt: now(), tail: [] };
    applyFilterUI();
    $("#logbar").dataset.open = String(!!S.prefs.log_open);
    $("#log-toggle").textContent = S.prefs.log_open ? "收起" : "展开";
    if (S.prefs.log_open) renderLogBody();
    renderAll();
    if (!S.prefs.welcome_seen) openHelp(true);
    refresh({ force: true });
    startTicker();
    api("front_ready");
  }

  if (window.pywebview && window.pywebview.api) boot();
  else window.addEventListener("pywebviewready", boot, { once: true });
})();
