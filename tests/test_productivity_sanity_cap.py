"""Backend sanity-cap on today.multiplier mirrors the dashboard widget rule
introduced in theme androman v0.8.30 — a 0.1h session estimated at 18.5h
yields a ×185 artefact, so any consumer of the snapshot (not just the widget)
must see a clamped 0 instead.
"""
import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import server  # noqa: E402


class TodayProductivityBlockSanityCapTests(unittest.TestCase):
    def _payload(self, active, estimate_active, estimated, hours_saved):
        return {
            "active_hours": active,
            "active_hours_for_estimate": estimate_active,
            "estimated_hours": estimated,
            "hours_saved": hours_saved,
            "sessions_total": 1,
            "estimated_sessions_covered": 1,
        }

    def test_normal_values_pass_through(self):
        block = server._today_productivity_block(
            self._payload(active=1.9, estimate_active=1.9, estimated=27.5, hours_saved=25.6)
        )
        self.assertAlmostEqual(block["multiplier"], 14.474, places=3)
        self.assertAlmostEqual(block["hours_saved"], 25.6, places=1)

    def test_tiny_active_time_artefact_is_clamped(self):
        block = server._today_productivity_block(
            self._payload(active=0.1, estimate_active=0.1, estimated=18.5, hours_saved=18.4)
        )
        self.assertEqual(block["multiplier"], 0.0)
        self.assertEqual(block["hours_saved"], 0.0)

    def test_extreme_multiplier_is_clamped_even_with_decent_active_hours(self):
        block = server._today_productivity_block(
            self._payload(active=1.0, estimate_active=1.0, estimated=60.0, hours_saved=59.0)
        )
        self.assertEqual(block["multiplier"], 0.0)
        self.assertEqual(block["hours_saved"], 0.0)

    def test_at_threshold_50x_passes(self):
        block = server._today_productivity_block(
            self._payload(active=1.0, estimate_active=1.0, estimated=50.0, hours_saved=49.0)
        )
        self.assertAlmostEqual(block["multiplier"], 50.0, places=1)
        self.assertAlmostEqual(block["hours_saved"], 49.0, places=1)

    def test_below_active_hours_floor_is_clamped(self):
        block = server._today_productivity_block(
            self._payload(active=0.4, estimate_active=0.4, estimated=2.8, hours_saved=2.4)
        )
        self.assertEqual(block["multiplier"], 0.0)
        self.assertEqual(block["hours_saved"], 0.0)

    def test_multiplier_below_one_still_clamped(self):
        block = server._today_productivity_block(
            self._payload(active=2.0, estimate_active=2.0, estimated=1.5, hours_saved=-0.5)
        )
        self.assertEqual(block["multiplier"], 0.0)
        self.assertEqual(block["hours_saved"], 0.0)


if __name__ == "__main__":
    unittest.main()
