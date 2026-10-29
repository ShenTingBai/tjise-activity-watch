#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手机重登 worker 侧编排测试：fake page/input/token/post/pipe/clock。

不启动真实浏览器、不读实际 session、不连学校；凭据绝不落盘、不进 HTTP。

@author LiXinwen
"""
from __future__ import annotations

import io
import json
import queue
import sys
import threading
import unittest
from pathlib import Path

LOCAL = Path(__file__).resolve().parents[1]
if str(LOCAL) not in sys.path:
    sys.path.insert(0, str(LOCAL))

import login_relay  # noqa: E402
from login_relay import (  # noqa: E402
    ChallengeBook, extract_captcha_image, fill_login_form, parse_client_line,
    run_relay, valid_code,
)

PNG_B64 = ("data:image/png;base64,"
           "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
           "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


class FakeStdin:
    """管道替身：行由测试写入，close 后 reader 收 EOF。"""

    def __init__(self):
        self._lines = queue.Queue()

    def write_line(self, text):
        self._lines.put(str(text))

    def close(self):
        self._lines.put(None)

    def __iter__(self):
        return self

    def __next__(self):
        item = self._lines.get()
        if item is None:
            raise StopIteration
        return item


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    def nth(self, index):
        self.page.nth_calls.append((self.selector, index))
        return self

    def fill(self, text):
        self.page.fills.append((self.selector, text))

    def click(self):
        self.page.clicks.append(self.selector)
        if self.selector == login_relay.SUBMIT_SELECTOR and self.page.on_submit:
            self.page.on_submit()


class FakeResponse:
    def __init__(self, url, payload):
        self.url = url
        self._body = json.dumps(payload).encode("utf-8")

    def body(self):
        return self._body


class FakeCtx:
    def __init__(self):
        self.closed = False

    def storage_state(self):
        return {"cookies": []}

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakePage:
    def __init__(self):
        self.fills = []
        self.clicks = []
        self.nth_calls = []
        self.handlers = {}
        self.closed = False
        self.token = ""
        self.storage = {}
        self.on_submit = None
        self.on_goto = None
        self.fail_locate = False
        self.clock = None

    def locator(self, selector):
        if self.fail_locate:
            raise RuntimeError("no such element: %s" % selector)
        return FakeLocator(self, selector)

    def on(self, event, fn):
        self.handlers[event] = fn

    def goto(self, url):
        self.gone_to = url
        if self.on_goto:
            self.on_goto()

    def is_closed(self):
        return self.closed

    def wait_for_timeout(self, ms):
        if self.clock is not None:
            self.clock["now"] += max(0.0, ms / 1000.0)

    def evaluate(self, js):
        if "getItem('%s')" % login_relay.TOKEN_KEY in js or "xgUniToken" in js:
            return self.token
        return dict(self.storage)

    def emit_response(self, url, payload):
        self.handlers["response"](FakeResponse(url, payload))


class FakeDeps:
    login_url = "http://school.example/stu/#/login"

    def __init__(self):
        self.clock = {"now": 100.0}
        self.page = FakePage()
        self.page.clock = self.clock
        self.ctx = FakeCtx()
        self.browser = FakeBrowser()
        self.posts = []
        self.post_result = {"code": "1"}
        self.sessions = []
        self.states = []
        self.ids = 0
        self.fail_open = False

    def now(self):
        return self.clock["now"]

    def sleep(self, seconds):
        self.clock["now"] += float(seconds)

    def new_id(self):
        self.ids += 1
        return "chg-%d" % self.ids

    def open_page(self):
        if self.fail_open:
            raise RuntimeError("no browser")
        return self.page, self.ctx, self.browser

    def post(self, path, payload, tok):
        self.posts.append((path, payload, tok))
        return self.post_result

    def save_session(self, session):
        self.sessions.append(session)

    def save_state(self, state):
        self.states.append(state)


def run_scripted(deps, stdin, lines):
    """把脚本行先写进 FakeStdin 再跑 run_relay；返回 (code, 输出行)。"""
    for line in lines:
        stdin.write_line(json.dumps(line, ensure_ascii=False))
    out = io.StringIO()
    code = run_relay(deps=deps, stdin=stdin, stdout=out)
    stdin.close()          # 放行 reader 线程退出（生产里父进程握着管道）
    parsed = []
    for raw in out.getvalue().splitlines():
        if raw.startswith(login_relay.LOGIN_PREFIX):
            parsed.append(json.loads(raw[len(login_relay.LOGIN_PREFIX):]))
    return code, parsed


def credentials(flow="flow-1"):
    return {"kind": "credentials", "flow_id": flow,
            "username": "user-x", "password": "secret-y"}


class PureRuleTests(unittest.TestCase):
    def test_valid_code_requires_four_alnum(self):
        self.assertTrue(valid_code("Ab12"))
        self.assertFalse(valid_code("abc"))
        self.assertFalse(valid_code("Ab123"))
        self.assertFalse(valid_code("ab 1"))
        self.assertFalse(valid_code(""))

    def test_parse_client_line_rejects_garbage(self):
        self.assertIsNone(parse_client_line("not-json"))
        self.assertIsNone(parse_client_line("[1,2]"))
        self.assertIsNone(parse_client_line("x" * 5000))
        self.assertEqual(parse_client_line('{"kind":"cancel"}')["kind"], "cancel")

    def test_extract_captcha_image_requires_png(self):
        self.assertEqual(extract_captcha_image({"data": {"image": PNG_B64}}), PNG_B64)
        self.assertIsNone(extract_captcha_image({"data": {"image": "data:image/png;base64,zzz"}}))
        self.assertIsNone(extract_captcha_image({"data": {"image": "data:image/jpeg;base64,AAAA"}}))
        self.assertIsNone(extract_captcha_image({"data": {}}))
        self.assertIsNone(extract_captcha_image(None))

    def test_fill_login_form_uses_server_selectors(self):
        page = FakePage()
        fill_login_form(page, "u", "p", "Ab12")
        self.assertEqual(page.nth_calls, [("input", 0), ("input", 1), ("input", 2)])
        self.assertEqual(page.clicks, ["uni-checkbox", "uni-button.view-form-box-btn"])
        self.assertEqual([text for _sel, text in page.fills], ["u", "p", "Ab12"])


class ChallengeBookTests(unittest.TestCase):
    def test_accepts_once_then_consumed(self):
        book = ChallengeBook("flow-1")
        self.assertTrue(book.open_challenge("chg-1", 10.0))
        ok, err = book.accept("flow-1", "chg-1", "Ab12", 10.0)
        self.assertTrue(ok)
        again, err = book.accept("flow-1", "chg-1", "Ab12", 10.0)
        self.assertFalse(again)
        self.assertEqual(err, "consumed")

    def test_stale_flow_and_challenge_rejected(self):
        book = ChallengeBook("flow-1")
        book.open_challenge("chg-1", 10.0)
        self.assertEqual(book.accept("other", "chg-1", "Ab12", 10.0)[1], "stale_flow")
        self.assertEqual(book.accept("flow-1", "chg-9", "Ab12", 10.0)[1], "stale_challenge")

    def test_expired_and_bad_code_rejected(self):
        book = ChallengeBook("flow-1", ttl_s=60)
        book.open_challenge("chg-1", 10.0)
        self.assertEqual(book.accept("flow-1", "chg-1", "Ab12", 71.0)[1], "expired")
        book.open_challenge("chg-2", 100.0)
        self.assertEqual(book.accept("flow-1", "chg-2", "ab!", 100.0)[1], "bad_code")

    def test_refresh_gap_blocks_rapid_reopen(self):
        book = ChallengeBook("flow-1", refresh_gap_s=3.0)
        self.assertTrue(book.open_challenge("chg-1", 10.0))
        self.assertFalse(book.open_challenge("chg-2", 11.0))
        self.assertTrue(book.open_challenge("chg-3", 13.0))

    def test_max_tries_stops_submissions(self):
        book = ChallengeBook("flow-1", max_tries=3, refresh_gap_s=0.0)
        for index in range(3):
            book.open_challenge("chg-%d" % index, 10.0 + index)
            self.assertTrue(book.accept("flow-1", "chg-%d" % index, "Ab12",
                                        10.0 + index)[0])
        book.open_challenge("chg-9", 20.0)
        self.assertEqual(book.accept("flow-1", "chg-9", "Ab12", 20.0)[1], "max_tries")


class RelaySessionTests(unittest.TestCase):
    def _happy(self, deps, *, token_after="new-token-1", login_code=1):
        page = deps.page
        page.storage = {"user": json.dumps({"xm": "张三", "xh": "1"}), "xm": "张三"}

        def on_goto():
            page.emit_response("http://school.example/api/captcha",
                               {"data": {"image": PNG_B64}})

        def on_submit():
            page.emit_response("http://school.example/api/login",
                               {"code": login_code, "msg": "ok"})
            page.token = token_after

        page.on_goto = on_goto
        page.on_submit = on_submit
        return page

    def _submit(self, code="Ab12", flow="flow-1", challenge="chg-1"):
        return {"kind": "submit", "flow_id": flow, "challenge_id": challenge,
                "code": code}

    def test_success_requires_new_token_and_verified_post(self):
        deps = FakeDeps()
        self._happy(deps)
        stdin = FakeStdin()
        code, lines = run_scripted(deps, stdin, [credentials(), self._submit()])
        self.assertEqual(code, 0)
        self.assertTrue(any(line.get("kind") == "success" for line in lines))
        self.assertEqual(deps.posts[0][2], "new-token-1")
        self.assertEqual(deps.sessions[0]["token"], "new-token-1")
        self.assertEqual(deps.sessions[0]["name"], "张三")

    def test_old_token_passing_probe_is_not_success(self):
        """旧 token 轮询成功不得冒充新登录成功。"""
        deps = FakeDeps()
        page = self._happy(deps, token_after="old-token")
        page.token = "old-token"          # 开页前就有旧 token
        stdin = FakeStdin()
        code, lines = run_scripted(deps, stdin, [credentials(), self._submit()])
        self.assertEqual(code, 3)
        self.assertEqual(lines[-1]["error"], "token_unverified")
        self.assertEqual(deps.sessions, [], "失败不得改写旧会话")
        self.assertEqual(deps.states, [])

    def test_new_token_failing_probe_is_not_success(self):
        deps = FakeDeps()
        self._happy(deps)
        deps.post_result = {"code": 4001}
        stdin = FakeStdin()
        code, lines = run_scripted(deps, stdin, [credentials(), self._submit()])
        self.assertEqual(code, 3)
        self.assertEqual(lines[-1]["error"], "token_unverified")
        self.assertEqual(deps.sessions, [])
        self.assertEqual(deps.states, [], "校验失败不得覆盖 storage_state")

    def test_login_response_required_before_success(self):
        """只有 captcha、没有 login 应答：不得凭旧 token 成功。"""
        deps = FakeDeps()
        page = deps.page
        page.token = "old"

        def on_goto():
            page.emit_response("http://school.example/api/captcha",
                               {"data": {"image": PNG_B64}})

        page.on_goto = on_goto
        stdin = FakeStdin()
        code, lines = run_scripted(deps, stdin, [
            credentials(),
            {"kind": "cancel", "flow_id": "flow-1"},
        ])
        self.assertEqual(code, 5)
        self.assertEqual(deps.sessions, [])

    def test_credentials_never_appear_in_output(self):
        deps = FakeDeps()
        self._happy(deps)
        stdin = FakeStdin()
        _, lines = run_scripted(deps, stdin, [credentials(), self._submit()])
        text = json.dumps(lines, ensure_ascii=False)
        self.assertNotIn("secret-y", text)
        self.assertNotIn("user-x", text)
        self.assertNotIn("new-token-1", text)

    def test_bad_page_structure_fails_safely(self):
        deps = FakeDeps()
        page = self._happy(deps)
        page.fail_locate = True
        stdin = FakeStdin()
        code, lines = run_scripted(deps, stdin, [credentials(), self._submit()])
        self.assertEqual(code, 2)
        self.assertEqual(lines[-1]["error"], "bad_page")
        self.assertEqual(deps.sessions, [], "页面结构不符不得假成功")

    def test_open_page_failure_and_cancel_and_eof_cleanup(self):
        deps = FakeDeps()
        deps.fail_open = True
        stdin = FakeStdin()
        code, lines = run_scripted(deps, stdin, [credentials()])
        self.assertEqual(code, 2)
        self.assertEqual(lines[-1]["error"], "bad_page")

        deps = FakeDeps()
        self._happy(deps)
        stdin = FakeStdin()
        code, lines = run_scripted(deps, stdin, [
            credentials(),
            {"kind": "cancel", "flow_id": "flow-1"},
        ])
        self.assertEqual(code, 5)
        self.assertTrue(deps.ctx.closed and deps.browser.closed)

        deps = FakeDeps()
        stdin = FakeStdin()
        stdin.close()                    # 父进程先关管道：worker 必须收口
        code, lines = run_scripted(deps, stdin, [])
        self.assertEqual(code, 1)
        self.assertEqual(lines[-1]["error"], "eof")

    def test_stale_and_illegal_codes_are_rejected(self):
        deps = FakeDeps()
        self._happy(deps)
        stdin = FakeStdin()
        code, lines = run_scripted(deps, stdin, [
            credentials(),
            self._submit(code="Ab12", flow="old-flow"),
            self._submit(code="xy!"),
            self._submit(code="Ab12"),
        ])
        kinds = [(line.get("kind"), line.get("reason")) for line in lines]
        self.assertIn(("rejected", "stale_flow"), kinds)
        self.assertIn(("rejected", "bad_code"), kinds)
        self.assertTrue(any(kind == "success" for kind, _ in kinds))

    def test_expired_challenge_is_rejected(self):
        deps = FakeDeps()
        deps.challenge_ttl_s = -1.0        # 一开即过期
        self._happy(deps)
        stdin = FakeStdin()
        code, lines = run_scripted(deps, stdin, [credentials(), self._submit()])
        self.assertIn(("rejected", "expired"),
                      [(line.get("kind"), line.get("reason")) for line in lines])
        self.assertEqual(deps.sessions, [])

    def test_duplicate_code_and_old_challenge_rejected(self):
        deps = FakeDeps()
        self._happy(deps)
        stdin = FakeStdin()
        code, lines = run_scripted(deps, stdin, [
            credentials(),
            self._submit(code="Ab12"),
            self._submit(code="Bb34"),          # 旧挑战已消费
        ])
        kinds = [(line.get("kind"), line.get("reason")) for line in lines]
        self.assertIn(("rejected", "consumed"), kinds)
        self.assertTrue(any(kind == "success" for kind, _ in kinds))
