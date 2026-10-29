"use strict";

/* 桌面手机面板：启停受限入口、显示地址与一次性配对码、进程内准备重登账号。
 * 配对码只在这里显示，不进 URL、不落日志；密码输入后立刻清空 DOM。
 * 不共享电脑整页，也不把电脑专属更新/退出/路径暴露给手机。 */

const PhonePanel = {
  state: { running: false, address: "", pair_code: "", supported: true },
  busy: false,

  async refresh() {
    try {
      const info = await api("phone_access_state");
      if (info && info.ok) PhonePanel.state = Object.assign(PhonePanel.state, info);
    } catch (err) {
      /* 面板拿不到状态就显示未知，不动其他视图 */
    }
    PhonePanel.paint();
  },

  paint() {
    const box = $("#phone-panel");
    if (!box) return;
    const st = PhonePanel.state;
    const addr = $("#phone-address");
    const code = $("#phone-code");
    const start = $("#btn-phone-start");
    const stop = $("#btn-phone-stop");
    if (addr) addr.textContent = st.running ? String(st.address || "") : "未开启";
    if (code) code.textContent = st.running ? String(st.pair_code || "") : "—";
    if (start) start.hidden = !!st.running || st.supported === false;
    if (stop) stop.hidden = !st.running;
  },

  async toggleStart() {
    if (PhonePanel.busy) return;
    PhonePanel.busy = true;
    try {
      await api("start_phone_access", "127.0.0.1");
    } finally {
      PhonePanel.busy = false;
      await PhonePanel.refresh();
    }
  },

  async toggleStop() {
    if (PhonePanel.busy) return;
    PhonePanel.busy = true;
    try {
      await api("stop_phone_access");
    } finally {
      PhonePanel.busy = false;
      await PhonePanel.refresh();
    }
  },

  /* 准备手机重登：账号密码只进进程内存，响应回 ready；密码字段立刻清空 */
  async prepare() {
    const userEl = $("#phone-relogin-user");
    const passEl = $("#phone-relogin-pass");
    const noteEl = $("#phone-relogin-note");
    if (!userEl || !passEl) return;
    const username = String(userEl.value || "");
    const password = String(passEl.value || "");
    let result = null;
    try {
      result = await api("prepare_phone_relogin", username, password);
    } finally {
      /* 无论成败都不在 DOM 里留密码副本 */
      passEl.value = "";
    }
    if (noteEl) {
      noteEl.textContent = (result && result.ready)
        ? "已准备：手机上可以开始重登（只保存在本工具内存）"
        : "还没准备好：请填写账号和密码";
    }
    userEl.value = "";
    PhonePanel.paint();
  },

  wire() {
    const start = $("#btn-phone-start");
    const stop = $("#btn-phone-stop");
    const prep = $("#btn-phone-prepare");
    if (start) start.addEventListener("click", () => void PhonePanel.toggleStart());
    if (stop) stop.addEventListener("click", () => void PhonePanel.toggleStop());
    if (prep) prep.addEventListener("click", () => void PhonePanel.prepare());
    void PhonePanel.refresh();
  },
};

if (typeof document !== "undefined") {
  if (document.readyState !== "loading") PhonePanel.wire();
  else document.addEventListener("DOMContentLoaded", PhonePanel.wire());
}
