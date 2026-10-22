#!/usr/bin/env python3
"""校园活动自动报名 · 图形界面（本机版）

只是个壳：所有实际操作都变成调用 grab.py 子命令（list --json / check / login / grab），
这样界面和命令行共用同一套逻辑，出问题可以对照命令行输出排查，不会两套代码各自跑偏。

界面与业务进程分在两个线程：UI 线程只负责显示，抢报名在子进程里跑，
日志通过队列回传，避免 playwright 卡住窗口。
"""
from __future__ import annotations

import json
import os
import queue
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
PY = sys.executable or "python"

# Windows 下不弹黑框（子进程仍会正常拉起浏览器）
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def child_env():
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.proc: subprocess.Popen | None = None
        self.rows: list[dict] = []
        self.target: dict | None = None
        self.deadline: float | None = None

        root.title("校园活动自动报名 · 本机版")
        root.geometry("980x660")
        root.minsize(820, 560)
        self._build_style()
        self._build_ui()
        self.root.after(100, self._pump)
        self.log("提示：先点「检查登录」，显示「有效」后点「刷新活动」，选中活动再点「到点抢报名」。")

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
        style.configure("Big.TButton", font=("Microsoft YaHei UI", 11, "bold"), padding=(16, 9))
        style.configure("Treeview", font=base, rowheight=26)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Status.TLabel", font=("Microsoft YaHei UI", 10, "bold"))

    # ---------------------------------------------------------------- 布局
    def _build_ui(self):
        top = ttk.Frame(self.root, padding=(12, 10, 12, 4))
        top.pack(fill="x")

        self.status_var = tk.StringVar(value="登录态：未知")
        self.status_label = ttk.Label(top, textvariable=self.status_var, style="Status.TLabel")
        self.status_label.pack(side="left")

        self.countdown_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.countdown_var, foreground="#c0392b").pack(side="left", padx=16)

        self.btn_refresh = ttk.Button(top, text="刷新活动", command=self.refresh)
        self.btn_refresh.pack(side="right")
        self.btn_login = ttk.Button(top, text="登录 / 重新登录", command=self.login)
        self.btn_login.pack(side="right", padx=6)
        self.btn_check = ttk.Button(top, text="检查登录", command=self.check_login)
        self.btn_check.pack(side="right", padx=6)

        # ---- 活动表
        mid = ttk.Frame(self.root, padding=(12, 4))
        mid.pack(fill="both", expand=True)

        cols = ("index", "sign_start", "state", "name", "place")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="browse")
        headings = {
            "index": ("序号", 56), "sign_start": ("报名开始", 155),
            "state": ("状态", 190), "name": ("活动名", 360), "place": ("地点", 140),
        }
        for key, (text, width) in headings.items():
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor="w",
                             stretch=(key in ("name", "place")))
        self.tree.tag_configure("signed", foreground="#8a8a8a")
        self.tree.tag_configure("open", foreground="#1f6feb")
        self.tree.tag_configure("waiting", foreground="#0a7d32")
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        sb.pack(side="left", fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<Double-1>", lambda _e: self.start_grab())

        # ---- 操作条
        bar = ttk.Frame(self.root, padding=(12, 8))
        bar.pack(fill="x")

        self.btn_grab = ttk.Button(bar, text="到点抢选中的活动", style="Big.TButton",
                                   command=self.start_grab)
        self.btn_grab.pack(side="left")
        self.btn_stop = ttk.Button(bar, text="停止", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=8)

        self.headed_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="显示浏览器窗口（看着它点）",
                        variable=self.headed_var).pack(side="left", padx=12)

        ttk.Button(bar, text="打开数据目录", command=self.open_data).pack(side="right")

        # ---- 日志
        logframe = ttk.Frame(self.root, padding=(12, 0, 12, 10))
        logframe.pack(fill="both", expand=False)
        self.logbox = ScrolledText(logframe, height=12, wrap="none",
                                   font=("Consolas", 10), state="disabled",
                                   background="#fbfbfb", foreground="#202020")
        self.logbox.pack(fill="both", expand=True)

    # ---------------------------------------------------------------- 工具
    def log(self, text: str):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.logbox.configure(state="normal")
        self.logbox.insert("end", "%s  %s\n" % (stamp, text))
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def _set_busy(self, busy: bool):
        state = "disabled" if busy else "normal"
        for btn in (self.btn_refresh, self.btn_login, self.btn_check, self.btn_grab):
            btn.configure(state=state)
        self.btn_stop.configure(state="normal" if busy else "disabled")

    def _run(self, args: list[str], label: str, done=None):
        """起一个子进程跑 grab.py，输出逐行丢回 UI 队列。"""
        if self.proc is not None:
            return
        cmd = [PY, str(GRAB)] + args
        self.log("▶ " + label)
        self._set_busy(True)
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
                    self._apply_rows(payload)
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

    # ---------------------------------------------------------------- 动作
    def check_login(self):
        def done(rc):
            self.status_var.set("登录态：有效 ✓" if rc == 0 else "登录态：失效，请点「登录 / 重新登录」")
        self._run(["check"], "检查登录态", done)

    def login(self):
        if not messagebox.askokcancel(
                "登录", "接下来会弹出一个 Chrome 窗口，请在里面手动输入学号、密码、验证码并登录。\n\n"
                        "登录成功后窗口会自动关闭。\n\n继续吗？"):
            return

        def done(rc):
            if rc == 0:
                self.status_var.set("登录态：有效 ✓")
                self.log("登录成功，可以刷新活动列表了。")
                self.refresh()
            else:
                self.status_var.set("登录态：失效")
        self._run(["login"], "打开浏览器等待手动登录（最长 10 分钟）", done)

    def refresh(self):
        # 列表需要单独解析，这里用一次性调用，避免和其它命令抢 self.proc
        if self.proc is not None:
            messagebox.showinfo("稍等", "还有任务在跑，等它结束再刷新。")
            return
        self._set_busy(True)
        self.log("▶ 拉取活动列表")

        def worker():
            try:
                res = subprocess.run(
                    [PY, str(GRAB), "list", "--json"], cwd=str(HERE),
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    env=child_env(), creationflags=CREATE_NO_WINDOW, timeout=90)
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
            except Exception as exc:
                self.q.put(("log", "拉取异常：%s: %s" % (type(exc).__name__, exc)))
                self.q.put(("done", (1, None)))

        self.rows = []
        threading.Thread(target=worker, daemon=True).start()

    def _apply_rows(self, rows: list[dict]):
        self.rows = rows
        for item in self.tree.get_children():
            self.tree.delete(item)
        if not rows:
            self.log("（没有可报名的活动：要么还没公布报名时间，要么都已截止/已报名）")
            return
        for row in rows:
            tag = "signed" if row.get("signed") else (
                "open" if str(row.get("state", "")).startswith("开放") else "waiting")
            self.tree.insert("", "end", iid=row["id"], values=(
                row["index"], row["sign_start"] or "-", row.get("state", ""),
                row["name"], row.get("place", "")), tags=(tag,))
        self.log("已加载 %d 个活动。选中后点「到点抢选中的活动」，或双击一行。" % len(rows))

    def _selected_row(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return next((r for r in self.rows if r["id"] == sel[0]), None)

    def start_grab(self):
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

        tip = ("活动：%s\n报名开始：%s\n\n" % (row["name"], row["sign_start"]))
        if already:
            tip += "注意：报名已经开始，现在点会立刻尝试（可能已满员）。\n\n"
        if self.headed_var.get():
            tip += "（已勾选显示浏览器窗口）\n\n"
        tip += "确认开始？电脑要保持唤醒、别断网。"
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
                self.status_var.set("上次抢报名：已完成，结果见上方日志")
            else:
                self.status_var.set("上次抢报名：退出码 %s，结果见日志" % rc)
            self.refresh()
        self._run(args, "开始等待并抢报名", done)

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
