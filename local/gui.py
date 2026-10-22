#!/usr/bin/env python3
"""天软校园活动抢报名 · 本机图形界面

这一层只是壳：所有实际操作都变成调用 grab.py 子命令（check / list --json /
login / grab），界面与命令行共用同一套逻辑，出问题可以对照命令行输出排查。

界面与业务进程分在两个线程：UI 线程只管显示，抢报名在子进程里跑，输出通过队列
回传，避免 playwright 卡住窗口。

视觉上走“报名值守台”：晨雾蓝灰做背景，纸白承载信息，珊瑚色只标记需要行动的时刻。
tkinter 自己画不出抗锯齿圆角，所以卡片/按钮/输入框的底图都用 Pillow 以 3 倍超采样
渲染再缩放——保持轻量，同时让长时间值守时的层级更清楚。
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
from tkinter import font as tkfont
from tkinter import messagebox, ttk

from PIL import Image, ImageDraw, ImageTk

HERE = Path(__file__).resolve().parent
GRAB = HERE / "grab.py"
SESSION = HERE / "data" / "session.json"
DATA = HERE / "data"
PY = sys.executable or "python"

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
TOKEN_LIFETIME_H = 17.5     # 实测寿命，仅用于显示「约剩几小时」
PAREN = re.compile(r"（[^（）]*）$")

# ---- 配色：报名值守台的晨雾底色 + 珊瑚行动色 + 青绿色状态色 ----
BG = "#F2F5F8"
CARD = "#FFFFFF"
BORDER = "#DCE5EE"
TEXT = "#14243A"
MUTED = "#65758B"
INK = "#15253D"
INK_SOFT = "#223653"
INK_MUTED = "#B7C4D5"
ACCENT = "#C84B3F"
ACCENT_DEEP = "#A83A2E"
ACCENT_SOFT = "#FBEDEA"
OK = "#0F6B59"
BAD = "#B32E2E"
GREY = "#8C98A5"
ROW_ALT = "#F7F9FB"
SEL_ROW = "#E8EEF5"

FONT = "Microsoft YaHei UI"
SS = 3                      # 超采样倍数


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


def _font(size: int, bold: bool = False) -> tuple:
    return (FONT, size, "bold" if bold else "normal")


# ------------------------------------------------------------------ 圆角绘制
def _rounded(size: tuple, radius: int, fill: str, outline: str | None = None,
             shadow: bool = False) -> Image.Image:
    """抗锯齿圆角矩形；shadow 用三层错位半透明模拟柔和投影（不做高斯模糊，快）。"""
    w, h = max(size[0], 8), max(size[1], 8)
    img = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    r = radius * SS

    if shadow:
        for offset, alpha in ((3, 10), (2, 16), (1, 22)):
            draw.rounded_rectangle(
                (offset * SS, offset * SS, (w - 1) * SS, (h - 1) * SS),
                radius=r, fill=(22, 31, 43, alpha))

    draw.rounded_rectangle(
        (0, 0, (w - 1) * SS, (h - 1) * SS), radius=r, fill=fill,
        outline=outline or fill,
        width=SS if outline else 0)
    return img.resize((w, h), Image.LANCZOS)


class _Images:
    """同尺寸同配色只渲染一次——拖动窗口时会反复要同一批图。"""

    def __init__(self):
        self._cache: dict[tuple, ImageTk.PhotoImage] = {}

    def card(self, w: int, h: int, fill: str = CARD, radius: int = 18,
             shadow: bool = True, outline: str = BORDER) -> ImageTk.PhotoImage:
        key = ("card", w, h, fill, radius, shadow, outline)
        if key not in self._cache:
            self._cache[key] = ImageTk.PhotoImage(
                _rounded((w, h), radius, fill, outline, shadow))
        return self._cache[key]

    def shape(self, w: int, h: int, fill: str, radius: int = 10,
              outline: str | None = None) -> ImageTk.PhotoImage:
        key = ("shape", w, h, fill, radius, outline)
        if key not in self._cache:
            self._cache[key] = ImageTk.PhotoImage(
                _rounded((w, h), radius, fill, outline))
        return self._cache[key]


IMAGES = _Images()


class Card(tk.Canvas):
    """圆角面板。自身是画布，内容放 body；四边由 padding 留出圆角空间。"""

    def __init__(self, parent, radius: int = 16, padding=(22, 18, 22, 18),
                 shadow: bool = True, fill: str = CARD, outline: str = BORDER,
                 **kw):
        super().__init__(parent, bg=BG, highlightthickness=0, bd=0, **kw)
        self.radius = radius
        self.padding = padding
        self.shadow = shadow
        self.fill = fill
        self.outline = outline
        self.body = tk.Frame(self, bg=fill)
        self._win = self.create_window(padding[0], padding[1], anchor="nw",
                                       window=self.body)
        self._job = None
        self.bind("<Configure>", self._on_resize)

    def _on_resize(self, _event=None):
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(30, self._redraw)

    def _redraw(self):
        self._job = None
        w, h = self.winfo_width(), self.winfo_height()
        if w < 30 or h < 30:
            return
        pad_l, pad_t, pad_r, pad_b = self.padding
        self.delete("cardbg")
        self.create_image(0, 0, anchor="nw", tags="cardbg",
                          image=IMAGES.card(w, h, self.fill, self.radius,
                                             self.shadow, self.outline))
        self.tag_lower("cardbg")
        self.itemconfigure(self._win, width=max(10, w - pad_l - pad_r),
                           height=max(10, h - pad_t - pad_b))
        self.coords(self._win, pad_l, pad_t)


class RoundButton(tk.Canvas):
    """圆角按钮：primary 珊瑚、dark 深色面板、ghost 浅灰；状态各换一张底图。"""

    def __init__(self, parent, text: str, command=None, kind: str = "ghost",
                 width: int | None = None, height: int = 38, bg: str = CARD,
                 size: int = 10):
        self._font = _font(size, True)
        need = tkfont.Font(font=self._font).measure(text) + 46
        w = width or max(96, need)
        super().__init__(parent, width=w, height=height, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._bw, self._bh, self._kind, self._size = w, height, kind, size
        self._text, self._command = text, command
        self._enabled, self._hovered = True, False
        self._draw()
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))

    def _palette(self):
        # 深色面板上的次要按钮：底色必须比面板亮一档，否则只有文字浮在深色上，
        # 按钮形状看不见（实测 #1C2E49 对 #15253D 只有 1.13:1）。
        if self._kind == "dark":
            if not self._enabled:
                return "#1B2B44", "#8B9CB4"
            return ("#3A5A80" if self._hovered else "#2E4B6E"), "#F4F7FB"
        if not self._enabled:
            return "#EEF1F4", GREY
        if self._kind == "primary":
            return (ACCENT_DEEP if self._hovered else ACCENT), "#FFFFFF"
        if self._kind == "danger":
            return (BAD if self._hovered else "#F04438"), "#FFFFFF"
        return ("#EEF3F7" if self._hovered else "#F6F8FA"), TEXT

    def _draw(self):
        fill, fg = self._palette()
        self.delete("all")
        self.create_image(0, 0, anchor="nw",
                          image=IMAGES.shape(self._bw, self._bh, fill, 10))
        self.create_text(self._bw // 2, self._bh // 2, text=self._text,
                         fill=fg, font=self._font)

    def _set_hover(self, on: bool):
        self._hovered = on
        if self._enabled:
            self._draw()

    def _click(self, _event=None):
        if self._enabled and self._command:
            self._command()

    # 与 tk.Button 的 state 语义保持一致，方便老代码/测试复用
    def state(self, enabled: bool = True):
        self._enabled = bool(enabled)
        self.configure(cursor="hand2" if enabled else "arrow")
        self._draw()

    def set_text(self, text: str):
        self._text = text
        self._draw()


class Pill(tk.Canvas):
    """状态药丸：圆角底色 + 小圆点 + 文字。"""

    def __init__(self, parent, bg: str = CARD):
        super().__init__(parent, bg=bg, highlightthickness=0, bd=0,
                         width=120, height=30)
        self._text, self._fg, self._fill = "", MUTED, "#EEF2F4"
        self._f = _font(9, True)

    def set(self, text: str, fg: str, fill: str):
        self._text, self._fg, self._fill = text, fg, fill
        w = tkfont.Font(font=self._f).measure(text) + 44
        self.configure(width=w, height=30)
        self.delete("all")
        self.create_image(0, 0, anchor="nw",
                          image=IMAGES.shape(w, 30, fill, 15))
        self.create_oval(13, 11, 21, 19, fill=fg, width=0)
        self.create_text(28, 15, text=text, anchor="w", fill=fg, font=self._f)


class RoundEntry(tk.Canvas):
    """圆角输入框：圆角底图 + 无边框 Entry（聚焦时描边变珊瑚）。"""

    def __init__(self, parent, textvariable, width: int = 250,
                 height: int = 36, bg: str = BG, placeholder: str = ""):
        super().__init__(parent, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0)
        self._bw, self._bh = width, height
        self._var = textvariable
        self._ring = self.create_image(0, 0, anchor="nw",
                                       image=IMAGES.shape(width, height,
                                                          "#FFFFFF", 10, BORDER))
        self.entry = tk.Entry(self, textvariable=textvariable, relief="flat",
                              bd=0, highlightthickness=0, bg="#FFFFFF",
                              fg=TEXT, insertbackground=ACCENT,
                              font=_font(10))
        self.create_window(14, height // 2, anchor="w", window=self.entry,
                           width=width - 28, height=height - 14)
        # 占位提示必须用真控件（画布上的文字会被 create_window 的 Entry 盖住），
        # 而且 Entry 是子窗口，后创建的窗口才在上面——所以这一句要放在 Entry 之后。
        self._hint = tk.Label(self, text=placeholder, bg="#FFFFFF", fg="#A6B0BE",
                              font=_font(10), cursor="xterm")
        self._hint_win = self.create_window(15, height // 2, anchor="w",
                                            window=self._hint)
        self._hint.bind("<Button-1>", lambda _e: self.entry.focus_set())
        self._sync_hint()
        self.entry.bind("<FocusIn>", lambda _e: (self._paint(ACCENT),
                                                 self._sync_hint()))
        self.entry.bind("<FocusOut>", lambda _e: (self._paint(BORDER),
                                                  self._sync_hint()))
        textvariable.trace_add("write", lambda *_a: self._sync_hint())

    def _sync_hint(self):
        """占位提示只在「空 + 没聚焦」时出现；点进去就消失。"""
        empty = not self._var.get()
        focused = self.focus_get() is self.entry
        if empty and not focused:
            self.itemconfigure(self._hint_win, state="normal")
            self.tag_raise(self._hint_win)
        else:
            self.itemconfigure(self._hint_win, state="hidden")

    def _paint(self, color: str):
        self.delete(self._ring)
        self._ring = self.create_image(
            0, 0, anchor="nw",
            image=IMAGES.shape(self._bw, self._bh, "#FFFFFF", 10, color))
        self.tag_lower(self._ring)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.proc: subprocess.Popen | None = None
        self.all_rows: list[dict] = []
        self.rows: list[dict] = []
        self.target: dict | None = None
        self.deadline: float | None = None
        self._left_cache: dict[str, str] = {}
        self._token_ok: bool | None = None
        self._busy: str = ""

        root.title("天软校园活动 · 报名值守台")
        root.configure(bg=BG)
        root.geometry("1120x820")
        root.minsize(960, 720)
        self._center()
        self._build_style()
        self._build_ui()
        self.root.after(100, self._pump)
        self.root.after(400, self.refresh, True)

    def _center(self):
        self.root.update_idletasks()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        # 先按屏幕算能放多大，再让 minsize 不超过它——否则 768 高的屏上
        # minsize(...,720) 会把窗口顶出屏幕底部，日志卡直接看不见。
        w, h = min(1120, sw - 80), min(820, sh - 120)
        self.root.minsize(min(960, w), min(700, h))
        self.root.geometry("%dx%d+%d+%d" % (w, h, (sw - w) // 2,
                                            max((sh - h) // 3, 0)))

    # ---------------------------------------------------------------- 外观
    def _build_style(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("App.Treeview", background=CARD, fieldbackground=CARD,
                        foreground=TEXT, rowheight=35, borderwidth=0,
                        font=_font(10))
        style.layout("App.Treeview", [("App.Treeview.treearea",
                                       {"sticky": "nswe"})])
        style.configure("App.Treeview.Heading", background="#F5F8FB",
                        foreground="#587087", relief="flat", borderwidth=0,
                        font=_font(9, True), padding=(12, 9))
        style.map("App.Treeview.Heading", background=[("active", "#EEF1F4")])
        style.map("App.Treeview",
                  background=[("selected", SEL_ROW)],
                  foreground=[("selected", TEXT)])
        style.configure("App.Vertical.TScrollbar", background="#D7E1EA",
                        troughcolor=CARD, bordercolor=CARD, arrowcolor="#7A8B9D",
                        borderwidth=0, arrowsize=12)

    # ---------------------------------------------------------------- 布局
    def _build_ui(self):
        outer = tk.Frame(self.root, bg=BG)
        outer.pack(fill="both", expand=True, padx=24, pady=20)

        # ---- 值守头部：深靛蓝建立工具的主识别面，珊瑚只留给明确动作。
        head = Card(outer, padding=(28, 19, 26, 19), height=116,
                    fill=INK, outline=INK, shadow=False)
        head.pack(fill="x")
        head.pack_propagate(False)
        titles = tk.Frame(head.body, bg=INK)
        titles.pack(side="left", fill="y")
        tk.Label(titles, text="天软校园活动", bg=INK, fg="#F7FAFC",
                 font=_font(20, True)).pack(anchor="w")
        tk.Label(titles, text="报名值守台 · 只登录自己的账号，保持电脑唤醒、网络畅通",
                 bg=INK, fg=INK_MUTED, font=_font(9)).pack(anchor="w", pady=(6, 0))

        right = tk.Frame(head.body, bg=INK)
        right.pack(side="right")
        self.pill = Pill(right, bg=INK)
        self.pill.pack(side="left", padx=(0, 14))
        self._pill("未检查", GREY, "#EEF2F4")
        self.btn_refresh = RoundButton(right, "刷新活动  F5", self.refresh,
                                       kind="dark", bg=INK, height=40)
        self.btn_refresh.pack(side="left", padx=(0, 8))
        self.btn_login = RoundButton(right, "登录 / 重新登录", self.login,
                                     kind="primary", bg=INK, height=40)
        self.btn_login.pack(side="left")

        # ---- 筛选条：筛选与数量各占一端，搜索框保持明确的输入焦点。
        bar = tk.Frame(outer, bg=BG)
        bar.pack(fill="x", pady=(18, 12))
        tk.Label(bar, text="活动清单", bg=BG, fg=TEXT,
                 font=_font(10, True)).pack(side="left", padx=(3, 12))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_a: self._apply_filter())
        self.entry_wrap = RoundEntry(bar, self.filter_var, width=280,
                                     placeholder="活动名关键词")
        self.entry_wrap.pack(side="left")
        self.entry_wrap.entry.bind("<Return>", lambda _e: self._apply_filter())
        self.only_free = tk.BooleanVar(value=True)
        tk.Checkbutton(bar, text="只看还能报的", variable=self.only_free,
                       command=self._apply_filter, bg=BG, fg=TEXT,
                       activebackground=BG, selectcolor=BG, bd=0,
                       highlightthickness=0, font=_font(9)).pack(side="left",
                                                                 padx=(16, 0))
        self.count_var = tk.StringVar(value="")
        tk.Label(bar, textvariable=self.count_var, bg=BG, fg=MUTED,
                 font=_font(9)).pack(side="right", padx=(0, 3))

        # ---- 活动表：全界面唯一的高密度阅读面，避免每个区域都变成卡片。
        table = Card(outer, padding=(14, 14, 14, 14), shadow=False)
        table.pack(fill="both", expand=True)
        cols = ("index", "left", "sign_start", "state", "name", "place")
        self.tree = ttk.Treeview(table.body, columns=cols, show="headings",
                                 selectmode="browse", style="App.Treeview",
                                 height=7)
        headings = {
            "index": ("序号", 54), "left": ("距开放", 112),
            "sign_start": ("报名开始", 158), "state": ("状态", 92),
            "name": ("活动名", 420), "place": ("地点", 140),
        }
        for key, (text, width) in headings.items():
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor="w",
                             stretch=(key in ("name", "place")))
        # 整行只给「已报名」上灰；开放/待开放靠「距开放」「状态」两列本身表达，
        # 每行都染强调色会让表格变成一片噪音。
        self.tree.tag_configure("signed", foreground="#9AA5B1")
        self.tree.tag_configure("open", foreground=TEXT)
        self.tree.tag_configure("waiting", foreground=TEXT)
        self.tree.tag_configure("odd", background=ROW_ALT)
        self.tree.tag_configure("even", background=CARD)
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(table.body, orient="vertical",
                           command=self.tree.yview,
                           style="App.Vertical.TScrollbar")
        sb.pack(side="right", fill="y", padx=(8, 0))
        self.tree.configure(yscrollcommand=sb.set)
        self.empty_hint = tk.Label(table.body, bg=CARD, fg=MUTED,
                                   font=_font(10), justify="center")
        self.tree.bind("<Double-1>", lambda _e: self.start_grab())
        self.tree.bind("<Return>", lambda _e: self.start_grab())
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._update_selected())

        # ---- 操作卡：深色任务面把选中活动与倒计时从列表中提出来。
        act = Card(outer, padding=(26, 16, 22, 16), height=132,
                   fill=INK, outline=INK, shadow=False)
        act.pack(fill="x", pady=(14, 0))
        act.pack_propagate(False)
        self.selected_var = tk.StringVar(value="未选中活动 · 在上面的列表选择一项")
        tk.Label(act.body, textvariable=self.selected_var, bg=INK, fg="#F7FAFC",
                 font=_font(9), anchor="w").pack(fill="x")
        line = tk.Frame(act.body, bg=INK)
        line.pack(fill="x", pady=(9, 0))
        self.btn_grab = RoundButton(line, "到点抢选中活动", self.start_grab,
                                    kind="primary", bg=INK, width=178, height=42,
                                    size=11)
        self.btn_grab.pack(side="left")
        self.btn_stop = RoundButton(line, "停止", self.stop, kind="dark",
                                    bg=INK, width=82, height=42)
        self.btn_stop.pack(side="left", padx=(10, 0))
        self.btn_stop.state(False)
        self.headed_var = tk.BooleanVar(value=False)
        tk.Checkbutton(line, text="显示浏览器窗口（看着它点）",
                       variable=self.headed_var, bg=INK, fg="#E8EEF5",
                       activebackground=INK, selectcolor=INK, bd=0,
                       highlightthickness=0,
                       font=_font(9)).pack(side="left", padx=(16, 0))
        self.countdown_var = tk.StringVar(value="")
        tk.Label(line, textvariable=self.countdown_var, bg=INK, fg="#FFB2A9",
                 font=("Cascadia Mono", 11, "bold")).pack(side="left", padx=(16, 0))
        self.btn_open = RoundButton(line, "打开数据目录", self.open_data,
                                    kind="dark", bg=INK, width=132, height=42)
        self.btn_open.pack(side="right")

        # ---- 日志卡：固定成窄条，保留事件流但不抢活动列表的注意力。
        logcard = Card(outer, padding=(22, 14, 22, 14), height=156,
                       shadow=False)
        logcard.pack(fill="both", pady=(14, 0))
        logcard.pack_propagate(False)
        loghead = tk.Frame(logcard.body, bg=CARD)
        loghead.pack(fill="x")
        tk.Label(loghead, text="运行日志", bg=CARD, fg=TEXT,
                 font=_font(10, True)).pack(side="left")
        self.btn_clear = RoundButton(loghead, "清空", self.clear_log,
                                     width=78, height=30, size=9)
        self.btn_clear.pack(side="right")
        panel = tk.Frame(logcard.body, bg="#F5F8FB")
        panel.pack(fill="both", expand=True, pady=(10, 0))
        self.logbox = tk.Text(panel, height=7, wrap="none", bd=0,
                              relief="flat", highlightthickness=0,
                              bg="#F5F8FB", fg="#34465B",
                              insertbackground=TEXT, font=("Consolas", 9),
                              padx=12, pady=9)
        self.logbox.pack(fill="both", expand=True)
        self.logbox.configure(state="disabled")

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

    def _pill(self, text: str, color: str, fill: str = ACCENT_SOFT):
        self.pill.set(text, color, fill)

    def _refresh_pill(self):
        if self._token_ok is None:
            self._pill("未检查", GREY, "#EEF2F4")
        elif not self._token_ok:
            self._pill("登录态失效", BAD, "#FDECEC")
        else:
            left = self._token_hours_left()
            if left is None:
                self._pill("登录态有效", OK, "#E7F7F1")
            elif left <= 0:
                self._pill("有效 · 可能快过期", ACCENT_DEEP, ACCENT_SOFT)
            else:
                self._pill("有效 · 约剩 %.0f 小时" % left, OK, "#E7F7F1")

    def _token_hours_left(self) -> float | None:
        try:
            info = json.loads(SESSION.read_text(encoding="utf-8"))
            saved = datetime.strptime(info["saved_at"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
        return TOKEN_LIFETIME_H - (datetime.now() - saved).total_seconds() / 3600.0

    def _set_busy(self, busy: bool, label: str = ""):
        for btn in (self.btn_refresh, self.btn_login, self.btn_grab):
            btn.state(not busy)
        self.btn_stop.state(busy)
        self._busy = label if busy else ""
        if busy:
            self._pill("进行中：%s" % label, ACCENT_DEEP, ACCENT_SOFT)
        else:
            self._refresh_pill()

    # ---------------------------------------------------------------- 子进程
    def _run(self, args: list[str], label: str, done=None):
        if self.proc is not None:
            return
        cmd = [PY, str(GRAB)] + args
        self.log("▶ " + label)
        self._set_busy(True, label)
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=str(HERE), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", bufsize=1, env=child_env(),
                creationflags=CREATE_NO_WINDOW)
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
                elif kind == "hint":
                    self._show_empty_hint(payload)
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
            txt = ("距报名开放 %02d:%02d:%02d" % (h, m, s)) if h else \
                  ("距报名开放 %02d:%02d" % (m, s))
            self.countdown_var.set(txt)
        else:
            self.countdown_var.set("报名已开放，正在抢…")

    # ---------------------------------------------------------------- 列表
    def _refresh_countdowns(self):
        """每秒只改变化了的「距开放」单元格，避免整表重绘。"""
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
        """检查登录 + 拉列表，一次搞定；auto=True 是启动自检，失败不弹窗。"""
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
                    self.q.put(("hint", "登录态无效 · 点右上角「登录 / 重新登录」完成一次网页登录"))
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
            self._show_empty_hint(
                "还没有活动 · 先点右上角「登录 / 重新登录」，再点「刷新活动」")
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
            self.log("已加载 %d 个活动（显示 %d 个）。选中后点「到点抢选中活动」，或双击那一行。"
                     % (len(self.all_rows), len(rows)))
        self.count_var.set("共 %d 个，显示 %d 个" % (len(self.all_rows), len(rows)))
        if rows:
            self.empty_hint.place_forget()
        else:
            self._show_empty_hint(
                "还没有活动 · 先点右上角「登录 / 重新登录」，再点「刷新活动」"
                if not self.all_rows else
                "没有符合条件的活动 · 清除关键词，或取消「只看还能报的」")
        self._update_selected()

    def _show_empty_hint(self, text: str):
        self.empty_hint.configure(text=text)
        self.empty_hint.place(relx=0.5, rely=0.45, anchor="center")

    def _update_selected(self):
        row = self._selected_row()
        if not row:
            self.selected_var.set("未选中活动 · 在上面的列表选择一项")
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
        DATA.mkdir(exist_ok=True)
        try:
            os.startfile(str(DATA))          # noqa: S606 - Windows 专用
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
    try:
        root = tk.Tk()
        App(root)
        root.mainloop()
    except Exception:
        # pythonw 启动时没有控制台，异常会"静默退出"；落一份日志并弹窗，
        # 否则用户只会看到"双击没反应"。
        import traceback
        detail = traceback.format_exc()
        try:
            (HERE / "data").mkdir(exist_ok=True)
            (HERE / "data" / "gui-error.log").write_text(detail, encoding="utf-8")
        except Exception:
            pass
        try:
            messagebox.showerror(
                "界面出错了", detail[-1500:] + "\n\n已写入 data/gui-error.log")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
