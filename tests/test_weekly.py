#!/usr/bin/env python3
# 文件版本：V1.0｜更新日期：2026-11-24
# 周汇总单测：周五才触发、一周只发一次、全绿也有文案、推送用 info、非周五不推。

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import guard  # noqa: E402


def dt(y: int, m: int, d: int, hh: int = 17, mm: int = 30) -> datetime:
    return datetime(y, m, d, hh, mm, 0)


class WeekHelpersTest(unittest.TestCase):
    def test_week_start(self):
        self.assertEqual(guard.week_start(date(2026, 11, 24)), date(2026, 11, 23))
        self.assertEqual(guard.week_start(date(2026, 11, 27)), date(2026, 11, 23))
        self.assertEqual(guard.week_start(date(2026, 11, 29)), date(2026, 11, 23))

    def test_friday_constant(self):
        self.assertEqual(date(2026, 11, 27).weekday(), guard.WEEKLY_WEEKDAY)

    def test_record_and_prune(self):
        state = {"version": 1, "days": {}, "lastSummaryWeek": None}
        guard.record_week_run(state, [], dt(2026, 11, 2))
        guard.record_week_run(state, ["x"], dt(2026, 11, 24))
        self.assertEqual(sorted(state["days"]), ["2026-11-24"])
        self.assertEqual(state["days"]["2026-11-24"]["issues"], ["x"])

    def test_load_week_state_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "week-state.json"
            path.write_text("not json", encoding="utf-8")
            with mock.patch.object(guard, "WEEK_STATE_PATH", path):
                self.assertEqual(guard.load_week_state()["days"], {})

    def test_weekly_due_only_friday_and_once(self):
        state = {"days": {}, "lastSummaryWeek": None}
        self.assertTrue(guard.weekly_due(state, date(2026, 11, 27)))
        self.assertFalse(guard.weekly_due(state, date(2026, 11, 26)))
        self.assertFalse(guard.weekly_due(state, date(2026, 11, 28)))
        state["lastSummaryWeek"] = "2026-11-23"
        self.assertFalse(guard.weekly_due(state, date(2026, 11, 27)))


class SummaryTextTest(unittest.TestCase):
    @staticmethod
    def _state(days: dict) -> dict:
        return {"version": 1, "days": days, "lastSummaryWeek": None}

    def test_all_green_sentence(self):
        state = self._state({
            day: {"issues": [], "at": ""}
            for day in ("2026-11-23", "2026-11-24", "2026-11-25", "2026-11-26", "2026-11-27")
        })
        title, body = guard.weekly_summary(state, date(2026, 11, 27), [])
        self.assertIn("本周巡检汇总", title)
        self.assertIn("5 次巡检全部正常", body)
        self.assertIn("当前状态：全部正常", body)

    def test_mixed_lists_days_and_current(self):
        state = self._state({
            "2026-11-23": {"issues": [], "at": ""},
            "2026-11-24": {"issues": [], "at": ""},
            "2026-11-25": {"issues": ["A 有未提交改动", "B 超期未提交"], "at": ""},
            "2026-11-26": {"issues": [], "at": ""},
            "2026-11-27": {"issues": ["C 已 8 天无提交"], "at": ""},
        })
        title, body = guard.weekly_summary(state, date(2026, 11, 27), ["C 已 8 天无提交"])
        self.assertIn("3 次全部正常", body)
        self.assertIn("2 次有异常", body)
        self.assertIn("11-25（2 项）：A 有未提交改动；B 超期未提交", body)
        self.assertIn("当前状态：1 项异常", body)
        self.assertIn("异常记录", title + body)

    def test_missing_days_mentioned(self):
        state = self._state({"2026-11-23": {"issues": [], "at": ""}})
        _, body = guard.weekly_summary(state, date(2026, 11, 27), [])
        self.assertIn("另有 4 天没有记录", body)

    def test_empty_week(self):
        _, body = guard.weekly_summary(self._state({}), date(2026, 11, 27), [])
        self.assertIn("暂无巡检记录", body)


class PushKindTest(unittest.TestCase):
    @staticmethod
    def _argv(kind: str | None) -> list[str]:
        result = mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(guard.subprocess, "run", return_value=result) as run:
            if kind is None:
                guard.push({}, "t", "b")
            else:
                guard.push({}, "t", "b", kind=kind)
        return run.call_args[0][0]

    def test_daily_default_todo(self):
        argv = self._argv(None)
        self.assertEqual(argv[argv.index("--kind") + 1], "todo")

    def test_weekly_info(self):
        argv = self._argv("info")
        self.assertEqual(argv[argv.index("--kind") + 1], "info")


class RunWeeklyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.week_path = Path(self.tmp.name) / "week-state.json"
        patcher = mock.patch.object(guard, "WEEK_STATE_PATH", self.week_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        log_patcher = mock.patch.object(guard, "log")
        log_patcher.start()
        self.addCleanup(log_patcher.stop)

    def test_non_friday_no_push_but_records(self):
        with mock.patch.object(guard, "push") as push:
            rc = guard.run_weekly({}, [], now=dt(2026, 11, 24))
        self.assertEqual(rc, 0)
        push.assert_not_called()
        self.assertIn("2026-11-24", guard.load_week_state()["days"])

    def test_friday_pushes_once(self):
        with mock.patch.object(guard, "push", return_value=(True, "ok")) as push:
            rc1 = guard.run_weekly({}, [], now=dt(2026, 11, 27))
            rc2 = guard.run_weekly({}, [], now=dt(2026, 11, 27, 18, 0))
        self.assertEqual((rc1, rc2), (0, 0))
        self.assertEqual(push.call_count, 1)
        self.assertEqual(push.call_args.kwargs.get("kind"), "info")
        self.assertEqual(guard.load_week_state()["lastSummaryWeek"], "2026-11-23")

    def test_friday_failed_push_can_retry(self):
        with mock.patch.object(guard, "push", return_value=(False, "boom")):
            rc = guard.run_weekly({}, [], now=dt(2026, 11, 27))
        self.assertEqual(rc, 1)
        self.assertIsNone(guard.load_week_state()["lastSummaryWeek"])
        with mock.patch.object(guard, "push", return_value=(True, "ok")):
            rc2 = guard.run_weekly({}, [], now=dt(2026, 11, 27, 18, 0))
        self.assertEqual(rc2, 0)

    def test_preview_does_not_push_or_write(self):
        with mock.patch.object(guard, "push") as push:
            title, body = guard.weekly_preview([], now=dt(2026, 11, 27))
        push.assert_not_called()
        self.assertFalse(self.week_path.exists())
        self.assertIn("本周巡检汇总", title)
        self.assertTrue(body)


if __name__ == "__main__":
    unittest.main()
