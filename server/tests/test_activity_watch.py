"""公共活动动态发现（activity_watch）的离线边界测试。

只跑纯函数与状态文件读写，不联网、不读 server/data、不发任何消息。
所有 fixture 都落在 D:/@Temp（可用 TJISE_TEST_ROOT 覆盖）的临时目录里。

覆盖的场景就是计划里点名的那些：
首次发现未来活动 / 首次发现已开放活动 / 已截止过滤 / 未来转开放 / 开放时间变化 /
重复轮询不重复事件 / 活动暂时缺失不立即结束 / 接口失败保留快照 /
轮询频率选择 / 单飞锁让路 / dry-run 不写盘 / 推送失败保留待推事件。

@author LiXinwen
"""
import contextlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

try:  # Windows 控制台默认 GBK，打中文断言信息会直接抛 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import activity_watch as aw   # noqa: E402
import fetch as fetch_mod     # noqa: E402

PREFERRED_ROOT = "D:/@Temp/tjise-activity-watch-20261027"

# 测试用轮询配置：与 common 默认值一致，避免读真实 config.json
SETTINGS = {"enabled": True, "day_seconds": 180, "night_seconds": 600}


def _test_root() -> Path:
    root = Path(os.environ.get("TJISE_TEST_ROOT") or (
        PREFERRED_ROOT if os.name == "nt" else os.path.join(tempfile.gettempdir(),
                                                            "tjise-activity-watch")))
    root.mkdir(parents=True, exist_ok=True)
    return root


def make_tmpdir(prefix: str = "watch-") -> str:
    return tempfile.mkdtemp(prefix=prefix, dir=str(_test_root()))


@contextlib.contextmanager
def busy_lock(_path=None):
    """模拟「上一轮还没跑完」：非阻塞取锁直接失败。"""
    yield False


