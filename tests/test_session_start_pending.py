"""The SessionStart estimator must queue a session by whether it has an
ESTIMATE, not by whether a tasks.json entry exists.

Regression: backfill-sentiment-markers writes {profanity_count,
appreciation_count, transcript_path} into tasks.json on every deploy. The old
`session_id not in tasks` predicate treated that as "already handled", so the
estimator silently stopped firing on 2026-08-01 and 91 sessions were left with
no baseline — excluded from productivity on both sides of the ratio.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "nl_session_start", ROOT / "hooks" / "claude-session-start.py"
)
session_start = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(session_start)

needs_estimation = session_start.needs_estimation


class NeedsEstimationTests(unittest.TestCase):
    def test_missing_entry_needs_estimation(self):
        self.assertTrue(needs_estimation(None))

    def test_non_dict_entry_needs_estimation(self):
        self.assertTrue(needs_estimation("garbage"))

    def test_sentiment_only_entry_still_needs_estimation(self):
        # The exact shape the sentiment backfill leaves behind — the bug.
        self.assertTrue(needs_estimation({
            "transcript_path": "C:/x/y.jsonl",
            "profanity_count": 0,
            "appreciation_count": 0,
        }))

    def test_entry_with_baseline_is_done(self):
        self.assertFalse(needs_estimation({
            "ai_baseline_hours": 4.0,
            "estimated_at": "2026-08-18T10:00:00+03:00",
        }))

    def test_entry_with_estimated_at_but_null_hours_is_done(self):
        # Oracle ran and legitimately returned no baseline — not a retry case.
        self.assertFalse(needs_estimation({
            "estimated_at": "2026-08-18T10:00:00+03:00",
            "ai_baseline_hours": None,
        }))

    def test_manual_review_entry_is_not_retried_forever(self):
        self.assertFalse(needs_estimation({
            "needs_manual_review": True,
            "brief_description": "transcript not found",
        }))

    def test_human_corrected_only_entry_is_queued(self):
        # No oracle estimate and no manual-review flag → still owed a run.
        self.assertTrue(needs_estimation({"profanity_count": 3}))


class ManualReviewMergeTests(unittest.TestCase):
    """write_manual_review_entry must MERGE, or the fixed queue never drains.

    With the queue keyed on "has an estimate", a session whose transcript
    rotated away is dispatched, fails to find the transcript, and must be
    flagged. If flagging bailed out because a sentiment-only entry already
    existed, that session would be re-dispatched on every SessionStart forever.
    """

    def setUp(self):
        import json, tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self._orig_tasks = session_start.TASKS_FILE
        self._orig_lock = session_start.TASKS_LOCK_FILE
        session_start.TASKS_FILE = root / "tasks.json"
        session_start.TASKS_LOCK_FILE = root / "tasks.lock"
        self.addCleanup(setattr, session_start, "TASKS_FILE", self._orig_tasks)
        self.addCleanup(setattr, session_start, "TASKS_LOCK_FILE", self._orig_lock)
        self._json = json

    def _write(self, data):
        session_start.TASKS_FILE.write_text(self._json.dumps(data), encoding="utf-8")

    def _read(self):
        return self._json.loads(session_start.TASKS_FILE.read_text(encoding="utf-8"))

    def test_merges_into_sentiment_only_entry(self):
        self._write({"s1": {"profanity_count": 2, "appreciation_count": 1}})
        session_start.write_manual_review_entry("s1", "", "transcript not found")
        entry = self._read()["s1"]
        self.assertTrue(entry["needs_manual_review"])
        self.assertEqual(entry["profanity_count"], 2)   # existing data preserved
        self.assertFalse(session_start.needs_estimation(entry))  # queue drains

    def test_does_not_clobber_an_existing_estimate(self):
        self._write({"s1": {"ai_baseline_hours": 5.0, "estimated_at": "2026-08-18T10:00:00+03:00"}})
        session_start.write_manual_review_entry("s1", "", "transcript not found")
        entry = self._read()["s1"]
        self.assertEqual(entry["ai_baseline_hours"], 5.0)
        self.assertNotIn("needs_manual_review", entry)

    def test_creates_entry_when_absent(self):
        self._write({})
        session_start.write_manual_review_entry("s1", "C:/t.jsonl", "transcript not found")
        entry = self._read()["s1"]
        self.assertTrue(entry["needs_manual_review"])
        self.assertEqual(entry["transcript_path"], "C:/t.jsonl")


if __name__ == "__main__":
    unittest.main()
