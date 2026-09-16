"""Phase 1c: the closed-day queue picks the accounting's unit and nothing else.

The 1b accounting (676ff0d) counts a (session, closed local day) pair only when
it carries a day estimate. Before this queue nothing produced those estimates for
a long session: the SessionStart hook re-estimates only sessions without any
estimate, and the day loop inside estimate_session runs only from there.
"""
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "tracker"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import summary  # noqa: E402


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def queue():
    return load("closed_day_queue", "tracker/estimate-closed-days.py")


TODAY = "2026-05-04"
SUMMARY_TEXT = "This session is being continued from a previous conversation"


def ledger(session_id, day, provider="anthropic", origin=None, count=3):
    """Ledger rows as summary.read_events returns them (naive local time)."""
    rows = []
    for index in range(count):
        row = {"ts": f"{day}T10:{index:02d}:00", "session_id": session_id, "provider": provider,
               "model": "claude-opus-4-7" if provider == "anthropic" else "gpt-5.6-sol",
               "event_id": f"{session_id}:{day}:{index}"}
        if origin:
            row["codex_origin"] = origin
        rows.append(row)
    return rows


def claude(role, content, timestamp, **flags):
    return {"type": role, "timestamp": timestamp,
            "message": {"role": role, "content": content}, **flags}


def tool_result(timestamp):
    return claude("user", [{"type": "tool_result", "tool_use_id": "t", "content": "output"}], timestamp)


def write(path, events):
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    return path


def placeholder(*_args):
    return Path("transcript-that-is-not-read.jsonl")


def found(candidates):
    return [(c.session_id, c.day, c.category) for c in candidates]


def test_new_closed_days_of_an_estimated_long_session_are_missing(queue):
    hook = load("nl_session_start_1c", "hooks/claude-session-start.py")
    tasks = {"long": {"ai_baseline_hours": 30.0, "estimated_at": "2026-05-01T20:00:00+03:00"},
             "long:2026-05-01": {"ai_baseline_hours": 6.0, "estimated_at": "2026-05-02T12:00:00+00:00"}}
    events = (ledger("long", "2026-05-01") + ledger("long", "2026-05-02")
              + ledger("long", "2026-05-03") + ledger("long", TODAY))

    # The only queue before 1c: the hook takes a session that has no estimate.
    assert hook.needs_estimation(tasks["long"]) is False
    assert found(queue.build_queue(events, tasks, TODAY, placeholder)) == [
        ("long", "2026-05-03", "missing"),
        ("long", "2026-05-02", "missing"),
    ]


@pytest.mark.parametrize("entry", [
    None,
    {"ai_baseline_hours": 24.0, "estimated_at": f"{TODAY}T12:00:00+00:00"},
    {"ai_baseline_hours": 5.0, "estimated_at": "2026-05-01T12:00:00+00:00"},
], ids=["missing", "estimated-today", "estimated-before-the-fix"])
@pytest.mark.parametrize("provider,origin", [("anthropic", None), ("openai", "desktop")],
                         ids=["claude", "codex"])
def test_open_day_is_never_queued(queue, entry, provider, origin):
    tasks = {} if entry is None else {f"s:{TODAY}": entry}
    assert queue.build_queue(ledger("s", TODAY, provider, origin), tasks, TODAY, placeholder) == []


def test_codex_day_estimated_before_the_input_fix_is_blind_until_reestimated(queue):
    day, today = "2026-09-10", "2026-09-15"
    events = ledger("cx", day, "openai", "desktop")
    before_fix = {"ai_baseline_hours": 7.0, "estimated_at": "2026-09-11T05:00:00+00:00",
                  "estimation_mode": "calendar-day-chunk-live"}

    candidates = queue.build_queue(events, {f"cx:{day}": before_fix}, today, placeholder)
    assert [(c.category, c.hours, c.provider) for c in candidates] == [("codex_blind", 7.0, "codex")]
    assert queue.DAY_INPUT_FIXED_AT < datetime.fromisoformat("2026-09-11T06:00:00+00:00")

    after_fix = dict(before_fix, estimated_at="2026-09-11T12:00:00+00:00")
    assert queue.build_queue(events, {f"cx:{day}": after_fix}, today, placeholder) == []

    reestimated = dict(before_fix, ai_baseline_hours=3.0, estimated_at="2026-09-15T12:00:00+00:00",
                       estimation_mode="calendar-day-single", day_input_version=2)
    assert queue.build_queue(events, {f"cx:{day}": reestimated}, today, placeholder) == []