class WatchTestCase(unittest.TestCase):
    NOW = datetime(2026, 10, 27, 13, 0)

    def setUp(self):
        self.dir = Path(make_tmpdir())
        self.addCleanup(lambda: shutil.rmtree(self.dir, ignore_errors=True))
        self.state_path = self.dir / "activity_watch.json"
        self.lock_path = self.dir / ".activity_watch.lock"
        for patcher in (
            mock.patch.object(aw, "STATE_FILE", self.state_path),
            mock.patch.object(aw, "LOCK_FILE", self.lock_path),
            # 不读仓库里的 config.json：本地那份可能被改过，会让断言随环境漂移
            mock.patch.object(aw, "load_watch_config", return_value=dict(SETTINGS)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    # ---------- fixture ----------
    def item(self, **over):
        base = {
            "id": "evt-1",
            "name": "活动一",
            "type": "讲座",
            "place": "礼堂",
            "sign_start": "2026-10-28 18:00:00",
            "sign_end": "2026-10-29 18:00:00",
            "act_start": "2026-10-30 18:00:00",
            "act_end": "2026-10-30 20:00:00",
            "checkin": "",
        }
        base.update(over)
        return base

    def rows(self, *items, now=None):
        return fetch_mod.classify_public(list(items), now or self.NOW)

    def read_state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def seeded_state(self) -> dict:
        """已经建过基线的状态（只含一条旧活动）。

        第二轮起的增量语义都从这里出发：空状态的第一轮现在只建基线、
        不产生事件（见 initialize_baseline）。
        """
        state = aw._empty_state()
        yesterday = self.NOW - timedelta(days=1)
        state["activities"], _ = aw.compute_events(
            {}, self.rows(self.item(id="evt-old", name="旧活动"), now=yesterday), yesterday)
        state["initialized_at"] = yesterday.strftime(aw.FMT)
        state["baseline_initialized"] = True
        return state


class DiscoveryTests(WatchTestCase):
    """首次发现与状态转移。"""

    def test_new_pending_records_discovered_at(self):
        activities, events = aw.compute_events({}, self.rows(self.item()), self.NOW)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["kind"], aw.EVENT_NEW_PENDING)
        self.assertEqual(event["status"], "pending")
        self.assertEqual(event["discovered_at"], "2026-10-27 13:00:00")
        self.assertEqual(event["sign_start"], "2026-10-28 18:00:00")
        self.assertEqual(activities["evt-1"]["first_status"], "pending")
        self.assertEqual(activities["evt-1"]["discovered_at"], "2026-10-27 13:00:00")

    def test_new_open_keeps_original_sign_start(self):
        # 一发现就已经开放：必须同时给「原定开放」和「本次发现」，
        # 否则用户分不清自己是刚赶上还是已经晚了半天。
        item = self.item(sign_start="2026-10-27 08:00:00")
        _, events = aw.compute_events({}, self.rows(item), self.NOW)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], aw.EVENT_NEW_OPEN)
        self.assertEqual(events[0]["sign_start"], "2026-10-27 08:00:00")
        self.assertEqual(events[0]["discovered_at"], "2026-10-27 13:00:00")
        self.assertEqual(events[0]["first_status"], "open")

    def test_closed_is_recorded_without_event(self):
        item = self.item(sign_start="2026-10-25 08:00:00",
                         sign_end="2026-10-25 18:00:00",
                         act_start="2026-10-26 18:00:00",
                         act_end="2026-10-26 20:00:00")
        activities, events = aw.compute_events({}, self.rows(item), self.NOW)
        self.assertEqual(events, [], "已截止的活动不该补发一条")
        self.assertEqual(activities["evt-1"]["status"], "closed")

    def test_unknown_without_sign_start_is_recorded_without_event(self):
        item = self.item(sign_start="", sign_end="", act_start="2026-12-01 18:00:00")
        activities, events = aw.compute_events({}, self.rows(item), self.NOW)
        self.assertEqual(events, [], "没有报名时间的活动发出去也没法行动")
        self.assertEqual(activities["evt-1"]["status"], "unknown")

    def test_pending_turns_open(self):
        item = self.item(sign_start="2026-10-27 18:00:00")
        activities, first = aw.compute_events({}, self.rows(item), self.NOW)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["kind"], aw.EVENT_NEW_PENDING)
        later = self.NOW + timedelta(hours=6)      # 19:00，已过 18:00 开放
        _, events = aw.compute_events(activities, self.rows(item, now=later), later)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], aw.EVENT_OPENED)
        self.assertEqual(events[0]["status"], "open")
        self.assertEqual(events[0]["discovered_at"], "2026-10-27 13:00:00",
                         "发现时刻不能被后来的事件改写")

    def test_time_change_emits_changed(self):
        activities, _ = aw.compute_events({}, self.rows(self.item()), self.NOW)
        changed_item = self.item(sign_end="2026-10-31 18:00:00")
        _, events = aw.compute_events(activities, self.rows(changed_item), self.NOW)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], aw.EVENT_CHANGED)
        self.assertEqual(events[0]["changed_fields"], ["sign_end"])
        self.assertEqual(events[0]["previous"]["sign_end"], "2026-10-29 18:00:00")

    def test_name_change_alone_does_not_notify(self):
        activities, _ = aw.compute_events({}, self.rows(self.item()), self.NOW)
        renamed = self.item(name="活动一（改名）")
        activities2, events = aw.compute_events(activities, self.rows(renamed), self.NOW)
        self.assertEqual(events, [], "改字不打扰群，下一档日报自然会用新名称")
        self.assertEqual(activities2["evt-1"]["name"], "活动一（改名）")

    def test_repeat_polling_does_not_repeat_events(self):
        activities, first = aw.compute_events({}, self.rows(self.item()), self.NOW)
        self.assertEqual(len(first), 1)
        _, second = aw.compute_events(activities, self.rows(self.item()),
                                     self.NOW + timedelta(minutes=3))
        _, third = aw.compute_events(activities, self.rows(self.item()),
                                    self.NOW + timedelta(minutes=6))
        self.assertEqual(second, [])
        self.assertEqual(third, [])


