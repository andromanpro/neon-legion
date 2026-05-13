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

from backend import readmodel


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


class ReadModelTests(unittest.TestCase):
    def test_build_empty_dir_returns_connection(self):
        with temporary_events_dir() as tmp:
            conn = readmodel.build(Path(tmp))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
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
            self.assertIn("built_at", meta)
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

    def test_indexes_exist(self):
        with temporary_events_dir() as tmp:
            conn = readmodel.build(Path(tmp))
            indexes = {
                row[1]
                for row in conn.execute("PRAGMA index_list('events')").fetchall()
            }
            self.assertTrue({"idx_events_ts", "idx_events_session", "idx_events_provider"} <= indexes)
            conn.close()


if __name__ == "__main__":
    unittest.main()
