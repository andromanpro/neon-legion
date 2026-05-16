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


class ProductivityMultiplierTests(unittest.TestCase):
    def setUp(self):
        self._read_tasks = summary.read_tasks
        self.addCleanup(self._restore_read_tasks)

    def _restore_read_tasks(self):
        summary.read_tasks = self._read_tasks

    def _set_tasks(self, tasks):
        summary.read_tasks = lambda: tasks

    def _event(self, session_id, minute):
        ts = datetime(2026, 5, 16, tzinfo=timezone.utc) + timedelta(minutes=minute)
        return {
            "ts": ts.isoformat(),
            "session_id": session_id,
            "model": "claude-opus-4-7",
        }

    def test_overlapping_sessions_use_global_merged_denominator(self):
        self._set_tasks({
            "s1": {"ai_baseline_hours": 2.0},
            "s2": {"ai_baseline_hours": 2.0},
        })
        events = [
            self._event("s1", 0),
            self._event("s1", 60),
            self._event("s2", 0),
            self._event("s2", 60),
        ]

        result = summary.summarize_productivity(events, gap_minutes=60)

        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["active_hours_with_ai"], 1.0, places=6)
        self.assertAlmostEqual(result["active_hours_per_session_sum"], 2.0, places=6)
        self.assertAlmostEqual(result["hours_without_ai"], 4.0, places=6)
        self.assertEqual(result["baseline_floor_clamped"], 0)
        self.assertAlmostEqual(result["hours_floor_added"], 0.0, places=6)

    def test_baseline_is_floored_to_own_session_active_time(self):
        self._set_tasks({"s1": {"ai_baseline_hours": 1.0}})
        events = [
            self._event("s1", 0),
            self._event("s1", 240),
        ]

        result = summary.summarize_productivity(events, gap_minutes=240)

        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["active_hours_with_ai"], 4.0, places=6)
        self.assertAlmostEqual(result["active_hours_per_session_sum"], 4.0, places=6)
        self.assertAlmostEqual(result["hours_without_ai"], 4.0, places=6)
        self.assertEqual(result["baseline_floor_clamped"], 1)
        self.assertAlmostEqual(result["hours_floor_added"], 3.0, places=6)

    def test_non_overlapping_sessions_match_previous_per_session_sum(self):
        self._set_tasks({
            "s1": {"ai_baseline_hours": 1.5},
            "s2": {"ai_baseline_hours": 1.5},
        })
        events = [
            self._event("s1", 0),
            self._event("s1", 60),
            self._event("s2", 181),
            self._event("s2", 241),
        ]

        result = summary.summarize_productivity(events, gap_minutes=60)

        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["active_hours_with_ai"], 2.0, places=6)
        self.assertAlmostEqual(result["active_hours_per_session_sum"], 2.0, places=6)
        self.assertAlmostEqual(result["hours_without_ai"], 3.0, places=6)
        self.assertEqual(result["baseline_floor_clamped"], 0)
        self.assertAlmostEqual(result["hours_floor_added"], 0.0, places=6)


if __name__ == "__main__":
    unittest.main()