class SnapshotTests(WatchTestCase):
    """快照的稳定性：缺失、失败都不能推断「活动结束」。"""

    def test_missing_once_does_not_end_activity(self):
        activities, _ = aw.compute_events({}, self.rows(self.item()), self.NOW)
        activities, events = aw.compute_events(activities, [],
                                               self.NOW + timedelta(minutes=3))
        self.assertEqual(events, [])
        self.assertEqual(activities["evt-1"]["missing_rounds"], 1)

    def test_missing_three_rounds_removes_state(self):
        activities, _ = aw.compute_events({}, self.rows(self.item()), self.NOW)
        for index in range(1, aw.MISSING_ROUNDS_LIMIT):
            activities, events = aw.compute_events(
                activities, [], self.NOW + timedelta(minutes=3 * index))
            self.assertEqual(events, [])
            self.assertIn("evt-1", activities)
        activities, events = aw.compute_events(
            activities, [], self.NOW + timedelta(minutes=30))
        self.assertEqual(events, [])
        self.assertNotIn("evt-1", activities)

    def test_appearing_again_after_gap_keeps_discovered_at(self):
        activities, _ = aw.compute_events({}, self.rows(self.item()), self.NOW)
        activities, _ = aw.compute_events(activities, [], self.NOW + timedelta(minutes=3))
        _, events = aw.compute_events(activities, self.rows(self.item()),
                                     self.NOW + timedelta(minutes=6))
        self.assertEqual(events, [], "只是少返回一轮，不是新活动")
        self.assertEqual(activities["evt-1"]["discovered_at"], "2026-10-27 13:00:00")


