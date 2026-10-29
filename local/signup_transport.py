"""直发传输：urllib 守护线程，物理并发 <= 2，无等待队列，禁自动 redirect/重试。

只负责把一笔已签名的 GET signUp 发出去并回一个安全事件（不含 token / 完整 URL）。
线程启动失败 / 关闭 / 过期的 ticket 不产生 started，因而不耗调用方的尝试次数。
逻辑名额 TTL（释放 pending 名额）由调度账本（BurstPolicy）负责；本层只保证物理
并发不越过 active_count 上限，不因 TTL 而放行新的物理请求。
"""
from __future__ import annotations

import math
import queue
import threading
import time
import urllib.error
import urllib.request

# 物理并发上限（与调度账本 MAX_INFLIGHT 一致）
MAX_PHYSICAL_INFLIGHT = 2
# 单请求超时硬上限（秒）
MAX_TIMEOUT_S = 8.0


def _build_default_opener():
    """构造不自动跟随重定向的 opener：3xx 原样返回，不重试。"""

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None  # 不跟随，交给上层按 3xx 原样处理

    return urllib.request.build_opener(_NoRedirect)


class DirectTransport:
    """已签名直发传输：try_submit 立即返回，物理满额 / 关闭 / 重复则拒绝。"""

    def __init__(self, event_queue, opener=None, monotonic_fn=time.monotonic):
        self._queue = event_queue if event_queue is not None else queue.Queue()
        self._opener = opener if opener is not None else _build_default_opener()
        self._monotonic = monotonic_fn
        self._lock = threading.Lock()
        self._active = set()  # 在物理在途的 request_id
        self._seen = set()  # 已接纳过的 request_id（拒重）
        self._closed = False

    # ---- 对外接口 ----

    def try_submit(self, ticket: dict) -> bool:
        """仅在未关闭、未重复、有物理空槽时立即启动守护线程；否则返回 False。"""
        request_id = ticket["request_id"]
        with self._lock:
            if self._closed:
                return False
            if request_id in self._seen:
                return False  # 重复 request_id 拒绝
            if len(self._active) >= MAX_PHYSICAL_INFLIGHT:
                return False  # 物理满额：无隐藏等待队列
            self._seen.add(request_id)
            self._active.add(request_id)
        thread = threading.Thread(target=self._run, args=(ticket,), daemon=True)
        try:
            thread.start()
        except RuntimeError:
            # 线程启动失败：不产生 started，不耗 attempts，回收名额
            with self._lock:
                self._active.discard(request_id)
            return False
        return True

    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def close(self) -> None:
        """停止接纳新 ticket；不等待在途线程，不假装取消已发出的请求。"""
        with self._lock:
            self._closed = True

    # ---- 内部 ----

    def _emit(self, ticket: dict, kind: str, **extra) -> None:
        event = {
            "kind": kind,
            "run_id": ticket.get("run_id"),
            "request_id": ticket.get("request_id"),
            "attempt_no": ticket.get("attempt_no"),
        }
        event.update(extra)
        self._queue.put(event)

    def _run(self, ticket: dict) -> None:
        request_id = ticket["request_id"]
        committed = False
        try:
            # 接纳提交点（F02/F04 返修）：单调读数、截止/有效性检查、timeout
            # 计算与 closed 检查全部在锁内完成并紧邻 started 入队——线程等锁
            # 期间越过 dispatch/request 截止时必须用新鲜读数判废，不得沿用旧
            # now 发射。close 用同一把锁，close 返回后不再产生新的 started；
            # 锁不覆盖后面的阻塞 open/read。
            with self._lock:
                now = self._monotonic()
                try:
                    timeout_s = self._effective_timeout(ticket, now)
                    invalid = (not math.isfinite(timeout_s) or timeout_s <= 0
                               or now >= float(ticket["dispatch_deadline"])
                               or now >= float(ticket["request_deadline"]))
                except Exception:
                    timeout_s = 0.0
                    invalid = True
                if self._closed or invalid:
                    self._emit(ticket, "not_started")
                    return
                self._emit(ticket, "started")
                committed = True
            self._perform(ticket, timeout_s)
        except Exception as exc:
            # 提交点后任何未预期异常必须补安全事件，不能 started 后无事件
            if committed:
                self._emit(ticket, "failed", error_class=type(exc).__name__)
            else:
                self._emit(ticket, "not_started")
        finally:
            with self._lock:
                self._active.discard(request_id)

    def _effective_timeout(self, ticket: dict, now: float) -> float:
        """网络 timeout = min(票据预算, 8 秒硬上限, request_deadline 剩余)。

        不再用 dispatch_deadline 剩余当网络 timeout（F02）：发射窗只限最晚
        启动，不限响应等待；也不延长原捕获预算。
        """
        cand = float(ticket.get("timeout_s", MAX_TIMEOUT_S))
        budget_left = float(ticket["request_deadline"]) - now
        if math.isnan(cand) or math.isnan(budget_left):
            return float("nan")
        return min(cand, MAX_TIMEOUT_S, budget_left)

    def _perform(self, ticket: dict, timeout_s: float) -> None:
        try:
            request = urllib.request.Request(
                ticket["url"], headers=ticket.get("headers", {}), method="GET",
            )
        except Exception as exc:
            # 构造 Request 异常也要给安全事件（started 已提交）
            self._emit(ticket, "failed", error_class=type(exc).__name__)
            return
        try:
            response = self._opener.open(request, timeout=timeout_s)
        except urllib.error.HTTPError as exc:
            # HTTPError 也读 status / body，走 completed（含 3xx 原样）
            self._complete_from(ticket, exc)
            return
        except Exception as exc:
            # 网络异常：failed，只报安全错误类别（不含 URL / token）
            self._emit(ticket, "failed", error_class=type(exc).__name__)
            return
        self._complete_from(ticket, response)

    def _complete_from(self, ticket: dict, response) -> None:
        try:
            status = int(getattr(response, "status", None) or getattr(response, "code", 200))
            body = response.read()
        except Exception as exc:
            self._emit(ticket, "failed", error_class=type(exc).__name__)
            return
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        self._emit(ticket, "completed", http_status=status, body_bytes=body)
