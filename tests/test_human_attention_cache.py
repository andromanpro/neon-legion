"""The frozen human-attention cache must keep a session measuring HUMAN time
after its transcript is rotated away — instead of silently reverting to the
AI-busy fallback (which inflates the denominator and slides the multiplier)."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("nl_summary", ROOT / "tracker" / "summary.py")
summary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(summary)

BASE = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def _cache_file(tmp: Path, mapping: dict) -> Path:
    path = tmp / "human-attention.json"
    payload = {
        sid: {"ts": [t.timestamp() for t in stamps], "prompts": len(stamps)}
        for sid, stamps in mapping.items()
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class ReadCacheTests(unittest.TestCase):
    def tearDown(self) -> None:
        summary.reset_human_attention_cache()

    def test_roundtrip_restores_sorted_aware_datetimes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stamps = [BASE + timedelta(minutes=m) for m in (10, 0, 5)]
            path = _cache_file(Path(tmp), {"s1": stamps})
            got = summary.read_human_attention_cache(path)
        self.assertEqual(list(got), ["s1"])
        restored = got["s1"]
        self.assertEqual(len(restored), 3)
        self.assertTrue(all(t.tzinfo is not None for t in restored))
        self.assertEqual(restored, sorted(restored))
        # same instants as written (tz representation may differ)
        self.assertEqual([t.timestamp() for t in restored], sorted(t.timestamp() for t in stamps))

    def test_missing_or_corrupt_file_degrades_to_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(summary.read_human_attention_cache(Path(tmp) / "nope.json"), {})
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            self.assertEqual(summary.read_human_attention_cache(bad), {})


class CacheRescuesRotatedTranscriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_cache_file = summary.HUMAN_ATTENTION_CACHE_FILE

    def tearDown(self) -> None:
        summary.HUMAN_ATTENTION_CACHE_FILE = self._orig_cache_file
        summary.reset_human_attention_cache()

    def test_gone_transcript_uses_cache_not_ai_fallback(self) -> None:
        # Session ran 6h of AI wall-clock, but the human only prompted twice,
        # 2 minutes apart → ~2 min of human attention.
        human = [BASE, BASE + timedelta(minutes=2)]
        # AI events every 2 min for 6h — dense enough to merge under the 5-min
        # gap, i.e. ~6h of continuous "AI was busy" runtime.
        ai = [BASE + timedelta(minutes=m) for m in range(0, 361, 2)]

        with tempfile.TemporaryDirectory() as tmp:
            path = _cache_file(Path(tmp), {"s1": human})
            summary.reset_human_attention_cache()
            summary.HUMAN_ATTENTION_CACHE_FILE = path

            # tasks has NO usable transcript_path → live read yields nothing
            tasks = {"s1": {"transcript_path": str(Path(tmp) / "gone.jsonl")}}
            hours, fallbacks = summary._human_attention_hours_for_units(
                [("s1", None)], tasks, {"s1": ai}, gap_minutes=5
            )

        # Cache served it: no fallback, and hours reflect HUMAN time (~2 min),
        # not the 6h of AI runtime.
        self.assertEqual(fallbacks, 0)
        self.assertLess(hours, 0.2)

    def test_without_cache_same_session_falls_back_to_ai_time(self) -> None:
        ai = [BASE + timedelta(minutes=m) for m in range(0, 361, 2)]
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty.json"
            empty.write_text("{}", encoding="utf-8")
            summary.reset_human_attention_cache()
            summary.HUMAN_ATTENTION_CACHE_FILE = empty

            tasks = {"s1": {"transcript_path": str(Path(tmp) / "gone.jsonl")}}
            hours, fallbacks = summary._human_attention_hours_for_units(
                [("s1", None)], tasks, {"s1": ai}, gap_minutes=5
            )

        # No cache → AI-busy time leaks into the denominator (the bug this fixes)
        self.assertEqual(fallbacks, 1)
        self.assertGreater(hours, 5.0)


if __name__ == "__main__":
    unittest.main()