def compaction_transcript(path, a, b, c):
    return write(path, [
        claude("user", "Prompt A1", f"{a}T09:00:00Z"),
        claude("user", SUMMARY_TEXT, f"{a}T09:05:00Z", isCompactSummary=True, isVisibleInTranscriptOnly=True),
        claude("user", "Prompt A2", f"{a}T09:06:00Z"),
        tool_result(f"{b}T09:00:00Z"),
        claude("user", "Prompt B1", f"{b}T09:01:00Z"),
        claude("user", "Prompt B2", f"{b}T09:02:00Z"),
        claude("user", "Prompt B3", f"{b}T09:03:00Z"),
        claude("user", SUMMARY_TEXT, f"{b}T09:04:00Z", isCompactSummary=True, isVisibleInTranscriptOnly=True),
        tool_result(f"{c}T09:00:00Z"),
        tool_result(f"{c}T09:01:00Z"),
        tool_result(f"{c}T09:02:00Z"),
        claude("user", SUMMARY_TEXT, f"{c}T09:03:00Z", isCompactSummary=True, isVisibleInTranscriptOnly=True),
        claude("user", "Prompt C1", f"{c}T09:04:00Z"),
    ])


def test_old_input_had_the_summary_among_the_first_three_user_messages(queue, tmp_path):
    a, b, c = "2026-09-01", "2026-09-02", "2026-09-03"
    transcript = compaction_transcript(tmp_path / "claude.jsonl", a, b, c)

    # Day B: the summary came fourth. Day C: tool results are not messages.
    assert queue.old_input_compaction_days(transcript, {a, b, c}) == {a, c}

    tasks = {f"cl:{day}": {"ai_baseline_hours": 4.0, "estimated_at": "2026-09-10T12:00:00+00:00"}
             for day in (a, b, c)}
    events = ledger("cl", a) + ledger("cl", b) + ledger("cl", c)
    candidates = queue.build_queue(events, tasks, "2026-09-15", lambda *_: transcript)
    assert found(candidates) == [("cl", c, "compaction_input"), ("cl", a, "compaction_input")]

    fixed = {key: dict(entry, estimated_at="2026-09-12T12:00:00+00:00") for key, entry in tasks.items()}
    assert queue.build_queue(events, fixed, "2026-09-15", lambda *_: transcript) == []


def test_unreadable_transcript_flags_no_compaction(queue, tmp_path):
    assert queue.old_input_compaction_days(tmp_path / "gone.jsonl", {"2026-09-01"}) == set()


def test_unversioned_rechunk_entry_is_old_input_whenever_it_was_written(queue):
    # Audit 1c, P1: tools/rechunk_estimate.py kept the Claude-only reader, so an
    # entry it wrote after DAY_INPUT_FIXED_AT still saw a blind Codex day.
    day, today = "2026-09-10", "2026-09-15"
    events = ledger("cx", day, "openai", "desktop")
    rechunked = {"ai_baseline_hours": 8.0, "estimated_at": "2026-09-12T12:00:00+00:00",
                 "estimation_mode": "calendar-day-chunk"}

    candidates = queue.build_queue(events, {f"cx:{day}": rechunked}, today, placeholder)
    assert [(c.category, c.hours) for c in candidates] == [("codex_blind", 8.0)]

    versioned = dict(rechunked, day_input_version=2)
    assert queue.build_queue(events, {f"cx:{day}": versioned}, today, placeholder) == []


def test_day_read_before_midnight_is_partial_although_the_answer_came_after(queue):
    # Audit 1c, P1: estimated_at is stamped when the oracle answers; the input is
    # what was on disk when the transcript was read.
    day, today = "2026-09-14", "2026-09-16"
    events = ledger("s", day)
    entry = {"ai_baseline_hours": 5.0, "day_input_version": 2,
             "day_input_read_at": datetime(2026, 9, 14, 23, 57).astimezone().isoformat(timespec="seconds"),
             "estimated_at": datetime(2026, 9, 15, 0, 1).astimezone().isoformat(timespec="seconds")}

    candidates = queue.build_queue(events, {f"s:{day}": entry}, today, placeholder)
    assert [c.category for c in candidates] == ["partial_day"]

    read_after_close = dict(entry, day_input_read_at=datetime(2026, 9, 15, 0, 0, 5).astimezone()
                            .isoformat(timespec="seconds"))
    assert queue.build_queue(events, {f"s:{day}": read_after_close}, today, placeholder) == []


