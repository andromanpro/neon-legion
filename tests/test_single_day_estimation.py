"""Phase 1c: one calendar day is estimated on its own, with the day loop's input.

Days used to be cut only inside estimate_session: one oracle call for the whole
session, then one per day, and only for sessions the SessionStart hook still
considered unestimated. `--day` estimates a single day with the same input code
and no whole-session call, so the closed-day queue can drive it one day at a time.
"""
import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tracker"))

SENTIMENT_KEYS = {"frustration_score", "appreciation_score", "mood_arc", "sentiment_intensity"}


@pytest.fixture
def estimator(tmp_path):
    spec = importlib.util.spec_from_file_location("day_estimator", ROOT / "tracker/estimate-task.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.TRACKER_DIR = tmp_path
    module.TASKS_FILE = tmp_path / "tasks.json"
    module.TASKS_LOCK_FILE = tmp_path / ".tasks.lock"
    module.LOG_DIR = tmp_path / ".estimation-logs"
    module.ORACLE_PROMPT_FILE = tmp_path / "oracle-prompt.txt"
    module.ORACLE_PROMPT_FILE.write_text("oracle prompt", encoding="utf-8")
    return module


class Oracle:
    """Stub oracle: hands out queued hours (or raises) and records every prompt."""

    def __init__(self, *results):
        self.results = list(results)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if not self.results:
            raise AssertionError("unexpected oracle call")
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return {
            "ai_baseline_hours": float(result),
            "human_corrected_hours": None,
            "brief_description": f"estimate {result}",
            "estimated_at": "2026-09-15T12:00:00+03:00",
            "estimation_confidence": "medium",
            "needs_manual_review": False,
            "frustration_score": 0.1,
            "appreciation_score": 0.0,
            "mood_arc": "calm",
            "sentiment_intensity": "low",
        }


def claude(role, content, timestamp, **flags):
    return {"type": role, "timestamp": timestamp,
            "message": {"role": role, "content": content}, **flags}


def codex(role, text, timestamp):
    kind = "input_text" if role == "user" else "output_text"
    return {"type": "response_item", "timestamp": timestamp,
            "payload": {"type": "message", "role": role,
                        "content": [{"type": kind, "text": text}]}}


LONG_CLAUDE = [
    claude("user", "Day one task", "2026-05-15T10:00:00Z"),
    claude("assistant", [{"type": "text", "text": "Done one"},
                         {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}],
           "2026-05-15T10:01:00Z"),
    claude("user", "Day two task", "2026-05-16T10:00:00Z"),
    claude("assistant", "Working on day two", "2026-05-16T10:02:00Z"),
    claude("user", "This session is being continued from a previous conversation",
           "2026-05-16T11:00:00Z", isCompactSummary=True, isVisibleInTranscriptOnly=True),
    claude("user", "After compaction", "2026-05-16T11:01:00Z"),
    claude("assistant", "Done two", "2026-05-16T11:03:00Z"),
]

LONG_CODEX = [
    {"type": "session_meta", "timestamp": "2026-05-15T09:59:00Z",
     "payload": {"session_id": "long", "source": "vscode"}},
    codex("user", "Day one task", "2026-05-15T10:00:00Z"),
    codex("assistant", "Done one", "2026-05-15T10:01:00Z"),
    codex("user", "Day two task", "2026-05-16T10:00:00Z"),
    {"type": "response_item", "timestamp": "2026-05-16T10:01:00Z",
     "payload": {"type": "function_call", "name": "shell", "arguments": "{}", "call_id": "c1"}},
    codex("assistant", "Done two", "2026-05-16T10:02:00Z"),
]


def write(path, events):
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    return path


def read_tasks(estimator):
    if not estimator.TASKS_FILE.exists():
        return {}
    return json.loads(estimator.TASKS_FILE.read_text(encoding="utf-8"))


def day_of(estimator, events, text):
    event = next(e for e in events if text in json.dumps(e))
    return estimator.chunk_date(estimator.parse_transcript_ts(event["timestamp"]))


def events_on(estimator, events, day):
    return sum(estimator.chunk_date(estimator.parse_transcript_ts(e["timestamp"])) == day for e in events)


@pytest.mark.parametrize("events", [LONG_CLAUDE, LONG_CODEX], ids=["claude", "codex"])
def test_single_day_prompt_equals_the_session_day_loop_prompt(estimator, tmp_path, events):
    path = write(tmp_path / "long.jsonl", events)
    day_two = day_of(estimator, events, "Day two task")
    loop = Oracle(10, 1, 2)
    estimator.run_oracle = loop
    estimator.estimate_session("long", str(path))
    assert len(loop.prompts) == 3  # session + day one + day two

    single = Oracle(3)
    estimator.run_oracle = single
    estimator.estimate_day("long", str(path), day_two)

    assert single.prompts == [loop.prompts[2]]
    assert "Day two task" in single.prompts[0]
    assert "Day one task" not in single.prompts[0]


def test_single_day_writes_only_the_day_entry(estimator, tmp_path):
    path = write(tmp_path / "long.jsonl", LONG_CLAUDE)
    day_two = day_of(estimator, LONG_CLAUDE, "Day two task")
    session_entry = {"ai_baseline_hours": 40.0, "estimated_at": "2026-05-15T12:00:00+03:00",
                     "transcript_path": str(path)}
    estimator.TASKS_FILE.write_text(json.dumps({"long": session_entry}), encoding="utf-8")
    estimator.run_oracle = Oracle(3)

    estimator.estimate_day("long", str(path), day_two)

    tasks = read_tasks(estimator)
    assert tasks["long"] == session_entry
    assert set(tasks) == {"long", f"long:{day_two}"}
    day = tasks[f"long:{day_two}"]
    assert day["ai_baseline_hours"] == 3.0
    assert day["chunk_date"] == day_two
    assert day["source_session_id"] == "long"
    assert day["transcript_path"] == str(path)
    assert day["chunk_event_count"] == events_on(estimator, LONG_CLAUDE, day_two)
    assert day["estimation_mode"] == "calendar-day-single"
    assert day["day_input_version"] == estimator.DAY_INPUT_VERSION == 2
    assert not SENTIMENT_KEYS & set(day)
    assert "replaced_estimates" not in day


def test_reestimation_keeps_the_replaced_value_and_the_human_correction(estimator, tmp_path):
    path = write(tmp_path / "long.jsonl", LONG_CLAUDE)
    day_two = day_of(estimator, LONG_CLAUDE, "Day two task")
    previous = {"ai_baseline_hours": 24.0, "estimated_at": "2026-05-16T19:33:19+03:00",
                "estimation_mode": "calendar-day-chunk-live", "estimation_confidence": "medium",
                "brief_description": "estimated while the day was open",
                "human_corrected_hours": 5.0, "chunk_date": day_two, "source_session_id": "long"}
    estimator.TASKS_FILE.write_text(json.dumps({f"long:{day_two}": previous}), encoding="utf-8")

    estimator.run_oracle = Oracle(11)
    estimator.estimate_day("long", str(path), day_two, reason="partial_day")
    estimator.run_oracle = Oracle(12)
    estimator.estimate_day("long", str(path), day_two, reason="compaction_input")

    day = read_tasks(estimator)[f"long:{day_two}"]
    assert day["ai_baseline_hours"] == 12.0
    assert day["human_corrected_hours"] == 5.0
    assert day["replaced_estimates"] == [
        {"ai_baseline_hours": 24.0, "estimated_at": "2026-05-16T19:33:19+03:00",
         "estimation_mode": "calendar-day-chunk-live", "estimation_confidence": "medium",
         "brief_description": "estimated while the day was open",
         "day_input_version": None, "reason": "partial_day"},
        {"ai_baseline_hours": 11.0, "estimated_at": "2026-09-15T12:00:00+03:00",
         "estimation_mode": "calendar-day-single", "estimation_confidence": "medium",
         "brief_description": "estimate 11", "day_input_version": 2, "reason": "compaction_input"},
    ]


def test_day_mode_failure_writes_nothing_and_keeps_the_hook_lock(estimator, tmp_path, capsys):
    path = write(tmp_path / "long.jsonl", LONG_CLAUDE)
    day_two = day_of(estimator, LONG_CLAUDE, "Day two task")
    tasks = {"long": {"ai_baseline_hours": 40.0, "estimated_at": "2026-05-15T12:00:00+03:00"},
             f"long:{day_two}": {"ai_baseline_hours": 24.0, "estimated_at": "2026-05-16T12:00:00+03:00"}}
    estimator.TASKS_FILE.write_text(json.dumps(tasks), encoding="utf-8")
    estimator.LOG_DIR.mkdir()
    hook_lock = estimator.LOG_DIR / "long.lock"
    hook_lock.write_text("held by the SessionStart hook", encoding="utf-8")
    estimator.run_oracle = Oracle(RuntimeError("codex exited with 1"))

    rc = estimator.main(["--day", day_two, "--reason", "partial_day", "long", str(path)])

    assert rc != 0
    assert read_tasks(estimator) == tasks
    assert hook_lock.exists()
    assert "estimation failed: codex exited with 1" in capsys.readouterr().err


def test_day_without_transcript_events_fails_before_the_oracle(estimator, tmp_path):
    path = write(tmp_path / "long.jsonl", LONG_CLAUDE)
    estimator.run_oracle = Oracle()

    with pytest.raises(ValueError, match="no transcript events"):
        estimator.estimate_day("long", str(path), "2026-01-01")

    assert not estimator.TASKS_FILE.exists()


def test_session_day_loop_marks_day_entries_with_the_input_version(estimator, tmp_path):
    path = write(tmp_path / "long.jsonl", LONG_CLAUDE)
    estimator.run_oracle = Oracle(10, 1, 2)

    estimator.estimate_session("long", str(path))

    days = {key: entry for key, entry in read_tasks(estimator).items() if ":" in key}
    assert len(days) == 2
    assert {entry["day_input_version"] for entry in days.values()} == {2}
    assert {entry["estimation_mode"] for entry in days.values()} == {"calendar-day-chunk-live"}


def test_concurrent_reestimation_keeps_every_replaced_value(estimator, tmp_path):
    # Audit 1c, P2: the history was built before the tasks lock, so a run that
    # landed in between vanished from replaced_estimates.
    path = write(tmp_path / "long.jsonl", LONG_CLAUDE)
    day_two = day_of(estimator, LONG_CLAUDE, "Day two task")
    key = f"long:{day_two}"
    estimator.TASKS_FILE.write_text(
        json.dumps({key: {"ai_baseline_hours": 24.0, "estimated_at": "2026-05-16T19:33:19+03:00"}}),
        encoding="utf-8",
    )
    real_acquire = estimator.acquire_tasks_lock
    raced = []

    def another_run_lands_first(*args, **kwargs):
        if not raced:
            raced.append(True)
            estimator.run_oracle = Oracle(11)
            estimator.estimate_day("long", str(path), day_two, reason="partial_day")
        return real_acquire(*args, **kwargs)

    estimator.run_oracle = Oracle(12)
    estimator.acquire_tasks_lock = another_run_lands_first
    estimator.estimate_day("long", str(path), day_two, reason="codex_blind")

    day = read_tasks(estimator)[key]
    assert day["ai_baseline_hours"] == 12.0
    assert [(r["ai_baseline_hours"], r["reason"]) for r in day["replaced_estimates"]] == [
        (24.0, "partial_day"),
        (11.0, "codex_blind"),
    ]


def test_day_entries_record_when_the_transcript_was_read(estimator, tmp_path, monkeypatch):
    # Audit 1c, P1: estimated_at is stamped when the oracle answers, possibly
    # after midnight; the queue needs the moment the input was taken.
    path = write(tmp_path / "long.jsonl", LONG_CLAUDE)
    day_two = day_of(estimator, LONG_CLAUDE, "Day two task")
    monkeypatch.setattr(estimator, "input_read_time", lambda: "2026-05-16T23:57:00+03:00", raising=False)

    estimator.run_oracle = Oracle(10, 1, 2)
    estimator.estimate_session("long", str(path))
    loop_days = {key: entry for key, entry in read_tasks(estimator).items() if ":" in key}
    assert {entry.get("day_input_read_at") for entry in loop_days.values()} == {"2026-05-16T23:57:00+03:00"}

    monkeypatch.setattr(estimator, "input_read_time", lambda: "2026-05-17T09:00:00+03:00", raising=False)
    estimator.run_oracle = Oracle(3)
    entry = estimator.estimate_day("long", str(path), day_two)
    assert entry.get("day_input_read_at") == "2026-05-17T09:00:00+03:00"
    assert read_tasks(estimator)[f"long:{day_two}"]["day_input_read_at"] == "2026-05-17T09:00:00+03:00"


@pytest.fixture
def rechunk():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from tools import rechunk_estimate
    return rechunk_estimate


@pytest.mark.parametrize("events", [LONG_CLAUDE, LONG_CODEX], ids=["claude", "codex"])
def test_rechunk_tool_builds_the_day_loop_prompt(estimator, rechunk, tmp_path, events):
    # Audit 1c, P1: tools/rechunk_estimate.py was a second, Claude-only copy of
    # the day input (Codex text dropped, compaction summaries kept).
    path = write(tmp_path / "long.jsonl", events)
    day_two = day_of(estimator, events, "Day two task")
    loop = Oracle(10, 1, 2)
    estimator.run_oracle = loop
    estimator.estimate_session("long", str(path))

    plan = rechunk.ChunkPlan(session_id="long", task_key=f"long:{day_two}", date=day_two,
                             transcript_path=path, events=tuple(rechunk.transcript_chunks(path)[day_two]))

    assert rechunk.build_chunk_prompt(estimator, plan) == loop.prompts[2]


def test_rechunk_entries_carry_the_input_version_and_read_time(estimator, rechunk, tmp_path):
    path = write(tmp_path / "long.jsonl", LONG_CODEX)
    day_two = day_of(estimator, LONG_CODEX, "Day two task")
    plan = rechunk.ChunkPlan(session_id="long", task_key=f"long:{day_two}", date=day_two,
                             transcript_path=path, events=tuple(rechunk.transcript_chunks(path)[day_two]),
                             read_at="2026-05-16T23:57:00+03:00")
    estimator.run_oracle = Oracle(4, 5, 6)

    entry = rechunk.estimate_chunk(estimator, plan, samples=3)

    assert entry["ai_baseline_hours"] == 5.0
    assert entry["day_input_version"] == estimator.DAY_INPUT_VERSION
    assert entry["day_input_read_at"] == "2026-05-16T23:57:00+03:00"


def test_rechunk_entry_without_a_read_time_does_not_invent_one(estimator, rechunk, tmp_path):
    # A time taken after the oracle answered would repeat the midnight bug; with
    # no read time the queue falls back to estimated_at, as for old entries.
    path = write(tmp_path / "long.jsonl", LONG_CODEX)
    day_two = day_of(estimator, LONG_CODEX, "Day two task")
    plan = rechunk.ChunkPlan(session_id="long", task_key=f"long:{day_two}", date=day_two,
                             transcript_path=path, events=tuple(rechunk.transcript_chunks(path)[day_two]))
    estimator.run_oracle = Oracle(4)

    entry = rechunk.estimate_chunk(estimator, plan, samples=1)

    assert "day_input_read_at" not in entry


def test_rechunk_plans_record_when_the_transcript_was_read(rechunk, tmp_path):
    path = write(tmp_path / "long.jsonl", LONG_CODEX)

    plans, _stats = rechunk.covered_session_plans({"long": {"ai_baseline_hours": 5.0, "transcript_path": str(path)}})

    assert len(plans) == 2
    assert all(plan.read_at for plan in plans)
