#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""报名计划核心的本地单元测试，不连接学校站点。"""

from __future__ import annotations

import threading
import time
import unittest

from local.signup_plan import (
    PlanCoordinator,
    TaskSpec,
    group_by_sign_start,
    intervals_overlap,
    parse_timestamp,
    validate_plan,
)


def activity(aid, sign, start, end, name=None, signed=False):
    return {
        "id": aid,
        "name": name or aid,
        "sign_start": sign,
        "act_start": start,
        "act_end": end,
        "signed": signed,
    }


class SignupPlanValidationTests(unittest.TestCase):
    def test_same_open_non_overlapping_can_share_batch(self):
        rows = [
            activity("a", "2026-10-23 10:00:00", "2026-10-23 12:00:00", "2026-10-23 13:00:00"),
            activity("b", "2026-10-23 10:00:00", "2026-10-23 14:00:00", "2026-10-23 15:00:00"),
        ]
        result = validate_plan(rows)
        self.assertTrue(result["ok"])
        self.assertFalse(result["conflicts"])
        self.assertEqual(len(group_by_sign_start(result["specs"])), 1)

    def test_same_open_overlapping_is_conflict(self):
        rows = [
            activity("a", "2026-10-23 10:00:00", "2026-10-23 12:00:00", "2026-10-23 13:00:00"),
            activity("b", "2026-10-23 10:00:00", "2026-10-23 12:30:00", "2026-10-23 14:00:00"),
        ]
        result = validate_plan(rows)
        self.assertFalse(result["ok"])
        self.assertEqual(result["conflicts"][0]["left"]["activity_id"], "a")

    def test_different_open_overlapping_is_also_conflict(self):
        rows = [
            activity("a", "2026-10-23 10:00:00", "2026-10-23 12:00:00", "2026-10-23 13:00:00"),
            activity("b", "2026-10-23 11:00:00", "2026-10-23 12:30:00", "2026-10-23 14:00:00"),
        ]
        result = validate_plan(rows)
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["conflicts"]), 1)

    def test_touching_boundaries_do_not_overlap(self):
        left_start = parse_timestamp("2026-10-23 12:00:00")
        left_end = parse_timestamp("2026-10-23 13:00:00")
        right_start = parse_timestamp("2026-10-23 13:00:00")
        right_end = parse_timestamp("2026-10-23 14:00:00")
        self.assertFalse(intervals_overlap(left_start, left_end, right_start, right_end))

    def test_missing_invalid_and_duplicate_items_are_rejected(self):
        rows = [
            activity("a", "2026-10-23 10:00:00", "", ""),
            activity("a", "2026-10-23 10:00:00", "2026-10-23 12:00:00", "2026-10-23 13:00:00"),
        ]
        result = validate_plan(rows)
        kinds = {item["kind"] for item in result["errors"]}
        self.assertFalse(result["ok"])
        self.assertIn("activity_time_unknown", kinds)
        self.assertIn("duplicate_id", kinds)


class SignupPlanCoordinatorTests(unittest.TestCase):
    def _wait(self, coordinator):
        thread = coordinator._thread
        self.assertIsNotNone(thread)
        thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_same_batch_starts_in_parallel_and_failure_is_isolated(self):
        specs = [
            TaskSpec("task-a", "a", "a", "2026-10-22 10:00:00", "2026-10-23 12:00:00", "2026-10-23 13:00:00"),
            TaskSpec("task-b", "b", "b", "2026-10-22 10:00:00", "2026-10-23 14:00:00", "2026-10-23 15:00:00"),
        ]
        events = []
        barrier = threading.Barrier(2)

        def emit(kind, payload):
            events.append((kind, payload))

        def runner(spec, cancel_event, status):
            barrier.wait(1)
            if spec.activity_id == "b":
                return {"status": "failed", "reason": "测试失败"}
            return {"status": "success", "code": 0}

        coordinator = PlanCoordinator("plan-1", specs, runner, emit)
        coordinator.start()
        self._wait(coordinator)
        task_states = {p["task_id"]: p["status"] for kind, p in events if kind == "task"}
        self.assertEqual(task_states["task-a"], "success")
        self.assertEqual(task_states["task-b"], "failed")
        done = [p for kind, p in events if kind == "plan_done"][-1]
        self.assertEqual(done["status"], "partial")

    def test_cancel_waiting_task_does_not_cancel_other_task(self):
        specs = [
            TaskSpec("task-a", "a", "a", "2099-10-22 10:00:00", "2099-10-23 12:00:00", "2099-10-23 13:00:00"),
            TaskSpec("task-b", "b", "b", "2099-10-22 10:00:00", "2099-10-23 14:00:00", "2099-10-23 15:00:00"),
        ]
        events = []

        def emit(kind, payload):
            events.append((kind, payload))

        def runner(spec, cancel_event, status):
            return {"status": "success"}

        coordinator = PlanCoordinator("plan-2", specs, runner, emit)
        coordinator.start()
        time.sleep(0.02)
        self.assertTrue(coordinator.cancel_task("task-a"))
        coordinator.stop()
        self._wait(coordinator)
        task_states = {p["task_id"]: p["status"] for kind, p in events if kind == "task"}
        self.assertEqual(task_states["task-a"], "canceled")


if __name__ == "__main__":
    unittest.main()
