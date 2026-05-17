import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tracker") not in sys.path:
    sys.path.insert(0, str(ROOT / "tracker"))

import summary  # noqa: E402

ESTIMATOR_PATH = ROOT / "tracker" / "estimate-task.py"
SENTIMENT_KEYS = {
    "frustration_score",
    "appreciation_score",
    "mood_arc",
    "sentiment_intensity",
}


def load_estimator():
    spec = importlib.util.spec_from_file_location("estimate_task", ESTIMATOR_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def transcript_event(role: str, timestamp: str, text: str, tool_count: int = 0) -> dict:
    content = [{"type": "text", "text": text}]
    for index in range(tool_count):
        content.append({"type": "tool_use", "id": f"tool-{index}", "name": "Read"})
    return {
        "type": role,
        "timestamp": timestamp,
        "message": {"role": role, "content": content},
    }


def write_jsonl(path: Path, events: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as target:
        for event in events:
            target.write(json.dumps(event) + "\n")


class LiveChunkEstimationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp_path = Path(self.tempdir.name)
        self.estimator = load_estimator()
        self.estimator.TRACKER_DIR = self.tmp_path
        self.estimator.TASKS_FILE = self.tmp_path / "tasks.json"
        self.estimator.TASKS_LOCK_FILE = self.tmp_path / ".tasks.lock"
        self.estimator.ORACLE_PROMPT_FILE = self.tmp_path / "oracle-prompt.txt"
        self.estimator.ORACLE_PROMPT_FILE.write_text("oracle prompt", encoding="utf-8")

    def read_tasks(self) -> dict:
        if not self.estimator.TASKS_FILE.exists():
            return {}
        return json.loads(self.estimator.TASKS_FILE.read_text(encoding="utf-8"))

    def oracle_entry(self, hours: float, index: int) -> dict:
        return {
            "ai_baseline_hours": float(hours),
            "human_corrected_hours": None,
            "brief_description": f"estimate {hours}",
            "estimated_at": f"2026-05-17T00:00:{index:02d}+00:00",
            "estimation_confidence": "medium",
            "needs_manual_review": False,
            "frustration_score": 0.25,
            "appreciation_score": 0.5,
            "mood_arc": "steady",
            "sentiment_intensity": "medium",
        }

    def set_oracle_results(self, *results):
        remaining = list(results)
        calls = []

        def fake_oracle(prompt: str) -> dict:
            calls.append(prompt)
            if not remaining:
                raise AssertionError("unexpected oracle call")
            result = remaining.pop(0)
            if isinstance(result, BaseException):
                raise result
            return self.oracle_entry(float(result), len(calls))

        self.estimator.run_oracle = fake_oracle
        return calls

    def test_multi_day_session_writes_session_and_one_chunk_per_day(self):
        transcript = self.tmp_path / "multi.jsonl"
        write_jsonl(transcript, [
            transcript_event("user", "2026-05-15T10:00:00Z", "start"),
            transcript_event("assistant", "2026-05-15T10:01:00Z", "working", tool_count=1),
            transcript_event("user", "2026-05-16T00:00:00Z", "continue"),
            transcript_event("assistant", "2026-05-16T00:01:00Z", "done"),
        ])
        day_one = summary.chunk_date(self.estimator.parse_transcript_ts("2026-05-15T10:00:00Z"))
        day_two = summary.chunk_date(self.estimator.parse_transcript_ts("2026-05-16T00:00:00Z"))
        calls = self.set_oracle_results(10, 1, 2)

        self.estimator.estimate_session("multi", str(transcript))

        tasks = self.read_tasks()
        self.assertEqual(set(tasks), {"multi", f"multi:{day_one}", f"multi:{day_two}"})
        self.assertEqual(len(calls), 3)
        self.assertIn("events=2", calls[1])
        self.assertIn("tool_calls=1", calls[1])
        self.assertIn("events=2", calls[2])
        first_chunk = tasks[f"multi:{day_one}"]
        self.assertEqual(first_chunk["source_session_id"], "multi")
        self.assertEqual(first_chunk["chunk_date"], day_one)
        self.assertEqual(first_chunk["chunk_event_count"], 2)
        self.assertEqual(first_chunk["estimation_mode"], "calendar-day-chunk-live")
        self.assertEqual(first_chunk["transcript_path"], str(transcript))
        self.assertEqual(tasks[f"multi:{day_two}"]["ai_baseline_hours"], 2.0)

    def test_past_day_is_frozen_and_latest_day_is_rewritten(self):
        transcript = self.tmp_path / "rewrite.jsonl"
        day_one = "2026-05-15"
        day_two = "2026-05-16"
        write_jsonl(transcript, [
            transcript_event("user", f"{day_one}T10:00:00Z", "past"),
            transcript_event("user", f"{day_two}T10:00:00Z", "latest"),
        ])
        self.set_oracle_results(100, 10, 20)
        self.estimator.estimate_session("rewrite", str(transcript))
        first_tasks = self.read_tasks()
        frozen_entry = dict(first_tasks[f"rewrite:{day_one}"])

        write_jsonl(transcript, [
            transcript_event("user", f"{day_one}T10:00:00Z", "past"),
            transcript_event("user", f"{day_two}T10:00:00Z", "latest"),
            transcript_event("assistant", f"{day_two}T10:01:00Z", "more current work"),
        ])
        calls = self.set_oracle_results(101, 21)
        self.estimator.estimate_session("rewrite", str(transcript))

        tasks = self.read_tasks()
        self.assertEqual(len(calls), 2)
        self.assertEqual(tasks[f"rewrite:{day_one}"], frozen_entry)
        self.assertEqual(tasks["rewrite"]["ai_baseline_hours"], 101.0)
        self.assertEqual(tasks[f"rewrite:{day_two}"]["ai_baseline_hours"], 21.0)
        self.assertEqual(tasks[f"rewrite:{day_two}"]["chunk_event_count"], 2)
        self.assertIn("events=2", calls[1])

    def test_single_day_session_writes_one_live_chunk(self):
        transcript = self.tmp_path / "single.jsonl"
        write_jsonl(transcript, [
            transcript_event("user", "2026-05-15T10:00:00Z", "only day"),
            transcript_event("assistant", "2026-05-15T10:01:00Z", "done"),
        ])
        calls = self.set_oracle_results(5, 2)

        self.estimator.estimate_session("single", str(transcript))

        tasks = self.read_tasks()
        self.assertEqual(set(tasks), {"single", "single:2026-05-15"})
        self.assertEqual(len(calls), 2)
        self.assertEqual(tasks["single:2026-05-15"]["chunk_event_count"], 2)
        self.assertEqual(tasks["single:2026-05-15"]["ai_baseline_hours"], 2.0)

    def test_chunk_failure_does_not_abort_and_failed_past_day_retries(self):
        transcript = self.tmp_path / "failure.jsonl"
        write_jsonl(transcript, [
            transcript_event("user", "2026-05-15T10:00:00Z", "day one"),
            transcript_event("user", "2026-05-16T10:00:00Z", "day two"),
            transcript_event("user", "2026-05-17T10:00:00Z", "day three"),
        ])
        calls = self.set_oracle_results(100, RuntimeError("boom"), 20, 30)
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            self.estimator.estimate_session("failure", str(transcript))

        tasks = self.read_tasks()
        self.assertEqual(len(calls), 4)
        self.assertIn("chunk-estimate-failed\tfailure\t2026-05-15\tboom", stderr.getvalue())
        self.assertIn("failure", tasks)
        self.assertNotIn("failure:2026-05-15", tasks)
        self.assertEqual(tasks["failure:2026-05-16"]["ai_baseline_hours"], 20.0)
        self.assertEqual(tasks["failure:2026-05-17"]["ai_baseline_hours"], 30.0)

        calls = self.set_oracle_results(101, 11, 31)
        self.estimator.estimate_session("failure", str(transcript))

        tasks = self.read_tasks()
        self.assertEqual(len(calls), 3)
        self.assertEqual(tasks["failure:2026-05-15"]["ai_baseline_hours"], 11.0)
        self.assertEqual(tasks["failure:2026-05-16"]["ai_baseline_hours"], 20.0)
        self.assertEqual(tasks["failure:2026-05-17"]["ai_baseline_hours"], 31.0)

    def test_profanity_and_sentiment_stay_on_session_entry_only(self):
        transcript = self.tmp_path / "sentiment.jsonl"
        write_jsonl(transcript, [
            transcript_event("user", "2026-05-15T10:00:00Z", "fuck this"),
            transcript_event("assistant", "2026-05-15T10:01:00Z", "done"),
        ])
        self.set_oracle_results(8, 1)

        self.estimator.estimate_session("sentiment", str(transcript))

        tasks = self.read_tasks()
        session_entry = tasks["sentiment"]
        chunk_entry = tasks["sentiment:2026-05-15"]
        self.assertGreater(session_entry["profanity_count"], 0)
        for key in SENTIMENT_KEYS:
            self.assertIn(key, session_entry)
        self.assertNotIn("profanity_count", chunk_entry)
        for key in SENTIMENT_KEYS:
            self.assertNotIn(key, chunk_entry)


if __name__ == "__main__":
    unittest.main()
