"""手动更新纯逻辑与桥接契约；所有HTTP均为替身，不访问学校或GitHub。

@author LiXinwen
"""
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

LOCAL = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
if str(LOCAL) not in sys.path:
    sys.path.insert(0, str(LOCAL))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
import update_check as updates
import app as local_app
from fixtures.business_clock import TestWallClock  # noqa: E402


def release(tag="v1.1.0", **extra):
    return {"tag_name": tag, "draft": False, "prerelease": False, **extra}


class ReleaseComparisonTests(unittest.TestCase):
    def test_numeric_comparison(self):
        self.assertEqual(updates.evaluate_latest_release("1.9.9", release("v1.10.0"))["state"], "available")

    def test_same_version(self):
        self.assertEqual(updates.evaluate_latest_release("1.1.0", release())["state"], "current")

    def test_preview_never_suggests_downgrade(self):
        result = updates.evaluate_latest_release("1.1.0", release("v1.0.2"))
        self.assertEqual(result["state"], "preview")
        self.assertIn("预览", result["text"])

    def test_v_prefix_and_fixed_public_url(self):
        result = updates.evaluate_latest_release("1.0.0", release("1.1.0", html_url="https://invalid.example"))
        self.assertEqual(result["latest_version"], "1.1.0")
        self.assertEqual(result["url"], updates.LATEST_URL)

    def test_invalid_versions_and_payloads(self):
        for tag in (None, "1.1", "1.1.0-beta", "v01.1.0", "anything", "1.2.3.4", "9" * 100):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                updates.evaluate_latest_release("1.1.0", release(tag))
        for payload in ([], None, {}, release(draft=True), release(prerelease=True)):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                updates.evaluate_latest_release("1.1.0", payload)


class UpdateNetworkTests(unittest.TestCase):
    def request(self, body=None, error=None):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = body if body is not None else json.dumps(release()).encode()
        with patch.object(updates, "urlopen", return_value=response, side_effect=error) as fetch:
            result = updates.check_latest_release("1.1.0")
        return result, fetch, response

    def test_one_request_fixed_endpoint_and_bounded_read(self):
        result, fetch, response = self.request()
        fetch.assert_called_once()
        request = fetch.call_args.args[0]
        self.assertEqual(request.full_url, updates.LATEST_API)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(fetch.call_args.kwargs["timeout"], 8)
        self.assertNotIn("Authorization", request.headers)
        response.read.assert_called_once_with(updates.MAX_BYTES + 1)
        self.assertEqual(result["state"], "current")

    def test_no_release(self):
        result, _, _ = self.request(error=HTTPError(updates.LATEST_API, 404, "", {}, io.BytesIO()))
        self.assertEqual(result["state"], "unavailable")

    def test_rate_limit(self):
        for code in (403, 429):
            result, _, _ = self.request(error=HTTPError(updates.LATEST_API, code, "", {}, io.BytesIO()))
            self.assertEqual(result["error_code"], "rate_limited")

    def test_other_http_error(self):
        result, _, _ = self.request(error=HTTPError(updates.LATEST_API, 500, "", {}, io.BytesIO()))
        self.assertEqual(result["error_code"], "http_error")

    def test_timeout_and_network(self):
        for error, code in ((TimeoutError(), "timeout"), (URLError(TimeoutError()), "timeout"),
                            (URLError("offline"), "network_error"), (OSError(), "network_error")):
            with self.subTest(error=error):
                result, fetch, _ = self.request(error=error)
                self.assertEqual(result["state"], "error")
                self.assertEqual(result["error_code"], code)
                fetch.assert_called_once()

    def test_invalid_json_and_oversize(self):
        for body in (b"not json", b"[]", b"\xff", b"x" * (updates.MAX_BYTES + 1)):
            with self.subTest(size=len(body)):
                result, _, _ = self.request(body=body)
                self.assertEqual(result["error_code"], "invalid_response")


class UpdateBridgeTests(unittest.TestCase):
    def setUp(self):
        self.api = local_app.Api(clock=TestWallClock())
        self.api._emit = Mock()

    def test_constructor_and_bootstrap_do_not_query(self):
        with patch.object(local_app, "check_latest_release") as fetch, \
             patch.object(local_app, "load_prefs", return_value={}), \
             patch.object(local_app, "login_state", return_value={}), \
             patch.object(local_app, "get_lifecycle") as lifecycle:
            lifecycle.return_value.snapshot.return_value = {}
            info = self.api.bootstrap()
        fetch.assert_not_called()
        self.assertFalse(info["update_check"]["checking"])

    def test_immediate_ack_single_flight(self):
        with patch.object(local_app.threading, "Thread") as thread, patch.object(local_app, "check_latest_release") as fetch:
            first = self.api.check_update("first")
            second = self.api.check_update("second")
        self.assertTrue(first["queued"])
        self.assertEqual(second["request_id"], "first")
        self.assertTrue(second["skipped"])
        thread.assert_called_once()
        fetch.assert_not_called()

    def test_result_correlation_and_finished_snapshot(self):
        self.api._updating = True
        self.api._update_request_id = "request"
        expected = updates.evaluate_latest_release("1.1.0", release())
        with patch.object(local_app, "check_latest_release", return_value=expected):
            self.api._do_check_update("request")
        self.assertFalse(self.api._updating)
        self.assertEqual(self.api._update_result["request_id"], "request")
        self.api._emit.assert_called_once_with("update", self.api._update_result)

    def test_unexpected_error_or_bad_checker_return_unlocks(self):
        for options in ({"side_effect": RuntimeError()}, {"return_value": None}):
            with self.subTest(options=options):
                self.api._updating = True
                with patch.object(local_app, "check_latest_release", **options):
                    self.api._do_check_update("request")
                self.assertFalse(self.api._updating)
                self.assertEqual(self.api._update_result["state"], "error")

    def test_thread_start_failure_unlocks(self):
        with patch.object(local_app.threading, "Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError()
            result = self.api.check_update("request")
        self.assertFalse(result["ok"])
        self.assertFalse(self.api._updating)

    def test_close_and_invalid_id_reject_without_thread(self):
        with patch.object(local_app.threading, "Thread") as thread:
            self.assertFalse(self.api.check_update("bad id")["ok"])
            self.api._closing = True
            self.assertFalse(self.api.check_update("request")["ok"])
        thread.assert_not_called()

    def test_closing_drops_result_event(self):
        self.api._closing = True
        with patch.object(local_app, "check_latest_release", return_value={"state": "current"}):
            self.api._do_check_update("request")
        self.api._emit.assert_not_called()
        self.assertFalse(self.api._updating)


if __name__ == "__main__":
    unittest.main()
