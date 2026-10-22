/* 界面逻辑：只做「展示 + 筛选 + 调后端」。
   后端通过 window.pywebview.api.* 暴露方法，通过 window.onBackend.* 推事件。
   业务判断（能不能抢、怎么抢）全在 grab.py，这里不做。 */

const $ = (sel) => document.querySelector(sel);
document.title = "天软校园活动 · 报名值守台";

const S = {
  items: [],
  filter: { q: "", status: "all", time: "all", sort: "open" },
  selected: null,          // {id, name, at}
  busy: false,
  prefs: {},
  logOpen: false,
};

/* ---------------- 启动 ---------------- */

async function boot() {
  await ready();
  const info = await api("bootstrap");
  S.prefs = info.prefs || {};
  // 恢复上次的筛选
  const f = S.prefs.filter || {};
  S.filter.q = $("#q").value = f.q || "";
  S.filter.status = f.status || "all";
  S.filter.time = f.time || "all";
  S.filter.sort = f.sort || "open";
  $("#sort").value = S.filter.sort;
  $("#time").value = S.filter.time;
  chipState();
  fillHelp(info.help);
  setLogin(info.login ? info.login.hours_left : null, null);
  if (info.prefs && info.prefs.welcome_seen !== true) showHelp();
  api("front_ready").catch(() => {});
  addLog("界面已就绪，正在拉取活动列表…");
  refresh(true);
}

function ready() {
  // pywebview 的注入时机可能早于本脚本挂监听，事件会漏；轮询更稳。
  return new Promise((resolve) => {
    const t0 = Date.now();
    const timer = setInterval(() => {
      // api 对象可能先出现、方法后挂上，认具体方法更可靠
      if (window.pywebview && window.pywebview.api &&
          typeof window.pywebview.api.bootstrap === "function") {
        clearInterval(timer);
        resolve();
      } else if (Date.now() - t0 > 15000) {
        clearInterval(timer);
        addLog("[X] 后端没有就绪：请用 python app.py 启动，不要直接开 index.html");
        resolve();
      }
    }, 50);
  });
}

function api(name, ...args) {
  const fn = window.pywebview && window.pywebview.api
    ? window.pywebview.api[name] : null;
  if (typeof fn !== "function") {
    return Promise.reject(new Error("后端方法不可用：" + name));
  }
  return fn(...args);
}

/* ---------------- 事件（后端推送） ---------------- */

window.onBackend = {
  log(line) { addLog(line); },
  state(payload) { if (payload) setLogin(payload.hours_left, payload.ok); },
  activities(payload) { applyActivities(payload, S.refreshQuiet); },
  check(payload) {
    setLogin(payload.hours_left, payload.ok);
    const line = (payload.text || "").trim().split("\n").pop();
    addLog(payload.ok ? "[OK] 登录态有效" : "[X] " + (line || "登录态失效"));
  },
  done(payload) {
    setBusy(false);
    const ok = payload && payload.ok;
    if (payload && payload.kind === "login") {
      setResult(ok ? "登录成功，正在刷新活动列表…"
                   : "登录没有完成：" + (payload.reason || "退出码 " + payload.code), ok);
      refresh(true);
      return;
    }
    const reason = String(payload.reason || "未知原因")
      .replace(/^\[X\]\s*/, "").replace(/^没抢到[：:]\s*/, "");
    setResult(ok ? "报名成功：" + stripLabel(payload.label)
                 : "没抢到：" + reason, ok);
    // 结束后重新拉一次列表：已报名的行要变灰
    refresh(true);
  },
};

function stripLabel(label) {
  return String(label || "").replace(/^等待并抢报名[：:]/, "");
}

/* ---------------- 拉数据 ---------------- */

function refresh(quiet) {
  S.refreshQuiet = !!quiet;
  api("list_activities").catch((err) => {
    addLog("[X] 拉取活动失败：" + (err && err.message ? err.message : err));
  });
}

/* 结果由后端事件推回来：js_api 里不能干重活，否则窗口会卡死 */
function applyActivities(res, quiet) {
  if (!res) return;
  if (!res.ok) {
    setLogin(null, false);
    addLog(res.text || "[X] 拉取活动失败");
    if (!quiet) setResult(res.text || "拉取活动失败", false);
    render();
    return;
  }
  S.items = res.items || [];
  setLogin(null, true);
  addLog("已加载 " + S.items.length + " 个活动");
  render();
}

/* ---------------- 渲染 ---------------- */