def test_estimate_made_before_the_day_closed_is_partial(queue):
    day, today = "2026-09-14", "2026-09-16"
    events = ledger("s", day)
    same_day = {"ai_baseline_hours": 24.0, "estimated_at": f"{day}T12:00:00+00:00"}
    next_day = {"ai_baseline_hours": 24.0, "estimated_at": "2026-09-15T12:00:00+00:00"}

    candidates = queue.build_queue(events, {f"s:{day}": same_day}, today, placeholder)
    assert [(c.category, c.hours) for c in candidates] == [("partial_day", 24.0)]
    assert queue.build_queue(events, {f"s:{day}": next_day}, today, placeholder) == []


def test_order_is_newest_day_then_category_priority(queue, tmp_path):
    # Owner's call (2026-09-15): the dashboard's 7- and 30-day windows complete
    # first; within one day, a wrong value already counted beats an absent one.
    today = "2026-09-20"
    compacted = write(tmp_path / "cl2.jsonl", [
        claude("user", SUMMARY_TEXT, "2026-09-02T09:00:00Z", isCompactSummary=True, isVisibleInTranscriptOnly=True),
    ])
    plain = write(tmp_path / "cl1.jsonl", [claude("user", "Prompt", "2026-09-17T09:00:00Z")])
    transcripts = {"cl1": plain, "cl2": compacted, "cl3": plain, "cx": plain, "cx2": plain}
    events = (ledger("cl1", "2026-09-18") + ledger("cl1", "2026-09-17") + ledger("cl1", "2026-09-19")
              + ledger("cl3", "2026-09-19")
              + ledger("cx", "2026-09-05", "openai", "desktop") + ledger("cx", "2026-09-06", "openai", "desktop")
              + ledger("cx2", "2026-09-06", "openai", "desktop")
              + ledger("cl2", "2026-09-02"))
    tasks = {"cl1:2026-09-17": {"ai_baseline_hours": 2.0, "estimated_at": "2026-09-17T12:00:00+00:00"},
             "cl3:2026-09-19": {"ai_baseline_hours": 4.0, "estimated_at": "2026-09-19T12:00:00+00:00"},
             "cx:2026-09-05": {"ai_baseline_hours": 1.0, "estimated_at": "2026-09-06T12:00:00+00:00"},
             "cx:2026-09-06": {"ai_baseline_hours": 1.0, "estimated_at": "2026-09-07T12:00:00+00:00"},
             "cl2:2026-09-02": {"ai_baseline_hours": 3.0, "estimated_at": "2026-09-03T12:00:00+00:00"}}

    candidates = queue.build_queue(events, tasks, today, lambda sid, _provider: transcripts[sid])

    assert found(candidates) == [
        ("cl3", "2026-09-19", "partial_day"),
        ("cl1", "2026-09-19", "missing"),
        ("cl1", "2026-09-18", "missing"),
        ("cl1", "2026-09-17", "partial_day"),
        ("cx", "2026-09-06", "codex_blind"),
        ("cx2", "2026-09-06", "missing"),
        ("cx", "2026-09-05", "codex_blind"),
        ("cl2", "2026-09-02", "compaction_input"),
    ]
    assert found(queue.runnable(candidates, categories={"missing"})) == [
        ("cl1", "2026-09-19", "missing"),
        ("cl1", "2026-09-18", "missing"),
        ("cx2", "2026-09-06", "missing"),
    ]


def test_day_without_a_transcript_is_listed_but_never_run(queue):
    candidates = queue.build_queue(ledger("gone", "2026-05-02"), {}, TODAY, lambda *_: None)
    assert [(c.category, c.transcript) for c in candidates] == [("missing", None)]
    assert queue.runnable(candidates) == []


