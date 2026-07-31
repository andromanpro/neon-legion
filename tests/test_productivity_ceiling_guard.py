import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tracker") not in sys.path:
    sys.path.insert(0, str(ROOT / "tracker"))
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

import summary  # noqa: E402
import server  # noqa: E402


class ProductivityCeilingGuardTests(unittest.TestCase):
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

    def test_trivial_stub_session_clamps_to_active_footprint(self):
        self._set_tasks({"stub": {"ai_baseline_hours": 36.0}})
        events = [
            self._event("stub", 0),
            self._event("stub", 0.5),
        ]

        result = summary.summarize_productivity(events, gap_minutes=2)

        active = 30 / 3600
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["hours_without_ai"], active, places=6)
        self.assertEqual(result["baseline_floor_clamped"], 0)
        self.assertAlmostEqual(result["hours_floor_added"], 0.0, places=6)
        self.assertEqual(result["baseline_ceiling_clamped"], 1)
        self.assertAlmostEqual(result["hours_ceiling_removed"], 36.0 - active, places=6)
        self.assertAlmostEqual(result["baseline_per_event_p95"], 18.0, places=6)

    def test_plausibility_band_clamps_synthetic_high_hours_per_event(self):
        effective, kind = summary.effective_session_hours(
            baseline_hours=500.0,
            session_active_hours=1.0,
            event_count=50,
        )

        self.assertEqual(kind, "ceiling_band")
        self.assertAlmostEqual(effective, 50.0, places=6)

    def test_legit_big_session_remains_normal(self):
        effective, kind = summary.effective_session_hours(
            baseline_hours=80.0,
            session_active_hours=5.0,
            event_count=1000,
        )

        self.assertEqual(kind, "normal")
        self.assertAlmostEqual(effective, 80.0, places=6)

    def test_floor_session_is_not_also_ceiling_counted(self):
        self._set_tasks({"floor": {"ai_baseline_hours": 1.0}})
        events = [
            self._event("floor", 0),
            self._event("floor", 240),
        ]

        result = summary.summarize_productivity(events, gap_minutes=240)

        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["hours_without_ai"], 4.0, places=6)
        self.assertEqual(result["baseline_floor_clamped"], 1)
        self.assertAlmostEqual(result["hours_floor_added"], 3.0, places=6)
        self.assertEqual(result["baseline_ceiling_clamped"], 0)
        self.assertAlmostEqual(result["hours_ceiling_removed"], 0.0, places=6)

    def test_today_payload_threads_ceiling_diagnostics(self):
        events = [
            self._event("stub", 0),
            self._event("stub", 0.5),
        ]
        payload = server._today_payload(
            events,
            sessions_recent=[],
            tasks={"stub": {"ai_baseline_hours": 36.0}},
            today_session_ids=["stub"],
        )

        self.assertEqual(payload["baseline_floor_clamped"], 0)
        self.assertEqual(payload["baseline_ceiling_clamped"], 1)
        self.assertAlmostEqual(payload["hours_ceiling_removed"], 36.0, places=1)
        self.assertAlmostEqual(payload["baseline_per_event_p95"], 18.0, places=1)

    def test_productivity_periods_include_ceiling_diagnostics(self):
        base_payload = {
            "active_hours": 1.0,
            "active_hours_per_session_sum": 1.0,
            "calendar_span_hours": 2.0,
            "multiplier": 4.0,
            "hours_saved": 3.0,
            "baseline_floor_clamped": 0,
            "hours_floor_added": 0.0,
            "baseline_ceiling_clamped": 2,
            "hours_ceiling_removed": 10.0,
            "baseline_per_event_p95": 9.0,
            "sessions_total": 3,
            "sessions_covered": 2,
        }
        # Today is now built from the same build_productivity pipeline
        # (1-day window) — the stub below serves it too.
        original_build_productivity = server.build_productivity
        server.build_productivity = lambda query: base_payload
        self.addCleanup(setattr, server, "build_productivity", original_build_productivity)

        periods = server._productivity_periods(60, base_payload)

        for key in ("today", "all", "7d", "30d", "60d"):
            self.assertEqual(periods[key]["baseline_ceiling_clamped"], 2)
            self.assertAlmostEqual(periods[key]["hours_ceiling_removed"], 10.0, places=1)
            self.assertAlmostEqual(periods[key]["baseline_per_event_p95"], 9.0, places=1)


    def test_money_math_invariants_hold_across_matrix(self):
        """Regression guard (DeepSeek nit 6): over the physically-valid domain
        (a session's active time is bounded by (events-1)*gap_minutes, so a
        2-event session can have <=2 min active), a ceiling can only ever
        *remove* hours (hours_ceiling_removed >= 0), effective is
        non-negative, and no session contributes less than its own active
        footprint — for any future constant retune."""
        gap_hours = 2.0 / 60.0  # default gap_minutes=2
        for baseline in (0.0, 0.5, 1.0, 3.0, 18.0, 36.0, 183.0, 500.0):
            for events in (0, 1, 2, 5, 6, 50, 655, 3164):
                max_active = max(0.0, (events - 1) * gap_hours)
                for frac in (0.0, 0.25, 0.5, 1.0):
                    active = max_active * frac
                    eff, kind = summary.effective_session_hours(
                        baseline, active, events
                    )
                    ctx = (baseline, active, events, kind)
                    self.assertGreaterEqual(eff, 0.0, ctx)
                    self.assertGreaterEqual(eff, active - 1e-9, ctx)
                    if kind.startswith("ceiling"):
                        # hours_ceiling_removed = baseline - eff must be >= 0
                        self.assertLessEqual(eff, baseline + 1e-9, ctx)
                    if kind == "floor":
                        self.assertLess(baseline, active, ctx)


if __name__ == "__main__":
    unittest.main()
