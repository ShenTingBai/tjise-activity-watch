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

        root.title("校园活动自动报名 · 本机版")
        root.geometry("1020x700")
        root.minsize(860, 600)
        self._build_style()
        self._build_ui()
        self.root.after(100, self._pump)
        # 打开即自检：省掉"先点检查、再点刷新"两步
        self.root.after(400, self.refresh, True)

    # ---------------------------------------------------------------- 外观
    def _build_style(self):
        style = ttk.Style()
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        base = ("Microsoft YaHei UI", 10)
        style.configure("TLabel", font=base)
        style.configure("TButton", font=base, padding=(12, 6))
        style.configure("Big.TButton", font=("Microsoft YaHei UI", 11, "bold"), padding=(18, 10))
        style.configure("Treeview", font=base, rowheight=28)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Status.TLabel", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Hint.TLabel", foreground="#666666")

    # ---------------------------------------------------------------- 布局
    def _build_ui(self):
        top = ttk.Frame(self.root, padding=(12, 10, 12, 4))
        top.pack(fill="x")

        self.status_var = tk.StringVar(value="登录态：检查中…")
        ttk.Label(top, textvariable=self.status_var, style="Status.TLabel").pack(side="left")

        self.countdown_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.countdown_var, foreground="#c0392b",
                  font=("Microsoft YaHei UI", 10, "bold")).pack(side="left", padx=14)

        self.btn_refresh = ttk.Button(top, text="刷新（F5）", command=self.refresh)
        self.btn_refresh.pack(side="right")
        self.btn_login = ttk.Button(top, text="登录 / 重新登录", command=self.login)
        self.btn_login.pack(side="right", padx=6)

        # ---- 筛选条
        filt = ttk.Frame(self.root, padding=(12, 2))
        filt.pack(fill="x")
        ttk.Label(filt, text="筛选：").pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_a: self._apply_filter())
        entry = ttk.Entry(filt, textvariable=self.filter_var, width=24)
        entry.pack(side="left")
        ttk.Label(filt, text="（活动名关键词，留空显示全部）", style="Hint.TLabel").pack(side="left", padx=6)
        self.only_free = tk.BooleanVar(value=True)
        ttk.Checkbutton(filt, text="只看还能报的", variable=self.only_free,
                        command=self._apply_filter).pack(side="left", padx=12)
        self.count_var = tk.StringVar(value="")
        ttk.Label(filt, textvariable=self.count_var, style="Hint.TLabel").pack(side="right")

        # ---- 活动表
        mid = ttk.Frame(self.root, padding=(12, 4))
        mid.pack(fill="both", expand=True)

        cols = ("index", "left", "sign_start", "state", "name", "place")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="browse")
        headings = {
            "index": ("序号", 52), "left": ("距开放", 108), "sign_start": ("报名开始", 150),
            "state": ("状态", 92), "name": ("活动名", 390), "place": ("地点", 140),
        }
        for key, (text, width) in headings.items():
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor="w",
                             stretch=(key in ("name", "place")))
        self.tree.tag_configure("signed", foreground="#8a8a8a")
        self.tree.tag_configure("open", foreground="#1f6feb")
        self.tree.tag_configure("waiting", foreground="#0a7d32")
        self.tree.tag_configure("soon", foreground="#c0392b")
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        sb.pack(side="left", fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<Double-1>", lambda _e: self.start_grab())
        self.tree.bind("<Return>", lambda _e: self.start_grab())
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._update_selected())

        # ---- 操作条
        bar = ttk.Frame(self.root, padding=(12, 8))
        bar.pack(fill="x")

        self.selected_var = tk.StringVar(value="未选中活动 —— 在上面点一行")
        ttk.Label(bar, textvariable=self.selected_var,
                  style="Hint.TLabel").pack(side="top", anchor="w", pady=(0, 6))

        line = ttk.Frame(bar)
        line.pack(fill="x")
        self.btn_grab = ttk.Button(line, text="到点抢选中的活动（Enter）", style="Big.TButton",
                                   command=self.start_grab)
        self.btn_grab.pack(side="left")
        self.btn_stop = ttk.Button(line, text="停止", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        self.headed_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(line, text="显示浏览器窗口（看着它点）",
                        variable=self.headed_var).pack(side="left", padx=12)
        ttk.Button(line, text="打开数据目录", command=self.open_data).pack(side="right")

        # ---- 日志
        logframe = ttk.Frame(self.root, padding=(12, 0, 12, 10))
        logframe.pack(fill="both", expand=False)
        self.logbox = ScrolledText(logframe, height=9, wrap="none",
                                   font=("Consolas", 10), state="disabled",
                                   background="#fbfbfb", foreground="#202020")
        self.logbox.pack(fill="both", expand=True)

        # ---- 快捷键（Enter 只绑在表格上：在筛选框里回车不该触发抢报名）
        self.root.bind("<F5>", lambda _e: self.refresh())

    # ---------------------------------------------------------------- 工具
    def log(self, text: str):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.logbox.configure(state="normal")
        self.logbox.insert("end", "%s  %s\n" % (stamp, text))
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def _set_busy(self, busy: bool, label: str = ""):
        state = "disabled" if busy else "normal"
        for btn in (self.btn_refresh, self.btn_login, self.btn_grab):
            btn.configure(state=state)
        self.btn_stop.configure(state="normal" if busy else "disabled")
        self._busy_label = label
        if busy:
            self.status_var.set("登录态：" + self._token_text() + "  ·  进行中：" + label)

    def _token_text(self) -> str:
        """登录态文字：有效/失效/未登录 + 大约还剩多少小时（按登录时间估算）。"""
        if self.proc is not None and self._busy_label:
            return "忙"
        ok = getattr(self, "_token_ok", None)
        if ok is None:
            return "未知"
        if not ok:
            return "失效，请点「登录 / 重新登录」"
        left = self._token_hours_left()
        if left is None:
            return "有效 ✓"
        if left <= 0:
            return "有效 ✓（可能快过期了）"
        return "有效 ✓ · 约剩 %.0f 小时" % left

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
                    self.status_var.set("登录态：" + self._token_text())
                elif kind == "done":
                    rc, done = payload
                    self.proc = None
                    self._set_busy(False)
                    self.status_var.set("登录态：" + self._token_text())
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
        self.status_var.set("登录态：" + self._token_text())
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
        for row in rows:
            tag = "signed" if row.get("signed") else (
                "open" if str(row.get("state", "")).startswith("开放") else "waiting")
            state = PAREN.sub("", row.get("state", "") or "")
            self.tree.insert("", "end", iid=row["id"], values=(
                row["index"], "", row["sign_start"] or "-", state,
                row["name"], row.get("place", "")), tags=(tag,))
        self._refresh_countdowns()
        if announce:
            self.log("已加载 %d 个活动（显示 %d 个）。选中后点「到点抢选中的活动」，或双击那一行。"
                     % (len(self.all_rows), len(rows)))
        self.count_var.set("共 %d 个，显示 %d 个" % (len(self.all_rows), len(rows)))
        self._update_selected()

    def _update_selected(self):
        row = self._selected_row()
        if not row:
            self.selected_var.set("未选中活动 —— 在上面点一行")
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
