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
    def _payload(
        self,
        active,
        estimate_active,
        estimated,
        hours_saved,
        active_per_session=None,
        floor_clamped=0,
        floor_added=0.0,
    ):
        return {
            "active_hours": active,
            "active_hours_for_estimate": estimate_active,
            "active_hours_per_session_sum": (
                estimate_active if active_per_session is None else active_per_session
            ),
            "estimated_hours": estimated,
            "hours_saved": hours_saved,
            "baseline_floor_clamped": floor_clamped,
            "hours_floor_added": floor_added,
            "sessions_total": 1,
            "estimated_sessions_covered": 1,
        }

    def test_normal_values_pass_through(self):
        block = server._today_productivity_block(
            self._payload(active=1.9, estimate_active=1.9, estimated=27.5, hours_saved=25.6)
        )
        self.assertAlmostEqual(block["multiplier"], 14.474, places=3)
        self.assertAlmostEqual(block["hours_saved"], 25.6, places=1)
        self.assertAlmostEqual(block["active_hours_per_session_sum"], 1.9, places=1)
        self.assertEqual(block["baseline_floor_clamped"], 0)
        self.assertAlmostEqual(block["hours_floor_added"], 0.0, places=1)

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

    def test_productivity_block_surfaces_denominator_and_floor_diagnostics(self):
        block = server._productivity_block({
            "active_hours": 1.0,
            "active_hours_per_session_sum": 2.0,
            "calendar_span_hours": 3.0,
            "multiplier": 4.0,
            "hours_saved": 3.0,
            "baseline_floor_clamped": 1,
            "hours_floor_added": 3.0,
            "sessions_total": 2,
            "sessions_covered": 1,
        })

        self.assertAlmostEqual(block["active_hours"], 1.0, places=1)
        self.assertAlmostEqual(block["active_hours_per_session_sum"], 2.0, places=1)
        self.assertEqual(block["baseline_floor_clamped"], 1)
        self.assertAlmostEqual(block["hours_floor_added"], 3.0, places=1)

    def test_productivity_periods_include_diagnostics_for_every_period(self):
        base_payload = {
            "active_hours": 1.0,
            "active_hours_per_session_sum": 2.0,
            "calendar_span_hours": 3.0,
            "multiplier": 4.0,
            "hours_saved": 3.0,
            "baseline_floor_clamped": 1,
            "hours_floor_added": 3.0,
            "sessions_total": 2,
            "sessions_covered": 1,
        }
        today_payload = self._payload(
            active=1.0,
            estimate_active=1.0,
            estimated=4.0,
            hours_saved=3.0,
            active_per_session=2.0,
            floor_clamped=1,
            floor_added=3.0,
        )
        original_build_productivity = server.build_productivity
        server.build_productivity = lambda query: base_payload
        self.addCleanup(setattr, server, "build_productivity", original_build_productivity)

        periods = server._productivity_periods(60, base_payload, today_payload)

        for key in ("today", "all", "7d", "30d", "60d"):
            self.assertIn("active_hours_per_session_sum", periods[key])
            self.assertIn("baseline_floor_clamped", periods[key])
            self.assertIn("hours_floor_added", periods[key])


if __name__ == "__main__":
    unittest.main()
