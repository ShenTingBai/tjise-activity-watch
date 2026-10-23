/* 交互原型外壳：把 app.html 装进一个 Windows 窗口，旁边放控制台。
 * 和 iframe 里的 mock-backend.js 用 postMessage 说话（file:// 下也能用）。 */
(function () {
  "use strict";
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const params = new URLSearchParams(location.search);
  let scene = params.get("scene") || "ready";
  let size = params.get("size") === "min" ? "min" : "default";
  const SIZES = { default: [1120, 768], min: [980, 668] };
  const frame = $("#app");
  let last = null;

  function send(m) {
    if (frame.contentWindow) frame.contentWindow.postMessage(Object.assign({ src: "zd-play" }, m), "*");
  }
  function syncUrl() {
    const q = new URLSearchParams({ scene });
    if (size === "min") q.set("size", "min");
    history.replaceState(null, "", "?" + q.toString());
  }
  function load() {
    $("#chrome").hidden = true;
    $("#tb-chrome").hidden = true;
    frame.src = "app.html?scene=" + encodeURIComponent(scene);
    syncUrl();
  }
  frame.addEventListener("load", () => send({ cmd: "hello" }));

  /* ---------------- 窗口尺寸与缩放 */
  function applySize() {
    const [w, h] = SIZES[size];
    const win = $("#win");
    win.style.setProperty("--w", w + "px");
    frame.style.setProperty("--w", w + "px");
    frame.style.setProperty("--h", h + "px");
    $$('[data-env="size"] button').forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === size)));
    syncUrl();
    fit();
  }
  function fit() {
    const wrap = $("#stage-wrap");
    const stage = $("#stage");
    const [w, h] = SIZES[size];
    const sw = (wrap.clientWidth - 40) / (w + 60);
    const sh = (wrap.clientHeight - 32) / (h + 32);
    const s = Math.min(1, sw, sh);
    stage.style.transform = `scale(${s.toFixed(4)})`;
  }
  window.addEventListener("resize", fit);

  /* ---------------- 控制台 */
  function renderScenes(list, current) {
    const box = $("#cs-scenes");
    const groups = [];
    list.forEach((s) => {
      let g = groups.find((x) => x.name === s.group);
      if (!g) { g = { name: s.group, items: [] }; groups.push(g); }
      g.items.push(s);
    });
    box.innerHTML = groups.map((g) => `<div class="cs-group"><span>${g.name}</span>${g.items.map((s) =>
      `<button type="button" data-scene="${s.id}" aria-current="${s.id === current}" title="${s.desc}">${s.title}</button>`).join("")}</div>`).join("");
    const cur = list.find((s) => s.id === current);
    $("#cs-desc").textContent = cur ? cur.desc : "";
  }
  $("#cs-scenes").addEventListener("click", (e) => {
    const b = e.target.closest("[data-scene]");
    if (!b) return;
    scene = b.dataset.scene;
    load();
  });

  const pad = (n) => String(n).padStart(2, "0");
  const hms = (ms) => { const d = new Date(ms); return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };

  function renderStatus(st) {
    last = st;
    $("#cs-now").textContent = hms(st.now);
    $("#tb-clock").textContent = hms(st.now).slice(0, 5);
    $("#cs-paused").hidden = !st.paused;
    $("#cs-pause").textContent = st.paused ? "继续" : "暂停";
    let slot = "任务位：空闲";
    if (st.proc && st.proc.kind === "login") slot = "任务位：登录中";
    if (st.proc && st.proc.kind === "grab") {
      slot = "任务位：自动报名" + (st.proc.at ? `，${hms(st.proc.at)} 开放` : "") + (st.proc.attempts ? `，已提交 ${st.proc.attempts} 次` : "");
    }
    $("#cs-slot").textContent = slot;
    $$(".needs-grab").forEach((b) => { b.disabled = !(st.proc && st.proc.kind === "grab" && st.proc.at); });
    $$('[data-key="grabOutcome"] input').forEach((i) => { i.checked = i.value === st.grabOutcome; });
    $$('[data-key="loginMode"] input').forEach((i) => { i.checked = i.value === st.loginMode; });
    const press = (env, v) => $$(`[data-env="${env}"] button`).forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === v)));
    press("net", st.online ? "on" : "off");
    press("skew", String(st.skew));
    press("feishu", st.feishu ? "1" : "0");
    let sess = "fresh";
    if (st.hoursLeft == null) sess = "none";
    else if (!st.tokenOk || st.hoursLeft <= 0) sess = "expired";
    else if (st.hoursLeft < 2) sess = "short";
    press("session", sess);
  }

  document.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b || !b.closest(".console")) return;
    if (b.dataset.clock === "toggle") send({ cmd: "clock", op: last && last.paused ? "resume" : "pause" });
    if (b.dataset.shift) send({ cmd: "clock", op: "shift", ms: +b.dataset.shift });
    if (b.dataset.jump) send({ cmd: "jump", offset: +b.dataset.jump });
    if (b.id === "cs-sleep") send({ cmd: "sleep", ms: 25 * 60e3 });
    const env = b.closest("[data-env]");
    if (env && b.dataset.v != null) {
      const k = env.dataset.env, v = b.dataset.v;
      if (k === "net") send({ cmd: "net", online: v === "on" });
      if (k === "skew") send({ cmd: "skew", value: +v });
      if (k === "session") send({ cmd: "session", value: v });
      if (k === "feishu") send({ cmd: "set", key: "feishu", value: v === "1" });
      if (k === "size") { size = v; applySize(); }
    }
  });
  $$(".cs-opts").forEach((box) => box.addEventListener("change", (e) => {
    if (e.target.matches("input[type=radio]")) send({ cmd: "set", key: box.dataset.key, value: e.target.value });
  }));

  function setConsole(open) {
    $("#console").hidden = !open;
    $("#cs-open").hidden = open;
    fit();
  }
  $("#cs-fold").addEventListener("click", () => setConsole(false));
  $("#cs-open").addEventListener("click", () => setConsole(true));
  document.addEventListener("keydown", (e) => { if (e.key === "`") setConsole($("#console").hidden); });

  /* ---------------- 模拟 Chrome 登录窗 */
  $("#cw-form").addEventListener("submit", (e) => {
    e.preventDefault();
    send({ cmd: "login", action: "submit" });
    $("#chrome").hidden = true;
    $("#tb-chrome").hidden = true;
  });
  $("#cw-close").addEventListener("click", () => {
    send({ cmd: "login", action: "close" });
    $("#chrome").hidden = true;
    $("#tb-chrome").hidden = true;
  });

  let noteTimer = null;
  function note(text) {
    const n = $("#note");
    n.textContent = text;
    n.hidden = false;
    clearTimeout(noteTimer);
    noteTimer = setTimeout(() => { n.hidden = true; }, 2600);
  }

  window.addEventListener("message", (e) => {
    const m = e.data;
    if (!m || m.src !== "zd-proto") return;
    if (m.type === "scenes") renderScenes(m.list, m.current);
    if (m.type === "status") renderStatus(m);
    if (m.type === "chrome") { $("#chrome").hidden = !m.open; $("#tb-chrome").hidden = !m.open; }
    if (m.type === "note") note(m.text);
  });

  applySize();
  load();
  requestAnimationFrame(fit);
})();
