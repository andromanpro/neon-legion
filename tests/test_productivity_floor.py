"""Regression tests for the human-attention denominator FLOOR in
backend/server.py:productivity_payload (Codex-audit HIGH findings).

Covers:
  - HIGH: valid zero human-attention must apply the per-session floor, NOT fall
    back to AI-active wall-clock (which would re-inject autonomous runtime).
  - HIGH: the floor divides by DISTINCT sessions, so chunk mode (where a
    multi-day session spans several day-chunks) cannot over-floor.
  - HIGH: empty/uncovered input (productivity is None) must not raise
    UnboundLocalError.
"""

import importlib.util
import unittest
from datetime import datetime, timedelta, timezone

_summary_spec = importlib.util.spec_from_file_location(
    "nl_summary", "tracker/summary.py"
)
summary = importlib.util.module_from_spec(_summary_spec)
_summary_spec.loader.exec_module(summary)

_server_spec = importlib.util.spec_from_file_location("nl_server", "backend/server.py")
server = importlib.util.module_from_spec(_server_spec)
_server_spec.loader.exec_module(server)

# server.py imports its OWN summary module instance; patch THAT one, not the
# importlib copy above, or the stubs never take effect.
summary = server.summary

FLOOR_H = summary.HUMAN_ATTENTION_FLOOR_MIN_PER_SESSION / 60.0


def _event(session_id, ts, baseline=None):
    ev = {"session_id": session_id, "ts": ts.isoformat()}
    if baseline is not None:
        ev["ai_baseline_hours"] = baseline
    return ev


class ProductivityFloorTests(unittest.TestCase):
    def setUp(self):
        self._orig_unit = summary.PRODUCTIVITY_UNIT
        self._orig_read_tasks = summary.read_tasks

    def tearDown(self):
        summary.PRODUCTIVITY_UNIT = self._orig_unit
        summary.read_tasks = self._orig_read_tasks

    def _stub_tasks(self, tasks):
        summary.read_tasks = lambda: tasks

    def test_empty_input_does_not_crash(self):
        # HIGH: productivity is None branch previously left human vars unbound.
        result = server.productivity_payload([], 5)
        self.assertEqual(result["multiplier"], 0.0)
        self.assertEqual(result["active_hours"], 0.0)

    def test_zero_human_attention_uses_floor_not_ai_active(self):
        # Two sessions, prompts far apart (no real transcripts) → human attention
        # merges to 0. Denominator must be the per-session floor, not AI-active.
        summary.PRODUCTIVITY_UNIT = "session"
        self._stub_tasks({
            "s1": {"ai_baseline_hours": 5.0},
            "s2": {"ai_baseline_hours": 5.0},
        })
        base = datetime(2026, 5, 16, tzinfo=timezone.utc)
        events = [
            _event("s1", base),
            _event("s1", base + timedelta(minutes=90)),
            _event("s2", base + timedelta(hours=5)),
            _event("s2", base + timedelta(hours=5, minutes=90)),
        ]
        result = server.productivity_payload(events, 5)
        expected_floor = 2 * FLOOR_H  # 2 distinct sessions
        # Denominator (published as active_hours) is the per-session floor — not 0,
        # not AI-active wall-clock — even though measured human attention is exactly
        # 0. This is the core HIGH fix: sparse prompting still costs the floor, and
        # autonomous AI runtime never re-enters the denominator here.
        self.assertAlmostEqual(result["active_hours"], expected_floor, places=4)
        self.assertEqual(result["human_attention_hours"], 0.0)
        # The multiplier divides by that floor (numerator is 0 in this synthetic
        # fixture because the trivial-session ceiling clamps the unearned baseline),
        # so the multiplier is well-defined rather than an explosion.
        self.assertEqual(
            round(result["multiplier"], 6),
            round(result["hours_without_ai_estimate"] / expected_floor, 6),
        )

    def test_chunk_mode_floor_uses_distinct_sessions_not_chunks(self):
        # One session spanning 3 calendar days → 3 chunks but 1 distinct session.
        # Floor must be 1×, not 3×.
        summary.PRODUCTIVITY_UNIT = "chunk"
        self._stub_tasks({"s1": {"ai_baseline_hours": 9.0}})
        events = []
        for day in range(3):
            d = datetime(2026, 5, 16 + day, 12, tzinfo=timezone.utc)
            events.append(_event("s1", d))
            events.append(_event("s1", d + timedelta(minutes=90)))
        result = server.productivity_payload(events, 5)
        expected_floor = 1 * FLOOR_H  # 1 distinct session, not 3 chunks
        self.assertAlmostEqual(result["active_hours"], expected_floor, places=4)


if __name__ == "__main__":
    unittest.main()


class FloorDrivenDenominatorTests(unittest.TestCase):
    """A multiplier computed against the safety floor is not a measurement.

    2026-08-18: the estimator had been disabled for two weeks, so the 7d window
    kept only 2 covered sessions carrying 4.7 minutes of measured attention.
    The floor (2 x 5 min) became the divisor and the dashboard published x144.
    """

    def _payload(self, **over):
        base = {
            "active_hours": 0.167,
            "active_hours_per_session_sum": 0.167,
            "calendar_span_hours": 8.0,
            "multiplier": 144.0,
            "hours_saved": 23.8,
            "sessions_covered": 2,
            "sessions_total": 13,
            "unit": "chunk",
        }
        base.update(over)
        return base

    def test_floor_driven_multiplier_is_suppressed(self):
        block = server._productivity_block(self._payload(denominator_is_floor=True))
        self.assertEqual(block["multiplier"], 0.0)
        self.assertEqual(block["hours_saved"], 0.0)

    def test_measured_denominator_passes_through(self):
        block = server._productivity_block(self._payload(
            denominator_is_floor=False, active_hours=67.2, multiplier=4.5, hours_saved=236.2,
        ))
        self.assertAlmostEqual(block["multiplier"], 4.5, places=2)
        self.assertAlmostEqual(block["hours_saved"], 236.2, places=1)

    def test_absent_flag_behaves_as_measured(self):
        block = server._productivity_block(self._payload(multiplier=4.5, hours_saved=236.2))
        self.assertAlmostEqual(block["multiplier"], 4.5, places=2)
