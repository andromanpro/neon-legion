"""Tests for the Stop hook collection contract (hooks/claude-track-calls.py).

Guards the four properties the 2026-08-18 rewrite established, each of
which was a live defect before it:

* every billable assistant message of a turn is recorded, not just the last;
* the event carries the transcript's timestamp, not the hook's wall clock;
* a lock left behind by a dead process is stolen instead of disabling
  collection indefinitely;
* losing the watermark re-emits rather than loses.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "claude_track_calls", ROOT / "hooks" / "claude-track-calls.py"
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


SESSION = "11111111-2222-3333-4444-555555555555"


def assistant_line(uuid: str, timestamp: str, output_tokens: int = 100, tool_uses: int = 1) -> str:
    """One transcript line shaped like a real billable assistant message."""
    content = [{"type": "text", "text": "hi"}]
    content += [{"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {}} for i in range(tool_uses)]
    return json.dumps({
        "type": "assistant",
        "uuid": uuid,
        "timestamp": timestamp,
        "message": {
            "model": "claude-opus-5",
            "stop_reason": "tool_use",
            "content": content,
            "usage": {
                "input_tokens": 4,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": 500,
                "cache_read_input_tokens": 90000,
            },
        },
    })


class HarnessMixin:
    """Redirects the module's file constants into a scratch directory."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._saved = {
            name: getattr(mod, name)
            for name in ("TRACKER_DIR", "EVENTS_FILE", "OPS_FILE", "LAST_UUIDS_FILE", "LOCK_FILE")
        }
        mod.TRACKER_DIR = root
        mod.EVENTS_FILE = root / "claude-events.jsonl"
        mod.OPS_FILE = root / "ops-events.jsonl"
        mod.LAST_UUIDS_FILE = root / ".last-uuids.json"
        mod.LOCK_FILE = root / ".claude-events.lock"
        self.root = root
        self.transcript = root / "transcript.jsonl"

    def tearDown(self) -> None:
        for name, value in self._saved.items():
            setattr(mod, name, value)
        self._tmp.cleanup()

    def write_transcript(self, lines: list[str]) -> None:
        self.transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def run_hook(self) -> int:
        payload = json.dumps({
            "session_id": SESSION,
            "transcript_path": str(self.transcript),
            "cwd": "C:/work/legion",
        })
        saved_stdin = sys.stdin
        sys.stdin = io.StringIO(payload)
        try:
            return mod.main()
        finally:
            sys.stdin = saved_stdin

    def ledger(self) -> list[dict]:
        if not mod.EVENTS_FILE.exists():
            return []
        return [
            json.loads(line)
            for line in mod.EVENTS_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def ops(self) -> list[dict]:
        if not mod.OPS_FILE.exists():
            return []
        return [
            json.loads(line)
            for line in mod.OPS_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


class EveryCallIsRecordedTests(HarnessMixin, unittest.TestCase):
    def test_whole_tool_loop_lands_in_the_ledger(self) -> None:
        """The regression this rewrite exists for: N calls in one turn used
        to produce one entry."""
        self.write_transcript([
            assistant_line("u1", "2026-08-18T10:00:01.000Z"),
            assistant_line("u2", "2026-08-18T10:00:02.000Z"),
            assistant_line("u3", "2026-08-18T10:00:03.000Z"),
            assistant_line("u4", "2026-08-18T10:00:04.000Z"),
        ])
        self.run_hook()
        self.assertEqual([e["message_uuid"] for e in self.ledger()], ["u1", "u2", "u3", "u4"])

    def test_second_turn_only_appends_what_is_new(self) -> None:
        self.write_transcript([
            assistant_line("u1", "2026-08-18T10:00:01.000Z"),
            assistant_line("u2", "2026-08-18T10:00:02.000Z"),
        ])
        self.run_hook()
        self.write_transcript([
            assistant_line("u1", "2026-08-18T10:00:01.000Z"),
            assistant_line("u2", "2026-08-18T10:00:02.000Z"),
            assistant_line("u3", "2026-08-18T10:00:05.000Z"),
        ])
        self.run_hook()
        self.assertEqual([e["message_uuid"] for e in self.ledger()], ["u1", "u2", "u3"])

    def test_idle_turn_appends_nothing(self) -> None:
        self.write_transcript([assistant_line("u1", "2026-08-18T10:00:01.000Z")])
        self.run_hook()
        self.run_hook()
        self.assertEqual(len(self.ledger()), 1)

    def test_synthetic_and_usageless_messages_stay_out(self) -> None:
        self.write_transcript([
            json.dumps({"type": "user", "uuid": "x", "message": {"content": "hi"}}),
            json.dumps({
                "type": "assistant", "uuid": "syn", "timestamp": "2026-08-18T10:00:00.000Z",
                "message": {"model": "<synthetic>", "usage": {"input_tokens": 1}, "content": []},
            }),
            json.dumps({
                "type": "assistant", "uuid": "nousage", "timestamp": "2026-08-18T10:00:00.000Z",
                "message": {"model": "claude-opus-5", "content": []},
            }),
            assistant_line("u1", "2026-08-18T10:00:01.000Z"),
        ])
        self.run_hook()
        self.assertEqual([e["message_uuid"] for e in self.ledger()], ["u1"])

    def test_half_written_tail_line_does_not_lose_the_turn(self) -> None:
        """The session appends while we read; a truncated last line used to
        raise and take every earlier record with it."""
        self.transcript.write_text(
            assistant_line("u1", "2026-08-18T10:00:01.000Z") + "\n"
            + assistant_line("u2", "2026-08-18T10:00:02.000Z") + "\n"
            + '{"type": "assistant", "uuid": "u3", "mess',
            encoding="utf-8",
        )
        self.run_hook()
        self.assertEqual([e["message_uuid"] for e in self.ledger()], ["u1", "u2"])


class SubagentSweepTests(HarnessMixin, unittest.TestCase):
    """A subagent never fires Stop, so the parent's Stop has to collect it.
    6046 such calls were sitting uncounted on disk when this was written."""

    def agent_transcript(self, relative: str, lines: list[str]) -> Path:
        path = self.transcript.with_suffix("") / "subagents" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    @staticmethod
    def agent_line(uuid: str, timestamp: str, session: str = SESSION, cwd: str = "C:/work/other") -> str:
        payload = json.loads(assistant_line(uuid, timestamp))
        payload["isSidechain"] = True
        payload["sessionId"] = session
        payload["cwd"] = cwd
        return json.dumps(payload)

    def test_agent_calls_reach_the_ledger(self) -> None:
        self.write_transcript([assistant_line("m1", "2026-08-18T10:00:01.000Z")])
        self.agent_transcript("agent-aaa.jsonl", [self.agent_line("a1", "2026-08-18T10:00:02.000Z")])
        self.run_hook()
        self.assertEqual([e["message_uuid"] for e in self.ledger()], ["m1", "a1"])

    def test_agent_call_is_attributed_to_the_parent_session(self) -> None:
        self.write_transcript([assistant_line("m1", "2026-08-18T10:00:01.000Z")])
        self.agent_transcript("agent-aaa.jsonl", [self.agent_line("a1", "2026-08-18T10:00:02.000Z")])
        self.run_hook()
        agent_event = [e for e in self.ledger() if e["message_uuid"] == "a1"][0]
        self.assertEqual(agent_event["session_id"], SESSION)
        self.assertEqual(agent_event["event_id"], f"claude:{SESSION}:a1")
        self.assertEqual(agent_event["agent_id"], "agent-aaa")
        self.assertTrue(agent_event["is_sidechain"])
        # cwd of the record, not of the parent hook input.
        self.assertEqual(agent_event["working_dir"], "C:/work/other")

    def test_workflow_agents_are_found_too(self) -> None:
        self.write_transcript([assistant_line("m1", "2026-08-18T10:00:01.000Z")])
        self.agent_transcript(
            "workflows/wf_abc/agent-bbb.jsonl",
            [self.agent_line("w1", "2026-08-18T10:00:03.000Z")],
        )
        self.run_hook()
        event = [e for e in self.ledger() if e["message_uuid"] == "w1"][0]
        self.assertEqual(event["agent_id"], "workflows/wf_abc/agent-bbb")

    def test_same_stem_in_two_directories_are_two_streams(self) -> None:
        """The reason the cursor key is the whole relative path: one cursor
        keyed by stem would hide the other agent's work entirely."""
        self.write_transcript([assistant_line("m1", "2026-08-18T10:00:01.000Z")])
        self.agent_transcript("agent-same.jsonl", [self.agent_line("x1", "2026-08-18T10:00:02.000Z")])
        self.agent_transcript(
            "workflows/wf_abc/agent-same.jsonl",
            [self.agent_line("y1", "2026-08-18T10:00:03.000Z")],
        )
        self.run_hook()
        self.assertEqual(
            sorted(e["message_uuid"] for e in self.ledger()), ["m1", "x1", "y1"],
        )

    def test_each_stream_advances_its_own_cursor(self) -> None:
        self.write_transcript([assistant_line("m1", "2026-08-18T10:00:01.000Z")])
        self.agent_transcript("agent-aaa.jsonl", [self.agent_line("a1", "2026-08-18T10:00:02.000Z")])
        self.run_hook()
        self.agent_transcript(
            "agent-aaa.jsonl",
            [
                self.agent_line("a1", "2026-08-18T10:00:02.000Z"),
                self.agent_line("a2", "2026-08-18T10:00:04.000Z"),
            ],
        )
        self.run_hook()
        self.assertEqual([e["message_uuid"] for e in self.ledger()], ["m1", "a1", "a2"])
        cursors = json.loads(mod.LAST_UUIDS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(cursors[SESSION], "m1")
        self.assertEqual(cursors[f"{SESSION}#agent-aaa"], "a2")

    def test_a_session_without_agents_behaves_as_before(self) -> None:
        self.write_transcript([assistant_line("m1", "2026-08-18T10:00:01.000Z")])
        self.run_hook()
        event = self.ledger()[0]
        self.assertNotIn("agent_id", event)
        self.assertNotIn("is_sidechain", event)

    def test_stream_key_of_event_round_trips(self) -> None:
        self.assertEqual(mod.stream_key_of_event({"session_id": "s"}), "s")
        self.assertEqual(
            mod.stream_key_of_event({"session_id": "s", "agent_id": "workflows/w/agent-1"}),
            "s#workflows/w/agent-1",
        )


class EventShapeTests(HarnessMixin, unittest.TestCase):
    def test_timestamp_comes_from_the_transcript(self) -> None:
        self.write_transcript([assistant_line("u1", "2026-08-18T23:59:59.000Z")])
        self.run_hook()
        self.assertEqual(self.ledger()[0]["ts"], "2026-08-18T23:59:59.000Z")

    def test_event_id_is_the_stable_identity(self) -> None:
        self.write_transcript([assistant_line("u1", "2026-08-18T10:00:01.000Z")])
        self.run_hook()
        self.assertEqual(self.ledger()[0]["event_id"], f"claude:{SESSION}:u1")

    def test_cost_and_tool_uses_survive(self) -> None:
        self.write_transcript([assistant_line("u1", "2026-08-18T10:00:01.000Z", tool_uses=3)])
        self.run_hook()
        event = self.ledger()[0]
        self.assertEqual(event["tool_uses"], 3)
        self.assertEqual(event["model"], "claude-opus-5")
        # 4 in + 100 out + 500 write + 90000 read at the Opus family rate:
        # 0.00002 + 0.0025 + 0.003125 + 0.045.
        self.assertAlmostEqual(event["cost_estimate_usd"], 0.0506, places=4)


class CacheWriteTtlPricingTests(HarnessMixin, unittest.TestCase):
    """A 1-hour cache write costs 2x base input, a 5-minute one 1.25x. Every
    write was charged at the 5-minute rate while 95% of the tokens carried a
    1-hour TTL."""

    @staticmethod
    def line_with_split(uuid: str, one_hour: int, five_minute: int) -> str:
        payload = json.loads(assistant_line(uuid, "2026-08-18T10:00:01.000Z"))
        payload["message"]["usage"] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": one_hour + five_minute,
            "cache_creation": {
                "ephemeral_1h_input_tokens": one_hour,
                "ephemeral_5m_input_tokens": five_minute,
            },
        }
        return json.dumps(payload)

    def cost_of(self, line: str) -> float:
        self.write_transcript([line])
        self.run_hook()
        return self.ledger()[0]["cost_estimate_usd"]

    def test_one_hour_write_is_twice_base_input(self) -> None:
        # 1M tokens at the Opus base rate of $5/1M → $10 at 2x.
        self.assertAlmostEqual(self.cost_of(self.line_with_split("u1", 1_000_000, 0)), 10.0, places=6)

    def test_five_minute_write_is_one_and_a_quarter_base_input(self) -> None:
        self.assertAlmostEqual(self.cost_of(self.line_with_split("u1", 0, 1_000_000)), 6.25, places=6)

    def test_a_mixed_write_is_charged_per_part(self) -> None:
        self.assertAlmostEqual(self.cost_of(self.line_with_split("u1", 400_000, 600_000)), 7.75, places=6)

    def test_the_split_is_stored_beside_the_total(self) -> None:
        self.write_transcript([self.line_with_split("u1", 300, 200)])
        self.run_hook()
        event = self.ledger()[0]
        self.assertEqual(event["cache_creation_tokens"], 500)
        self.assertEqual(event["cache_creation_1h_tokens"], 300)
        self.assertEqual(event["cache_creation_5m_tokens"], 200)

    def test_a_usage_without_a_split_keeps_the_cheaper_answer(self) -> None:
        """Old-shaped usage must not suddenly cost more — understating is the
        safe direction when the TTL is unknown."""
        payload = json.loads(assistant_line("u1", "2026-08-18T10:00:01.000Z"))
        payload["message"]["usage"] = {
            "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 1_000_000,
        }
        self.assertAlmostEqual(self.cost_of(json.dumps(payload)), 6.25, places=6)

    def test_an_unaccounted_remainder_is_flagged(self) -> None:
        payload = json.loads(assistant_line("u1", "2026-08-18T10:00:01.000Z"))
        payload["message"]["usage"] = {
            "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 1000,
            "cache_creation": {"ephemeral_1h_input_tokens": 600, "ephemeral_5m_input_tokens": 100},
        }
        self.write_transcript([json.dumps(payload)])
        self.run_hook()
        self.assertIn("cache_creation_unclassified", [o["code"] for o in self.ops()])

    def test_a_small_call_no_longer_rounds_to_nothing(self) -> None:
        """Rounding each event to cents floored the cheapest calls to zero
        before they were ever summed."""
        payload = json.loads(assistant_line("u1", "2026-08-18T10:00:01.000Z"))
        payload["message"]["usage"] = {
            "input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        self.assertGreater(self.cost_of(json.dumps(payload)), 0.0)


class UnpricedUsageIsVisibleTests(HarnessMixin, unittest.TestCase):
    def test_server_side_tool_calls_raise_an_ops_signal(self) -> None:
        """Web search and web fetch bill separately and no collector counts
        them. Zero so far — the point is that the first one is not silent."""
        payload = json.loads(assistant_line("u1", "2026-08-18T10:00:01.000Z"))
        payload["message"]["usage"]["server_tool_use"] = {"web_search_requests": 3}
        self.write_transcript([json.dumps(payload)])
        self.run_hook()
        self.assertIn("server_tool_use_unpriced", [o["code"] for o in self.ops()])

    def test_a_non_standard_service_tier_raises_an_ops_signal(self) -> None:
        payload = json.loads(assistant_line("u1", "2026-08-18T10:00:01.000Z"))
        payload["message"]["usage"]["service_tier"] = "priority"
        self.write_transcript([json.dumps(payload)])
        self.run_hook()
        self.assertIn("service_tier_unpriced", [o["code"] for o in self.ops()])

    def test_ordinary_usage_stays_quiet(self) -> None:
        """Negative control: an ops channel that cries on every turn is one
        nobody reads."""
        self.write_transcript([assistant_line("u1", "2026-08-18T10:00:01.000Z")])
        self.run_hook()
        self.assertEqual(self.ops(), [])


class WatermarkTests(HarnessMixin, unittest.TestCase):
    def test_losing_the_cursor_re_emits_rather_than_loses(self) -> None:
        self.write_transcript([
            assistant_line("u1", "2026-08-18T10:00:01.000Z"),
            assistant_line("u2", "2026-08-18T10:00:02.000Z"),
        ])
        self.run_hook()
        mod.LAST_UUIDS_FILE.unlink()
        self.run_hook()
        events = self.ledger()
        self.assertEqual(len(events), 4)
        # Re-emitted lines are identical to the originals, so the receiver
        # folds them into one dedupe group instead of counting twice.
        self.assertEqual(events[0], events[2])
        self.assertEqual(events[1], events[3])

    def test_cursor_pointing_at_a_foreign_uuid_re_emits(self) -> None:
        self.write_transcript([assistant_line("u1", "2026-08-18T10:00:01.000Z")])
        mod.LAST_UUIDS_FILE.write_text(json.dumps({SESSION: "not-in-this-transcript"}), encoding="utf-8")
        self.run_hook()
        self.assertEqual(len(self.ledger()), 1)

    def test_corrupt_cursor_file_is_survivable(self) -> None:
        self.write_transcript([assistant_line("u1", "2026-08-18T10:00:01.000Z")])
        mod.LAST_UUIDS_FILE.write_text("{not json", encoding="utf-8")
        self.run_hook()
        self.assertEqual(len(self.ledger()), 1)


class LockTests(HarnessMixin, unittest.TestCase):
    @staticmethod
    def dead_pid() -> int:
        """A pid that is almost certainly not running. Verified through the
        module's own probe so the test fails loudly rather than silently
        asserting nothing on a machine where it happens to be taken."""
        for candidate in (999999, 999997, 999995):
            if not mod.process_alive(candidate):
                return candidate
        raise unittest.SkipTest("no reliably dead pid available")

    def test_lock_of_a_dead_holder_is_stolen(self) -> None:
        mod.LOCK_FILE.write_text(str(self.dead_pid()), encoding="ascii")
        self.write_transcript([assistant_line("u1", "2026-08-18T10:00:01.000Z")])
        self.run_hook()
        self.assertEqual(len(self.ledger()), 1)
        self.assertIn("lock_stale_cleared", [o["code"] for o in self.ops()])

    def test_lock_of_a_live_holder_is_respected(self) -> None:
        mod.LOCK_FILE.write_text(str(os.getpid()), encoding="ascii")
        self.write_transcript([assistant_line("u1", "2026-08-18T10:00:01.000Z")])
        self.run_hook()
        self.assertEqual(self.ledger(), [])
        self.assertIn("lock_unavailable", [o["code"] for o in self.ops()])

    def test_a_live_holder_keeps_its_lock_however_long_it_works(self) -> None:
        """backfill.py holds this while walking every transcript on disk. An
        age-based steal would put two writers on the ledger at once."""
        mod.LOCK_FILE.write_text(str(os.getpid()), encoding="ascii")
        ancient = time.time() - (mod.LOCK_STALE_SECONDS * 10)
        os.utime(mod.LOCK_FILE, (ancient, ancient))
        self.write_transcript([assistant_line("u1", "2026-08-18T10:00:01.000Z")])
        self.run_hook()
        self.assertTrue(mod.LOCK_FILE.exists(), "a live holder must not be evicted")
        self.assertEqual(self.ledger(), [])

    def test_pidless_lock_is_cleared_once_it_ages_out(self) -> None:
        """The age cap still applies when there is no pid to ask about."""
        mod.LOCK_FILE.write_text("", encoding="ascii")
        ancient = time.time() - (mod.LOCK_STALE_SECONDS + 60)
        os.utime(mod.LOCK_FILE, (ancient, ancient))
        self.write_transcript([assistant_line("u1", "2026-08-18T10:00:01.000Z")])
        self.run_hook()
        self.assertEqual(len(self.ledger()), 1)

    def test_a_skipped_run_is_recovered_by_the_next_one(self) -> None:
        """No advancing of the cursor on a miss, so nothing is lost — this is
        what makes fail-open acceptable."""
        mod.LOCK_FILE.write_text(str(os.getpid()), encoding="ascii")
        self.write_transcript([assistant_line("u1", "2026-08-18T10:00:01.000Z")])
        self.run_hook()
        mod.LOCK_FILE.unlink()
        self.run_hook()
        self.assertEqual([e["message_uuid"] for e in self.ledger()], ["u1"])

    def test_process_alive_recognises_this_process(self) -> None:
        """Positive control: without it, a probe that always answered "dead"
        would pass every test above."""
        self.assertTrue(mod.process_alive(os.getpid()))


class LedgerWriteTests(HarnessMixin, unittest.TestCase):
    def test_unterminated_ledger_is_not_concatenated_onto(self) -> None:
        mod.EVENTS_FILE.write_text('{"schema_version":1,"message_uuid":"old"}', encoding="utf-8")
        self.write_transcript([assistant_line("u1", "2026-08-18T10:00:01.000Z")])
        self.run_hook()
        self.assertEqual([e["message_uuid"] for e in self.ledger()], ["old", "u1"])

    def test_writer_leaves_no_temp_files_behind(self) -> None:
        self.write_transcript([assistant_line("u1", "2026-08-18T10:00:01.000Z")])
        self.run_hook()
        leftovers = [p.name for p in self.root.iterdir() if ".tmp." in p.name]
        self.assertEqual(leftovers, [])

    def test_existing_ledger_is_not_rewritten(self) -> None:
        """Append-only: the bytes already on disk must not move."""
        mod.EVENTS_FILE.write_text('{"schema_version":1,"message_uuid":"old"}\n', encoding="utf-8")
        before = mod.EVENTS_FILE.read_bytes()
        self.write_transcript([assistant_line("u1", "2026-08-18T10:00:01.000Z")])
        self.run_hook()
        self.assertEqual(mod.EVENTS_FILE.read_bytes()[: len(before)], before)


class FailureIsVisibleTests(HarnessMixin, unittest.TestCase):
    def test_a_broken_run_leaves_an_ops_record_and_exits_clean(self) -> None:
        self.write_transcript([assistant_line("u1", "2026-08-18T10:00:01.000Z")])
        original = mod.append_new_events
        mod.append_new_events = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("disk on fire"))
        try:
            self.assertEqual(self.run_hook(), 0)
        finally:
            mod.append_new_events = original
        codes = [o["code"] for o in self.ops()]
        self.assertIn("hook_failed", codes)
        self.assertIn("disk on fire", self.ops()[-1]["detail"])

    def test_ops_channel_is_not_a_ledger(self) -> None:
        """Operational records must never land where the dashboard sums."""
        self.assertNotEqual(mod.OPS_FILE.name, mod.EVENTS_FILE.name)
        self.assertFalse(mod.OPS_FILE.name.startswith("claude-"))


if __name__ == "__main__":
    unittest.main()
