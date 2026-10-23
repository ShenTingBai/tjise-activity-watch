/* 原型专用：冒充 local/app.py + grab.py，让界面在浏览器里就能跑完整流程。
 *
 * 照抄真实后端的地方（改界面时可以放心对照）：
 *   - 方法名、参数、立即返回值：bootstrap / help / front_ready / check / list_activities / login / grab / stop / open_data / save_prefs
 *   - 事件：onBackend.log / check / activities / done，载荷字段与 app.py 一致
 *   - 同一时间只允许一个子进程（app.py _spawn 的锁）：登录和报名共用
 *   - 日志原话、退出码，以及 done 的 ok / reason 算法（app.py _pump）
 *   - 时序：开放前 100 秒打日志、开放前 5 秒开始找按钮、找不到按钮时 2→8 秒退避刷新、开放后最多 120 秒（grab.py cmd_grab）
 *   - check 失败时 text 只剩最后一行「请运行 …」（app.py _do_check 只取最后一行）
 *
 * 时间走 ProtoClock：控制台可以暂停、快进；场景（?scene=）决定初始数据和自动操作。
 * 这个文件不进 local/ui。
 */
(function () {
  "use strict";

  /* ------------------------------------------------------------ 时钟 */
  const ProtoClock = {
    offset: 0,
    paused: false,
    pausedAt: 0,
    now() { return this.paused ? this.pausedAt : Date.now() + this.offset; },
    set(ms) { if (this.paused) this.pausedAt = ms; else this.offset = ms - Date.now(); },
    shift(d) { this.set(this.now() + d); },
    pause() { if (!this.paused) { this.pausedAt = this.now(); this.paused = true; } },
    resume() { if (this.paused) { this.offset = this.pausedAt - Date.now(); this.paused = false; } },
  };
  window.ProtoClock = ProtoClock;
  const now = () => ProtoClock.now();

  const params = new URLSearchParams(location.search);
  const SCENE_ID = params.get("scene") || "ready";
  const FREEZE = params.get("freeze") === "1";

  /* ------------------------------------------------------------ 工具 */
  const H = 3600e3, MIN = 60e3, SEC = 1e3;
  const pad = (n) => String(n).padStart(2, "0");
  const ceilTo = (ms, step) => Math.ceil(ms / step) * step;
  const floorTo = (ms, step) => Math.floor(ms / step) * step;
  function fmtTs(ms) {
    if (ms == null) return "";
    const d = new Date(ms);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }
  function parseTs(s) {
    const m = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})$/.exec(String(s || ""));
    return m ? new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]).getTime() : null;
  }
  function dayAt(t, dayOffset, h, m, s) {
    const d = new Date(t);
    d.setDate(d.getDate() + dayOffset);
    d.setHours(h, m, s || 0, 0);
    return d.getTime();
  }
  const later = (ms, fn) => setTimeout(fn, ms);
  const rand = (a, b) => a + Math.floor(Math.random() * (b - a));
  const P = (v) => Promise.resolve(v);
  const signed = (n) => (n > 0 ? "+" : "") + n;

  /* ------------------------------------------------------------ 世界 */
  const W = {
    sessionAt: null,        // 上次登录成功的时刻；null = 这台电脑没登录过
    tokenOk: true,          // 学校还认不认这个登录态
    online: true,
    skew: 0,                // 学校服务器时间 - 本机时间（秒），与 grab.py clock_skew 同号
    acts: [],
    prefs: { welcome_seen: true },
    proc: null,
    grabOutcome: "success", // success | late | full | not-open | no-response | token-expire | network | already | no-browser
    loginMode: "window",    // window（等控制台里的模拟 Chrome）| auto | closed | timeout | unreachable | verify
    feishu: false,
    hasPlay: false,
  };
  const TOKEN_H = 17.5;
  const find = (id) => W.acts.find((a) => a.id === id);
  const tokenValid = () => W.sessionAt != null && W.tokenOk && now() - W.sessionAt < TOKEN_H * H;
  const hoursLeft = () => (W.sessionAt == null ? null : Math.max(0, TOKEN_H - (now() - W.sessionAt) / H));
  function fresh(hoursAgo) { W.sessionAt = now() - hoursAgo * H; W.tokenOk = true; }

  function seed(t) {
    const next = ceilTo(t + 6 * MIN + 20 * SEC, MIN);
    const lab = ceilTo(t + 2.5 * H, 30 * MIN);
    const ai = ceilTo(t + 5 * H, 30 * MIN);
    const opened = floorTo(t - 23 * MIN, MIN);
    const half = (ms) => floorTo(ms, 30 * MIN);
    return [
      { id: "act-open", name: "专升本学习经验交流会", place: "求是楼 201", start: opened, end: opened + 4 * H, act: dayAt(t, 1, 19, 0) },
      { id: "act-security", name: "网络安全·数字时代大长城", place: "教学楼 A301", start: next, end: next + 3 * H, act: dayAt(t, 2, 14, 0) },
      { id: "act-lab", name: "程序设计实验室开放日", place: "实训楼 B204", start: lab, end: lab + 20 * H, act: dayAt(t, 3, 9, 30) },
      { id: "act-ai", name: "人工智能应用实践工作坊", place: "实训楼 B205", start: ai, end: ai + 20 * H, act: dayAt(t, 3, 14, 0) },
      { id: "act-resume", name: "简历门诊：一对一改简历", place: "线上（腾讯会议）", start: dayAt(t, 1, 9, 0), end: dayAt(t, 2, 18, 0), act: dayAt(t, 2, 19, 30) },
      { id: "act-english", name: "英语角（第 8 期）", place: "外语楼 105", start: dayAt(t, 1, 12, 0), end: dayAt(t, 2, 12, 0), act: dayAt(t, 2, 18, 30) },
      { id: "act-youth", name: "「青年大学习」主题团日活动暨优秀团支部风采展示（秋季学期）", place: "大学生活动中心多功能厅", start: dayAt(t, 2, 10, 0), end: dayAt(t, 3, 10, 0), act: dayAt(t, 4, 15, 0) },
      { id: "act-python", name: "Python 数据分析入门", place: "实训楼 C305", start: dayAt(t, 2, 0, 59, 59), end: dayAt(t, 3, 12, 0), act: dayAt(t, 4, 14, 0) },
      { id: "act-library", name: "图书馆志愿服务（第 12 期）", place: "图书馆一层", start: dayAt(t, 3, 8, 0), end: dayAt(t, 4, 8, 0), act: dayAt(t, 5, 8, 30) },
      { id: "act-marathon", name: "校园马拉松志愿服务", place: "体育场", start: dayAt(t, 4, 18, 0), end: dayAt(t, 5, 18, 0), act: dayAt(t, 6, 7, 0) },
      { id: "act-photo", name: "宿舍文化节摄影征集", place: "线上", start: null, end: null, act: dayAt(t, 9, 0, 0) },
      { id: "act-career", name: "职业生涯规划讲座", place: "报告厅", start: half(t - 26 * H), end: floorTo(t - 20 * MIN, MIN), act: dayAt(t, 1, 15, 0) },
      { id: "act-face", name: "我与学生面对面（天软信息化发展之路）", place: "报告厅", start: half(t - 5 * H), end: half(t + 20 * H), act: dayAt(t, 1, 14, 0), signed: true },
      { id: "act-reading", name: "读书分享会·技术人文", place: "图书馆 3F", start: half(t - 30 * H), end: half(t + 6 * H), act: dayAt(t, 2, 19, 0), signed: true },
      { id: "act-movie", name: "红色观影《长津湖》", place: "大礼堂", start: half(t - 50 * H), end: half(t + 2 * H), act: dayAt(t, 1, 18, 30), signed: true },
    ];
  }

  /* grab.py sign_state */
  function signState(a, t) {
    if (!a.start) return "时间未知";
    if (t < a.start) {
      const left = Math.floor((a.start - t) / 1000);
      return `未开放（${Math.floor(left / 3600)} 小时 ${Math.floor((left % 3600) / 60)} 分后）`;
    }
    if (a.end && t > a.end) return "报名已截止";
    return "开放中";
  }

  /* grab.py cmd_list --json + app.py _do_list 补的 seconds_to_open */
  function listJson() {
    const t = now();
    const rows = W.acts.filter((a) => !a.end || a.end >= t - H);   // still_relevant
    const rank = (a) => (a.signed ? 2 : a.start && t < a.start ? 0 : 1);
    rows.sort((a, b) => rank(a) - rank(b) || (fmtTs(a.start) || "9999").localeCompare(fmtTs(b.start) || "9999") || a.name.localeCompare(b.name));
    return rows.map((a, i) => ({
      index: i + 1,
      id: a.id,
      name: a.name,
      sign_start: fmtTs(a.start),
      sign_end: fmtTs(a.end),
      act_start: fmtTs(a.act),
      place: a.place,
      signed: !!a.signed,
      state: a.signed ? "已报名" : signState(a, t),
      seconds_to_open: a.start ? Math.trunc((a.start - t) / 1000) : null,
    }));
  }

  /* ------------------------------------------------------------ 事件推送（app.py _emit） */
  function emit(kind, payload) {
    later(0, () => {
      const fn = window.onBackend && window.onBackend[kind];
      if (typeof fn === "function") fn(payload);
    });
  }
  const emitLog = (line) => emit("log", line);

  /* 子进程的一行输出：app.py _pump 会跳过空行，并只留最后 40 行做判断 */
  function out(p, line) {
    if (!line) return;
    p.tail.push(line);
    if (p.tail.length > 40) p.tail.shift();
    emitLog(line);
  }
  /* grab.py log()：带时间戳 */
  const logTs = (p, msg) => out(p, `${fmtTs(now())} ${msg}`);

  /* app.py _pump 结尾：算 ok 和 reason */
  function exit(p, code) {
    if (p.exited) return;
    p.exited = true;
    let ok;
    if (p.kind === "login") ok = code === 0;
    else ok = code === 0 && p.tail.some((l) => l.indexOf("报名成功") >= 0);
    let reason = "";
    if (!ok) {
      for (let i = p.tail.length - 1; i >= 0; i--) {
        if (p.tail[i].startsWith("[X] ")) { reason = p.tail[i].slice(4).trim(); break; }
      }
      reason = reason || `退出码 ${code}`;
    }
    if (p.kind === "login") post({ type: "chrome", open: false });
    later(30, () => {
      emit("done", { ok, code, reason, label: p.label, kind: p.kind });
      if (W.proc === p) W.proc = null;
    });
  }

  /* ------------------------------------------------------------ 登录进程（grab.py cmd_login） */
  function startLogin() {
    const p = { kind: "login", label: "登录", tail: [], t0: now(), phase: "boot" };
    emitLog("▶ 登录");
    out(p, "=".repeat(64));
    out(p, "即将打开浏览器，请在窗口里完成学生登录（账号 + 密码 + 图形验证码）。");
    out(p, "登录成功会自动保存登录态并关闭窗口；最多等待 600 秒。");
    out(p, "=".repeat(64));
    p.openAt = now() + 1200;
    return p;
  }
  function loginSucceeded(p) {
    W.sessionAt = now();
    W.tokenOk = true;
    out(p, "[OK] 登录成功，登录态已保存（有效期约 17 小时，失效后重新运行本步即可）。");
    exit(p, 0);
  }
  function stepLogin(p, t) {
    if (p.exited) return;
    if (p.phase === "boot") {
      if (t < p.openAt) return;
      if (W.loginMode === "unreachable" || !W.online) { out(p, "[X] 打不开登录页：TimeoutError"); return exit(p, 2); }
      p.phase = "wait";
      if (W.loginMode === "window") post({ type: "chrome", open: true });
      p.decideAt = t + (W.loginMode === "window" ? (W.hasPlay ? Infinity : 4500) : W.loginMode === "timeout" ? 600e3 : 2600);
      return;
    }
    if (p.phase === "wait") {
      if (t >= p.t0 + 600e3) { out(p, "[X] 等了 600 秒仍未检测到登录成功，请重试。"); return exit(p, 1); }
      if (p.submitAt && t >= p.submitAt) return loginSucceeded(p);
      if (t < p.decideAt) return;
      if (W.loginMode === "closed") { out(p, "[X] 窗口被关掉了，登录未完成。"); return exit(p, 1); }
      if (W.loginMode === "verify") { W.sessionAt = now(); W.tokenOk = false; out(p, "[X] 已保存登录态，但接口校验失败：登录态已过期（身份验证失败）"); return exit(p, 2); }
      if (W.loginMode === "timeout") return;
      return loginSucceeded(p);
    }
  }

  /* ------------------------------------------------------------ 抢报名进程（grab.py cmd_grab） */
  function startGrab(id, name, at, headed) {
    const p = { kind: "grab", label: "等待并抢报名：" + name, tail: [], id, name, at, headed, phase: "boot", next: now() + 450 };
    emitLog("▶ " + p.label);
    return p;
  }
  function traceNet(p) {
    out(p, "Traceback (most recent call last):");
    out(p, '  File "grab.py", line 650, in cmd_grab');
    out(p, '    page.reload(wait_until="domcontentloaded", timeout=60000)');
    out(p, "playwright._impl._errors.Error: Page.reload: net::ERR_INTERNET_DISCONNECTED");
    exit(p, 1);
  }
  function buttonVisible(p, t) {
    const serverT = t + W.skew * 1000;     // 按钮按学校的钟出现
    switch (W.grabOutcome) {
      case "full":
      case "token-expire": return false;
      default: return serverT >= p.atTs + 300;
    }
  }
  function attemptResult(p) {
    switch (W.grabOutcome) {
      case "late": return p.attempts < 3 ? { ok: false, reason: "code:0 未到报名时间" } : { ok: true, reason: "code:1 操作成功" };
      case "not-open": return { ok: false, reason: "code:0 报名尚未开始" };
      case "no-response": return { ok: false, reason: "未收到 signUp 响应" };
      default: return { ok: true, reason: "code:1 操作成功" };
    }
  }
  function finishGrab(p) {
    const a = find(p.id);
    const ok = p.ok || (a && a.signed);
    if (ok) {
      if (a) a.signed = true;
      logTs(p, `[OK] 报名成功（尝试 ${p.attempts} 次）`);
      out(p, `[OK] 报名成功：${p.name}`);
      out(p, "     不想去请尽快在活动页取消，别占名额。");
      if (W.feishu) logTs(p, "[i] 飞书通知已发送");
      return exit(p, 0);
    }
    logTs(p, `[X] 报名失败（尝试 ${p.attempts} 次，最近原因 ${p.lastReason || "名额已满/未开放"}）`);
    out(p, `[X] 没抢到：${p.lastReason || "名额已满／页面未放开"}`);
    out(p, "    建议手动打开活动页确认，必要时再试一次。");
    if (W.feishu) logTs(p, "[i] 飞书通知已发送");
    return exit(p, 2);
  }
  function stepGrab(p, t) {
    if (p.exited) return;
    switch (p.phase) {
      case "boot": {
        if (t < p.next) return;
        // check_token
        if (W.sessionAt == null) { out(p, "[X] 本机还没有登录过（data/session.json 不存在）"); out(p, "    请先运行：python grab.py login"); return exit(p, 3); }
        if (!W.online) { out(p, "[X] 接口异常：URLError"); out(p, "    请先运行：python grab.py login"); return exit(p, 3); }
        if (!tokenValid()) { out(p, "[X] 登录态已过期（身份验证失败）"); out(p, "    请先运行：python grab.py login"); return exit(p, 3); }
        if (Math.abs(W.skew) > 3) out(p, `[!] 本机时钟与服务器相差 ${signed(W.skew)} 秒，建议校准系统时间后再抢。`);
        // resolve_target
        if (p.at) p.atTs = parseTs(p.at);
        if (!p.atTs) {
          const a = find(p.id);
          if (!a || !a.start) { out(p, `[X] 活动 ${p.id} 找不到报名开始时间，请显式传 --at`); return exit(p, 0); }
          p.atTs = a.start; p.at = fmtTs(a.start);
        }
        if (p.atTs - t > 86400e3) out(p, "[i] 报名开始时间在 24 小时之后，确认无误再跑。");
        out(p, "[i] 目标：" + p.name);
        out(p, `[i] 报名开始：${p.at}；提前 100 秒打开页面；失败重试窗口 120 秒`);
        if (W.grabOutcome === "no-browser") {
          out(p, "Traceback (most recent call last):");
          out(p, '  File "grab.py", line 607, in cmd_grab');
          out(p, "RuntimeError: 没找到可用的浏览器：请先装 Chrome 或 Edge，或执行 python -m playwright install chromium 下载自带内核。");
          return exit(p, 1);
        }
        p.phase = "launch"; p.next = t + 2600;   // 拉起浏览器 + goto + 等 1.5 秒
        return;
      }
      case "launch": {
        if (t < p.next) return;
        if (!W.online) return traceNet(p);
        logTs(p, "[i] 详情页已加载");
        const a = find(p.id);
        if ((a && a.signed) || W.grabOutcome === "already") {
          logTs(p, "[i] 该活动已是报名状态，跳过提交");
          out(p, "[i] 你已经报过这个活动了。");
          return exit(p, 0);
        }
        if (!p.headed) out(p, "[i] 后台等待中，到点自动提交；想看着它点就加 --headed。");
        p.phase = "wait";
        return;
      }
      case "wait":
        if (t < p.atTs - 100e3) return;
        logTs(p, "[i] 开始等待开放");
        p.phase = "prepoll";
        return;
      case "prepoll":
        if (t < p.atTs - 5e3) return;
        p.phase = "poll"; p.deadline = p.atTs + 120e3; p.attempts = 0; p.lastReason = "";
        p.lastReload = 0; p.gap = 2000; p.nextCheck = t;
        return;
      case "poll": {
        if (W.grabOutcome === "token-expire" && t >= p.atTs) W.tokenOk = false;
        if (W.grabOutcome === "network" && t >= p.atTs + 800) { W.online = false; setOnline(false); }
        if (t >= p.deadline) return finishGrab(p);
        if (t < p.nextCheck) return;
        if (!buttonVisible(p, t)) {
          if (!W.online) return traceNet(p);
          if (t - p.lastReload >= p.gap) { p.lastReload = t; p.gap = Math.min(8000, p.gap * 1.5); p.nextCheck = t + 550; }
          else p.nextCheck = t + 300;
          return;
        }
        p.attempts += 1;
        const r = attemptResult(p);
        p.lastReason = r.reason;
        logTs(p, `[i] 第 ${p.attempts} 次提交：${r.reason}`);
        if (r.ok) { p.ok = true; return finishGrab(p); }
        p.nextCheck = t + (r.reason.indexOf("signUp") >= 0 ? 12000 : 2400);
        return;
      }
      default:
    }
  }

  setInterval(() => {
    const p = W.proc;
    if (!p) return;
    const t = now();
    if (p.kind === "login") stepLogin(p, t); else stepGrab(p, t);
  }, 100);

  /* ------------------------------------------------------------ 冒充 window.pywebview.api */
  const HELP = {
    title: "准点怎么用",
    subtitle: "登录一次，选好一场，到报名开放那一秒替你点「立即报名」。",
    steps: [
      { no: 1, title: "先登录", text: "点右上角「登录」，在弹出的 Chrome 里登录一次。" },
      { no: 2, title: "选一场", text: "在时刻表里点一行，再点「到点自动报名」。" },
      { no: 3, title: "电脑别睡", text: "等的时候电脑别睡眠、别断网；窗口可以最小化，别关。" },
    ],
    note: "只给本人账号使用，不要借别人的账号，也不要同时开多个。",
  };

  function doCheck() {
    const hl = hoursLeft();
    const hint = "    请运行：python grab.py login   （或双击 1-登录.bat）";
    if (W.sessionAt == null || !W.online || !tokenValid()) return emit("check", { ok: false, hours_left: hl, text: hint });
    const text = Math.abs(W.skew) > 3
      ? `[!] 本机时钟与服务器相差 ${signed(W.skew)} 秒，建议校准系统时间，否则会抢跑或迟到。`
      : "[OK] 登录态有效。";
    emit("check", { ok: true, hours_left: hl, text });
  }
  function doList() {
    const hint = "    请先运行：python grab.py login（或点界面上的「登录」）";
    if (W.sessionAt == null) return emit("activities", { ok: false, text: "[X] 本机还没有登录过（data/session.json 不存在）\n" + hint, items: [] });
    if (!W.online) return emit("activities", { ok: false, text: "[X] 接口异常：URLError\n" + hint, items: [] });
    if (!tokenValid()) return emit("activities", { ok: false, text: "[X] 登录态已过期（身份验证失败）\n" + hint, items: [] });
    emit("activities", { ok: true, items: listJson() });
  }

  function spawn(make) {
    if (W.proc && !W.proc.exited) return P({ ok: false, text: "已经有一个任务在跑，先等它结束" });
    W.proc = make();
    return P({ ok: true });
  }

  const api = {
    bootstrap: () => P({ version: "2.0.0", prefs: JSON.parse(JSON.stringify(W.prefs)), help: HELP, login: { hours_left: hoursLeft() }, busy: !!W.proc }),
    help: () => P(HELP),
    front_ready: () => { later(0, onAppReady); return P({ ok: true }); },
    check: () => { later(rand(500, 900), doCheck); return P({ ok: true, queued: true }); },
    list_activities: () => { later(rand(800, 1300), doList); return P({ ok: true, queued: true }); },
    login: () => spawn(startLogin),
    grab: (id, name, at, headed) => spawn(() => startGrab(String(id), String(name), String(at || ""), !!headed)),
    stop: () => {
      const p = W.proc;
      if (p && !p.exited) {
        emitLog("已请求停止");
        p.phase = "dead";
        later(250, () => exit(p, 1));    // Windows 上 terminate() 的退出码是 1
        return P({ ok: true });
      }
      return P({ ok: false, text: "当前没有在跑的任务" });
    },
    open_data: () => { post({ type: "note", text: "（原型）这里会用资源管理器打开 local\\data" }); return P({ ok: true }); },
    save_prefs: (patch) => { Object.assign(W.prefs, patch || {}); return P({ ok: true }); },
  };
  window.pywebview = { api };

  /* ------------------------------------------------------------ 网络开关（让界面收到真实的 online/offline 事件） */
  function setOnline(on) {
    W.online = on;
    try { Object.defineProperty(navigator, "onLine", { configurable: true, get: () => W.online }); } catch (e) { /* 忽略 */ }
    window.dispatchEvent(new Event(on ? "online" : "offline"));
  }

  /* ------------------------------------------------------------ 场景 */
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  async function waitFor(test, timeout) {
    const t0 = Date.now();
    const lim = timeout || 9000;
    for (;;) {
      const v = typeof test === "string" ? document.querySelector(test) : test();
      if (v) return v;
      if (Date.now() - t0 > lim) throw new Error("等不到：" + test);
      await wait(60);
    }
  }
  async function click(sel) { const el = await waitFor(sel); el.click(); await wait(80); return el; }
  const selectRow = (id) => click(`.row[data-id="${id}"]`);
  const armSelected = () => click('#board [data-act="arm"], #board [data-act="arm-anyway"]');
  const waitClock = (ms) => waitFor(() => now() >= ms, 600e3);
  const boardHas = (s) => () => (document.querySelector("#board") || {}).textContent.indexOf(s) >= 0;
  const pageReady = () => waitFor(() => W.proc && W.proc.kind === "grab" && ["wait", "prepoll", "poll"].indexOf(W.proc.phase) >= 0, 12000);
  function soon(id, ms) { const a = find(id); a.start = ceilTo(now() + ms, SEC); a.end = a.start + 3 * H; return a; }

  const SCENES = [
    { id: "ready", group: "日常", title: "打开就是这样", desc: "登录有效。时刻表按开放时间排，大屏挂着下一场。",
      setup() { fresh(3.2); } },
    { id: "first-run", group: "第一次", title: "第一次打开", desc: "没登录过：先弹使用说明，大屏和列表都只引导一件事：登录。",
      setup() { W.sessionAt = null; W.prefs = {}; } },
    { id: "logging-in", group: "第一次", title: "正在登录", desc: "登录也占着唯一的任务位：大屏显示三步和剩余等待时间（最多 10 分钟）。",
      setup() { W.sessionAt = null; W.loginMode = "window"; },
      async run() { await waitFor(boardHas("先登录一次")); await click("#btn-login"); await waitFor(() => W.proc && W.proc.phase === "wait"); } },
    { id: "login-ok", group: "第一次", title: "登录好了", desc: "大屏转绿，给出估算的到期时间，接着自动拉活动列表。",
      setup() { W.sessionAt = null; W.loginMode = "auto"; },
      async run() { await waitFor(boardHas("先登录一次")); await click("#btn-login"); await waitFor('#board[data-tone="ok"]', 15000); await wait(1600); } },
    { id: "login-fail", group: "第一次", title: "登录没完成", desc: "用户关掉了 Chrome 窗口：说清发生了什么，给「再试一次」。",
      setup() { W.sessionAt = null; W.loginMode = "closed"; },
      async run() { await waitFor(boardHas("先登录一次")); await click("#btn-login"); await waitFor('#board[data-tone="bad"]', 15000); await wait(1200); } },
    { id: "preview", group: "挑一场", title: "选中一场", desc: "点一行，它就上了大屏。动手之前先看：登录撑不撑得到开放、时钟准不准。",
      setup() { fresh(3.2); }, async run() { await selectRow("act-security"); } },
    { id: "preview-warn", group: "挑一场", title: "登录撑不到开放", desc: "登录约 1.9 小时后过期，开放在 2.5 小时后：主按钮换成「先重新登录」。",
      setup() { fresh(15.6); }, async run() { await selectRow("act-lab"); } },
    { id: "preview-open", group: "挑一场", title: "已经开放的场次", desc: "按钮变成「立即替我报名」：at 传当前时间，2 分钟重试窗口从现在算。",
      setup() { fresh(3.2); }, async run() { await selectRow("act-open"); } },
    { id: "all", group: "日常", title: "全部视图", desc: "已报名、刚截止、时间待公布各成一组；「现在」线隔开已开放和未开放。",
      setup() { fresh(3.2); },
      async run() { await click('[data-status="all"]'); const l = await waitFor("#list"); l.scrollTop = l.scrollHeight; await wait(100); } },
    { id: "no-match", group: "挑一场", title: "筛选没结果", desc: "说清一共有多少场，给「清除筛选」。",
      setup() { fresh(3.2); },
      async run() { const q = await waitFor("#q"); await waitFor(".row"); q.value = "量子计算"; q.dispatchEvent(new Event("input", { bubbles: true })); await wait(300); } },
    { id: "armed", group: "等开放", title: "等开放", desc: "大屏点亮：倒计时 + 线路图，每一站都是脚本里真实的时间点。",
      setup() { fresh(3.2); soon("act-security", 2 * H + 14 * MIN + 37 * SEC); },
      async run() { await selectRow("act-security"); await armSelected(); await pageReady(); await wait(300); } },
    { id: "final", group: "等开放", title: "开放前 5 秒", desc: "开始每 0.3 秒找一次「立即报名」，数字转红。",
      setup() { fresh(3.2); soon("act-security", 13 * SEC); },
      async run() { await selectRow("act-security"); await armSelected(); if (FREEZE) await waitClock(find("act-security").start - 3200); } },
    { id: "submitting", group: "等开放", title: "正在提交", desc: "前两次学校回「未到报名时间」，还在 2 分钟窗口里接着试。",
      setup() { fresh(3.2); W.grabOutcome = "late"; soon("act-security", 8 * SEC); },
      async run() { await selectRow("act-security"); await armSelected(); if (FREEZE) { await waitFor(boardHas("第 2 次"), 30000); await wait(200); } } },
    { id: "switch", group: "等开放", title: "想换一场", desc: "一次只能等一场：选别的行会提示「换成这一场」，确认后先停再换。",
      setup() { fresh(3.2); soon("act-security", 2 * H + 14 * MIN); },
      async run() { await selectRow("act-security"); await armSelected(); await pageReady(); await selectRow("act-lab"); await waitFor(".row-extra"); } },
    { id: "success", group: "结果", title: "报名成功", desc: "大屏转绿：活动名、去活动页确认一眼、不想去就尽快取消。",
      setup() { fresh(3.2); W.grabOutcome = "success"; soon("act-security", 7 * SEC); },
      async run() { await selectRow("act-security"); await armSelected(); await waitFor('#board[data-tone="ok"]', 30000); await wait(1800); } },
    { id: "fail-full", group: "结果", title: "没抢到：名额已满", desc: "开放后 2 分钟里按钮一直没出现。说原因，给下一步。",
      setup() { fresh(3.2); W.grabOutcome = "full"; soon("act-security", 6 * SEC); },
      async run() {
        await selectRow("act-security"); await armSelected();
        await waitClock(find("act-security").start + 1000); ProtoClock.shift(117e3);
        await waitFor('#board[data-tone="bad"]', 15000); await wait(2600);
      } },
    { id: "fail-login", group: "结果", title: "没抢到：登录中途失效", desc: "脚本只看到按钮没出现；界面顺手复查登录，指出真正的原因。",
      setup() { fresh(3.2); W.grabOutcome = "token-expire"; soon("act-security", 6 * SEC); },
      async run() {
        await selectRow("act-security"); await armSelected();
        await waitClock(find("act-security").start + 1000); ProtoClock.shift(117e3);
        await waitFor(boardHas("登录态已经失效"), 20000); await wait(300);
      } },
    { id: "already", group: "结果", title: "已经报过", desc: "脚本打开活动页发现已经是报名状态，直接收手。",
      setup() { fresh(3.2); W.grabOutcome = "already"; soon("act-security", 40 * MIN); },
      async run() { await selectRow("act-security"); await armSelected(); await waitFor(boardHas("你已经报过这一场"), 15000); } },
    { id: "stopped", group: "结果", title: "已停止", desc: "用户主动停下：不算失败，给「重新开始等」。",
      setup() { fresh(3.2); soon("act-security", 2 * H + 14 * MIN); },
      async run() {
        await selectRow("act-security"); await armSelected(); await pageReady();
        await click('#board [data-act="stop"]'); await click('#dlg-confirm[open] [data-answer="yes"]');
        await waitFor(boardHas("已停止等待"), 8000);
      } },
    { id: "expired", group: "出状况", title: "登录态失效", desc: "胶囊变红，大屏和列表都只给一个动作：重新登录。",
      setup() { W.sessionAt = now() - 18 * H; W.tokenOk = false; } },
    { id: "offline", group: "出状况", title: "断网", desc: "说清是网络问题，不让人白白去重新登录。",
      setup() { fresh(3.2); setOnline(false); } },
    { id: "empty", group: "出状况", title: "列表是空的", desc: "这里还没有活动：点「刷新」拉一次。",
      setup() { fresh(3.2); W.acts = []; } },
    { id: "clock-skew", group: "挑一场", title: "本机时钟慢 6 秒", desc: "顶栏时钟标黄；挂之前的检查也提醒，并告诉去哪里同步。",
      setup() { fresh(3.2); W.skew = 6; }, async run() { await selectRow("act-security"); } },
    { id: "sleep", group: "等开放", title: "电脑睡过一觉", desc: "心跳断了 25 分钟：提醒可能错过了开放时间。",
      setup() { fresh(3.2); soon("act-security", 2 * H + 14 * MIN); },
      async run() {
        await selectRow("act-security"); await armSelected(); await pageReady();
        window.__zd.simulateSleep(25 * MIN); await waitFor('.banner[data-tone="warn"]', 4000);
      } },
  ];
  const scene = SCENES.find((s) => s.id === SCENE_ID) || SCENES[0];

  W.acts = seed(now());
  if (scene.setup) scene.setup();

  let appReady = false;
  async function onAppReady() {
    if (appReady) return;
    appReady = true;
    try {
      await waitFor(() => document.querySelector(".row, .empty"), 9000);
      if (scene.run) await scene.run();
    } catch (e) {
      console.warn("[原型] 场景没跑完：", e && e.message);
    }
    if (FREEZE) ProtoClock.pause();
    document.documentElement.dataset.sceneReady = "1";
    post({ type: "scene-ready", id: scene.id });
  }

  /* ------------------------------------------------------------ 和 play.html 的控制台对话 */
  function post(msg) {
    if (window.parent === window) return;
    try { window.parent.postMessage(Object.assign({ src: "zd-proto" }, msg), "*"); } catch (e) { /* 忽略 */ }
  }
  function status() {
    const p = W.proc;
    return {
      type: "status",
      now: now(), paused: ProtoClock.paused, scene: scene.id,
      proc: p && !p.exited ? { kind: p.kind, phase: p.phase, at: p.atTs || null, attempts: p.attempts || 0 } : null,
      online: W.online, skew: W.skew, hoursLeft: hoursLeft(), tokenOk: W.tokenOk,
      grabOutcome: W.grabOutcome, loginMode: W.loginMode, feishu: W.feishu,
    };
  }
  setInterval(() => post(status()), 500);

  function clickRefresh() { const b = document.getElementById("btn-refresh"); if (b && !b.disabled) b.click(); }

  window.addEventListener("message", (e) => {
    const m = e.data;
    if (!m || m.src !== "zd-play") return;
    const p = W.proc;
    switch (m.cmd) {
      case "hello":
        W.hasPlay = true;
        post({ type: "scenes", list: SCENES.map((s) => ({ id: s.id, group: s.group, title: s.title, desc: s.desc })), current: scene.id });
        break;
      case "set":
        if (m.key === "grabOutcome" || m.key === "loginMode") W[m.key] = m.value;
        if (m.key === "feishu") W.feishu = !!m.value;
        break;
      case "net": setOnline(!!m.online); break;
      case "skew": W.skew = m.value | 0; clickRefresh(); break;
      case "session":
        if (m.value === "fresh") fresh(0.1);
        else if (m.value === "short") fresh(TOKEN_H - 1.4);
        else if (m.value === "expired") { W.sessionAt = now() - 18 * H; W.tokenOk = false; }
        else if (m.value === "none") W.sessionAt = null;
        clickRefresh();
        break;
      case "clock":
        if (m.op === "pause") ProtoClock.pause();
        else if (m.op === "resume") ProtoClock.resume();
        else if (m.op === "shift") ProtoClock.shift(m.ms || 0);
        break;
      case "jump":
        if (p && p.kind === "grab" && p.atTs) ProtoClock.set(p.atTs + (m.offset || 0));
        else post({ type: "note", text: "先点「到点自动报名」挂上一场，才能跳到开放前后" });
        break;
      case "sleep": if (window.__zd) window.__zd.simulateSleep(m.ms || 25 * MIN); break;
      case "login":
        if (p && p.kind === "login" && p.phase === "wait" && !p.exited) {
          if (m.action === "submit") p.submitAt = now() + 1600;     // 检测到 token（≤1 秒）+ 等页面写完 1.5 秒
          if (m.action === "close") { out(p, "[X] 窗口被关掉了，登录未完成。"); exit(p, 1); }
        }
        break;
      default:
    }
    post(status());
  });
})();
