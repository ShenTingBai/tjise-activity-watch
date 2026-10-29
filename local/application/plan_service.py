# -*- coding: utf-8 -*-
"""计划任务用例：单活动 worker 的启动、监督与终态收尾。

端口全部显式注入（worker 命令、环境、进程树创建、关闭判定、学校时间换算、
时钟不可用异常类型），application 不碰 Windows API 与具体路径，也不直接
依赖 business_clock/time_contract 等混合具体模块；进程树所有权在
infrastructure.process_tree。取消只终止对应进程，不影响同批其他任务。

@author LiXinwen
"""
from __future__ import annotations

import queue
import subprocess
import threading
import time

from domain.errors import ClosingError
from domain.plan_rules import classify_task_stop
from domain.retry_rules import ATTEMPT_RE, attempts_of, parse_retry_line
from domain.time_text import task_deadline
from domain.worker_lines import parse_checkpoint_line


def run_plan_task(spec, cancel_event: threading.Event, status: callable, *,
                  emit, lock, plan_procs: dict, clock_now, is_closing,
                  worker_command, env, spawn, cwd: str, grace_s: float,
                  epoch_of, time_text_of, clock_unavailable) -> dict:
    """运行一个单活动脚本；取消只终止对应进程，不影响同批其他任务。

    emit(kind, payload) 发结构化事件；lock/plan_procs 是 Api 的进程登记表；
    spawn(args, cwd=..., env=...) 创建并取得进程树所有权（infrastructure）。
    epoch_of/time_text_of 是学校时间换算纯函数（app.py 注入 time_contract）；
    clock_unavailable 是业务时钟读数失败的异常类型（app.py 注入 business_clock）。
    """
    # 已开放的场次不能再把原开放时刻传给脚本：grab.py 的重试窗口
    # 是「at + 120 秒」，开放超过 2 分钟的 at 会让循环一次都不进、
    # 直接报「名额已满」。这里把过期的 at 换成现在，窗口从现在起算。
    at_text = spec.sign_start
    # 学校时间一律按 UTC+8 解析：本机时区不同也不能差 8 小时
    at_ts = epoch_of(spec.sign_start)
    # 是否「已开放」只由业务北京时间判断：错误 OS 日期不得把未来场次改写成现在
    try:
        business_now = clock_now()
    except clock_unavailable:
        business_now = None
    if at_ts is not None and business_now is not None and at_ts <= business_now:
        # 已开放场次把 --at 换成「现在」只是让等待逻辑立即通过；
        # 窗口长度由下面冻结的 --retry-start 决定，不随这一刻后移
        at_text = time_text_of(epoch=business_now)
    elif at_ts is None and business_now is not None and not spec.sign_start:
        at_text = time_text_of(epoch=business_now)
    # 业务时刻读不到时保持原定开放：worker 会按自己的时钟门禁收成
    # clock_unavailable，绝不把系统日期当成北京时间
    args = worker_command("grab", "--activity-id", spec.activity_id, "--name", spec.name,
                          "--at", at_text,
                          "--lead", str(max(0, int(spec.lead_s or 0))),
                          "--retry-for", str(spec.retry_for_s),
                          "--max-attempts", str(spec.max_attempts))
    if spec.sign_end:
        args += ["--sign-end", spec.sign_end]
    # 计划接纳时冻结的重试起点：worker 解码/鉴权/校时/开页都消耗这段预算，
    # 慢启动不会把用户设的窗口整体后移（R04）。前端不提交这个锚点。
    if getattr(spec, "retry_started_at", 0):
        args += ["--retry-start", "%.3f" % float(spec.retry_started_at)]
    if spec.headed:
        args.append("--headed")
    with lock:
        # 关闭屏障：创建与登记同临界区——关闭先发生则零创建（取消语义）；
        # 创建已开始则 shutdown 必等登记完成才能取快照，收走不会漏。
        # 创建本身也在这里完成：worker 先挂起、归属 Job、再恢复，所以
        # 任何进度回调与日志都发生在「所有权已确定」之后（FR02）。
        if is_closing():
            raise ClosingError("程序正在关闭，本场次未启动")
        tree = spawn(args, cwd=cwd, env=env())
        proc = tree.proc
        plan_procs[spec.task_id] = proc
    if status and business_now is not None:
        # Popen 只是「父进程把 worker 拉起来了」，不是 worker 自己的入口时间：
        # run_started_at 必须等 [checkpoint] worker_started 到达再写（R10）。
        # 业务时刻读不到就不填，绝不拿系统时间补一个假节点。
        status("submitting", {"worker_spawned_at": business_now})
    # 任务入口文案按模式说清在等什么：预报名是「等开放」，立即模式是
    # 「抢名额」——不能让已经开放的活动显示成在等待（RC-04）。
    mode = str(getattr(spec, "signup_mode", "") or "")
    if mode == "pre_register":
        emit("log", "▶ [%s] 预报名：等待报名开放" % spec.name)
    elif mode == "instant_signup":
        emit("log", "▶ [%s] 立即报名：抢名额" % spec.name)
    else:
        emit("log", "▶ [%s] 等待并抢报名" % spec.name)

    # 父层监督：到期后收走本场进程树，不让「不 EOF 的管道」把任务永远挂着。
    # 所有权在进程恢复执行前就已取得（Job Object）：onefile 引导进程先退出时，
    # 解压出的真实 worker 仍属于这个 job，收走不依赖一个还活着的父 PID。
    supervised = {"fired": False}
    observed = {"attempts": 0}
    deadline = task_deadline(spec, epoch_of)
    cancel_watch = threading.Event()
    lines: "queue.Queue[str]" = queue.Queue()
    reader_done = threading.Event()
    tail: list[str] = []
    canceled = False
    killed = False

    def read_output() -> None:
        try:
            for line in proc.stdout or []:
                lines.put(line.rstrip("\r\n"))
        finally:
            reader_done.set()

    reader = threading.Thread(target=read_output, name="signup-output", daemon=True)
    reader.start()

    def _watchdog() -> None:
        """到「有效截止 + 清理余量」就收走本场树。

        等待用 Event 而不是 time.sleep：正常收尾/取消/关闭都在 finally
        置位，线程立刻退出，不把一个睡到截止的后台线程留在进程里。
        退出条件不看父进程是否还活着：父已退出、孙进程握着 stdout 时，
        仍然必须由这里结束监控。
        """
        if watch_delay is None:
            return
        if cancel_watch.wait(watch_delay):
            return
        supervised["fired"] = True
        tree.terminate()

    # 延迟只算一次，看门狗与主循环共用同一个上限：各算各的会在
    # 「截止已过」时出现主循环认为已超时、看门狗还在等 grace 的分叉
    # 监督上限按业务时刻换算成一次性延迟：重校向后纠正不得延长已接纳的时长。
    # 校时失效时退回「本场最长可能时长 + grace」的独立经过时长上限——
    # 只用于收走本场进程树，不重写树所有权，也不把系统时间当业务时刻。
    if deadline:
        if business_now is not None:
            watch_delay = max(0.0, deadline - business_now) + grace_s
        else:
            watch_delay = float(spec.retry_for_s or 120) + grace_s
    else:
        watch_delay = None
    watchdog = None
    if watch_delay is not None:
        watchdog = threading.Thread(target=_watchdog, name="signup-watchdog",
                                    daemon=True)
        watchdog.start()

    def feed(line: str) -> None:
        """输出行统一入口：落日志、存尾巴，并把源事件时间与遥测推成任务 detail。

        节点时间只认 worker 自己记录的 [checkpoint] 源时间：日志转发慢 2 秒，
        「页面已打开」也不会被记成晚 2 秒的接收时刻（R10）。没有结构化事件
        就不填，让界面老实显示「待打开/待发生」。
        """
        tail.append(line)
        del tail[:-40]
        emit("log", "[%s] %s" % (spec.name, line))
        checkpoint = parse_checkpoint_line(line)
        if checkpoint and status:
            kind, at = checkpoint
            field = {"worker_started": "run_started_at",
                     "page_opened": "page_opened_at",
                     "watch_started": "watch_started_at"}[kind]
            status("submitting", {field: at})
        hit = ATTEMPT_RE.match(line)
        if hit:
            observed["attempts"] = max(observed["attempts"], int(hit.group(1)))
            if status:
                # 只数真正发起 signUp 的行；刷新 / is_joined 查询不会长这样
                status("submitting", {"attempts": int(hit.group(1)),
                                      "last_reason": hit.group(2).strip()})
        parsed = parse_retry_line(line)
        if parsed:
            # 结构化提交行也要计数：人话行可能被日志转发吞掉，次数不能
            # 因此归零；两路按「已承认的最大值」归并，不重复累加。
            raw_attempts = parsed.get("attempts")
            if raw_attempts not in (None, ""):
                try:
                    observed["attempts"] = max(observed["attempts"], int(raw_attempts))
                except (TypeError, ValueError):
                    pass
        if parsed and status:
            # 窗口确定/请求发起都是进度行：先把截止与次数推上去，
            # 不必等进程收尾；终态行才带 stop_reason，由收尾逻辑消费
            payload = {}
            if parsed.get("retry_deadline"):
                payload["retry_deadline"] = str(parsed["retry_deadline"])
            if parsed.get("attempts") and not parsed.get("stop_reason"):
                try:
                    payload["attempts"] = int(parsed["attempts"])
                except (TypeError, ValueError):
                    pass
            if payload:
                status("submitting", payload)

    code = None
    try:
        hard_stop = (time.monotonic() + watch_delay) if watch_delay is not None else None
        # 正常收尾要求 reader_done（输出读完）；取消与父层监督只要求
        # 进程树死透——onefile 引导进程被收走后，真实 worker 可能还握着
        # stdout 管道，再等 EOF 这条循环就永远出不去，plan_done 也发不出去。
        while True:
            if cancel_event.is_set() and not killed:
                canceled = True
                killed = True
                tree.terminate()
            if proc.poll() is not None and (canceled or reader_done.is_set()):
                break
            if supervised["fired"]:
                # 监督已收走进程树：不再等 EOF，也不把管道当「还活着」的证据
                break
            if hard_stop is not None and time.monotonic() >= hard_stop:
                # 看门狗线程可能被调度延迟；主循环自己再守一次同一个上限
                supervised["fired"] = True
                tree.terminate()
                break
            try:
                while True:
                    line = lines.get_nowait()
                    if line:
                        feed(line)
            except queue.Empty:
                pass
            time.sleep(0.05)
        # 收走之后把队列里剩下的行读完（有界）：最后一条终态不能丢
        reader_done.wait(1.5)
        while True:
            try:
                line = lines.get_nowait()
            except queue.Empty:
                break
            if line:
                feed(line)
        try:
            code = proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            # 父进程不退出：树已终止还等不到就按已收走处理，绝不再等
            supervised["fired"] = True
            tree.terminate()
            try:
                code = proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                code = -1
    finally:
        # 监控循环无论怎么退出（含异常）都要：撤销监督、收走本场树、
        # 清进程表、关管道。顺序是硬要求——先置取消、再有界 join 监督
        # 线程，最后才终止/释放 Job 句柄：否则迟到的 watchdog 可能拿着
        # 已关闭（甚至已被复用）的句柄去 TerminateJobObject（FR02）。
        cancel_watch.set()
        if watchdog is not None:
            watchdog.join(timeout=2.0)
        try:
            tree.terminate()
        except Exception:
            pass
        reader.join(timeout=2.0)     # 有界 join：不在另一线程持读锁时同步 close
        if reader_done.is_set() and proc.stdout:
            try:
                proc.stdout.close()
            except Exception:
                pass
        try:
            tree.close()
        except Exception:
            pass
        with lock:
            plan_procs.pop(spec.task_id, None)
    # 结构化终态行是策略信息的第一来源：只认带 stop_reason 的那一条，
    # 进度行（窗口确定/请求发起）不得冒充终态
    stop: dict = {}
    for line in reversed(tail):
        parsed = parse_retry_line(line)
        if parsed and parsed.get("stop_reason"):
            stop = parsed
            break

    # 终态分类（纯策略）：已获得的证据优先于本地怎么结束的（FR03）
    attempts_seen = max(observed["attempts"], attempts_of(stop))
    return classify_task_stop(spec, code, tail, canceled,
                              supervised["fired"], stop, attempts_seen,
                              epoch_of)
