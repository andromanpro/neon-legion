"""Daily accounting must preserve unknown work and exclude the open local day."""

import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "tracker", ROOT / "backend"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import summary  # noqa: E402
import server  # noqa: E402


def day_events(session_id, day):
    start = datetime.fromisoformat(f"{day}T10:00:00")
    return [{"ts": (start + timedelta(minutes=index)).isoformat(),
             "session_id": session_id, "model": "claude-opus-4-7",
             "event_id": f"{session_id}:{day}:{index}"}
            for index in range(6)]


class DailyAccountingTests(unittest.TestCase):
    def setUp(self):
        self.tasks = {"long": {"ai_baseline_hours": 12.0},
                      "long:2026-05-01": {"ai_baseline_hours": 3.0}}
        self.early = day_events("long", "2026-05-01")
        self.late = day_events("long", "2026-05-02")
        self.enterContext(patch.object(summary, "PRODUCTIVITY_UNIT", "chunk"))
        self.enterContext(patch.object(summary, "read_tasks", side_effect=lambda: self.tasks))
        self.enterContext(patch.object(summary, "current_local_date", return_value="2026-05-03", create=True))
        self.enterContext(patch.object(summary, "read_human_attention_cache", return_value={}))
        self.real_lifetime_reader = summary.read_lifetime_session_event_counts
        self.enterContext(patch.object(summary, "read_lifetime_session_event_counts", return_value={"long": 12}))

    def test_partial_breakdown_uses_only_daily_hours_and_attention(self):
        result = summary.summarize_productivity(self.early + self.late)
        early = summary.summarize_productivity(self.early)

        self.assertAlmostEqual(result["hours_without_ai"], 3.0)
        self.assertAlmostEqual(result["human_attention_hours_with_ai"], early["human_attention_hours_with_ai"])
        self.assertEqual(result["active_hours_with_ai"], early["active_hours_with_ai"])
        self.assertEqual(result["calendar_hours_with_ai"], early["calendar_hours_with_ai"])
        self.assertEqual(result["units_eligible"], 2)
        self.assertEqual(result["units_estimated"], 1)
        self.assertEqual(result["units_unknown"], 1)
        self.assertEqual(result["units_open"], 0)
        self.assertEqual(result["sessions_covered"], 1)
        self.assertEqual(result["sessions_total"], 2)
        self.assertEqual(result["distinct_sessions_covered"], 1)
        self.assertFalse(result["window_complete"])

    def test_closed_window_does_not_drift_when_ledger_grows(self):
        # Exercise a real append and the old fingerprinted lifetime reader.
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "events.jsonl"
            ledger.write_text("".join(json.dumps(event) + "\n" for event in self.early + self.late), encoding="utf-8")
            with patch.object(summary, "read_lifetime_session_event_counts", self.real_lifetime_reader), \
                 patch.object(summary, "_event_file_specs", return_value=((ledger, "anthropic"),)):
                summary.reset_lifetime_event_count_cache()
                before = summary.summarize_productivity(self.early + self.late)
                explicit_before = summary.summarize_productivity(self.early)
                with ledger.open("a", encoding="utf-8") as target:
                    for event in day_events("long", "2026-05-03"):
                        target.write(json.dumps(event) + "\n")
                after = summary.summarize_productivity(self.early + self.late)
                explicit_after = summary.summarize_productivity(self.early)
                summary.reset_lifetime_event_count_cache()

        self.assertEqual(before["hours_without_ai"], after["hours_without_ai"])
        self.assertEqual(explicit_before, explicit_after)
        self.assertEqual(before, after)

    def test_window_becomes_complete_only_after_every_closed_pair_is_estimated(self):
        events = self.early + self.late
        incomplete = summary.summarize_productivity(events)
        self.assertEqual(incomplete["sessions_covered"], 1)
        self.assertFalse(incomplete["window_complete"])

        self.tasks["long:2026-05-02"] = {"ai_baseline_hours": 4.0}
        complete = summary.summarize_productivity(events)
        self.assertEqual(complete["hours_without_ai"], 7.0)
        self.assertEqual(complete["units_eligible"], 2)
        self.assertEqual(complete["units_estimated"], 2)
        self.assertEqual(complete["units_unknown"], 0)
        self.assertTrue(complete["window_complete"])

    def test_open_day_does_not_contribute_even_with_a_daily_estimate(self):
        self.tasks["long:2026-05-03"] = {"ai_baseline_hours": 4.0}
        result = summary.summarize_productivity(self.early + day_events("long", "2026-05-03"))
        self.assertEqual(result["hours_without_ai"], 3.0)
        self.assertEqual(result["sessions_total"], 1)
        self.assertEqual(result["sessions_covered"], 1)
        self.assertEqual(result["units_open"], 1)
        self.assertEqual(result["units_unknown"], 0)
        self.assertTrue(result["window_complete"])

    def test_empty_task_store_preserves_unknown_and_open_counts(self):
        self.tasks.clear()
        result = summary.summarize_productivity(self.early + day_events("long", "2026-05-03"))
        self.assertIsNotNone(result)
        self.assertEqual(result["hours_without_ai"], 0.0)
        self.assertEqual(result["human_attention_hours_with_ai"], 0.0)
        self.assertEqual(result["units_eligible"], 1)
        self.assertEqual(result["units_estimated"], 0)
        self.assertEqual(result["units_unknown"], 1)
        self.assertEqual(result["units_open"], 1)
        self.assertFalse(result["window_complete"])

    def test_empty_window_is_not_complete(self):
        result = summary.summarize_productivity([])
        self.assertIsNotNone(result)
        self.assertEqual(result["units_eligible"], 0)
        self.assertEqual(result["units_open"], 0)
        self.assertFalse(result["window_complete"])

    def test_zero_estimate_is_known_but_missing_numeric_estimate_is_unknown(self):
        self.tasks["long:2026-05-01"] = {"ai_baseline_hours": 0.0}
        self.tasks["long:2026-05-02"] = {"ai_baseline_hours": None}
        result = summary.summarize_productivity(self.early + self.late)
        self.assertEqual(result["sessions_covered"], 1)
        self.assertEqual(result["units_estimated"], 1)
        self.assertEqual(result["units_unknown"], 1)
        # The existing active-time baseline floor still applies to an explicit zero.
        self.assertEqual(result["baseline_floor_clamped"], 1)
        self.assertFalse(result["window_complete"])

    def test_diagnostics_are_not_inputs_to_daily_accounting(self):
        with patch.object(summary, "read_lifetime_session_event_counts", side_effect=AssertionError("lifetime read")), \
             patch.object(summary, "_known_chunk_event_counts", side_effect=AssertionError("metadata read")):
            result = summary.summarize_productivity(self.early + self.late)
        self.assertEqual(result["hours_without_ai"], 3.0)

    def test_backend_preserves_nonzero_partial_multiplier_and_coverage(self):
        with patch.object(summary, "_human_attention_hours_for_units", return_value=(1.0, 0)):
            payload = server.productivity_payload(self.early + self.late, 2)
        block = server._productivity_block(payload)
        self.assertEqual(block["multiplier"], 3.0)
        self.assertEqual(block["estimated_hours"], 3.0)
        for value in (payload, block):
            self.assertEqual(value["units_eligible"], 2)
            self.assertEqual(value["units_estimated"], 1)
            self.assertEqual(value["units_unknown"], 1)
            self.assertEqual(value["units_open"], 0)
            self.assertFalse(value["window_complete"])
        self.assertFalse(block["multiplier_complete"])

    def test_complete_multiplier_requires_unsuppressed_ratio(self):
        for floor, multiplier, saved, expected in ((False, 3.0, 2.0, True),
                                                    (True, 3.0, 2.0, False),
                                                    (False, 0.5, -0.5, False)):
            with self.subTest(floor=floor, multiplier=multiplier):
                block = server._productivity_block({"active_hours": 1.0, "multiplier": multiplier,
                    "hours_saved": saved, "denominator_is_floor": floor, "window_complete": True,
                    "units_eligible": 1, "units_estimated": 1, "units_unknown": 0, "units_open": 0})
                self.assertEqual(block["multiplier_complete"], expected)

    def test_window_attention_covers_every_day_including_the_open_one(self):
        # 2026-09-16: the active-time panel read 0.0 h on a working day and 8.3 h
        # for a week, because attention was measured over estimated days only.
        events = self.early + self.late + day_events("long", "2026-05-03")
        result = summary.summarize_productivity(events)
        covered = summary.summarize_productivity(self.early)["human_attention_hours_with_ai"]

        self.assertGreater(covered, 0.0)
        self.assertAlmostEqual(result["human_attention_hours_with_ai"], covered)
        self.assertAlmostEqual(result.get("window_human_attention_hours") or 0.0, 3 * covered)
        self.assertAlmostEqual(result.get("window_calendar_hours") or 0.0, 3 * result["calendar_hours_with_ai"])

    def test_period_block_shows_window_hours_but_keeps_the_ratio_on_estimated_days(self):
        hours_per_unit = lambda units, *args, **kwargs: (float(len(units)), 0)  # noqa: E731
        with patch.object(summary, "_human_attention_hours_for_units", side_effect=hours_per_unit):
            payload = server.productivity_payload(self.early + self.late, 2)
        block = server._productivity_block(payload)

        self.assertEqual(block["estimate_active_hours"], 1.0)
        self.assertEqual(block["active_hours"], 2.0)
        self.assertEqual(block["estimated_hours"], 3.0)
        self.assertEqual(block["multiplier"], 3.0)
        self.assertEqual(block["hours_saved"], 2.0)

    def test_day_blocks_give_each_day_its_own_numbers(self):
        # 2026-09-16, owner: the dashboard must show how a given day went.
        self.tasks["long:2026-05-02"] = {"ai_baseline_hours": None}
        events = (self.early + self.late + day_events("long", "2026-05-03")
                  + day_events("long", "2026-04-02"))
        hours_per_unit = lambda units, *args, **kwargs: (float(len(units)), 0)  # noqa: E731
        with patch.object(summary, "_human_attention_hours_for_units", side_effect=hours_per_unit):
            days = server._productivity_days(events, date(2026, 5, 3), 2, days=30)

        self.assertEqual(sorted(days), ["2026-05-01", "2026-05-02", "2026-05-03"])
        estimated = days["2026-05-01"]
        self.assertTrue(estimated["multiplier_complete"])
        self.assertEqual(estimated["multiplier"], 3.0)
        self.assertEqual((estimated["units_estimated"], estimated["units_eligible"]), (1, 1))
        self.assertEqual(estimated["days"], 1)
        pending = days["2026-05-02"]
        self.assertFalse(pending["multiplier_complete"])
        self.assertEqual((pending["units_estimated"], pending["units_eligible"]), (0, 1))
        self.assertEqual(pending["active_hours"], 1.0)
        open_day = days["2026-05-03"]
        self.assertEqual(open_day["units_open"], 1)
        self.assertEqual(open_day["active_hours"], 1.0)
        self.assertIsNone(open_day["estimated_hours"])

    def test_day_blocks_exist_only_in_chunk_mode(self):
        with patch.object(summary, "PRODUCTIVITY_UNIT", "session"):
            self.assertEqual(server._productivity_days(self.early, date(2026, 5, 3), 2), {})

    def test_prompt_timestamps_are_read_once_per_snapshot_build(self):
        # Day blocks multiply transcript reads; within one build each transcript
        # is read once, outside a build always from disk.
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "t.jsonl"
            transcript.write_text("", encoding="utf-8")
            tasks = {"s": {"transcript_path": str(transcript)}}
            units = [("s", "2026-05-01"), ("s", "2026-05-02")]
            reads = []

            def reader(path):
                reads.append(str(path))
                return []

            with patch.object(summary, "read_human_message_timestamps", side_effect=reader):
                summary._human_attention_hours_for_units(units, tasks, {})
                summary._human_attention_hours_for_units(units, tasks, {})
                self.assertEqual(len(reads), 2)
                with summary.human_timestamp_memo():
                    summary._human_attention_hours_for_units(units, tasks, {})
                    summary._human_attention_hours_for_units(units, tasks, {})
                self.assertEqual(len(reads), 3)
                summary._human_attention_hours_for_units(units, tasks, {})
                self.assertEqual(len(reads), 4)

    def test_open_day_shows_its_hours_and_leaves_the_estimate_pending(self):
        # The open day cannot be estimated yet; zeros read as "nothing happened".
        today = day_events("long", "2026-05-03")
        hours_per_unit = lambda units, *args, **kwargs: (1.5 * len(units), 0)  # noqa: E731
        for tasks in ({}, {"long:2026-05-03": {"ai_baseline_hours": 4.0}}):
            with self.subTest(estimated=bool(tasks)):
                self.tasks = tasks
                with patch.object(summary, "_human_attention_hours_for_units", side_effect=hours_per_unit):
                    payload = server.productivity_payload(today, 2)
                block = server._today_productivity_block(payload)
                self.assertEqual(block["active_hours"], 1.5)
                self.assertEqual(block["estimate_active_hours"], 1.5)
                self.assertIsNone(block["estimated_hours"])
                self.assertIsNone(block["hours_saved"])
                self.assertEqual(block["multiplier"], 0.0)
                self.assertEqual(block["sessions_covered"], 0)
                self.assertEqual(block["sessions_total"], 1)
                self.assertEqual(block["units_open"], 1)
                self.assertEqual(block["units_eligible"], 0)
                self.assertEqual(block["units_estimated"], 0)
                self.assertEqual(block["units_unknown"], 0)
                self.assertFalse(block["window_complete"])
                self.assertFalse(block["multiplier_complete"])

    def test_today_guard_clears_multiplier_completeness(self):
        for active, multiplier in ((0.2, 3.0), (1.0, 60.0)):
            with self.subTest(active=active, multiplier=multiplier):
                block = server._today_productivity_block({"active_hours": active,
                    "multiplier": multiplier, "hours_saved": active * (multiplier - 1),
                    "window_complete": True})
                self.assertEqual(block["multiplier"], 0.0)
                self.assertFalse(block["multiplier_complete"])
