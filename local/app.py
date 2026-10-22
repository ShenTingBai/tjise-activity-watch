#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校园活动自动报名 · 本机版（WebView2 外壳）。

界面在 ui/ 下用 HTML/CSS 写，这里只做三件事：

1. 起窗口、加载 ui/index.html；
2. 把 grab.py 的能力暴露成页面可调的方法（window.pywebview.api.*）；
3. 把子进程输出实时推给页面（window.onBackend.log / .state / .done）。

业务逻辑一律留在 grab.py：换界面不用碰它，界面出问题也能直接命令行复现。

@author LiXinwen
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import webview

HERE = Path(__file__).resolve().parent
UI = HERE / "ui" / "index.html"
GRAB = HERE / "grab.py"
PREFS = HERE / "data" / "ui.json"
TOKEN_LIFETIME_H = 17.5

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# --demo：用假数据起界面，方便调样式、截图，不需要登录
DEMO = False


def demo_items() -> list[dict]:
    now = time.time()
    raw = [
        ("demo-security", "网络安全·数字时代大长城", "实践学习", "教学 A301", -3, 3600, False),
        ("demo-face", "我与学生面对面（天软信息化发展之路）", "讲座", "报告厅", -7200, 7200, True),
        ("demo-lab", "程序设计实验室开放日", "实践学习", "实训楼 B204", 1800, 5400, False),
        ("demo-career", "简历门诊：一对一改简历", "就业指导", "线上", 86400, 90000, False),
        ("demo-volunteer", "图书馆志愿服务（第 12 期）", "志愿服务", "图书馆一层", 172800, 176400, False),
    ]
    out = []
    for i, (aid, name, kind, place, s_off, e_off, signed) in enumerate(raw):
        start = datetime.fromtimestamp(now + s_off).strftime("%Y-%m-%d %H:%M:%S")
        end = datetime.fromtimestamp(now + e_off).strftime("%Y-%m-%d %H:%M:%S")
        out.append({"index": i + 1, "id": aid, "name": name, "type": kind,
                    "place": place, "sign_start": start, "sign_end": end,
                    "act_start": start, "signed": signed,
                    "state": "signed" if signed else (
                        "open" if s_off <= 0 else "waiting"),
                    "seconds_to_open": s_off})
    return out


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def load_prefs() -> dict:
    try:
        return json.loads(PREFS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_prefs(prefs: dict) -> None:
    PREFS.parent.mkdir(exist_ok=True)
    tmp = PREFS.with_suffix(".tmp")
    tmp.write_text(json.dumps(prefs, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, PREFS)


def token_hours_left() -> float | None:
    """按 session.json 的 saved_at 估算剩余时间（足够给界面显示，不参与判断）。"""
    try:
        data = json.loads((HERE / "data" / "session.json").read_text(encoding="utf-8"))
        saved = datetime.strptime(data["saved_at"], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None
    used = (datetime.now() - saved).total_seconds() / 3600.0
    return max(0.0, TOKEN_LIFETIME_H - used)


class Api:
    def __init__(self) -> None:
        self._window: webview.Window | None = None
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    # ---------- 事件推送 ----------
    def _emit(self, kind: str, payload) -> None:
        if not self._window:
            return
        js = "window.onBackend && window.onBackend.%s(%s)" % (
            kind, json.dumps(payload, ensure_ascii=False))
        try:
            self._window.evaluate_js(js)
        except Exception:
            pass

    # ---------- 读数据 ----------
    def bootstrap(self) -> dict:
        prefs = load_prefs()
        return {
            "version": "2.0.0",
            "prefs": prefs,
            "help": self.help(),
            "login": {"hours_left": token_hours_left()},
            "busy": self._proc is not None and self._proc.poll() is None,
        }

    def help(self) -> dict:
        return {
            "title": "报名值守台",
            "subtitle": "天软校园活动 · 只为你自己的账号抢报名",
            "steps": [
                {"no": 1, "title": "先登录",
                 "text": "点右上角「登录」，在弹出的 Chrome 里登录一次。"
                         "登录态大约能用 17 小时，过期后点同一个按钮重登。"},
                {"no": 2, "title": "选活动",
                 "text": "在列表里点一行，再点「到点抢选中的活动」。"
                         "程序会等到报名开放的那一刻自动提交。"},
                {"no": 3, "title": "保持在线",
                 "text": "电脑别睡眠、别断网；窗口可以最小化，但不要关掉。"
                         "抢到或没抢到都会弹窗提示。"},
            ],
            "note": "只给本人账号使用，不要借别人的账号，也不要同时开多个。",
        }

    def front_ready(self) -> dict:
        # 仅排障：前端 boot 完成时打一声招呼，确认桥接真的通了
        self._trace("front_ready")
        return {"ok": True}

    def check(self) -> dict:
        # js_api 的处理跑在 UI 线程上：一旦在这里跑 subprocess，
        # 窗口会直接"未响应"。所以只负责起线程，结果用事件推回去。
        threading.Thread(target=self._do_check, daemon=True).start()
        return {"ok": True, "queued": True}

    def list_activities(self) -> dict:
        threading.Thread(target=self._do_list, daemon=True).start()
        return {"ok": True, "queued": True}

    def _do_check(self) -> None:
        res = self._run([sys.executable, str(GRAB), "check"], timeout=90)
        self._emit("check", {
            "ok": res["code"] == 0,
            "hours_left": token_hours_left(),
            "text": (res["out"].strip().splitlines() or [""])[-1],
        })

    def _do_list(self) -> None:
        if DEMO:
            self._emit("activities", {"ok": True, "items": demo_items()})
            return
        res = self._run([sys.executable, str(GRAB), "list", "--json"], timeout=120)
        if res["code"] != 0:
            self._emit("activities", {"ok": False,
                                      "text": res["out"].strip(), "items": []})
            return
        try:
            items = json.loads(res["out"].strip() or "[]")
        except json.JSONDecodeError:
            self._emit("activities", {"ok": False,
                                      "text": "活动列表解析失败", "items": []})
            return
        now = time.time()
        for it in items:
            it["seconds_to_open"] = _seconds_until(it.get("sign_start"), now)
        self._emit("activities", {"ok": True, "items": items})

    # ---------- 动作 ----------
    def login(self) -> dict:
        return self._spawn([sys.executable, str(GRAB), "login"], "登录")

    def grab(self, activity_id: str, name: str, at: str = "",
             headed: bool = False) -> dict:
        args = [sys.executable, str(GRAB), "grab",
                "--activity-id", activity_id, "--name", name]
        if at:
            args += ["--at", at]
        if headed:
            args.append("--headed")
        return self._spawn(args, "等待并抢报名：%s" % name)

    def stop(self) -> dict:
        with self._lock:
            proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
            self._emit("log", "已请求停止")
            return {"ok": True}
        return {"ok": False, "text": "当前没有在跑的任务"}

    def open_data(self) -> dict:
        path = HERE / "data"
        path.mkdir(exist_ok=True)
        os.startfile(str(path))            # noqa: S606 - Windows 桌面工具
        return {"ok": True}

    def save_prefs(self, prefs: dict) -> dict:
        merged = load_prefs()
        merged.update(prefs or {})
        save_prefs(merged)
        return {"ok": True}

    # ---------- 内部 ----------
    def _trace(self, msg: str) -> None:
        """调试用：只有设了 TJISE_TRACE 才落盘，平时零副作用。"""
        path = os.environ.get("TJISE_TRACE")
        if not path:
            return
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(str(msg) + "\n")
        except Exception:
            pass

    def _run(self, args: list[str], timeout: int) -> dict:
        try:
            res = subprocess.run(args, cwd=str(HERE), capture_output=True,
                                 text=True, encoding="utf-8", errors="replace",
                                 env=_env(), timeout=timeout,
                                 creationflags=CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired:
            return {"code": -1, "out": "[X] 执行超时（%d 秒）" % timeout}
        out = (res.stdout or "") + (res.stderr or "")
        return {"code": res.returncode, "out": out}

    def _spawn(self, args: list[str], label: str) -> dict:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return {"ok": False, "text": "已经有一个任务在跑，先等它结束"}
            self._proc = subprocess.Popen(
                args, cwd=str(HERE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=_env(), creationflags=CREATE_NO_WINDOW)
            proc = self._proc

        threading.Thread(target=self._pump, args=(proc, label),
                         daemon=True).start()
        return {"ok": True}

    def _pump(self, proc: subprocess.Popen, label: str) -> None:
        self._emit("log", "▶ " + label)
        tail: list[str] = []
        for line in proc.stdout or []:
            line = line.rstrip("\r\n")
            if not line:
                continue
            tail.append(line)
            del tail[:-40]
            self._emit("log", line)
        proc.wait()
        ok = proc.returncode == 0 and any("报名成功" in t for t in tail)
        reason = ""
        if not ok:
            for t in reversed(tail):
                if t.startswith("[X] "):
                    reason = t[4:].strip()
                    break
            reason = reason or ("退出码 %s" % proc.returncode)
        self._emit("done", {"ok": ok, "code": proc.returncode,
                            "reason": reason, "label": label})
        with self._lock:
            if self._proc is proc:
                self._proc = None


def _seconds_until(ts: str | None, now: float) -> int | None:
    if not ts:
        return None
    try:
        return int(datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").timestamp() - now)
    except Exception:
        return None


def main() -> int:
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    global DEMO
    if "--demo" in sys.argv:
        DEMO = True
    if not UI.exists():
        print("找不到界面文件：%s" % UI, file=sys.stderr)
        return 1
    api = Api()
    window = webview.create_window(
        "天软校园活动 · 报名值守台", str(UI),
        js_api=api, width=1120, height=800, min_size=(980, 700),
        background_color="#E9EEF6",
    )
    api._window = window
    webview.start(debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
