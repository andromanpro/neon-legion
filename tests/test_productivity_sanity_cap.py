"""Backend sanity-cap on today.multiplier mirrors the dashboard widget rule
introduced in theme androman v0.8.30 — a 0.1h day estimated at 18.5h yields a
×185 artefact, so any consumer of the snapshot (not just the widget) must see
a clamped 0 instead.

Since the Codex-audit fix, the today block is built from the SAME
productivity_payload shape as 7d/30d (human-attention denominator over a
1-day window) — the tests feed that shape and pin the cap semantics on top.
"""
import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import server  # noqa: E402


class TodayProductivityBlockSanityCapTests(unittest.TestCase):
    def _day_productivity(
        self,
        attention,
        saved,
        multiplier,
        floor_clamped=0,
        floor_added=0.0,
    ):
        """A productivity_payload-shaped dict as build_productivity returns it
        for a 1-day window: active_hours == the human-attention denominator."""
        return {
            "active_hours": attention,
            "human_attention_hours": attention,
            "human_attention_fallbacks": 0,
            "ai_active_wall_clock_hours": attention,
            "active_hours_per_session_sum": attention,
            "calendar_span_hours": attention,
            "hours_without_ai_estimate": attention + saved,
            "hours_saved": saved,
            "multiplier": multiplier,
            "baseline_floor_clamped": floor_clamped,
            "hours_floor_added": floor_added,
            "baseline_ceiling_clamped": 0,
            "hours_ceiling_removed": 0.0,
            "baseline_per_event_p95": 0.1,
            "sessions_covered": 1,
            "sessions_total": 1,
            "unit": "chunk",
        }

    def test_normal_values_pass_through(self):
        block = server._today_productivity_block(
            self._day_productivity(attention=1.9, saved=25.6, multiplier=14.474)
        )
        self.assertAlmostEqual(block["multiplier"], 14.474, places=3)
        self.assertAlmostEqual(block["hours_saved"], 25.6, places=1)
        self.assertEqual(block["days"], 1)
        self.assertEqual(block["calendar_hours"], 24.0)
        self.assertEqual(block["baseline_floor_clamped"], 0)

    def test_tiny_attention_artefact_is_clamped(self):
        # 6 minutes of attention "estimated" at 18.5h — suppress.
        block = server._today_productivity_block(
            self._day_productivity(attention=0.1, saved=18.4, multiplier=185.0)
        )
        self.assertEqual(block["multiplier"], 0.0)
        self.assertEqual(block["hours_saved"], 0.0)

    def test_extreme_multiplier_is_clamped_even_with_decent_attention(self):
        block = server._today_productivity_block(
            self._day_productivity(attention=1.0, saved=59.0, multiplier=60.0)
        )
        self.assertEqual(block["multiplier"], 0.0)
        self.assertEqual(block["hours_saved"], 0.0)

    def test_at_threshold_50x_passes(self):
        block = server._today_productivity_block(
            self._day_productivity(attention=1.0, saved=49.0, multiplier=50.0)
        )
        self.assertAlmostEqual(block["multiplier"], 50.0, places=1)
        self.assertAlmostEqual(block["hours_saved"], 49.0, places=1)

    def test_below_attention_floor_is_clamped(self):
        block = server._today_productivity_block(
            self._day_productivity(attention=0.4, saved=2.4, multiplier=7.0)
        )
        self.assertEqual(block["multiplier"], 0.0)
        self.assertEqual(block["hours_saved"], 0.0)

    def test_multiplier_below_one_still_clamped(self):
        # _productivity_block itself zeroes sub-1 ratios; the day wrapper must
        # not resurrect them.
        block = server._today_productivity_block(
            self._day_productivity(attention=2.0, saved=-0.5, multiplier=0.75)
        )
        self.assertEqual(block["multiplier"], 0.0)
        self.assertEqual(block["hours_saved"], 0.0)

    def test_productivity_block_surfaces_denominator_and_floor_diagnostics(self):
        block = server._productivity_block({
            "active_hours": 1.0,
            "active_hours_per_session_sum": 1.0,
            "calendar_span_hours": 2.0,
            "multiplier": 3.0,
            "hours_saved": 2.0,
            "baseline_floor_clamped": 2,
            "hours_floor_added": 0.7,
            "baseline_ceiling_clamped": 1,
            "hours_ceiling_removed": 1.2,
            "baseline_per_event_p95": 0.4,
            "sessions_total": 5,
            "sessions_covered": 4,
            "unit": "session",
        })
        self.assertEqual(block["baseline_floor_clamped"], 2)
        self.assertAlmostEqual(block["hours_floor_added"], 0.7, places=1)
        self.assertEqual(block["baseline_ceiling_clamped"], 1)
        self.assertAlmostEqual(block["hours_ceiling_removed"], 1.2, places=1)
        self.assertAlmostEqual(block["baseline_per_event_p95"], 0.4, places=1)


if __name__ == "__main__":
    unittest.main()