def test_unestimated_candidates_equal_the_accounting_units_unknown(queue, monkeypatch):
    today = "2026-05-10"
    events = (ledger("a", "2026-05-01") + ledger("a", "2026-05-02") + ledger("a", "2026-05-09")
              + ledger("a", today)
              + ledger("cx", "2026-05-03", "openai", "desktop") + ledger("cx", "2026-05-04", "openai", "tui")
              + ledger("exec", "2026-05-03", "openai", "headless")
              + ledger("b", "2026-05-05"))
    tasks = {"a:2026-05-01": {"ai_baseline_hours": 2.0, "estimated_at": "2026-05-02T12:00:00+00:00"},
             "a:2026-05-09": {"ai_baseline_hours": None, "needs_manual_review": True},
             "cx:2026-05-03": {"ai_baseline_hours": 0.0, "estimated_at": "2026-05-03T12:00:00+00:00"},
             "b:2026-05-05": {"human_corrected_hours": 1.5}}
    monkeypatch.setattr(summary, "PRODUCTIVITY_UNIT", "chunk")
    monkeypatch.setattr(summary, "read_tasks", lambda: tasks)
    monkeypatch.setattr(summary, "current_local_date", lambda: today)
    monkeypatch.setattr(summary, "read_human_attention_cache", lambda *args, **kwargs: {})

    window = [event for event in events if event["ts"] >= "2026-05-04"]
    for rows, expected in ((events, 3), (window, 2)):
        accounting = summary.summarize_productivity(rows)
        candidates = queue.build_queue(rows, tasks, today, lambda *_: None)
        unestimated = [c for c in candidates if c.category == "missing"]
        assert len(unestimated) == accounting["units_unknown"] == expected


def test_reestimated_day_is_not_taken_again(queue, tmp_path):
    day, today = "2026-09-10", "2026-09-15"
    events = ledger("cx", day, "openai", "desktop")
    tasks = {f"cx:{day}": {"ai_baseline_hours": 24.0, "estimated_at": f"{day}T12:00:00+00:00"}}
    transcript = write(tmp_path / "cx.jsonl", [])
    calls, lines = [], []

    def runner(candidate, timeout):
        calls.append((candidate.session_id, candidate.day, candidate.category, timeout))
        tasks[f"cx:{day}"] = dict(tasks[f"cx:{day}"], ai_baseline_hours=6.0, day_input_version=2,
                                  estimated_at=datetime.now().astimezone().isoformat(timespec="seconds"))
        return 0, ""

    candidates = queue.build_queue(events, tasks, today, lambda *_: transcript)
    counts = queue.run_queue(candidates, runner, lambda: tasks, tmp_path / "attempts.json",
                             timeout=7, out=lines.append)

    assert calls == [("cx", day, "codex_blind", 7)]
    assert counts == {"ok": 1, "partial": 0, "failed": 0}
    assert any("24.0" in line and "6.0" in line for line in lines)
    assert queue.build_queue(events, tasks, today, lambda *_: transcript) == []


def test_exit_code_zero_without_a_fresh_estimate_is_a_failure(queue, tmp_path):
    day, today = "2026-09-14", "2026-09-16"
    tasks = {f"s:{day}": {"ai_baseline_hours": 9.0, "day_input_version": 2,
                          "estimated_at": f"{day}T12:00:00+00:00"}}
    transcript = write(tmp_path / "s.jsonl", [])
    candidates = queue.build_queue(ledger("s", day), tasks, today, lambda *_: transcript)
    assert [c.category for c in candidates] == ["partial_day"]

    counts = queue.run_queue(candidates, lambda candidate, timeout: (0, ""), lambda: tasks,
                             tmp_path / "attempts.json", timeout=5, out=lambda *_: None)

    assert counts == {"ok": 0, "partial": 0, "failed": 1}


