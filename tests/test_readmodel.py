import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tracker") not in sys.path:
    sys.path.insert(0, str(ROOT / "tracker"))

from backend import readmodel
import summary


DOCUMENTED_EVENT_FIELDS = {
    "provider",
    "ts",
    "session_id",
    "message_uuid",
    "model",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "total_tokens",
    "cost_estimate_usd",
    "duration_ms",
    "working_dir",
    "tool_uses",
    "stop_reason",
}

# DeepSeek audit on #60 (HIGH 1+2): the fast path also surfaces these for
# dedup parity with the slow path. Slow path may or may not include them
# depending on what was in the raw JSONL — they are NOT documented baseline.
FAST_PATH_DEDUP_FIELDS = {
    "event_id",
    "tracking_run_id",
    "cached_input_tokens",
    "reasoning_tokens",
    "exit_code",
}


@contextmanager
def temporary_events_dir():
    temp = tempfile.TemporaryDirectory(dir=ROOT, ignore_cleanup_errors=True)
    try:
        probe = Path(temp.name) / ".probe"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            yield temp.name
            return
        except PermissionError:
            pass
    finally:
        try:
            temp.cleanup()
        except PermissionError:
            temp._finalizer.detach()

    fallback = ROOT / f"tmpreadmodel{uuid.uuid4().hex[:8]}"
    os.mkdir(fallback)
    try:
        yield str(fallback)
    finally:
        shutil.rmtree(fallback)


def write_jsonl(directory, filename, rows):
    path = Path(directory) / filename
    with path.open("w", encoding="utf-8") as target:
        for row in rows:
            if isinstance(row, str):
                target.write(row + "\n")
            else:
                target.write(json.dumps(row, separators=(",", ":")) + "\n")


def event(day, session_id="s1", provider=None, **extra):
    row = {
        "ts": f"2026-05-{day:02d}T10:00:00+03:00",
        "session_id": session_id,
        "model": "m",
        "input_tokens": 1,
        "output_tokens": 2,
        "total_tokens": 3,
    }
    if provider is not None:
        row["provider"] = provider
    row.update(extra)
    return row


def bus_event(transition, task_id="ulid:01HQZBUS0000000000000", **extra):
    row = {
        "schema_version": 1,
        "provider": "bus",
        "ts": "2026-05-13T22:30:00.123+03:00",
        "task_id": task_id,
        "session_id": task_id,
        "kind": "echo",
        "transition": transition,
        "exec_id": "win-codex-01-1700000000-abc123",
        "target_host": "win-codex-01",
        "issue_number": 50,
        "lease_seconds": 600,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_estimate_usd": 0,
        "duration_ms": 0,
    }
    row.update(extra)
    return row


