#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实数据契约回归（完全离线，不读真实数据根、不访问学校）。

样本来源：2026-10-27 对 EXE 数据根的只读查询（7 场活动）与详情接口
`/dr/api/rjyHd/getInfo/{activity_id}`。id、名称、地点已替换为脱敏占位，
只保留字段名、值类型与时间形状。

覆盖两条真实缺陷：
- 报名成功但学校列表仍为 signed=false 时的回写与门禁；
- 已报名活动占用举办时段，必须参与冲突基线。

@author LiXinwen
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

LOCAL = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for _path in (str(LOCAL), str(TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from fixtures.business_clock import TestWallClock  # noqa: E402
from local.signup_plan import TaskSpec, validate_plan  # noqa: E402


def list_row(aid, name, sign_start, sign_end, act_start, act_end, signed=False):
    """学校列表项：真实列表的签到/签退字段全部为 null，这里也保持 null。"""
    return {
        "id": aid, "name": name, "place": "教学楼",
        "sign_start": sign_start, "sign_end": sign_end,
        "act_start": act_start, "act_end": act_end,
        "signed": signed,
        "checkin_required": None, "checkout_required": None,
    }


class SignedLagContractTests(unittest.TestCase):
    """报名成功后列表暂时为 false：不能撤销已确认成功。"""

    def _api(self):
        sys.path.insert(0, str(LOCAL))
        try:
            import app as local_app
        except Exception as exc:
            self.skipTest("无法导入 local/app.py：%s" % exc)
        finally:
            sys.path.pop(0)
        self._app = local_app
        return local_app.Api(clock=TestWallClock())

    def test_success_then_school_list_still_false(self):
        api = self._api()
        activity = list_row("act-0001", "脱敏活动甲",
                            "2026-10-27 09:00:00", "2026-10-27 23:59:59",
                            "2026-10-28 19:00:00", "2026-10-28 20:30:00")
        api._activities = {activity["id"]: dict(activity)}
        self.assertTrue(api._confirm_signed("act-0001", "success"))
        stale = [dict(activity, signed=False)]
        merged = api._merge_list_activities(stale)
        self.assertTrue(merged[0]["signed"])
        self.assertTrue(merged[0]["sync_pending"])
        self.assertEqual(merged[0]["signed_source"], "plan_success")

    def test_unconfirmed_never_written_as_signed(self):
        api = self._api()
        activity = list_row("act-0002", "脱敏活动乙",
                            "2026-10-27 09:00:00", "2026-10-27 23:59:59",
                            "2026-10-28 19:00:00", "2026-10-28 20:30:00")
        api._activities = {activity["id"]: dict(activity)}
        self.assertFalse(api._confirm_signed("act-0002", "failed"))
        self.assertFalse(api._activities["act-0002"]["signed"])


class SignedOccupancyContractTests(unittest.TestCase):
    """已报名活动与本次选择按举办时段判冲突。"""

    NOW = TestWallClock().now()

    def test_seven_real_shaped_activities_keep_selection_clean(self):
        # 真实列表有 7 场；这里保留同样数量级，验证全量基线不会误报
        signed = [
            list_row("s-1", "已报名甲", "2026-10-20 09:00:00", "2026-10-21 09:00:00",
                     "2026-10-24 19:00:00", "2026-10-24 20:00:00", signed=True),
            list_row("s-2", "已报名乙", "2026-10-20 09:00:00", "2026-10-21 09:00:00",
                     "2026-10-25 09:00:00", "2026-10-25 11:00:00", signed=True),
        ]
        selected = [
            list_row("n-1", "待报丙", "2026-10-27 09:00:00", "2026-10-27 23:59:59",
                     "2026-10-28 09:00:00", "2026-10-28 11:00:00"),
        ]
        result = validate_plan(selected, now=self.NOW, signed_activities=signed)
        self.assertTrue(result["ok"], result["errors"])

    def test_overlap_with_signed_activity_is_blocked(self):
        signed = [list_row("s-1", "已报名甲", "2026-10-20 09:00:00", "2026-10-21 09:00:00",
                           "2026-10-28 19:00:00", "2026-10-28 20:00:00", signed=True)]
        selected = [list_row("n-1", "待报丙", "2026-10-27 09:00:00", "2026-10-27 23:59:59",
                             "2026-10-28 19:30:00", "2026-10-28 21:00:00")]
        result = validate_plan(selected, now=self.NOW, signed_activities=signed)
        self.assertFalse(result["ok"])
        self.assertEqual(result["errors"][0]["kind"], "signed_activity_overlap")
        self.assertEqual(result["errors"][0]["signed"]["activity_id"], "s-1")

    def test_same_open_time_is_not_conflict(self):
        """报名开放时间相同只决定同批调度，不是冲突依据。"""
        signed = [list_row("s-1", "已报名甲", "2026-10-27 09:00:00", "2026-10-27 23:59:59",
                           "2026-10-28 09:00:00", "2026-10-28 10:00:00", signed=True)]
        selected = [list_row("n-1", "待报丙", "2026-10-27 09:00:00", "2026-10-27 23:59:59",
                             "2026-10-28 10:00:00", "2026-10-28 11:00:00")]
        result = validate_plan(selected, now=self.NOW, signed_activities=signed)
        self.assertTrue(result["ok"], "首尾相接 + 同开放时刻都不是冲突")

    def test_signed_missing_window_blocks_conservatively(self):
        signed = [list_row("s-1", "已报名甲", "2026-10-20 09:00:00", "2026-10-21 09:00:00",
                           "", "", signed=True)]
        selected = [list_row("n-1", "待报丙", "2026-10-27 09:00:00", "2026-10-27 23:59:59",
                             "2026-10-28 09:00:00", "2026-10-28 11:00:00")]
        result = validate_plan(selected, now=self.NOW, signed_activities=signed)
        self.assertFalse(result["ok"])
        self.assertEqual(result["errors"][0]["kind"], "signed_activity_time_unknown")


import grab  # noqa: E402


class AttendanceDetailContractTests(unittest.TestCase):
    """详情字段契约：需要签到 + 无需签退、需要签退、时间缺失、未知值。"""

    REQUIRED_IN_ONLY_OUT = {
        "code": 1,
        "data": {"sfxyqd": "1", "qdkssj": "2026-10-24 19:00:00",
                 "qdjssj": "2026-10-24 19:40:00",
                 "sfxyqt": "0", "qtkssj": None, "qtjssj": None},
    }
    REQUIRED_BOTH_WITH_TIMES = {
        "code": 1,
        "data": {"sfxyqd": 1, "qdkssj": "2026-10-24 18:30:00",
                 "qdjssj": "2026-10-24 19:00:00",
                 "sfxyqt": 1, "qtkssj": "2026-10-24 21:00:00",
                 "qtjssj": "2026-10-24 21:20:00"},
    }
    UNKNOWN_ALL_NULL = {
        "code": 1,
        "data": {"sfxyqd": None, "qdkssj": None, "qdjssj": None,
                 "sfxyqt": None, "qtkssj": None, "qtjssj": None},
    }

    def test_checkin_required_and_checkout_not_needed(self):
        parsed = grab.parse_attendance_detail(self.REQUIRED_IN_ONLY_OUT)
        self.assertTrue(parsed["checkin_required"])
        self.assertEqual(parsed["checkin_start"], "2026-10-24 19:00:00")
        self.assertEqual(parsed["checkin_end"], "2026-10-24 19:40:00")
        self.assertFalse(parsed["checkout_required"])
        self.assertEqual(parsed["checkout_start"], "")
        self.assertEqual(parsed["checkout_end"], "")

    def test_both_required_with_time_ranges(self):
        parsed = grab.parse_attendance_detail(self.REQUIRED_BOTH_WITH_TIMES)
        self.assertTrue(parsed["checkin_required"])
        self.assertTrue(parsed["checkout_required"])
        self.assertEqual(parsed["checkout_start"], "2026-10-24 21:00:00")
        self.assertEqual(parsed["checkout_end"], "2026-10-24 21:20:00")

    def test_flag_missing_but_time_present_implies_required(self):
        parsed = grab.parse_attendance_detail({
            "code": 1, "data": {"qdkssj": "2026-10-24 19:00:00"}})
        self.assertTrue(parsed["checkin_required"])
        self.assertIsNone(parsed["checkout_required"], "没有签退时间不能推导成需要")

    def test_all_null_stays_unknown(self):
        parsed = grab.parse_attendance_detail(self.UNKNOWN_ALL_NULL)
        self.assertIsNone(parsed["checkin_required"])
        self.assertIsNone(parsed["checkout_required"])

    def test_enrich_dedupes_by_activity_id_and_degrades_per_item(self):
        calls = []

        def fake_detail(activity_id, timeout=None):
            calls.append(activity_id)
            if activity_id == "bad":
                return None
            return {"checkin_required": True, "checkin_start": "2026-10-24 19:00:00",
                    "checkin_end": "2026-10-24 19:40:00",
                    "checkout_required": False, "checkout_start": "", "checkout_end": ""}

        rows = [
            {"id": "act-1", "checkin_required": None, "checkout_required": None},
            {"id": "act-1", "checkin_required": None, "checkout_required": None},
            {"id": "bad", "checkin_required": None, "checkout_required": None},
        ]
        with mock.patch.object(grab, "fetch_activity_detail", side_effect=fake_detail):
            grab.enrich_attendance_items(rows)
        self.assertEqual(calls, ["act-1", "bad"], "同一 id 只查一次详情")
        self.assertEqual(rows[0]["attendance_state"], "ready")
        self.assertEqual(rows[0]["attendance_source"], "detail")
        self.assertTrue(rows[0]["checkin_required"])
        self.assertEqual(rows[2]["attendance_state"], "pending")
        self.assertEqual(rows[2]["attendance_error"], "detail_unavailable")
        self.assertIsNone(rows[2]["checkin_required"], "详情失败不得猜成无需")


class TaskSpecSnapshotContractTests(unittest.TestCase):
    """计划快照只带判定所需字段，绝不携带凭证类键。"""

    def test_spec_has_no_credential_fields(self):
        row = list_row("act-0001", "脱敏活动甲", "2026-10-27 09:00:00",
                       "2026-10-27 23:59:59", "2026-10-28 19:00:00", "2026-10-28 20:30:00")
        payload = TaskSpec.from_activity(row, "task-01", now=TestWallClock().now()).as_dict()
        for forbidden in ("token", "cookie", "storage_state", "password", "account"):
            self.assertNotIn(forbidden, payload)


if __name__ == "__main__":
    unittest.main()
