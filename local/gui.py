#!/usr/bin/env python3
"""校园活动自动报名 · 图形界面（本机版）

只是个壳：所有实际操作都变成调用 grab.py 子命令（check / list --json / login / grab），
这样界面和命令行共用同一套逻辑，出问题可以对照命令行输出排查，不会两套代码各自跑偏。

界面与业务进程分在两个线程：UI 线程只负责显示，抢报名在子进程里跑，
日志通过队列回传，避免 playwright 卡住窗口。

交互上按"打开就能用"来设计：
- 启动自动检查登录并拉一次列表（没有登录态时只提示，不报错弹窗）；
- 「刷新」一次完成检查 + 拉列表，不用先点检查再点刷新；
- 列表带实时倒计时、关键词筛选、双击即抢、F5 刷新、Enter 抢选中的活动。

外观用 tkinter 手绘一套扁平卡片风：不使用第三方 UI 库，保证舍友那边装上 Python 就能跑；
ttk 只负责 Treeview 与滚动条，其余容器用 tk.Frame 才好上底色和描边。
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

HERE = Path(__file__).resolve().parent
GRAB = HERE / "grab.py"
SESSION = HERE / "data" / "session.json"
PY = sys.executable or "python"

# Windows 下不弹黑框（子进程仍会正常拉起浏览器）
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# token 实测寿命约 17.5 小时；只用来把"还剩多久"显示个大概，判有效仍靠接口。
TOKEN_LIFETIME_H = 17.5

PAREN = re.compile(r"（[^（）]*）$")

# 配色（浅色扁平风）：一屏里只有强调色和状态色两种饱和度，其余都是灰阶。
BG = "#F3F5F9"
CARD = "#FFFFFF"
BORDER = "#E3E8EF"
TEXT = "#1F2933"
MUTED = "#697586"
ACCENT = "#2F6FED"
ACCENT_DARK = "#1D4FD8"
ACCENT_SOFT = "#E8EFFD"
OK = "#12915B"
WARN = "#D92D20"
GREY = "#98A2B3"

FONT = "Microsoft YaHei UI"


def child_env():
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def run_grab(args: list[str], timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, str(GRAB)] + args, cwd=str(HERE), capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=child_env(),
        creationflags=CREATE_NO_WINDOW, timeout=timeout)


def card(parent, **kw) -> tk.Frame:
    """白底描边卡片：tkinter 没有圆角，用 1px 描边 + 留白做出层次。"""
    return tk.Frame(parent, bg=CARD, highlightbackground=BORDER,
                    highlightthickness=1, bd=0, **kw)


def flat_button(parent, text, command, kind="primary", width=None):
    """扁平按钮。tk.Button 在 Windows 上带边框，全部关掉再自己上色。"""
    palette = {
        "primary": (ACCENT, ACCENT_DARK, "#FFFFFF", "#FFFFFF"),
        "ghost": ("#F2F5F9", "#E4E9F2", TEXT, TEXT),
        "danger": ("#FEF3F2", "#FEE4E2", WARN, WARN),
    }
    bg, hover, fg, active_fg = palette[kind]
    btn = tk.Button(parent, text=text, command=command, bg=bg, fg=fg,
                    activebackground=hover, activeforeground=active_fg,
                    relief="flat", bd=0, highlightthickness=0, cursor="hand2",
                    font=(FONT, 10, "bold" if kind == "primary" else "normal"),
                    padx=14, pady=7)
    if width:
        btn.configure(width=width)
    btn.bind("<Enter>", lambda _e: btn.configure(bg=hover))
    btn.bind("<Leave>", lambda _e: btn.configure(bg=bg))
    return btn


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.proc: subprocess.Popen | None = None
        self.all_rows: list[dict] = []      # 接口返回的全量
        self.rows: list[dict] = []          # 过滤后正在表格里显示的
        self.target: dict | None = None
        self.deadline: float | None = None
        self._left_cache: dict[str, str] = {}
        self._busy_label = ""
        self._token_ok: bool | None = None

        root.title("校园活动自动报名 · 本机版")
        root.configure(bg=BG)
        root.geometry("1040x720")
        root.minsize(900, 620)
        self._center()
        self._build_style()
        self._build_ui()
        self.root.after(100, self._pump)
        # 打开即自检：省掉"先点检查、再点刷新"两步
        self.root.after(400, self.refresh, True)

    def _center(self):
        self.root.update_idletasks()
        w, h = 1040, 720
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry("%dx%d+%d+%d" % (w, h, (sw - w) // 2, max((sh - h) // 3, 0)))

    # ---------------------------------------------------------------- 外观
    def _build_style(self):
        style = ttk.Style()
        style.theme_use("clam")            # clam 才允许改配色，vista 主题会忽略

        style.configure("Treeview", background=CARD, fieldbackground=CARD,
                        foreground=TEXT, rowheight=30, borderwidth=0,
                        font=(FONT, 10))
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.configure("Treeview.Heading", background="#F7F9FC", foreground="#475467",
                        relief="flat", borderwidth=0, font=(FONT, 9, "bold"))
        style.map("Treeview.Heading", background=[("active", "#EEF2F7")])
        style.map("Treeview",
                  background=[("selected", ACCENT_SOFT)],
                  foreground=[("selected", "#11305F")])
        style.configure("Vertical.TScrollbar", background="#DDE3EC", troughcolor=CARD,
                        borderwidth=0, arrowsize=12, arrowcolor="#8A94A6")

    # ---------------------------------------------------------------- 布局
    def _build_ui(self):
        outer = tk.Frame(self.root, bg=BG)
        outer.pack(fill="both", expand=True)

        # ---- 顶部：标题 + 状态 + 操作
        head = card(outer)
        head.pack(fill="x", padx=14, pady=(14, 10))
        accent = tk.Frame(head, bg=ACCENT, width=4)
        accent.pack(side="left", fill="y")
        titles = tk.Frame(head, bg=CARD)
        titles.pack(side="left", padx=(12, 0), pady=12)
        tk.Label(titles, text="校园活动自动报名", bg=CARD, fg=TEXT,
                 font=(FONT, 14, "bold")).pack(anchor="w")
        tk.Label(titles, text="本机版 · 只登录你自己的账号 · 电脑要保持唤醒、别断网",
                 bg=CARD, fg=MUTED, font=(FONT, 9)).pack(anchor="w", pady=(2, 0))

        right = tk.Frame(head, bg=CARD)
        right.pack(side="right", padx=12)
        self.pill = tk.Label(right, text="检查中…", bg=GREY, fg="#FFFFFF",
                             font=(FONT, 9, "bold"), padx=10, pady=3)
        self.pill.pack(side="right", padx=(8, 0))
        self.btn_refresh = flat_button(right, "刷新  F5", self.refresh, "ghost")
        self.btn_refresh.pack(side="right", padx=6)
        self.btn_login = flat_button(right, "登录 / 重新登录", self.login, "primary")
        self.btn_login.pack(side="right")

        # ---- 筛选条
        filt = tk.Frame(outer, bg=BG)
        filt.pack(fill="x", padx=14)
        tk.Label(filt, text="筛选", bg=BG, fg=MUTED, font=(FONT, 9)).pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_a: self._apply_filter())
        wrap = tk.Frame(filt, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        wrap.pack(side="left", padx=6)
        self.entry = tk.Entry(wrap, textvariable=self.filter_var, relief="flat", bd=0,
                              bg=CARD, fg=TEXT, insertbackground=TEXT,
                              font=(FONT, 10), width=26)
        self.entry.pack(padx=8, pady=4)
        self.only_free = tk.BooleanVar(value=True)
        tk.Checkbutton(filt, text="只看还能报的", variable=self.only_free,
                       command=self._apply_filter, bg=BG, fg=TEXT, activebackground=BG,
                       selectcolor=CARD, font=(FONT, 9),
                       highlightthickness=0, bd=0).pack(side="left", padx=10)
        self.count_var = tk.StringVar(value="")
        tk.Label(filt, textvariable=self.count_var, bg=BG, fg=MUTED,
                 font=(FONT, 9)).pack(side="right")

        # ---- 活动表
        body = card(outer)
        body.pack(fill="both", expand=True, padx=14, pady=10)
        cols = ("index", "left", "sign_start", "state", "name", "place")
        # height 只影响"请求高度"，不限制实际行数；给 7 行是为了让日志区在 720 高的窗口里
        # 也完整显示，多出来的活动用滚动条看。
        self.tree = ttk.Treeview(body, columns=cols, show="headings", selectmode="browse",
                                 height=7)
        headings = {
            "index": ("序号", 52), "left": ("距开放", 112), "sign_start": ("报名开始", 150),
            "state": ("状态", 90), "name": ("活动名", 400), "place": ("地点", 140),
        }
        for key, (text, width) in headings.items():
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor="w",
                             stretch=(key in ("name", "place")))
        self.tree.tag_configure("signed", foreground="#98A2B3")
        self.tree.tag_configure("open", foreground="#1D4ED8")
        self.tree.tag_configure("waiting", foreground="#0F766E")
        self.tree.tag_configure("odd", background="#FAFBFD")
        self.tree.tag_configure("even", background="#FFFFFF")
        self.tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        sb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview,
                           style="Vertical.TScrollbar")
        sb.pack(side="left", fill="y", pady=8, padx=(0, 6))
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<Double-1>", lambda _e: self.start_grab())
        self.tree.bind("<Return>", lambda _e: self.start_grab())
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._update_selected())

        # ---- 操作区
        bar = card(outer)
        bar.pack(fill="x", padx=14, pady=(0, 10))
        self.selected_var = tk.StringVar(value="未选中活动 —— 在上面的列表点一行")
        tk.Label(bar, textvariable=self.selected_var, bg=CARD, fg=MUTED,
                 font=(FONT, 9), anchor="w").pack(fill="x", padx=14, pady=(10, 2))

        line = tk.Frame(bar, bg=CARD)
        line.pack(fill="x", padx=14, pady=(4, 12))
        self.btn_grab = flat_button(line, "到点抢选中的活动", self.start_grab, "primary")
        self.btn_grab.configure(font=(FONT, 11, "bold"), padx=18, pady=9)
        self.btn_grab.pack(side="left")
        self.btn_stop = flat_button(line, "停止", self.stop, "ghost")
        self.btn_stop.configure(state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        self.headed_var = tk.BooleanVar(value=False)
        tk.Checkbutton(line, text="显示浏览器窗口（看着它点）", variable=self.headed_var,
                       bg=CARD, fg=TEXT, activebackground=CARD, selectcolor=CARD,
                       font=(FONT, 9), highlightthickness=0,
                       bd=0).pack(side="left", padx=10)
        self.countdown_var = tk.StringVar(value="")
        tk.Label(line, textvariable=self.countdown_var, bg=CARD, fg=WARN,
                 font=("Consolas", 11, "bold")).pack(side="left", padx=10)
        flat_button(line, "打开数据目录", self.open_data, "ghost").pack(side="right")

        # ---- 日志
        logcard = card(outer)
        logcard.pack(fill="both", padx=14, pady=(0, 14))
        loghead = tk.Frame(logcard, bg=CARD)
        loghead.pack(fill="x", padx=12, pady=(8, 4))
        tk.Label(loghead, text="运行日志", bg=CARD, fg=MUTED,
                 font=(FONT, 9, "bold")).pack(side="left")
        flat_button(loghead, "清空", self.clear_log, "ghost").pack(side="right")
        self.logbox = ScrolledText(logcard, height=7, wrap="none",
                                   font=("Consolas", 10), state="disabled",
                                   relief="flat", bd=0, background="#FBFCFE",
                                   foreground="#334155", insertbackground=TEXT)
        self.logbox.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        # ---- 快捷键（Enter 只绑在表格上：在筛选框里回车不该触发抢报名）
        self.root.bind("<F5>", lambda _e: self.refresh())

    # ---------------------------------------------------------------- 工具
    def log(self, text: str):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.logbox.configure(state="normal")
        self.logbox.insert("end", "%s  %s\n" % (stamp, text))
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def clear_log(self):
        self.logbox.configure(state="normal")
        self.logbox.delete("1.0", "end")
        self.logbox.configure(state="disabled")

    def _pill(self, text: str, color: str):
        self.pill.configure(text=text, bg=color)

    def _set_busy(self, busy: bool, label: str = ""):
        state = "disabled" if busy else "normal"
        for btn in (self.btn_refresh, self.btn_login, self.btn_grab):
            btn.configure(state=state)
        self.btn_stop.configure(state="normal" if busy else "disabled")
        self._busy_label = label
        if busy:
            self._pill("进行中：%s" % label, ACCENT)
        else:
            self._refresh_pill()

    def _refresh_pill(self):
        if self._token_ok is None:
            self._pill("未检查", GREY)
        elif not self._token_ok:
            self._pill("登录态失效", WARN)
        else:
            left = self._token_hours_left()
            if left is None:
                self._pill("登录态有效", OK)
            elif left <= 0:
                self._pill("有效 · 可能快过期", ACCENT)
            else:
                self._pill("有效 · 约剩 %.0f 小时" % left, OK)

    def _token_hours_left(self) -> float | None:
        try:
            info = json.loads(SESSION.read_text(encoding="utf-8"))
            saved = datetime.strptime(info["saved_at"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
        return TOKEN_LIFETIME_H - (datetime.now() - saved).total_seconds() / 3600.0

    def _run(self, args: list[str], label: str, done=None):
        """起一个子进程跑 grab.py，输出逐行丢回 UI 队列。"""
        if self.proc is not None:
            return
        cmd = [PY, str(GRAB)] + args
        self.log("▶ " + label)
        self._set_busy(True, label)
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=str(HERE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=child_env(), creationflags=CREATE_NO_WINDOW)
        except Exception as exc:
            self.proc = None
            self._set_busy(False)
            self.log("✗ 启动失败：%s: %s" % (type(exc).__name__, exc))
            return

        def worker():
            assert self.proc is not None
            try:
                for line in self.proc.stdout:
                    self.q.put(("log", line.rstrip("\n")))
            except Exception as exc:
                self.q.put(("log", "（读取输出中断：%s）" % type(exc).__name__))
            try:
                rc = self.proc.wait(timeout=30)
            except Exception:
                rc = -1
            self.q.put(("done", (rc, done)))

        threading.Thread(target=worker, daemon=True).start()

    def _pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "rows":
                    self._set_rows(payload)
                elif kind == "status":
                    self._token_ok = payload
                    self._refresh_pill()
                elif kind == "done":
                    rc, done = payload
                    self.proc = None
                    self._set_busy(False)
                    self.log("── 结束，退出码 %s ──" % rc)
                    if done:
                        done(rc)
        except queue.Empty:
            pass
        self._tick()
        self.root.after(100, self._pump)

    def _tick(self):
        self._refresh_countdowns()
        if self.deadline is None:
            return
        left = self.deadline - time.time()
        if left > 0:
            h, rem = divmod(int(left), 3600)
            m, s = divmod(rem, 60)
            txt = "距报名开放 %02d:%02d:%02d" % (h, m, s) if h else "距报名开放 %02d:%02d" % (m, s)
            self.countdown_var.set(txt)
        else:
            self.countdown_var.set("报名已开放，正在抢…")

    # ---------------------------------------------------------------- 列表
    def _refresh_countdowns(self):
        """每秒刷新「距开放」列：只改文本变化的行，避免整表重绘。"""
        now = time.time()
        for row in self.rows:
            iid = row["id"]
            if not self.tree.exists(iid):
                continue
            text = self._left_text(row, now)
            if self._left_cache.get(iid) == text:
                continue
            self._left_cache[iid] = text
            self.tree.set(iid, "left", text)

    @staticmethod
    def _left_text(row: dict, now: float) -> str:
        if row.get("signed"):
            return "已报名"
        start = row.get("sign_start")
        if not start:
            return "-"
        try:
            ts = datetime.strptime(start, "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:
            return "-"
        left = ts - now
        if left <= 0:
            return "已开放"
        d, rem = divmod(int(left), 86400)
        h, rem = divmod(rem, 3600)
        m, s = divmod(rem, 60)
        if d:
            return "%d 天 %02d:%02d" % (d, h, m)
        return "%02d:%02d:%02d" % (h, m, s)

    def refresh(self, auto: bool = False):
        """检查登录 + 拉列表，一次搞定。auto=True 是启动时的自检，失败不弹窗。"""
        if self.proc is not None:
            if not auto:
                messagebox.showinfo("稍等", "还有任务在跑，等它结束再刷新。")
            return
        self._set_busy(True, "检查登录并拉取活动")

        def worker():
            try:
                chk = run_grab(["check"])
                valid = chk.returncode == 0
                self.q.put(("status", valid))
                if not valid:
                    for line in (chk.stdout or "").splitlines()[:3]:
                        if line.strip():
                            self.q.put(("log", line.strip()))
                    self.q.put(("log", "还没登录：点右上角「登录 / 重新登录」完成一次网页登录。"))
                    self.q.put(("done", (chk.returncode, None)))
                    return

                res = run_grab(["list", "--json"])
                out = (res.stdout or "").strip()
                err = (res.stderr or "").strip()
                if res.returncode != 0:
                    self.q.put(("log", err or out or "拉取失败，退出码 %s" % res.returncode))
                    self.q.put(("done", (res.returncode, None)))
                    return
                try:
                    rows = json.loads(out.splitlines()[-1])
                except Exception:
                    self.q.put(("log", "输出不是预期的 JSON：%s" % out[:200]))
                    self.q.put(("done", (1, None)))
                    return
                self.q.put(("rows", rows))
                self.q.put(("done", (0, None)))
            except subprocess.TimeoutExpired:
                self.q.put(("log", "拉取超时（90 秒），检查网络或稍后重试。"))
                self.q.put(("done", (1, None)))
            except Exception as exc:
                self.q.put(("log", "拉取异常：%s: %s" % (type(exc).__name__, exc)))
                self.q.put(("done", (1, None)))

        threading.Thread(target=worker, daemon=True).start()

    def _set_rows(self, rows: list[dict]):
        self.all_rows = rows
        self._token_ok = True
        self._refresh_pill()
        self._apply_filter(announce=True)
        # 自动选中最近一个还能报的活动，省掉"再点一行"
        for row in self.rows:
            if not row.get("signed"):
                self.tree.selection_set(row["id"])
                self.tree.focus(row["id"])
                self.tree.see(row["id"])
                break

    def _apply_filter(self, announce: bool = False):
        kw = self.filter_var.get().strip().lower()
        only_free = self.only_free.get()
        rows = []
        for row in self.all_rows:
            if only_free and row.get("signed"):
                continue
            if kw and kw not in (row.get("name") or "").lower():
                continue
            rows.append(row)
        self.rows = rows
        self._left_cache.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)
        if not self.all_rows:
            if announce:
                self.log("（没有可报名的活动：要么还没公布报名时间，要么都已截止）")
            self.count_var.set("")
            self._update_selected()
            return
        for i, row in enumerate(rows):
            tag = "signed" if row.get("signed") else (
                "open" if str(row.get("state", "")).startswith("开放") else "waiting")
            stripe = "odd" if i % 2 else "even"
            state = PAREN.sub("", row.get("state", "") or "")
            self.tree.insert("", "end", iid=row["id"], values=(
                row["index"], "", row["sign_start"] or "-", state,
                row["name"], row.get("place", "")), tags=(tag, stripe))
        self._refresh_countdowns()
        if announce:
            self.log("已加载 %d 个活动（显示 %d 个）。选中后点「到点抢选中的活动」，或双击那一行。"
                     % (len(self.all_rows), len(rows)))
        self.count_var.set("共 %d 个，显示 %d 个" % (len(self.all_rows), len(rows)))
        self._update_selected()

    def _update_selected(self):
        row = self._selected_row()
        if not row:
            self.selected_var.set("未选中活动 —— 在上面的列表点一行")
            return
        bits = [row["name"]]
        if row.get("sign_start"):
            bits.append("报名 " + row["sign_start"])
        if row.get("place"):
            bits.append(row["place"])
        if row.get("signed"):
            bits.append("已报名")
        self.selected_var.set("已选：" + " · ".join(bits))

    def _selected_row(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return next((r for r in self.rows if r["id"] == sel[0]), None)

    # ---------------------------------------------------------------- 动作
    def login(self):
        if not messagebox.askokcancel(
                "登录", "接下来会弹出一个 Chrome 窗口，请在里面手动输入学号、密码、验证码并登录。\n\n"
                        "登录成功后窗口会自动关闭。\n\n继续吗？"):
            return

        def done(rc):
            self._token_ok = (rc == 0)
            self._refresh_pill()
            if rc == 0:
                self.log("登录成功，正在刷新活动列表…")
                self.refresh()
        self._run(["login"], "打开浏览器等待手动登录（最长 10 分钟）", done)

    def start_grab(self):
        if self.proc is not None:
            return
        row = self._selected_row()
        if not row:
            messagebox.showinfo("先选活动", "在上面的列表里点一行，再点这个按钮。")
            return
        if row.get("signed"):
            messagebox.showinfo("已经报过", "「%s」你已经报过名了，不需要再抢。" % row["name"])
            return
        if not row.get("sign_start"):
            messagebox.showinfo("没有报名时间", "这个活动没有公布报名开始时间，没法到点抢。")
            return

        try:
            start_ts = datetime.strptime(row["sign_start"], "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:
            start_ts = None
        already = start_ts is not None and start_ts < time.time()

        tip = "活动：%s\n报名开始：%s\n" % (row["name"], row["sign_start"])
        if already:
            tip += "\n注意：报名已经开始了，现在点会立刻尝试（可能已满员）。\n"
        tip += "\n确认开始？期间电脑要保持唤醒、别断网。"
        if not messagebox.askyesno("到点抢报名", tip):
            return

        args = ["grab", "--activity-id", row["id"], "--at", row["sign_start"],
                "--name", row["name"]]
        if self.headed_var.get():
            args.append("--headed")

        self.target = row
        self.deadline = start_ts
        self.log("目标已锁定：%s（按 id 定位，不依赖序号）" % row["name"])

        def done(rc):
            self.deadline = None
            self.countdown_var.set("")
            if rc == 0:
                messagebox.showinfo("抢到了", "「%s」报名成功，结果见日志。" % row["name"])
            self.refresh()
        self._run(args, "等待并抢报名：" + row["name"], done)

    def stop(self):
        if self.proc is None:
            return
        if not messagebox.askyesno("停止", "确定停止当前任务？已打开的浏览器会被关掉。"):
            return
        self.log("▶ 正在停止…")
        try:
            self.proc.terminate()
        except Exception:
            pass
        self.deadline = None
        self.countdown_var.set("")

    def open_data(self):
        target = HERE / "data"
        target.mkdir(exist_ok=True)
        try:
            os.startfile(str(target))          # noqa: S606 - Windows 专用
        except Exception as exc:
            self.log("打不开目录：%s" % exc)


def main():
    # 高分屏下不设 DPI 感知，整个界面会被系统拉伸成模糊的
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
