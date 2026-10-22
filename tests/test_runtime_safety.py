"""运行时安全回归（离线）。

这里只跑「边界语义」：owner 绑定、通知成败判定、去重基线与扫描分批。
所有外部依赖（配置、会话、状态文件、HTTP、子进程）都在测试里打桩，
真正调用的是 notify / run / scan 里的生产函数，而不是复写的纯函数。

隔离约定：
- 任何 fixture 只写 D:/@Temp/tjise-runtime-hardening-20261020/（可用
  TJISE_TEST_ROOT 覆盖），不碰仓库里的 config.json 与 data/；
- urllib 的 urlopen 在进程内被替换成直接抛错，漏出的网络调用会让测试失败；
- 测试结束恢复所有环境变量与 mock。

@author LiXinwen
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import card  # noqa: E402
import notify  # noqa: E402
import render  # noqa: E402
import run  # noqa: E402
import scan  # noqa: E402

PREFERRED_ROOT = "D:/@Temp/tjise-runtime-hardening-20261020"
OWNER = "ou_owner_fixture"
STRANGER = "ou_stranger_fixture"


def _test_root() -> Path:
    root = Path(os.environ.get("TJISE_TEST_ROOT") or (
        PREFERRED_ROOT if os.name == "nt" else os.path.join(tempfile.gettempdir(),
                                                            "tjise-runtime-hardening")))
    root.mkdir(parents=True, exist_ok=True)
    return root


def make_tmpdir(prefix: str = "safety-") -> str:
    return tempfile.mkdtemp(prefix=prefix, dir=str(_test_root()))


def write_json(path: str, value) -> str:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False)
    return path


def read_json(path: str):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def p2p_record(chat_id: str, open_id: str = OWNER) -> dict:
    return {"open_id": open_id, "chat_type": "p2p", "chat_id": chat_id,
            "seen_at": "2026-10-20T00:00:00Z"}


def _forbid_network(*args, **kwargs):
    raise AssertionError("离线测试不允许发起网络请求")


def setUpModule():
    # 任何漏出的真实请求都直接失败，而不是悄悄连出去
    patcher = mock.patch("urllib.request.urlopen", _forbid_network)
    patcher.start()
    globals()["_network_patcher"] = patcher


def tearDownModule():
    patcher = globals().pop("_network_patcher", None)
    if patcher is not None:
        patcher.stop()


class OwnerBindingTests(unittest.TestCase):
    """私人收件人只能来自 owner 本人，且必须与显式配置一致。"""

    def setUp(self):
        self.tmp = make_tmpdir("owner-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def cfg(self, users: dict = None, chat_id: str = "") -> dict:
        users_file = ""
        if users is not None:
            users_file = write_json(os.path.join(self.tmp, "users.json"), users)
        return {
            "channel": "webhook",
            "platform": "feishu",
            "feishu_app": {"app_id": "cli_fixture", "app_secret": "secret_fixture"},
            "users_file": users_file,
            "private_chat_id": chat_id,
        }

    def owner_chat(self, cfg: dict) -> str:
        with mock.patch.dict(os.environ, {"PRIVATE_OWNER_OPEN_ID": OWNER}):
            return notify._p2p_chat_id(cfg)

    def test_stranger_first_does_not_win(self):
        # 陌生人先私聊、owner 后私聊：必须仍然解析到 owner 的会话
        users = {
            STRANGER: p2p_record("oc_stranger", STRANGER),
            OWNER: p2p_record("oc_owner"),
        }
        self.assertEqual(self.owner_chat(self.cfg(users)), "oc_owner")

    def test_missing_owner_record(self):
        users = {STRANGER: p2p_record("oc_stranger", STRANGER)}
        self.assertEqual(self.owner_chat(self.cfg(users)), "")

    def test_record_open_id_mismatch(self):
        users = {OWNER: p2p_record("oc_owner", STRANGER)}
        self.assertEqual(self.owner_chat(self.cfg(users)), "")

    def test_owner_record_is_group(self):
        rec = p2p_record("oc_group")
        rec["chat_type"] = "group"
        self.assertEqual(self.owner_chat(self.cfg({OWNER: rec})), "")

    def test_owner_record_without_chat_id(self):
        rec = p2p_record("")
        self.assertEqual(self.owner_chat(self.cfg({OWNER: rec})), "")

    def test_explicit_chat_id_conflict(self):
        users = {OWNER: p2p_record("oc_owner")}
        self.assertEqual(self.owner_chat(self.cfg(users, chat_id="oc_other")), "")

    def test_explicit_chat_id_matches(self):
        users = {OWNER: p2p_record("oc_owner")}
        self.assertEqual(self.owner_chat(self.cfg(users, chat_id="oc_owner")), "oc_owner")

    def test_owner_env_missing(self):
        users = {OWNER: p2p_record("oc_owner")}
        with mock.patch.dict(os.environ, {"PRIVATE_OWNER_OPEN_ID": ""}):
            self.assertEqual(notify._p2p_chat_id(self.cfg(users)), "")

    def test_users_file_unreadable(self):
        cfg = self.cfg(None)
        cfg["users_file"] = os.path.join(self.tmp, "missing.json")
        self.assertEqual(self.owner_chat(cfg), "")

    def test_send_private_without_owner_skips_everything(self):
        cfg = self.cfg({OWNER: p2p_record("oc_owner")})
        with mock.patch.dict(os.environ, {"PRIVATE_OWNER_OPEN_ID": ""}), \
                mock.patch.object(notify, "load_config",
                                  return_value={"notify": cfg}), \
                mock.patch.object(notify, "_tenant_token") as token, \
                mock.patch.object(notify, "_post") as post:
            self.assertIsNone(notify.send_private("fixture"))
        token.assert_not_called()
        post.assert_not_called()

    def test_send_private_targets_owner_chat(self):
        cfg = self.cfg({STRANGER: p2p_record("oc_stranger", STRANGER),
                        OWNER: p2p_record("oc_owner")})
        with mock.patch.dict(os.environ, {"PRIVATE_OWNER_OPEN_ID": OWNER}), \
                mock.patch.object(notify, "load_config",
                                  return_value={"notify": cfg}), \
                mock.patch.object(notify, "_tenant_token",
                                  return_value="token_fixture") as token, \
                mock.patch.object(notify, "_post",
                                  return_value=(True, '{"code":0}')) as post:
            self.assertTrue(notify.send_private("fixture"))
        token.assert_called_once()
        self.assertEqual(post.call_count, 1)
        args, kwargs = post.call_args
        self.assertEqual(args[1]["receive_id"], "oc_owner")
        self.assertEqual(kwargs.get("token"), "token_fixture")

    def test_send_private_reports_failure(self):
        cfg = self.cfg({OWNER: p2p_record("oc_owner")})
        with mock.patch.dict(os.environ, {"PRIVATE_OWNER_OPEN_ID": OWNER}), \
                mock.patch.object(notify, "load_config",
                                  return_value={"notify": cfg}), \
                mock.patch.object(notify, "_tenant_token",
                                  return_value="token_fixture"), \
                mock.patch.object(notify, "_post",
                                  return_value=(True, '{"code":230001}')):
            self.assertFalse(notify.send_private("fixture"))


class NotificationResultTests(unittest.TestCase):
    """send_card 只有「卡片或兜底文本真正成功」才算 True。

    历史上卡片失败会退回文本，但返回值只看卡片那一步，
    于是「卡片失败 + 文本成功」被判成失败，「卡片失败 + 文本也失败」的返回
    又取决于分支写法。这里把三种组合都钉死。
    """

    def notifier(self, platform: str = "feishu", url: str = "https://example.invalid/hook"):
        return {"notify": {"channel": "webhook", "platform": platform,
                           "webhook_url": url,
                           "feishu_app": {"app_id": "cli_fixture",
                                          "app_secret": "secret_fixture"}}}

    def test_card_success(self):
        with mock.patch.object(notify, "load_config", return_value=self.notifier()), \
                mock.patch.object(notify, "_post",
                                  return_value=(True, '{"code":0}')) as post:
            self.assertIs(notify.send_card({"k": "v"}, "fallback"), True)
        self.assertEqual(post.call_count, 1)

    def test_card_failure_falls_back_to_text(self):
        responses = [(True, '{"code":19001}'), (True, '{"code":0}')]
        with mock.patch.object(notify, "load_config", return_value=self.notifier()), \
                mock.patch.object(notify, "_post", side_effect=responses):
            self.assertIs(notify.send_card({"k": "v"}, "fallback"), True)

    def test_card_and_text_both_fail(self):
        responses = [(True, '{"code":19001}'), (True, '{"code":230001}')]
        with mock.patch.object(notify, "load_config", return_value=self.notifier()), \
                mock.patch.object(notify, "_post", side_effect=responses):
            self.assertIs(notify.send_card({"k": "v"}, "fallback"), False)

    def test_card_failure_without_fallback(self):
        with mock.patch.object(notify, "load_config", return_value=self.notifier()), \
                mock.patch.object(notify, "_post", return_value=(True, '{}')):
            self.assertIs(notify.send_card({"k": "v"}, ""), False)

    def test_unconfigured_returns_none(self):
        with mock.patch.object(notify, "load_config",
                               return_value=self.notifier(url="")), \
                mock.patch.object(notify, "_post") as post:
            self.assertIsNone(notify.send_card({"k": "v"}, ""))
        post.assert_not_called()

    def test_non_feishu_with_fallback_uses_text(self):
        with mock.patch.object(notify, "load_config",
                               return_value=self.notifier(platform="dingtalk")), \
                mock.patch.object(notify, "_post",
                                  return_value=(True, '{"errcode":0}')) as post:
            self.assertIs(notify.send_card({"k": "v"}, "fallback"), True)
        self.assertEqual(post.call_count, 1)


SUMMARY_FIXTURE = {
    "pending": [
        {"id": "evt-1", "name": "活动一", "sign_start": "2026-10-21 19:00",
         "sign_end": "2026-10-22 19:00", "type": "讲座"},
        {"id": "evt-2", "name": "活动二", "sign_start": "2026-10-22 19:00",
         "sign_end": "2026-10-23 19:00", "type": "实践"},
    ],
    "open_count": 0,
    "generated_at": "2026-10-20 15:00:00",
}


class StateStub:
    """内存版 state.json：load 返回副本，save 覆盖，update 走 mutator。"""

    def __init__(self, initial: dict = None):
        self.data = json.loads(json.dumps(initial or {}))
        self.saves = 0

    def load(self):
        return json.loads(json.dumps(self.data))

    def save(self, state):
        self.data = state
        self.saves += 1

    def update(self, mutator):
        result = mutator(self.data)
        self.data = self.data if result is None else result


class RunMainTests(unittest.TestCase):
    """run.main 的成功/失败语义：只有明确的 True 才写「已推送」。

    run.main 读 state、发通知、写 state 都经过模块级名字，
    这里整体替换为内存实现，跑的是真实的分支逻辑。
    """

    def setUp(self):
        self.lock_entries = []

    @contextlib.contextmanager
    def locked(self):
        self.lock_entries.append(1)
        yield

    def sender(self, stub: StateStub, result, on_send=None):
        def fake(*args, **kwargs):
            if on_send is not None:
                on_send()
            if isinstance(result, Exception):
                raise result
            return result
        return fake

    def run_main(self, initial: dict, result, on_send=None, summary: dict = None,
                 capture_stdout: bool = False):
        stub = StateStub(initial)
        payload = json.dumps(summary if summary is not None else SUMMARY_FIXTURE,
                             ensure_ascii=False)
        proc = mock.Mock(returncode=0,
                         stdout="SUMMARY_JSON_BEGIN" + payload + "SUMMARY_JSON_END",
                         stderr="")
        fake_sender = self.sender(stub, result, on_send)
        output = io.StringIO()
        with mock.patch.object(run.subprocess, "run", return_value=proc), \
                mock.patch.object(run, "load_state", side_effect=stub.load), \
                mock.patch.object(run, "save_state", side_effect=stub.save), \
                mock.patch.object(run, "update_state", side_effect=stub.update), \
                mock.patch.object(run, "state_lock", self.locked), \
                mock.patch.object(card, "digest_card",
                                  return_value={"card": "fixture"}), \
                mock.patch.object(notify, "send_card", side_effect=fake_sender), \
                mock.patch.object(notify, "send", side_effect=fake_sender), \
                mock.patch.object(sys, "argv", ["run.py"]):
            if capture_stdout:
                with contextlib.redirect_stdout(output):
                    code = run.main()
            else:
                code = run.main()
        return code, stub, output.getvalue()

    def test_true_marks_announced(self):
        code, stub, _ = self.run_main({"last_push_ts": 0.0}, True)
        self.assertEqual(code, 0)
        announced = stub.data.get("announced") or {}
        self.assertIn("evt-1", announced)
        self.assertIn("活动一", announced)
        self.assertNotEqual(stub.data.get("last_push_ts"), 0.0)

    def test_only_visible_items_are_marked_announced(self):
        # 清单只显示前 MAX_LIST_ITEMS 项；没显示出来的活动不能标成「已推送」，
        # 否则它们下次也不会再带 🆕，等于静默漏推。
        pending = [{"id": "evt-%d" % i, "name": "活动%d" % i,
                    "sign_start": "2026-10-21 19:00", "sign_end": "", "type": "讲座"}
                   for i in range(1, 11)]
        summary = {"pending": pending, "open_count": 0,
                   "generated_at": "2026-10-20 15:00:00"}
        code, stub, _ = self.run_main({"last_push_ts": 0.0}, True, summary=summary)
        self.assertEqual(code, 0)
        announced = stub.data.get("announced") or {}
        limit = run.MAX_LIST_ITEMS
        self.assertEqual(limit, 8)
        for i in range(1, limit + 1):
            self.assertIn("evt-%d" % i, announced)
        for i in range(limit + 1, 11):
            self.assertNotIn("evt-%d" % i, announced, "未显示的活动不能标成已推送")

    def test_open_items_take_slots_but_are_not_announced(self):
        # 报名中的活动排在清单最前面、会占掉名额，但它们没有「新增」语义，
        # 不能写进 announced——否则待开放的新活动会被挤掉，下次也不再标 🆕。
        open_items = [{"name": "报名中%d" % i, "type": "讲座",
                       "sign_end": "2026-10-2%d 19:00" % (i + 1), "place": "礼堂"}
                      for i in range(1, 6)]
        pending = [{"id": "evt-%d" % i, "name": "活动%d" % i, "type": "讲座",
                    "sign_start": "2026-10-21 19:00", "sign_end": ""}
                   for i in range(1, 11)]
        summary = {"pending": pending, "open": open_items, "open_count": 5,
                   "pending_count": 10, "generated_at": "2026-10-20 15:00:00"}
        code, stub, output = self.run_main({"last_push_ts": 0.0}, True,
                                           summary=summary, capture_stdout=True)
        self.assertEqual(code, 0)
        announced = stub.data.get("announced") or {}
        # 5 项报名中 + 前 3 项待开放 = 8 项
        for i in range(1, 4):
            self.assertIn("evt-%d" % i, announced)
        for i in range(4, 11):
            self.assertNotIn("evt-%d" % i, announced, "没显示出来的待开放不能标已推送")
        for it in open_items:
            self.assertNotIn(it["name"], announced,
                             "报名中的活动不该写「已推送」基线")
        self.assertIn("可报名 5 项", output)

    def test_false_releases_claim_and_writes_nothing(self):
        code, stub, _ = self.run_main({"last_push_ts": 100.0}, False)
        self.assertEqual(code, 1)
        self.assertEqual(stub.data.get("last_push_ts"), 100.0)
        self.assertFalse(stub.data.get("announced"))

    def test_none_returns_5_and_releases_claim(self):
        code, stub, _ = self.run_main({"last_push_ts": 100.0}, None)
        self.assertEqual(code, 5)
        self.assertEqual(stub.data.get("last_push_ts"), 100.0)
        self.assertFalse(stub.data.get("announced"))

    def test_illegal_return_is_failure(self):
        code, stub, _ = self.run_main({"last_push_ts": 100.0}, "ok")
        self.assertEqual(code, 1)
        self.assertEqual(stub.data.get("last_push_ts"), 100.0)
        self.assertFalse(stub.data.get("announced"))

    def test_exception_is_failure(self):
        code, stub, _ = self.run_main({"last_push_ts": 100.0},
                                      RuntimeError("boom"))
        self.assertEqual(code, 1)
        self.assertEqual(stub.data.get("last_push_ts"), 100.0)
        self.assertFalse(stub.data.get("announced"))

    def test_newer_claim_is_not_overwritten(self):
        # 发送期间另一个进程带 --force 又推了一轮：本次失败回滚不能抹掉它
        stub = StateStub({"last_push_ts": 100.0})
        payload = json.dumps(SUMMARY_FIXTURE, ensure_ascii=False)
        proc = mock.Mock(returncode=0,
                         stdout="SUMMARY_JSON_BEGIN" + payload + "SUMMARY_JSON_END",
                         stderr="")

        def fake_send(*args, **kwargs):
            stub.data["last_push_ts"] = 999.0
            return False

        with mock.patch.object(run.subprocess, "run", return_value=proc), \
                mock.patch.object(run, "load_state", side_effect=stub.load), \
                mock.patch.object(run, "save_state", side_effect=stub.save), \
                mock.patch.object(run, "update_state", side_effect=stub.update), \
                mock.patch.object(run, "state_lock", self.locked), \
                mock.patch.object(card, "digest_card", return_value={"card": 1}), \
                mock.patch.object(notify, "send_card", side_effect=fake_send), \
                mock.patch.object(notify, "send", side_effect=fake_send), \
                mock.patch.object(sys, "argv", ["run.py"]):
            code = run.main()
        self.assertEqual(code, 1)
        self.assertEqual(stub.data.get("last_push_ts"), 999.0)

    def test_legacy_seen_is_not_migrated(self):
        initial = {"last_push_ts": 0.0,
                   "seen": {"evt-1": "2026-10-19", "活动一": "2026-10-19"}}
        code, stub, output = self.run_main(initial, True, capture_stdout=True)
        self.assertEqual(code, 0)
        self.assertIn("🆕", output, "seen 里的活动不能被当成已推送过")
        self.assertNotIn("seen", stub.data)
        self.assertIn("evt-1", stub.data.get("announced") or {})


class ScanBatchTests(unittest.TestCase):
    """冲刺提醒分批：每批锁内发送，成功才落盘，失败整批不写。"""

    @contextlib.contextmanager
    def locked(self):
        yield

    def items(self, count: int) -> list:
        # 5 分钟后开放，落在 LEAD_MINUTES(10) 的窗口内
        base = datetime.now() + timedelta(minutes=5)
        out = []
        for i in range(count):
            start = (base + timedelta(seconds=i)).strftime("%Y-%m-%d %H:%M")
            out.append({"id": "evt-%d" % i, "name": "活动%d" % i, "type": "讲座",
                        "place": "", "sign_start": start, "sign_end": "",
                        "act_start": "", "act_end": "", "signed": False})
        return out

    def run_scan(self, items: list, send_results: list, initial: dict):
        stub = StateStub(initial)
        sent = []

        # scan.py 现在走 send_rush_card（卡片 + 纯文本兜底），签名是 (fresh, now, msg)
        def fake_send_rush_card(fresh, now, msg):
            sent.append(msg)
            return send_results.pop(0)

        with mock.patch.object(scan, "load_session",
                               return_value={"token": "token_fixture", "gender": ""}), \
                mock.patch.object(scan, "fetch_all", return_value=items), \
                mock.patch.object(scan, "state_lock", self.locked), \
                mock.patch.object(scan, "load_state", side_effect=stub.load), \
                mock.patch.object(scan, "save_state", side_effect=stub.save), \
                mock.patch.object(scan, "send_rush_card", side_effect=fake_send_rush_card), \
                mock.patch("sys.stdout", io.StringIO()):
            code = scan._scan()
        return code, stub, sent

    def test_six_items_are_sent_in_two_batches(self):
        items = self.items(6)
        code, stub, sent = self.run_scan(items, [True, True], {"reminded_signup": {}})
        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 2, "6 条命中应当拆成 5+1 两批")
        self.assertIn("活动0", sent[0])
        self.assertNotIn("活动5", sent[0])
        self.assertIn("活动5", sent[1])
        self.assertEqual(len(stub.data["reminded_signup"]), 6)

    def test_second_batch_failure_writes_only_first(self):
        items = self.items(6)
        code, stub, sent = self.run_scan(items, [True, False], {"reminded_signup": {}})
        self.assertEqual(code, 1)
        self.assertEqual(len(sent), 2)
        reminded = stub.data["reminded_signup"]
        self.assertEqual(len(reminded), 5, "失败的那一批不能预写")
        self.assertNotIn("evt-5", reminded)

    def test_unconfigured_channel_writes_nothing(self):
        items = self.items(2)
        code, stub, sent = self.run_scan(items, [None], {"reminded_signup": {}})
        self.assertEqual(code, 1, "send 返回 None 不能当成功")
        self.assertEqual(len(sent), 1)
        self.assertEqual(stub.data["reminded_signup"], {})

    def test_already_reminded_batch_is_not_resent(self):
        items = self.items(2)
        initial = {"reminded_signup": {it["id"]: it["sign_start"] for it in items}}
        code, stub, sent = self.run_scan(items, [], initial)
        self.assertEqual(code, 0)
        self.assertEqual(sent, [], "已提醒过的活动不能在下一轮重发")
        self.assertEqual(len(stub.data["reminded_signup"]), 2)


class DigestTests(unittest.TestCase):
    """推送清单的排序与截断：报名中的在前，卡片和文字共用同一个 8 项上限。

    这条规则的来源：抓取账号自己报过名的活动会被归到「报名中」，
    而以前的推送只渲染「待开放」，于是自己报过的活动反而从群里消失了。
    """

    NOW = datetime(2026, 10, 20, 15, 0)

    def summary(self, open_count=3, pending_count=7):
        open_items = [{"name": "报名中%d" % i, "type": "讲座",
                       "sign_end": "2026-10-%d 19:00" % (22 + i), "place": "礼堂"}
                      for i in range(1, open_count + 1)]
        pending = [{"id": "evt-%d" % i, "name": "活动%d" % i, "type": "讲座",
                    "sign_start": "2026-10-%d 19:00" % (21 + i), "sign_end": ""}
                   for i in range(1, pending_count + 1)]
        return {"pending": pending, "open": open_items,
                "open_count": open_count, "pending_count": pending_count,
                "generated_at": "2026-10-20 15:00:00"}

    def test_digest_items_put_open_first(self):
        kinds = [it["kind"] for it in render.digest_items(self.summary())]
        self.assertEqual(kinds[:3], ["open"] * 3)
        self.assertEqual(kinds[3], "pending")
        self.assertEqual(len(kinds), 10, "digest_items 只排序不截断")

    def test_digest_lines_cap_at_eight_for_text(self):
        lines = render.digest_lines(render.digest_items(self.summary()), self.NOW)
        bullets = [ln for ln in lines if ln.startswith(render.BULLET)]
        self.assertEqual(len(bullets), render.MAX_LIST_ITEMS)
        self.assertEqual(render.MAX_LIST_ITEMS, 8)
        self.assertIn("……另有 2 项", lines)

    def test_build_message_counts_open_and_pending(self):
        msg = run.build_message(self.summary())
        self.assertIn("可报名 3 项 · 待开放 7 项", msg)
        self.assertEqual(len([ln for ln in msg.splitlines()
                              if ln.startswith(render.BULLET)]), 8)
        self.assertIn("报名中1", msg)
        self.assertIn("活动5", msg)
        self.assertNotIn("活动6", msg, "第 6 项待开放已经超出 8 项上限")

    def test_digest_card_uses_same_eight_items(self):
        c = card.digest_card(self.summary())
        title = c["header"]["title"]["content"]
        self.assertEqual(title, "可报名 3 项 · 待开放 7 项")
        # 8 条正文 + 7 条分隔线；「……另有 2 项」合并进同一个 note 元素
        bodies = [e for e in c["elements"]
                  if e.get("tag") == "div" and e.get("text", {}).get("tag") == "lark_md"]
        self.assertEqual(len(bodies), 8)
        self.assertIn("报名中1", bodies[0]["text"]["content"])
        self.assertIn("活动5", bodies[-1]["text"]["content"])
        notes = [e for e in c["elements"] if e.get("tag") == "note"]
        self.assertTrue(any("另有 2 项" in n["elements"][0]["content"] for n in notes))

    def test_digest_card_without_open_matches_old_shape(self):
        c = card.digest_card(self.summary(open_count=0, pending_count=2))
        self.assertEqual(c["header"]["title"]["content"], "待开放报名 2 项")
        text = "\n".join(e["text"]["content"] for e in c["elements"]
                         if e.get("text", {}).get("tag") == "lark_md")
        self.assertIn("开放", text)
        self.assertNotIn("截止", text.split("\n")[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
