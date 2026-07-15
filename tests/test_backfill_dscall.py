"""Tests for tracker/backfill-dscall.py (direct DeepSeek ds-call.py ingestion)."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "backfill_dscall", ROOT / "tracker" / "backfill-dscall.py"
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


LINE = json.dumps({
    "ts": "2026-07-15T09:52:40",
    "model": "deepseek/deepseek-v4-pro",
    "effort": "high",
    "max_tokens": 20000,
    "prompt_tokens": 25609,
    "completion_tokens": 1794,
    "reasoning_tokens": 701,
    "content_chars": 3231,
    "cost_usd": 0.0208291398,
})


class BuildEventTests(unittest.TestCase):
    def test_field_mapping_and_provider(self) -> None:
        ev = mod.build_event(LINE, 1)
        self.assertEqual(ev["provider"], "openrouter")
        self.assertEqual(ev["model"], "deepseek/deepseek-v4-pro")
        self.assertEqual(ev["input_tokens"], 25609)
        self.assertEqual(ev["output_tokens"], 1794)
        self.assertEqual(ev["reasoning_tokens"], 701)
        self.assertEqual(ev["total_tokens"], 25609 + 1794 + 701)
        # cost taken verbatim from the log (real OpenRouter cost), rounded to 6dp
        self.assertAlmostEqual(ev["cost_estimate_usd"], 0.020829, places=6)
        self.assertEqual(ev["source"], "ds-call")

    def test_session_is_per_calendar_day(self) -> None:
        ev = mod.build_event(LINE, 1)
        self.assertEqual(ev["session_id"], "dscall-2026-07-15")

    def test_event_id_is_deterministic_content_hash(self) -> None:
        a = mod.build_event(LINE, 1)
        b = mod.build_event(LINE, 99)  # different seq, same content
        self.assertEqual(a["event_id"], b["event_id"])
        self.assertTrue(a["event_id"].startswith("dscall-"))

    def test_ts_becomes_tz_aware(self) -> None:
        ev = mod.build_event(LINE, 1)
        parsed = datetime.fromisoformat(ev["ts"])
        self.assertIsNotNone(parsed.tzinfo)

    def test_malformed_line_returns_none(self) -> None:
        self.assertIsNone(mod.build_event("{not json", 1))
        self.assertIsNone(mod.build_event(json.dumps({"model": "x"}), 1))  # no ts


class IdempotencyTests(unittest.TestCase):
    def test_second_run_adds_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "calls.jsonl"
            events = Path(tmp) / "dscall-events.jsonl"
            log.write_text(LINE + "\n" + LINE.replace("09:52:40", "10:00:00") + "\n", encoding="utf-8")

            rc1 = mod.main(["--log-path", str(log), "--events-file", str(events)])
            self.assertEqual(rc1, 0)
            first = events.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(first), 2)

            rc2 = mod.main(["--log-path", str(log), "--events-file", str(events)])
            self.assertEqual(rc2, 0)
            second = events.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(second), 2)  # no duplicates appended

    def test_missing_log_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "dscall-events.jsonl"
            rc = mod.main(["--log-path", str(Path(tmp) / "nope.jsonl"), "--events-file", str(events)])
            self.assertEqual(rc, 0)
            self.assertFalse(events.exists())


if __name__ == "__main__":
    unittest.main()
