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


class ChunkModeTests(unittest.TestCase):
    def setUp(self):
        self._read_tasks = summary.read_tasks
        self._unit = summary.PRODUCTIVITY_UNIT
        self.addCleanup(self._restore_state)

    def _restore_state(self):
        summary.read_tasks = self._read_tasks
        summary.PRODUCTIVITY_UNIT = self._unit

    def _set_tasks(self, tasks):
        summary.read_tasks = lambda: tasks

    def _set_unit(self, unit):
        summary.PRODUCTIVITY_UNIT = unit

    def _event_at(self, session_id, ts):
        return {
            "ts": ts.isoformat(),
            "session_id": session_id,
            "model": "claude-opus-4-7",
        }

    def test_chunk_date_buckets_same_instant_regardless_of_source_offset(self):
        # The real invariant (Codex-audit MED fix): a UTC transcript timestamp and
        # a local-offset AI timestamp for the SAME instant must land in the SAME
        # day-chunk, else late-night prompts get filtered out of their day. Assert
        # offset-independence rather than pinning a UTC calendar day (which is
        # machine-tz-coupled and encoded the old buggy behaviour).
        utc_z = summary.parse_event_ts("2026-05-15T23:59:00+00:00")
        # Same instant expressed with a +05:00 wall-clock offset.
        offset_five = summary.parse_event_ts("2026-05-16T04:59:00+05:00")
        self.assertEqual(utc_z, offset_five)  # sanity: same moment
        self.assertEqual(
            summary.chunk_date(utc_z),
            summary.chunk_date(offset_five),
        )

    def test_chunk_date_is_local_day_boundary(self):
        # A tz-independent check that bucketing uses the system-local day: a naive
        # datetime is treated as local, and an aware datetime for the same local
        # wall-clock instant buckets identically.
        naive = datetime(2026, 5, 16, 10, 0)
        aware_local = naive.astimezone()
        self.assertEqual(
            summary.chunk_date(naive),
            summary.chunk_date(aware_local),
        )
        self.assertEqual(summary.chunk_date(naive), "2026-05-16")

    def test_default_session_mode_matches_pinned_pre_change_shape(self):
        self._set_unit("session")
        self._set_tasks({
            "s1": {"ai_baseline_hours": 2.0},
            "s2": {"ai_baseline_hours": 1.5},
        })
        base = datetime(2026, 5, 16, tzinfo=timezone.utc)
        events = [
            self._event_at("s1", base),
            self._event_at("s1", base + timedelta(minutes=60)),
            self._event_at("s2", base + timedelta(minutes=180)),
            self._event_at("s2", base + timedelta(minutes=240)),
        ]

        result = summary.summarize_productivity(events, gap_minutes=60)

        self.assertEqual(result, {
            "active_hours_with_ai": 2.0,
            "active_hours_per_session_sum": 2.0,
            # No real transcripts in this synthetic fixture → human attention
            # falls back to pooled AI timestamps; the 60-min event gaps exceed
            # the 30-min HUMAN_ATTENTION_GAP_MINUTES, so merged human time is 0
            # and both sessions are counted as fallbacks.
            "human_attention_hours_with_ai": 0.0,
            "human_attention_fallbacks": 2,
            "calendar_hours_with_ai": 2.0,
            "gap_minutes": 60,
            "hours_without_ai": 3.5,
            "baseline_floor_clamped": 0,
            "hours_floor_added": 0.0,
            "baseline_ceiling_clamped": 0,
            "hours_ceiling_removed": 0.0,
            "baseline_per_event_p95": 1.0,
            "sessions_covered": 2,
            "distinct_sessions_covered": 2,
            "sessions_total": 2,
            "unit": "session",
        })

    def test_chunk_mode_without_chunk_keys_matches_session_aggregate(self):
        self._set_tasks({"s1": {"ai_baseline_hours": 4.0}})
        base = datetime(2026, 5, 15, 23, 59, tzinfo=timezone.utc)
        events = [
            self._event_at("s1", base),
            self._event_at("s1", base + timedelta(minutes=2)),
        ]

        self._set_unit("session")
        session_result = summary.summarize_productivity(events, gap_minutes=2)
        self._set_unit("chunk")
        chunk_result = summary.summarize_productivity(events, gap_minutes=2)

        self.assertEqual(session_result["unit"], "session")
        self.assertEqual(chunk_result["unit"], "chunk")
        self.assertEqual(
            {key: value for key, value in session_result.items() if key != "unit"},
            {key: value for key, value in chunk_result.items() if key != "unit"},
        )

    def test_chunk_mode_uses_day_baselines_and_guards_per_chunk(self):
        self._set_unit("chunk")
        self._set_tasks({
            "marathon:2026-05-15": {"ai_baseline_hours": 2.0},
            "marathon:2026-05-16": {"ai_baseline_hours": 36.0},
        })
        day_one = datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc)
        day_two = datetime(2026, 5, 16, 0, 0, tzinfo=timezone.utc)
        events = [
            self._event_at("marathon", day_one),
            self._event_at("marathon", day_one + timedelta(minutes=30)),
            self._event_at("marathon", day_two),
            self._event_at("marathon", day_two + timedelta(seconds=30)),
        ]

        result = summary.summarize_productivity(events, gap_minutes=60)

        stub_active = 30 / 3600
        self.assertIsNotNone(result)
        self.assertEqual(result["unit"], "chunk")
        self.assertAlmostEqual(result["hours_without_ai"], 2.0 + stub_active, places=6)
        self.assertEqual(result["baseline_floor_clamped"], 0)
        self.assertAlmostEqual(result["hours_floor_added"], 0.0, places=6)
        self.assertEqual(result["baseline_ceiling_clamped"], 1)
        self.assertAlmostEqual(result["hours_ceiling_removed"], 36.0 - stub_active, places=6)
        self.assertAlmostEqual(result["baseline_per_event_p95"], 18.0, places=6)
        self.assertEqual(result["sessions_covered"], 2)
        self.assertEqual(result["sessions_total"], 2)

    def test_today_payload_chunk_mode_threads_unit_and_diagnostics(self):
        self._set_unit("chunk")
        day_one = datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc)
        day_two = datetime(2026, 5, 16, 0, 0, tzinfo=timezone.utc)
        events = [
            self._event_at("marathon", day_one),
            self._event_at("marathon", day_one + timedelta(minutes=30)),
            self._event_at("marathon", day_two),
            self._event_at("marathon", day_two + timedelta(seconds=30)),
        ]

        payload = server._today_payload(
            events,
            sessions_recent=[],
            tasks={
                "marathon:2026-05-15": {"ai_baseline_hours": 2.0},
                "marathon:2026-05-16": {"ai_baseline_hours": 36.0},
            },
            today_session_ids=["marathon"],
        )

        self.assertEqual(payload["unit"], "chunk")
        self.assertEqual(payload["sessions_total"], 2)
        self.assertEqual(payload["estimated_sessions_covered"], 2)
        # _today_payload uses a hardcoded gap_minutes=2 (unlike the sibling
        # summary test which passes gap_minutes=60). At gap=2 the day-one
        # chunk's 30-min inter-event gap yields ~0 active hours, so BOTH
        # 2-event chunks (baseline 2.0 and 36.0, >1h, active≈0) are trivial
        # stubs by the #106-A per-chunk guard → both clamp.
        self.assertEqual(payload["baseline_ceiling_clamped"], 2)
        # removed ≈ (2.0 - 0.0) + (36.0 - 30s) ≈ 37.99
        self.assertAlmostEqual(payload["hours_ceiling_removed"], 38.0, places=1)
        self.assertAlmostEqual(payload["baseline_per_event_p95"], 18.0, places=1)


if __name__ == "__main__":
    unittest.main()