function filtered() {
  const now = Date.now() / 1000;
  const kw = S.filter.q.trim().toLowerCase();
  let rows = S.items.filter((it) => {
    if (kw && !String(it.name).toLowerCase().includes(kw)) return false;
    if (S.filter.status === "free" && it.signed) return false;
    if (S.filter.status === "signed" && !it.signed) return false;
    if (S.filter.time !== "all") {
      const t = toTs(it.sign_start);
      if (!t) return false;
      const d = new Date(t * 1000);
      const nowD = new Date(now * 1000);
      if (S.filter.time === "today") {
        if (d.toDateString() !== nowD.toDateString()) return false;
      } else if (S.filter.time === "week") {
        if (d - nowD > 7 * 86400 * 1000 || d.toDateString() === nowD.toDateString()) return false;
      }
    }
    return true;
  });

  if (S.filter.sort === "name") {
    rows.sort((a, b) => String(a.name).localeCompare(String(b.name), "zh"));
  } else {
    rows.sort((a, b) => (a.sign_start || "9999") + a.name >
                        (b.sign_start || "9999") + b.name ? 1 : -1);
  }
  return rows;
}

function render() {
  const rows = filtered();
  const tbody = $("#rows");
  tbody.innerHTML = "";
  $("#count").textContent = S.items.length
    ? "共 " + S.items.length + " 个，显示 " + rows.length + " 个"
    : "—";

  $("#empty").hidden = rows.length > 0;

  rows.forEach((it, i) => {
    const tr = document.createElement("tr");
    tr.dataset.id = it.id;
    if (it.signed) tr.classList.add("is-signed");
    if (S.selected && S.selected.id === it.id) tr.classList.add("is-selected");
    tr.innerHTML =
      `<td class="c-idx">${i + 1}</td>` +
      `<td class="c-left">${leftText(it)}</td>` +
      `<td class="c-time">${it.sign_start || "-"}</td>` +
      `<td class="c-state">${stateTag(it)}</td>` +
      `<td class="c-name" title="${esc(it.name)}">${esc(it.name)}</td>` +
      `<td class="c-place" title="${esc(it.place || "")}">${esc(it.place || "-")}</td>`;
    tr.addEventListener("click", () => select(it));
    tr.addEventListener("dblclick", () => { select(it); startGrab(); });
    tbody.appendChild(tr);
  });

  // 选中项如果被筛掉了，清掉选择
  if (S.selected && !rows.some((r) => r.id === S.selected.id)) {
    S.selected = null;
  }
  paintSelected();
}

function paintSelected() {
  const box = $("#selected");
  const on = !!S.selected;
  $("#btn-grab").disabled = !on || S.busy;
  if (!on) {
    box.textContent = "未选活动 · 在上面的列表里点一行";
    box.classList.remove("is-set");
    $("#countdown").textContent = "";
    return;
  }
  box.textContent = "已选：" + S.selected.name +
    (S.selected.at ? " · 报名 " + S.selected.at : "");
  box.classList.add("is-set");
}

function select(it) {
  S.selected = { id: it.id, name: it.name, at: it.sign_start || "" };
  document.querySelectorAll("tbody tr").forEach((tr) => {
    tr.classList.toggle("is-selected", tr.dataset.id === it.id);
  });
  paintSelected();
  tick();
}

function stateTag(it) {
  if (it.signed) return `<span class="state-tag state-signed">已报名</span>`;
  const s = it.seconds_to_open;
  if (s === null) return `<span class="state-tag">待公布</span>`;
  if (s <= 0) return `<span class="state-tag state-open">开放中</span>`;
  return `<span class="state-tag state-wait">未开放</span>`;
}

function leftText(it) {
  if (it.signed) return '<span class="state-signed">已报名</span>';
  const s = it.seconds_to_open;
  if (s === null) return "-";
  if (s <= 0) return "已开放";
  return fmt(s);
}

/* 每秒只改「距开放」这一格，不重排整张表 */
function tick() {
  document.querySelectorAll("tbody tr").forEach((tr) => {
    const it = S.items.find((x) => x.id === tr.dataset.id);
    if (!it || it.signed) return;
    const t = toTs(it.sign_start);
    if (!t) return;
    const left = Math.floor(t - Date.now() / 1000);
    it.seconds_to_open = left;
    const cell = tr.querySelector(".c-left");
    if (cell) cell.innerHTML = left <= 0
      ? "已开放" : `<span class="state-wait">${fmt(left)}</span>`;
  });
  if (S.selected) {
    const t = toTs(S.selected.at);
    $("#countdown").textContent = t
      ? (t - Date.now() / 1000 > 0
          ? "距离报名开放 " + fmt(t - Date.now() / 1000)
          : "报名已开放，正在提交…")
      : "";
  }
}

/* ---------------- 动作 ---------------- */

async function startGrab() {
  if (!S.selected || S.busy) return;
  const headed = $("#headed").checked;
  setBusy(true);
  setResult("", null);
  const res = await api("grab", S.selected.id, S.selected.name,
                        S.selected.at, headed);
  if (!res.ok) {
    setBusy(false);
    setResult(res.text || "启动失败", false);
  }
}

async function doStop() {
  const res = await api("stop");
  if (!res.ok && res.text) addLog(res.text);
}

async function doLogin() {
  setBusy(true);
  addLog("登录窗口即将打开，请在 Chrome 里完成登录…");
  const res = await api("login");
  if (!res.ok) {
    setBusy(false);
    setResult(res.text || "启动登录失败", false);
  }
}

