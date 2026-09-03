import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tracker") not in sys.path:
    sys.path.insert(0, str(ROOT / "tracker"))

import summary  # noqa: E402


class ChunkBaselineProrationTests(unittest.TestCase):
    def setUp(self):
        self._read_tasks = summary.read_tasks
        self._read_lifetime_counts = summary.read_lifetime_session_event_counts
        self._unit = summary.PRODUCTIVITY_UNIT
        summary.PRODUCTIVITY_UNIT = "chunk"
        self.addCleanup(self._restore_state)

    def _restore_state(self):
        summary.read_tasks = self._read_tasks
        summary.read_lifetime_session_event_counts = self._read_lifetime_counts
        summary.PRODUCTIVITY_UNIT = self._unit

    def _configure(self, tasks, lifetime_counts):
        summary.read_tasks = lambda: tasks
        summary.read_lifetime_session_event_counts = lambda: lifetime_counts

    @staticmethod
    def _day_events(session_id, day, count):
        start = datetime.fromisoformat(f"{day}T10:00:00+00:00")
        return [
            {
                "ts": (start + timedelta(seconds=index)).isoformat(),
                "session_id": session_id,
                "model": "claude-opus-4-7",
            }
            for index in range(count)
        ]

    def test_one_day_of_multiday_session_gets_event_share_not_full_baseline(self):
        self._configure(
            {"long": {"ai_baseline_hours": 12.0}},
            {"long": 60},
        )
        one_day = self._day_events("long", "2026-05-02", 20)

        result = summary.summarize_productivity(one_day)

        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["hours_without_ai"], 4.0)

    def test_sum_of_all_daily_shares_does_not_exceed_session_baseline(self):
        baseline = 12.0
        self._configure(
            {"long": {"ai_baseline_hours": baseline}},
            {"long": 60},
        )
        days = [
            self._day_events("long", f"2026-05-0{day}", 20)
            for day in (1, 2, 3)
        ]

        daily_hours = [
            summary.summarize_productivity(events)["hours_without_ai"]
            for events in days
        ]

        self.assertAlmostEqual(sum(daily_hours), baseline)
        self.assertLessEqual(sum(daily_hours), baseline)
        for hours in daily_hours:
            self.assertLessEqual(hours, baseline)

    def test_complete_day_chunk_breakdown_is_unchanged(self):
        day_one = self._day_events("split", "2026-05-01", 6)
        day_two = self._day_events("split", "2026-05-02", 6)
        self._configure(
            {
                "split:2026-05-01": {"ai_baseline_hours": 3.0},
                "split:2026-05-02": {"ai_baseline_hours": 4.0},
            },
            {"split": 12},
        )

        result = summary.summarize_productivity(day_one + day_two)

        self.assertAlmostEqual(result["hours_without_ai"], 7.0)
        self.assertEqual(result["sessions_covered"], 2)

    def test_session_entirely_inside_window_contributes_full_baseline(self):
        baseline = 12.0
        all_events = []
        for day in (1, 2, 3):
            all_events.extend(self._day_events("contained", f"2026-05-0{day}", 20))
        self._configure(
            {"contained": {"ai_baseline_hours": baseline}},
            {"contained": len(all_events)},
        )

        result = summary.summarize_productivity(all_events)

        self.assertAlmostEqual(result["hours_without_ai"], baseline)

    def test_day_without_session_events_gets_no_share(self):
        other_events = self._day_events("other", "2026-05-04", 6)
        self._configure(
            {
                "long": {"ai_baseline_hours": 12.0},
                "other:2026-05-04": {"ai_baseline_hours": 2.0},
            },
            {"long": 60, "other": 6},
        )

        result = summary.summarize_productivity(other_events)

        self.assertAlmostEqual(result["hours_without_ai"], 2.0)
        self.assertEqual(result["distinct_sessions_covered"], 1)


if __name__ == "__main__":
    unittest.main()