class ReadModelTests(unittest.TestCase):
    def test_build_empty_dir_returns_connection(self):
        with temporary_events_dir() as tmp:
            conn = readmodel.build(Path(tmp))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bus_tasks").fetchone()[0], 0)
            conn.close()

    def test_build_populates_events_from_one_provider(self):
        with temporary_events_dir() as tmp:
            write_jsonl(tmp, "claude-events.jsonl", [event(10), event(11), event(12)])
            conn = readmodel.build(Path(tmp))
            rows = conn.execute("SELECT provider, COUNT(*) FROM events GROUP BY provider").fetchall()
            self.assertEqual(rows, [("claude", 3)])
            conn.close()

    def test_build_populates_events_from_all_providers(self):
        with temporary_events_dir() as tmp:
            write_jsonl(tmp, "claude-events.jsonl", [event(10)])
            write_jsonl(tmp, "codex-events.jsonl", [event(10, provider="openai")])
            write_jsonl(tmp, "openclaw-events.jsonl", [event(10, provider="openrouter")])
            write_jsonl(tmp, "opencode-events.jsonl", [event(10, provider="opencode")])
            conn = readmodel.build(Path(tmp))
            providers = {
                row[0]
                for row in conn.execute("SELECT DISTINCT provider FROM events").fetchall()
            }
            self.assertEqual(providers, {"claude", "codex", "openclaw", "opencode"})
            conn.close()

    def test_build_handles_corrupt_line(self):
        with temporary_events_dir() as tmp:
            write_jsonl(tmp, "claude-events.jsonl", [event(10), "{bad json", event(11)])
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                conn = readmodel.build(Path(tmp))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 2)
            self.assertIn("[readmodel]", stderr.getvalue())
            self.assertIn("corrupt JSON skipped", stderr.getvalue())
            conn.close()

    def test_build_handles_missing_optional_fields(self):
        with temporary_events_dir() as tmp:
            write_jsonl(tmp, "claude-events.jsonl", [{"ts": "2026-05-10T10:00:00+03:00", "provider": "anthropic"}])
            conn = readmodel.build(Path(tmp))
            row = conn.execute(
                "SELECT session_id, message_uuid, model, input_tokens, cost_estimate_usd FROM events"
            ).fetchone()
            self.assertEqual(row, (None, None, None, 0, 0.0))
            conn.close()

    def test_build_populates_tasks_from_json(self):
        with temporary_events_dir() as tmp:
            tasks = {
                "s1": {"brief_description": "one", "ai_baseline_hours": 1.5, "needs_manual_review": False},
                "s2": {"brief_description": "two", "human_corrected_hours": 2, "profanity_count": 3},
            }
            (Path(tmp) / "tasks.json").write_text(json.dumps(tasks), encoding="utf-8")
            conn = readmodel.build(Path(tmp))
            rows = conn.execute(
                "SELECT session_id, brief_description FROM tasks ORDER BY session_id"
            ).fetchall()
            self.assertEqual(rows, [("s1", "one"), ("s2", "two")])
            conn.close()

    def test_build_with_meta_returns_counts(self):
        with temporary_events_dir() as tmp:
            write_jsonl(tmp, "claude-events.jsonl", [event(10), event(11)])
            (Path(tmp) / "tasks.json").write_text(json.dumps({"s1": {}, "s2": {}}), encoding="utf-8")
            conn, meta = readmodel.build_with_meta(Path(tmp))
            self.assertEqual(meta["events"], 2)
            self.assertEqual(meta["tasks"], 2)
            self.assertEqual(meta["bus_tasks"], 0)
            self.assertIn("built_at", meta)
            conn.close()

    def test_bus_events_jsonl_populates_bus_tasks(self):
        with temporary_events_dir() as tmp:
            write_jsonl(
                tmp,
                "bus-events.jsonl",
                [bus_event("claimed"), bus_event("in-progress"), bus_event("done")],
            )
            conn, meta = readmodel.build_with_meta(Path(tmp))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bus_tasks").fetchone()[0], 3)
            self.assertEqual(meta["bus_tasks"], 3)
            rows = conn.execute(
                "SELECT task_id, kind, transition, exec_id, target_host, issue_number, lease_seconds "
                "FROM bus_tasks ORDER BY id"
            ).fetchall()
            self.assertEqual([row[2] for row in rows], ["claimed", "in-progress", "done"])
            self.assertEqual(rows[0][0], "ulid:01HQZBUS0000000000000")
            self.assertEqual(rows[0][1], "echo")
            self.assertEqual(rows[0][3], "win-codex-01-1700000000-abc123")
            self.assertEqual(rows[0][4], "win-codex-01")
            self.assertEqual(rows[0][5], 50)
            self.assertEqual(rows[0][6], 600)
            conn.close()

    def test_bus_events_corrupt_line_skipped(self):
        with temporary_events_dir() as tmp:
            write_jsonl(tmp, "bus-events.jsonl", [bus_event("claimed"), "{bad json"])
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                conn, meta = readmodel.build_with_meta(Path(tmp))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bus_tasks").fetchone()[0], 1)
            self.assertEqual(meta["bus_tasks"], 1)
            self.assertIn("[readmodel]", stderr.getvalue())
            self.assertIn("corrupt JSON skipped", stderr.getvalue())
            conn.close()

    def test_read_events_filters_by_date(self):
        with temporary_events_dir() as tmp:
            rows = [event(9, session_id="s9"), event(10, session_id="s10"), event(11, session_id="s11")]
            write_jsonl(tmp, "claude-events.jsonl", rows)
            conn = readmodel.build(Path(tmp))
            result = readmodel.read_events(conn, date(2026, 5, 10), date(2026, 5, 10))
            self.assertEqual([row["session_id"] for row in result], ["s10"])
            conn.close()

    def test_read_events_filters_by_provider(self):
        with temporary_events_dir() as tmp:
            write_jsonl(tmp, "claude-events.jsonl", [event(10, session_id="claude")])
            write_jsonl(tmp, "codex-events.jsonl", [event(10, session_id="codex", provider="openai")])
            conn = readmodel.build(Path(tmp))
            result = readmodel.read_events(
                conn,
                date(2026, 5, 10),
                date(2026, 5, 10),
                providers=["claude"],
            )
            self.assertEqual([row["session_id"] for row in result], ["claude"])
            conn.close()

    def test_read_events_decodes_raw_json(self):
        with temporary_events_dir() as tmp:
            original = event(10, session_id="raw", provider="anthropic", custom={"x": 1})
            write_jsonl(tmp, "claude-events.jsonl", [original])
            conn = readmodel.build(Path(tmp))
            result = readmodel.read_events(conn, date(2026, 5, 10), date(2026, 5, 10))
            self.assertEqual(result, [json.loads(json.dumps(original, separators=(",", ":")))])
            conn.close()

    def test_read_events_fast_returns_same_shape(self):
        with temporary_events_dir() as tmp:
            original = event(
                10,
                session_id="shape",
                provider="claude",
                message_uuid="msg-1",
                cache_read_tokens=4,
                cache_creation_tokens=5,
                cost_estimate_usd=1.25,
                duration_ms=600,
                working_dir="/work",
                tool_uses=2,
                stop_reason="end_turn",
            )
            write_jsonl(tmp, "claude-events.jsonl", [original])
            conn = readmodel.build(Path(tmp))

            slow = readmodel.read_events(conn, date(2026, 5, 10), date(2026, 5, 10))
            fast = readmodel.read_events_fast(conn, date(2026, 5, 10), date(2026, 5, 10))

            self.assertEqual(len(fast), 1)
            # Fast path returns documented fields + extra dedup fields.
            self.assertTrue(DOCUMENTED_EVENT_FIELDS <= set(fast[0]))
            self.assertTrue(FAST_PATH_DEDUP_FIELDS <= set(fast[0]))
            self.assertEqual(set(slow[0]) & DOCUMENTED_EVENT_FIELDS, DOCUMENTED_EVENT_FIELDS)
            for field in DOCUMENTED_EVENT_FIELDS:
                self.assertEqual(fast[0][field], slow[0][field])
            conn.close()

    def test_read_events_fast_omits_raw_json(self):
        with temporary_events_dir() as tmp:
            write_jsonl(tmp, "claude-events.jsonl", [event(10, provider="claude")])
            conn = readmodel.build(Path(tmp))
            result = readmodel.read_events_fast(conn, date(2026, 5, 10), date(2026, 5, 10))
            self.assertNotIn("raw_json", result[0])
            conn.close()

    def test_read_events_fast_handles_nulls(self):
        with temporary_events_dir() as tmp:
            write_jsonl(
                tmp,
                "claude-events.jsonl",
                [
                    {
                        "provider": "claude",
                        "ts": "2026-05-10T10:00:00+03:00",
                        "session_id": None,
                        "message_uuid": None,
                        "model": None,
                        "working_dir": None,
                    }
                ],
            )
            conn = readmodel.build(Path(tmp))
            result = readmodel.read_events_fast(conn, date(2026, 5, 10), date(2026, 5, 10))
            self.assertEqual(result[0]["session_id"], None)
            self.assertEqual(result[0]["message_uuid"], None)
            self.assertEqual(result[0]["model"], None)
            self.assertEqual(result[0]["working_dir"], None)
            conn.close()

    def test_read_events_fast_filters(self):
        with temporary_events_dir() as tmp:
            write_jsonl(tmp, "claude-events.jsonl", [event(10, session_id="claude", provider="claude")])
            write_jsonl(
                tmp,
                "codex-events.jsonl",
                [
                    event(10, session_id="codex-in", provider="codex"),
                    event(11, session_id="codex-out", provider="codex"),
                ],
            )
            conn = readmodel.build(Path(tmp))
            result = readmodel.read_events_fast(
                conn,
                date(2026, 5, 10),
                date(2026, 5, 10),
                providers=["codex"],
            )
            self.assertEqual([row["session_id"] for row in result], ["codex-in"])
            conn.close()

    def test_indexes_exist(self):
        with temporary_events_dir() as tmp:
            conn = readmodel.build(Path(tmp))
            indexes = {
                row[1]
                for row in conn.execute("PRAGMA index_list('events')").fetchall()
            }
            self.assertTrue({"idx_events_ts", "idx_events_session", "idx_events_provider"} <= indexes)
            bus_indexes = {
                row[1]
                for row in conn.execute("PRAGMA index_list('bus_tasks')").fetchall()
            }
            self.assertTrue({"idx_bus_tasks_task", "idx_bus_tasks_ts"} <= bus_indexes)
            conn.close()

    # DeepSeek audit on #60 (HIGH #1) — dedup by event_id parity with slow path.
    def test_read_events_fast_dedupes_by_event_id_like_slow(self):
        # Two events with the SAME event_id but different token counts —
        # slow path drops the second by event_id; fast path now matches.
        ts = "2026-05-13T12:00:00+03:00"
        line1 = json.dumps({
            "event_id": "evt-001",
            "ts": ts,
            "model": "gpt-5",
            "input_tokens": 100,
            "output_tokens": 50,
            "session_id": "sess-A",
            "provider": "openai",
        })
        line2 = json.dumps({
            "event_id": "evt-001",
            "ts": ts,
            "model": "gpt-5",
            "input_tokens": 999,  # different — would be distinct without event_id dedup
            "output_tokens": 50,
            "session_id": "sess-A",
            "provider": "openai",
        })
        with temporary_events_dir() as tmp:
            (Path(tmp) / "codex-events.jsonl").write_text(line1 + "\n" + line2 + "\n", encoding="utf-8")
            conn = readmodel.build(Path(tmp))
            try:
                fast = readmodel.read_events_fast(conn)
                slow = readmodel.read_events(conn, date(2026, 5, 13), date(2026, 5, 13))
                self.assertEqual(len(fast), 1, "fast path must dedupe by event_id")
                self.assertEqual(len(slow), 1, "slow path baseline")
                self.assertEqual(fast[0]["input_tokens"], slow[0]["input_tokens"])
            finally:
                conn.close()

    # DeepSeek audit on #60 (HIGH #2) — legacy-key fields surfaced so events
    # differing in cached_input_tokens / reasoning_tokens / exit_code remain
    # distinct (not collapsed to one row).
    def test_read_events_fast_keeps_events_differing_in_legacy_fields(self):
        ts = "2026-05-13T12:00:00+03:00"
        # No event_id — fall through to legacy key. Same session+model+ts+tokens
        # but DIFFERENT cached_input_tokens. Slow path keeps both; fast path now does too.
        line1 = json.dumps({
            "ts": ts,
            "model": "gpt-5",
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_input_tokens": 10,
            "session_id": "sess-B",
            "provider": "openai",
        })
        line2 = json.dumps({
            "ts": ts,
            "model": "gpt-5",
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_input_tokens": 20,  # different
            "session_id": "sess-B",
            "provider": "openai",
        })
        with temporary_events_dir() as tmp:
            (Path(tmp) / "codex-events.jsonl").write_text(line1 + "\n" + line2 + "\n", encoding="utf-8")
            conn = readmodel.build(Path(tmp))
            try:
                fast = readmodel.read_events_fast(conn)
                slow = readmodel.read_events(conn, date(2026, 5, 13), date(2026, 5, 13))
                self.assertEqual(len(fast), 2, "fast path must distinguish events differing in cached_input_tokens")
                self.assertEqual(len(slow), 2, "slow path baseline")
                self.assertEqual(
                    sorted(e["cached_input_tokens"] for e in fast),
                    sorted(e["cached_input_tokens"] for e in slow),
                )
            finally:
                conn.close()

    def test_read_events_fast_provider_matches_slow_path(self):
        # DeepSeek MED #3: json_provider verbatim from JSONL on both paths.
        line = json.dumps({
            "event_id": "evt-prov",
            "ts": "2026-05-13T12:00:00+03:00",
            "model": "claude-opus-4",
            "input_tokens": 1,
            "provider": "anthropic",
            "session_id": "sess-P",
        })
        with temporary_events_dir() as tmp:
            (Path(tmp) / "claude-events.jsonl").write_text(line + "\n", encoding="utf-8")
            conn = readmodel.build(Path(tmp))
            try:
                fast = readmodel.read_events_fast(conn)
                slow = readmodel.read_events(conn, date(2026, 5, 13), date(2026, 5, 13))
                self.assertEqual(fast[0]["provider"], slow[0]["provider"])
                self.assertEqual(fast[0]["provider"], "anthropic")
            finally:
                conn.close()

    def test_aggregate_by_model_matches_summarize_by_model(self):
        with temporary_events_dir() as tmp:
            write_jsonl(
                tmp,
                "claude-events.jsonl",
                [
                    event(
                        10,
                        session_id="claude-1",
                        provider="anthropic",
                        model="claude-sonnet-4",
                        input_tokens=10,
                        output_tokens=5,
                        cache_read_tokens=2,
                        cached_input_tokens=3,
                        cache_creation_tokens=4,
                        reasoning_tokens=1,
                        total_tokens=19,
                        cost_estimate_usd=0.12,
                    ),
                    event(
                        10,
                        session_id="dedupe",
                        provider="anthropic",
                        model="claude-sonnet-4",
                        event_id="evt-dedupe",
                        input_tokens=100,
                        output_tokens=50,
                        cost_estimate_usd=1.0,
                    ),
                    event(
                        10,
                        session_id="dedupe",
                        provider="anthropic",
                        model="claude-sonnet-4",
                        event_id="evt-dedupe",
                        input_tokens=999,
                        output_tokens=50,
                        cost_estimate_usd=9.0,
                    ),
                ],
            )
            write_jsonl(
                tmp,
                "codex-events.jsonl",
                [
                    event(
                        10,
                        session_id="codex-1",
                        provider="openai",
                        model="gpt-5",
                        input_tokens=7,
                        output_tokens=8,
                        total_tokens=15,
                        cost_estimate_usd=0.34,
                    )
                ],
            )
            conn = readmodel.build(Path(tmp))
            try:
                fast = readmodel.read_events_fast(conn, date(2026, 5, 10), date(2026, 5, 10))
                expected_by_model, expected_total = summary.summarize_by_model(fast)
                actual_by_model, actual_total = readmodel.aggregate_by_model(
                    conn,
                    date(2026, 5, 10),
                    date(2026, 5, 10),
                )
                self.assertEqual(actual_by_model, expected_by_model)
                self.assertEqual(actual_total, expected_total)
            finally:
                conn.close()

    def test_aggregate_by_model_handles_unknown_pricing(self):
        with temporary_events_dir() as tmp:
            write_jsonl(
                tmp,
                "claude-events.jsonl",
                [
                    event(
                        10,
                        provider="anthropic",
                        model="claude-mystery-7",
                        input_tokens=11,
                        output_tokens=13,
                    )
                ],
            )
            conn = readmodel.build(Path(tmp))
            try:
                fast = readmodel.read_events_fast(conn, date(2026, 5, 10), date(2026, 5, 10))
                expected_by_model, expected_total = summary.summarize_by_model(fast)
                actual_by_model, actual_total = readmodel.aggregate_by_model(
                    conn,
                    date(2026, 5, 10),
                    date(2026, 5, 10),
                )
                self.assertEqual(
                    actual_by_model["anthropic/claude-mystery-7"]["unknown_pricing_events"],
                    expected_by_model["anthropic/claude-mystery-7"]["unknown_pricing_events"],
                )
                self.assertEqual(
                    actual_total["unknown_pricing_events"],
                    expected_total["unknown_pricing_events"],
                )
            finally:
                conn.close()

    def test_aggregate_by_model_filters_by_provider(self):
        with temporary_events_dir() as tmp:
            write_jsonl(
                tmp,
                "claude-events.jsonl",
                [event(10, session_id="claude", provider="anthropic", model="claude-sonnet-4")],
            )
            write_jsonl(
                tmp,
                "codex-events.jsonl",
                [event(10, session_id="codex", provider="openai", model="gpt-5")],
            )
            conn = readmodel.build(Path(tmp))
            try:
                fast = readmodel.read_events_fast(
                    conn,
                    date(2026, 5, 10),
                    date(2026, 5, 10),
                    providers=["claude"],
                )
                expected_by_model, expected_total = summary.summarize_by_model(fast)
                actual_by_model, actual_total = readmodel.aggregate_by_model(
                    conn,
                    date(2026, 5, 10),
                    date(2026, 5, 10),
                    providers=["claude"],
                )
                self.assertEqual(actual_by_model, expected_by_model)
                self.assertEqual(actual_total, expected_total)
                self.assertEqual(set(actual_by_model), {"anthropic/claude-sonnet-4"})
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