class RunOnceTests(WatchTestCase):
    """_run_once 的写盘顺序与失败语义。"""

    def test_fetch_failure_keeps_previous_snapshot(self):
        state = aw._empty_state()
        state["activities"], _ = aw.compute_events({}, self.rows(self.item()), self.NOW)
        before = json.loads(json.dumps(state["activities"]))
        with mock.patch.object(aw, "fetch_public", side_effect=RuntimeError("boom")):
            code = aw._run_once(state, SETTINGS, "token_fixture", self.NOW, False)
        self.assertEqual(code, 3)
        saved = self.read_state()
        self.assertEqual(saved["activities"], before, "失败不能清空上一轮快照")
        self.assertEqual(saved["pending_events"], [])
        self.assertEqual(saved["consecutive_failures"], 1)
        self.assertIn("boom", saved["last_error"])

    def test_permission_error_returns_2(self):
        state = aw._empty_state()
        with mock.patch.object(aw, "fetch_public",
                               side_effect=PermissionError("身份验证失败")):
            code = aw._run_once(state, SETTINGS, "token_fixture", self.NOW, False)
        self.assertEqual(code, 2)

    def test_send_failure_keeps_pending_events(self):
        state = self.seeded_state()
        with mock.patch.object(aw, "fetch_public", return_value=[self.item()]), \
                mock.patch.object(aw, "_send", return_value=False):
            code = aw._run_once(state, SETTINGS, "token_fixture", self.NOW, False)
        self.assertEqual(code, 1)
        saved = self.read_state()
        self.assertEqual(len(saved["pending_events"]), 1, "没发出去的事件必须留着")
        self.assertEqual(saved["pending_events"][0]["kind"], aw.EVENT_NEW_PENDING)

    def test_send_success_clears_pending_events(self):
        with mock.patch.object(aw, "fetch_public", return_value=[self.item()]), \
                mock.patch.object(aw, "_send", return_value=False):
            aw._run_once(self.seeded_state(), SETTINGS, "token_fixture", self.NOW, False)
        with mock.patch.object(aw, "fetch_public", return_value=[self.item()]), \
                mock.patch.object(aw, "_send", return_value=True) as sender:
            code = aw._run_once(aw.load_state(), SETTINGS, "token_fixture",
                                self.NOW + timedelta(minutes=3), False)
        self.assertEqual(code, 0)
        self.assertEqual(sender.call_count, 1, "上一轮没发出去的事件要在这一轮补发")
        self.assertEqual(self.read_state()["pending_events"], [])

    def test_first_run_initializes_baseline_silently(self):
        # 部署/升级那一刻活动库已有几十条历史活动：首轮只建基线，一条都不发。
        items = [self.item(), self.item(id="evt-2", name="活动二")]
        with mock.patch.object(aw, "fetch_public", return_value=items), \
                mock.patch.object(aw, "_send") as sender:
            code = aw._run_once(aw._empty_state(), SETTINGS, "token_fixture",
                                self.NOW, False)
        self.assertEqual(code, 0)
        sender.assert_not_called()
        saved = self.read_state()
        self.assertTrue(saved["baseline_initialized"])
        self.assertEqual(saved["initialized_at"], "2026-10-27 13:00:00")
        self.assertEqual(saved["last_success_at"], "2026-10-27 13:00:00")
        self.assertEqual(saved["pending_events"], [], "首轮不能留下待推事件")
        self.assertEqual(sorted(saved["activities"]), ["evt-1", "evt-2"])
        self.assertEqual(saved["activities"]["evt-1"]["discovered_at"],
                         "2026-10-27 13:00:00")
        self.assertTrue(saved["next_poll_at"])

    def test_second_run_with_same_items_is_silent(self):
        with mock.patch.object(aw, "fetch_public", return_value=[self.item()]), \
                mock.patch.object(aw, "_send") as sender:
            aw._run_once(aw._empty_state(), SETTINGS, "token_fixture", self.NOW, False)
            code = aw._run_once(aw.load_state(), SETTINGS, "token_fixture",
                                self.NOW + timedelta(minutes=3), False)
        self.assertEqual(code, 0)
        sender.assert_not_called()
        self.assertEqual(self.read_state()["pending_events"], [])

    def test_new_activity_after_baseline_emits_one_event(self):
        first = [self.item()]
        later = first + [self.item(id="evt-2", name="活动二")]
        with mock.patch.object(aw, "fetch_public", return_value=first), \
                mock.patch.object(aw, "_send"):
            aw._run_once(aw._empty_state(), SETTINGS, "token_fixture", self.NOW, False)
        with mock.patch.object(aw, "fetch_public", return_value=later), \
                mock.patch.object(aw, "_send", return_value=True) as sender:
            code = aw._run_once(aw.load_state(), SETTINGS, "token_fixture",
                                self.NOW + timedelta(minutes=3), False)
        self.assertEqual(code, 0)
        self.assertEqual(sender.call_count, 1)
        events = sender.call_args[0][0]
        self.assertEqual(len(events), 1, "基线之后只有新出现的那条产生事件")
        self.assertEqual(events[0]["id"], "evt-2")
        self.assertEqual(events[0]["kind"], aw.EVENT_NEW_PENDING)

    def test_first_run_failure_does_not_initialize(self):
        state = aw._empty_state()
        with mock.patch.object(aw, "fetch_public", side_effect=RuntimeError("boom")), \
                mock.patch.object(aw, "_send") as sender:
            code = aw._run_once(state, SETTINGS, "token_fixture", self.NOW, False)
        self.assertEqual(code, 3)
        sender.assert_not_called()
        saved = self.read_state()
        self.assertFalse(saved["baseline_initialized"], "失败不能写成功基线")
        self.assertEqual(saved["initialized_at"], "")
        self.assertEqual(saved["activities"], {})
        self.assertEqual(saved["consecutive_failures"], 1)

    def test_dry_run_writes_nothing(self):
        with mock.patch.object(aw, "load_session",
                               return_value={"token": "token_fixture"}), \
                mock.patch.object(aw, "fetch_public", return_value=[self.item()]), \
                mock.patch.object(aw, "_send") as sender:
            code = aw.main(["--dry-run"])
        self.assertEqual(code, 0)
        sender.assert_not_called()
        self.assertFalse(self.state_path.exists(), "dry-run 不写状态，反复跑看到同一批事件")

    def test_busy_lock_lets_previous_round_finish(self):
        with mock.patch.object(aw, "load_session",
                               return_value={"token": "token_fixture"}), \
                mock.patch.object(aw, "try_file_lock", busy_lock), \
                mock.patch.object(aw, "fetch_public") as fetch_mock:
            code = aw.main(["--force"])
        self.assertEqual(code, 0)
        self.assertEqual(fetch_mock.call_count, 0, "上一轮没跑完就不该再访问学校")

    def test_corrupt_state_is_reported_not_reset(self):
        self.state_path.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            aw.load_state()
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), "{not-json",
                         "坏状态文件不能被静默覆盖")
        with mock.patch.object(aw, "load_session",
                               return_value={"token": "token_fixture"}):
            self.assertEqual(aw.main(["--force"]), 4)