function doCheck() {
  api("check").catch((err) => {
    addLog("[X] 检查登录态失败：" + (err && err.message ? err.message : err));
  });
}

/* ---------------- 状态 ---------------- */

function setLogin(hoursLeft, ok) {
  const pill = $("#pill");
  const valid = ok === true || (ok === null && hoursLeft !== null);
  if (ok === false) {
    pill.textContent = "登录态失效";
    pill.dataset.state = "bad";
    pill.className = "pill is-bad";
    return;
  }
  if (hoursLeft === null || hoursLeft === undefined) {
    pill.textContent = "未登录";
    pill.className = "pill";
    return;
  }
  pill.textContent = "有效 · 约剩 " + Math.max(0, Math.round(hoursLeft)) + " 小时";
  pill.className = "pill is-ok";
}

function setBusy(busy) {
  S.busy = busy;
  ["#btn-login", "#btn-refresh"].forEach((s) => { $(s).disabled = busy; });
  $("#btn-grab").disabled = busy || !S.selected;
  $("#btn-stop").disabled = !busy;
  $("#pill").classList.toggle("is-busy", busy);
}

function setResult(text, ok) {
  const el = $("#result");
  if (!text) { el.hidden = true; el.textContent = ""; return; }
  el.hidden = false;
  el.textContent = (ok ? "✓ " : "✗ ") + text;
  el.className = "result " + (ok ? "is-ok" : "is-bad");
}

function addLog(line) {
  const box = $("#log");
  const stamp = new Date().toTimeString().slice(0, 8);
  box.textContent += stamp + "  " + line + "\n";
  if (box.textContent.length > 40000) {
    box.textContent = box.textContent.slice(-30000);
  }
  box.scrollTop = box.scrollHeight;
}

/* ---------------- 说明弹窗 ---------------- */

function fillHelp(help) {
  if (!help) return;
  $("#help-title").textContent = "欢迎使用 · " + help.title;
  $("#help-sub").textContent = help.subtitle;
  $("#help-note").textContent = help.note;
  $("#help-steps").innerHTML = (help.steps || []).map((s) =>
    `<li><span class="no">${s.no}</span><div>` +
    `<div class="t">${esc(s.title)}</div>` +
    `<div class="d">${esc(s.text)}</div></div></li>`).join("");
}

function showHelp() {
  $("#help").showModal();
  savePrefs({ welcome_seen: true });
}

function savePrefs(patch) {
  Object.assign(S.prefs, patch);
  if (window.pywebview && window.pywebview.api) api("save_prefs", patch);
}

/* ---------------- 工具 ---------------- */

function toTs(ts) {
  if (!ts) return null;
  const m = String(ts).match(/^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})$/);
  if (!m) return null;
  return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]).getTime() / 1000;
}

function fmt(sec) {
  sec = Math.max(0, Math.floor(sec));
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const pad = (n) => String(n).padStart(2, "0");
  if (d > 0) return `${d} 天 ${pad(h)}:${pad(m)}`;
  if (h > 0) return `${pad(h)}:${pad(m)}:${pad(s)}`;
  return `${pad(m)}:${pad(s)}`;
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function chipState() {
  document.querySelectorAll(".chip").forEach((c) => {
    c.classList.toggle("is-on", c.dataset.status === S.filter.status);
  });
}

/* ---------------- 绑定 ---------------- */

$("#q").addEventListener("input", (e) => {
  S.filter.q = e.target.value;
  savePrefs({ filter: S.filter });
  render();
});

$("#chips").addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (!chip) return;
  S.filter.status = chip.dataset.status;
  chipState();
  savePrefs({ filter: S.filter });
  render();
});

$("#time").addEventListener("change", (e) => {
  S.filter.time = e.target.value;
  savePrefs({ filter: S.filter });
  render();
});

$("#sort").addEventListener("change", (e) => {
  S.filter.sort = e.target.value;
  savePrefs({ filter: S.filter });
  render();
});

$("#btn-refresh").addEventListener("click", () => refresh(false));
$("#btn-login").addEventListener("click", doLogin);
$("#btn-help").addEventListener("click", showHelp);
$("#btn-open").addEventListener("click", () => api("open_data"));
$("#btn-grab").addEventListener("click", startGrab);
$("#btn-stop").addEventListener("click", doStop);
$("#btn-log-toggle").addEventListener("click", (e) => {
  S.logOpen = !S.logOpen;
  document.querySelector(".log-card").classList.toggle("is-open", S.logOpen);
  e.target.textContent = S.logOpen ? "收起" : "展开";
  savePrefs({ log_open: S.logOpen });
});
$("#btn-log-clear").addEventListener("click", () => {
  $("#log").textContent = "";
});

document.addEventListener("keydown", (e) => {
  if (e.key === "F5") { e.preventDefault(); refresh(false); }
  if (e.key === "Enter" && document.activeElement.tagName === "BODY") startGrab();
});

setInterval(tick, 1000);
boot().catch((err) => {
  addLog("[X] 界面初始化失败：" + (err && err.message ? err.message : err));
  console.error(err);
});