def test_fresh_estimate_from_an_interrupted_run_counts_as_partial(queue, tmp_path):
    day = "2026-05-02"
    tasks = {}
    attempts = tmp_path / "attempts.json"
    attempts.write_text(json.dumps({f"s:{day}": {"failures": 1}}), encoding="utf-8")
    transcript = write(tmp_path / "s.jsonl", [])
    candidates = queue.build_queue(ledger("s", day), tasks, TODAY, lambda *_: transcript,
                                   attempts=queue.read_attempts(attempts))

    def written_then_cut_off(candidate, timeout):
        tasks[f"s:{day}"] = {"ai_baseline_hours": 2.0, "day_input_version": 2,
                             "estimated_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        return None, "таймаут 5s"

    lines = []
    counts = queue.run_queue(candidates, written_then_cut_off, lambda: tasks, attempts, timeout=5,
                             out=lines.append)

    assert counts == {"ok": 0, "partial": 1, "failed": 0}
    assert queue.read_attempts(attempts) == {}
    assert "⚠" in lines[0] and "2.0" in lines[0]


def test_attempt_journal_keeps_what_a_concurrent_run_recorded(queue, tmp_path):
    # Audit 1c, P2: the journal was read once before the loop and written back
    # whole, dropping another run's records.
    attempts = tmp_path / "attempts.json"
    transcript = write(tmp_path / "s.jsonl", [])
    candidates = queue.build_queue(ledger("s", "2026-05-02"), {}, TODAY, lambda *_: transcript)

    def failing_while_another_run_writes(candidate, timeout):
        attempts.write_text(json.dumps({"other:2026-05-01": {"failures": 2}}), encoding="utf-8")
        return 1, "estimation failed: codex exited with 1"

    queue.run_queue(candidates, failing_while_another_run_writes, lambda: {}, attempts, timeout=5,
                    out=lambda *_: None)

    journal = queue.read_attempts(attempts)
    assert journal["other:2026-05-01"] == {"failures": 2}
    assert journal["s:2026-05-02"]["failures"] == 1


def hold_journal_lock_until_updater_waits(queue, monkeypatch, attempts, lock, handle=None):
    """Thread that keeps the journal lock until record_attempt is seen waiting on it,
    then writes another run's record and releases. The wait is observed through the
    queue's sleep, so the two updates really overlap instead of racing a timer."""
    updater_waiting = threading.Event()
    saw_wait = []
    real_sleep = time.sleep

    def sleep_while_waiting(seconds):
        updater_waiting.set()
        real_sleep(seconds)

    def holder():
        saw_wait.append(updater_waiting.wait(timeout=10))
        attempts.write_text(json.dumps({"other:2026-05-01": {"failures": 2}}), encoding="utf-8")
        if handle is not None:
            os.close(handle)
        lock.unlink()

    monkeypatch.setattr(queue.time, "sleep", sleep_while_waiting)
    thread = threading.Thread(target=holder)
    thread.start()
    return thread, saw_wait


def test_attempt_update_waits_for_a_held_journal_lock(queue, tmp_path, monkeypatch):
    # Re-audit 1c: the concurrent-run test above proves re-reading, not mutual
    # exclusion. Here another run holds the lock and writes before releasing it.
    attempts = tmp_path / "attempts.json"
    lock = tmp_path / "attempts.json.lock"
    lock.write_text("held by another run", encoding="utf-8")

    other, saw_wait = hold_journal_lock_until_updater_waits(queue, monkeypatch, attempts, lock)
    try:
        assert queue.record_attempt(attempts, "s:2026-05-02", "estimation failed") is True
    finally:
        other.join()

    assert saw_wait == [True]
    journal = queue.read_attempts(attempts)
    assert journal["other:2026-05-01"] == {"failures": 2}
    assert journal["s:2026-05-02"]["failures"] == 1
    assert not lock.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="relies on Windows refusing to delete a file another handle holds open")
def test_live_holder_keeps_a_lock_that_looks_stale(queue, tmp_path, monkeypatch):
    # Third audit 1c: nothing pinned that a lock whose holder is alive survives the
    # stale check. Its mtime is past the threshold, but the holder keeps a handle.
    attempts = tmp_path / "attempts.json"
    lock = tmp_path / "attempts.json.lock"
    lock.write_text("held by a long run", encoding="utf-8")
    old = time.time() - queue.ATTEMPTS_LOCK_STALE_SECONDS - 30
    os.utime(lock, (old, old))
    handle = os.open(str(lock), os.O_WRONLY)

    other, saw_wait = hold_journal_lock_until_updater_waits(queue, monkeypatch, attempts, lock, handle)
    try:
        assert queue.record_attempt(attempts, "s:2026-05-02", "estimation failed") is True
    finally:
        other.join()

    assert saw_wait == [True]
    journal = queue.read_attempts(attempts)
    assert journal["other:2026-05-01"] == {"failures": 2}
    assert journal["s:2026-05-02"]["failures"] == 1


def test_journal_lock_wait_outlasts_the_stale_threshold(queue, tmp_path, monkeypatch):
    # Third audit 1c: the stop test below stubs _acquire_lock and ignores its
    # timeout, so a return to the old 10-second wait went unnoticed.
    waits = []

    def no_lock(lock_path, timeout_seconds=10.0):
        waits.append(timeout_seconds)
        return None

    monkeypatch.setattr(queue, "_acquire_lock", no_lock)

    assert queue.record_attempt(tmp_path / "attempts.json", "s:2026-05-02", "estimation failed") is False
    assert len(waits) == 1 and waits[0] > queue.ATTEMPTS_LOCK_STALE_SECONDS