class ScheduleTests(WatchTestCase):
    """轮询节奏与单飞。只有白天/夜间两档，不做临近开放加密。"""

    def test_daytime_uses_three_minutes(self):
        self.assertEqual(aw.poll_interval(SETTINGS, self.NOW), 180)

    def test_night_uses_ten_minutes(self):
        night = datetime(2026, 10, 27, 2, 0)
        self.assertEqual(aw.poll_interval(SETTINGS, night), 600)

    def test_hour_boundaries(self):
        self.assertEqual(aw.poll_interval(SETTINGS, datetime(2026, 10, 27, 7, 0)), 180)
        self.assertEqual(aw.poll_interval(SETTINGS, datetime(2026, 10, 27, 22, 59)), 180)
        self.assertEqual(aw.poll_interval(SETTINGS, datetime(2026, 10, 27, 23, 0)), 600)

    def test_polling_does_not_speed_up_near_opening(self):
        # 计划明确删除了「临近开放每分钟检查」：无论有没有活动马上要开，
        # 白天都是 180 秒。临近提醒由 scan.py 负责，这条通道不重复承担。
        self.assertEqual(aw.poll_interval(SETTINGS, self.NOW), 180)
        self.assertEqual(aw.poll_interval(SETTINGS, self.NOW + timedelta(minutes=1)), 180)

    def test_dirty_interval_falls_back_to_default(self):
        # 配置写坏时回落默认：宁可慢一点，也不能变成每分钟打学校接口
        self.assertEqual(aw.poll_interval({"day_seconds": 0}, self.NOW), 180)
        self.assertEqual(aw.poll_interval({}, self.NOW), 180)

    def test_should_run_respects_next_poll(self):
        state = aw._empty_state()
        self.assertTrue(aw.should_run(state, self.NOW), "没有排期就该跑")
        state["next_poll_at"] = "2026-10-27 13:05:00"
        self.assertFalse(aw.should_run(state, self.NOW))
        self.assertTrue(aw.should_run(state, self.NOW + timedelta(minutes=6)))
        self.assertTrue(aw.should_run(state, self.NOW, force=True))

    def test_bad_next_poll_is_treated_as_due(self):
        state = aw._empty_state()
        state["next_poll_at"] = "不是时间"
        self.assertTrue(aw.should_run(state, self.NOW), "一条坏字段不该把监测永久停住")

    def test_backoff_grows_then_caps(self):
        self.assertEqual(aw.backoff_seconds(0), 0)
        self.assertEqual(aw.backoff_seconds(1), 120)
        self.assertEqual(aw.backoff_seconds(2), 300)
        self.assertEqual(aw.backoff_seconds(3), 900)
        self.assertEqual(aw.backoff_seconds(9), 900)

    def test_try_file_lock_acquires_fresh_lock(self):
        with aw.try_file_lock(self.lock_path) as got:
            self.assertTrue(got)

    def test_merge_pending_keeps_most_important_event(self):
        early = {"id": "a", "kind": aw.EVENT_NEW_PENDING, "status": "pending",
                 "discovered_at": "2026-10-27 10:00:00"}
        later = {"id": "a", "kind": aw.EVENT_OPENED, "status": "open",
                 "discovered_at": "2026-10-27 13:00:00"}
        merged = aw.merge_pending([early], [later])
        self.assertEqual(len(merged), 1, "同一条活动一轮只占一行")
        self.assertEqual(merged[0]["kind"], aw.EVENT_OPENED)
        self.assertEqual(merged[0]["discovered_at"], "2026-10-27 10:00:00",
                         "最早的发现时刻是事实，不能被覆盖")


class BoundaryTests(WatchTestCase):
    """边界：公开字段、只读、不碰个人状态。"""

    def test_events_carry_only_public_fields(self):
        item = dict(self.item(), signed=True)
        _, events = aw.compute_events({}, self.rows(item), self.NOW)
        blob = json.dumps(events, ensure_ascii=False)
        for key in ("signed", "bmzt", "attempts", "watchlist", "token"):
            self.assertNotIn(key, blob, "事件里不能出现个人执行字段：%s" % key)

    def test_public_item_drops_signed(self):
        public = fetch_mod.public_item(dict(self.item(), signed=True))
        self.assertNotIn("signed", public)
        self.assertEqual(public["id"], "evt-1")

    def test_source_has_no_personal_coupling(self):
        source = (ROOT / "scripts" / "activity_watch.py").read_text(encoding="utf-8")
        # 动态监测必须对个人清单与报名入口零引用：有引用就意味着某条分支可能去报名
        self.assertNotIn("watchlist", source)
        self.assertNotIn("grab_signup", source)

    def test_state_file_is_separate_from_push_state(self):
        self.assertNotEqual(Path(aw.STATE_FILE).name, "state.json")
        self.assertEqual(Path(aw.STATE_FILE).name, "activity_watch.json")


if __name__ == "__main__":
    unittest.main(verbosity=2)
