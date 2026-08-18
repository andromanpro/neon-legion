"""tracker/backfill.py must append to the ledger, never rewrite it.

It used to read the whole file, build a merged copy and os.replace it —
which contradicted the project's append-only contract exactly where it
mattered: a Stop hook appending during that window lost its line, and a
writer that died mid-copy left a full-size orphan behind.
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("backfill", ROOT / "tracker" / "backfill.py")
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def event(uuid: str) -> dict:
    return {
        "schema_version": 1,
        "ts": "2026-08-18T10:00:00.000Z",
        "session_id": "s1",
        "message_uuid": uuid,
        "event_id": f"claude:s1:{uuid}",
        "model": "claude-opus-5",
    }


class AppendOnlyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._saved = (mod.TRACKER_DIR, mod.EVENTS_FILE)
        mod.TRACKER_DIR = root
        mod.EVENTS_FILE = root / "claude-events.jsonl"

    def tearDown(self) -> None:
        mod.TRACKER_DIR, mod.EVENTS_FILE = self._saved
        self._tmp.cleanup()

    def lines(self) -> list[dict]:
        return [
            json.loads(line)
            for line in mod.EVENTS_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_existing_bytes_are_not_moved(self) -> None:
        mod.EVENTS_FILE.write_text(json.dumps(event("old")) + "\n", encoding="utf-8")
        before = mod.EVENTS_FILE.read_bytes()
        mod.append_events([event("new")])
        after = mod.EVENTS_FILE.read_bytes()
        self.assertEqual(after[: len(before)], before)
        self.assertEqual([e["message_uuid"] for e in self.lines()], ["old", "new"])

    def test_no_temp_copy_of_the_ledger_is_created(self) -> None:
        mod.EVENTS_FILE.write_text(json.dumps(event("old")) + "\n", encoding="utf-8")
        mod.append_events([event("new")])
        leftovers = [p.name for p in Path(mod.TRACKER_DIR).iterdir() if ".tmp." in p.name]
        self.assertEqual(leftovers, [])

    def test_unterminated_ledger_is_not_concatenated_onto(self) -> None:
        mod.EVENTS_FILE.write_text(json.dumps(event("old")), encoding="utf-8")  # no newline
        mod.append_events([event("new")])
        self.assertEqual([e["message_uuid"] for e in self.lines()], ["old", "new"])

    def test_empty_batch_touches_nothing(self) -> None:
        mod.append_events([])
        self.assertFalse(mod.EVENTS_FILE.exists())


if __name__ == "__main__":
    unittest.main()