def test_run_stops_when_an_attempt_cannot_be_recorded(queue, tmp_path, monkeypatch):
    # Re-audit 1c, P2: a failure that is not counted lets a broken day take more
    # oracle calls than the attempt limit allows, so the run must not go on.
    transcript = write(tmp_path / "s.jsonl", [])
    candidates = queue.build_queue(ledger("s", "2026-05-02") + ledger("s", "2026-05-03"), {}, TODAY,
                                   lambda *_: transcript)
    assert [c.day for c in candidates] == ["2026-05-03", "2026-05-02"]
    monkeypatch.setattr(queue, "_acquire_lock", lambda *args, **kwargs: None)
    estimated, lines = [], []

    def failing(candidate, timeout):
        estimated.append(candidate.day)
        return 1, "estimation failed: codex exited with 1"

    counts = queue.run_queue(candidates, failing, lambda: {}, tmp_path / "attempts.json", timeout=5,
                             out=lines.append)

    assert estimated == ["2026-05-03"]
    assert counts == {"ok": 0, "partial": 0, "failed": 1}
    assert any("журнал попыток" in line for line in lines)


def test_failed_day_is_retried_until_the_attempt_limit(queue, tmp_path):
    events = ledger("s", "2026-05-02")
    tasks = {}
    transcript = write(tmp_path / "s.jsonl", [])
    attempts = tmp_path / "attempts.json"

    def failing(candidate, timeout):
        return 1, "estimation failed: codex exited with 1"

    for _ in range(queue.MAX_DAY_ATTEMPTS):
        candidates = queue.build_queue(events, tasks, TODAY, lambda *_: transcript,
                                       attempts=queue.read_attempts(attempts))
        assert len(queue.runnable(candidates)) == 1
        counts = queue.run_queue(queue.runnable(candidates), failing, lambda: tasks, attempts,
                                 timeout=5, out=lambda *_: None)
        assert counts == {"ok": 0, "partial": 0, "failed": 1}

    candidates = queue.build_queue(events, tasks, TODAY, lambda *_: transcript,
                                   attempts=queue.read_attempts(attempts))
    assert candidates[0].failures == queue.MAX_DAY_ATTEMPTS
    assert queue.runnable(candidates) == []
    assert len(queue.runnable(candidates, retry_failed=True)) == 1
    assert tasks == {}

    def succeeding(candidate, timeout):
        tasks["s:2026-05-02"] = {"ai_baseline_hours": 2.0, "day_input_version": 2,
                                 "estimated_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        return 0, ""

    queue.run_queue(queue.runnable(candidates, retry_failed=True), succeeding, lambda: tasks, attempts,
                    timeout=5, out=lambda *_: None)
    assert queue.read_attempts(attempts) == {}


def test_complete_last_day_takes_all_of_yesterday_then_the_limit(queue, tmp_path):
    # 2026-09-16, owner: a day view needs whole days estimated; with newest-first
    # and three pairs per deploy, no day would ever be complete.
    today = "2026-09-17"
    transcript = write(tmp_path / "t.jsonl", [])
    events = (ledger("a", "2026-09-16") + ledger("b", "2026-09-16") + ledger("c", "2026-09-16")
              + ledger("d", "2026-09-16") + ledger("e", "2026-09-15") + ledger("f", "2026-09-14")
              + ledger("g", "2026-09-13") + ledger("h", "2026-09-12") + ledger("a", today))
    candidates = queue.runnable(queue.build_queue(events, {}, today, lambda *_: transcript))
    pairs = lambda selected: [(c.session_id, c.day) for c in selected]  # noqa: E731

    assert pairs(queue.select_for_run(candidates, today, 3)) == [
        ("a", "2026-09-16"), ("b", "2026-09-16"), ("c", "2026-09-16")]
    assert pairs(queue.select_for_run(candidates, today, 3, complete_last_day=True)) == [
        ("a", "2026-09-16"), ("b", "2026-09-16"), ("c", "2026-09-16"), ("d", "2026-09-16"),
        ("e", "2026-09-15"), ("f", "2026-09-14"), ("g", "2026-09-13")]
    assert queue.select_for_run(candidates, today, None) == candidates


def test_open_day_pairs_get_a_provisional_estimate_once_and_again_when_stale(queue, tmp_path):
    # Owner, 2026-09-16: show at least an approximate multiplier for today.
    today = "2026-09-17"
    now = datetime.fromisoformat("2026-09-17T18:00:00+03:00")
    transcript = write(tmp_path / "t.jsonl", [])
    events = (ledger("fresh", today) + ledger("stale", today) + ledger("none", today)
              + ledger("closed", "2026-09-16"))
    tasks = {f"fresh:{today}": {"ai_baseline_hours": 2.0, "day_input_version": 2,
                                "day_input_read_at": "2026-09-17T16:30:00+03:00"},
             f"stale:{today}": {"ai_baseline_hours": 1.0, "day_input_version": 2,
                                "day_input_read_at": "2026-09-17T10:00:00+03:00"}}

    candidates = queue.build_open_day_queue(events, tasks, today, lambda *_: transcript, now)

    assert [(c.session_id, c.category, c.hours) for c in candidates] == [
        ("none", "open_day", None), ("stale", "open_day", 1.0)]
    closed = queue.runnable(queue.build_queue(events, tasks, today, lambda *_: transcript))
    assert [c.session_id for c in closed] == ["closed"]
    selected = queue.select_for_run(closed, today, 0, complete_last_day=True, open_day=candidates)
    assert [(c.session_id, c.day) for c in selected] == [
        ("closed", "2026-09-16"), ("none", today), ("stale", today)]


def test_collect_reads_the_ledger_through_today(queue, monkeypatch):
    # The open day's pairs exist only in today's events; closed_units skips them.
    windows = []
    monkeypatch.setattr(summary, "current_local_date", lambda: "2026-09-17")
    monkeypatch.setattr(summary, "read_tasks", lambda: {})
    monkeypatch.setattr(summary, "read_events", lambda start, end: windows.append((start, end)) or [])

    queue.collect(type("Args", (), {"since": None})())

    assert [end.isoformat() for _start, end in windows] == ["2026-09-17"]


def test_main_passes_the_whole_last_day_to_the_run(queue, monkeypatch, tmp_path):
    today = "2026-09-17"
    transcript = write(tmp_path / "t.jsonl", [])
    events = ledger("a", "2026-09-16") + ledger("b", "2026-09-16") + ledger("c", "2026-09-10")
    taken = []
    monkeypatch.setattr(queue, "ATTEMPTS_FILE", tmp_path / "attempts.json")
    monkeypatch.setattr(queue, "collect", lambda args: (events, {}, today, lambda *_: transcript))
    monkeypatch.setattr(queue, "run_queue", lambda selected, *args, **kwargs: taken.extend(selected)
                        or {"ok": 0, "partial": 0, "failed": 0})

    assert queue.main(["--complete-last-day", "--limit", "0"]) == 0

    assert [(c.session_id, c.day) for c in taken] == [("a", "2026-09-16"), ("b", "2026-09-16")]

    taken.clear()
    events.extend(ledger("t", today))
    assert queue.main(["--complete-last-day", "--include-open-day", "--limit", "0"]) == 0

    assert [(c.session_id, c.day, c.category) for c in taken] == [
        ("a", "2026-09-16", "missing"), ("b", "2026-09-16", "missing"), ("t", today, "open_day")]


def test_list_mode_reports_categories_and_runs_nothing(queue, monkeypatch, tmp_path, capsys):
    events = ledger("s", "2026-05-02") + ledger("s", "2026-05-03", count=1)
    tasks = {"s:2026-05-03": {"ai_baseline_hours": 1.5, "estimated_at": "2026-05-03T12:00:00+00:00"}}
    transcript = write(tmp_path / "s.jsonl", [])
    monkeypatch.setattr(queue, "ATTEMPTS_FILE", tmp_path / "attempts.json")
    monkeypatch.setattr(queue, "collect", lambda args: (events, tasks, TODAY, lambda *_: transcript))
    monkeypatch.setattr(queue, "subprocess_runner", lambda *a: pytest.fail("--list must not estimate"))

    assert queue.main(["--list"]) == 0

    out = capsys.readouterr().out
    assert "partial_day" in out and "missing" in out
    assert "1.5" in out
    assert not (tmp_path / "attempts.json").exists()
