"""Regression tests for estimator inputs and genuine human attention.

The daily probe executes only the input-building statements from production.
This also runs against the old implementation for value-based negative controls,
without invoking an oracle, writing task entries, or copying a message parser.
"""
import ast
import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tracker"))
import codex_transcript  # noqa: E402
import summary  # noqa: E402


@pytest.fixture
def estimator():
    spec = importlib.util.spec_from_file_location("input_estimator", ROOT / "tracker/estimate-task.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def calls_in(tree, function_name):
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function_name)
    return {n.func.id for n in ast.walk(function) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


def daily_input(estimator, events):
    tree = ast.parse(Path(estimator.__file__).read_text(encoding="utf-8"))
    if hasattr(estimator, "build_day_input"):
        # Since phase 1c the day input has one home, shared by the session day
        # loop and `--day`; prove the loop still goes through it. The AST probe
        # below remains for runs against pre-1c code (negative controls).
        assert "build_day_entry" in calls_in(tree, "estimate_session")
        assert "build_day_input" in calls_in(tree, "build_day_entry")
        pairs = [(event, estimator.parse_transcript_ts(event["timestamp"])) for event in events]
        user_messages, assistant_messages, _metrics, context = estimator.build_day_input(pairs)
        return user_messages, assistant_messages, context
    session = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "estimate_session")
    day_loop = next(n for n in ast.walk(session) if isinstance(n, ast.For)
                    and isinstance(n.target, ast.Name) and n.target.id == "date_key")
    day_try = next(n for n in day_loop.body if isinstance(n, ast.Try))
    statements = []
    for node in day_try.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "chunk_prompt"
                                                for t in node.targets):
            break
        statements.append(node)
    else:
        raise AssertionError("Daily input boundary not found")
    assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id in {"run_oracle", "update_task_entry"}
                   for statement in statements for n in ast.walk(statement))
    wrapper = ast.parse("def probe(chunks, date_key):\n    pass\n")
    wrapper.body[0].body = statements + ast.parse(
        "return chunk_user_messages, chunk_assistant_messages, chunk_context"
    ).body
    ast.fix_missing_locations(wrapper)
    namespace = vars(estimator).copy()
    exec(compile(wrapper, estimator.__file__, "exec"), namespace)
    pairs = [(event, estimator.parse_transcript_ts(event["timestamp"])) for event in events]
    return namespace["probe"]({"day": pairs}, "day")


def claude(role, text, timestamp="2026-09-09T10:00:00Z", **flags):
    return {"type": role, "timestamp": timestamp,
            "message": {"role": role, "content": text}, **flags}


def codex(role, text, timestamp="2026-09-09T10:00:00Z"):
    return {"type": "response_item", "timestamp": timestamp,
            "payload": {"type": "message", "role": role,
                        "content": [{"type": "input_text" if role == "user" else "output_text",
                                     "text": text}]}}


def write_transcript(tmp_path, events):
    path = tmp_path / "transcript.jsonl"
    with path.open("w", encoding="utf-8") as target:
        for event in events:
            target.write(json.dumps(event) + "\n")
    return path


@pytest.mark.parametrize("day", ["2026-09-09", "2026-09-10"])
def test_codex_daily_messages_match_session_reader(estimator, tmp_path, day):
    events = [
        {"type": "session_meta", "timestamp": f"{day}T09:00:00Z", "payload": {}},
        codex("user", "<recommended_plugins>injected catalogue", f"{day}T10:00:00Z"),
        codex("user", "# AGENTS.md instructions for a project", f"{day}T10:00:01Z"),
        codex("developer", "App scaffolding", f"{day}T10:00:02Z"),
        codex("user", "Fix today's input", f"{day}T10:01:00Z"),
        codex("assistant", "Today's input is fixed", f"{day}T10:02:00Z"),
    ]
    path = write_transcript(tmp_path, events)
    expected = (["Fix today's input"], ["Today's input is fixed"])
    assert estimator.read_transcript(path) == expected
    users, assistants, context = daily_input(estimator, events)
    assert (users, assistants) == expected
    assert "injected catalogue" not in context
    assert "App scaffolding" not in context


def test_compact_summary_excluded_from_session_context(estimator, tmp_path):
    events = [
        claude("user", "Old weeks of work", isCompactSummary=True, isVisibleInTranscriptOnly=True),
        claude("user", "Today's real task"),
        claude("assistant", "Today's result"),
    ]
    path = write_transcript(tmp_path, events)
    assert estimator.read_transcript(path) == (["Today's real task"], ["Today's result"])
    assert "Old weeks of work" not in estimator.build_truncated_context(str(path))


def test_compact_summary_excluded_from_first_three_daily_prompts(estimator):
    events = [
        {"type": "system", "subtype": "compact_boundary", "timestamp": "2026-09-09T10:00:00Z"},
        claude("user", "Old weeks of work", isCompactSummary=True, isVisibleInTranscriptOnly=True),
        *[claude("user", f"Real task {i}") for i in range(1, 5)],
        claude("assistant", "Done"),
    ]
    users, assistants, context = daily_input(estimator, events)
    assert users[:3] == ["Real task 1", "Real task 2", "Real task 3"]
    assert assistants == ["Done"]
    assert "Old weeks of work" not in context
    assert "Real task 3" in context
    assert "Real task 4" not in context


def attention_events():
    return [
        claude("user", "Start", "2026-09-09T10:00:00Z"),
        claude("user", "Synthetic bridge", "2026-09-09T10:20:00Z",
               isCompactSummary=True, isVisibleInTranscriptOnly=True),
        claude("user", "Continue", "2026-09-09T10:40:00Z"),
    ]


def test_compact_summary_excluded_from_attention_timestamps(tmp_path):
    path = write_transcript(tmp_path, attention_events())
    stamps = summary.read_human_message_timestamps(path)
    assert len(stamps) == 2
    assert [stamp.minute for stamp in stamps] == [0, 40]


def test_compact_summary_cannot_bridge_attention_gap(tmp_path, monkeypatch):
    path = write_transcript(tmp_path, attention_events())
    monkeypatch.setattr(summary, "read_human_attention_cache", lambda: {})
    stamps = [summary.parse_event_ts(event["timestamp"]) for event in attention_events()]
    hours, fallbacks = summary._human_attention_hours_for_units(
        [("session", summary.chunk_date(stamps[0]))],
        {"session": {"transcript_path": str(path)}}, {"session": stamps},
    )
    assert hours == 0.0
    assert fallbacks == 0


@pytest.mark.parametrize("flag, expected", [(True, False), (False, True), (None, True)])
def test_compact_summary_flag_controls_human_classification(flag, expected):
    event = claude("user", "This session is being continued from a previous conversation...",
                   isCompactSummary=flag, isVisibleInTranscriptOnly=True)
    assert summary.is_human_prompt(event) is expected


@pytest.mark.parametrize("content", [
    "A real string prompt",
    [{"type": "tool_result", "content": "Tool output"}, {"type": "text", "text": "A real prompt"}],
])
def test_real_prompt_content_survives_both_input_paths(estimator, tmp_path, content):
    event = claude("user", content)
    path = write_transcript(tmp_path, [event])
    assert summary.is_human_prompt(event)
    assert len(summary.read_human_message_timestamps(path)) == 1
    users, assistants, context = daily_input(estimator, [event])
    assert (users, assistants) == estimator.read_transcript(path)
    assert len(users) == 1
    assert "Tool output" not in context


@pytest.mark.parametrize("event", [
    claude("user", "Subagent prompt", isSidechain=True),
    claude("user", [{"type": "tool_result", "content": "Tool output"}]),
    claude("user", "   "),
    claude("assistant", "Assistant response"),
])
def test_nonhuman_attention_filters_are_preserved(event):
    assert summary.is_human_prompt(event) is False


def test_codex_compacted_history_is_not_replayed(estimator, tmp_path):
    compacted = {
        "type": "compacted", "timestamp": "2026-09-09T10:20:00Z",
        "payload": {"message": "Old synthetic summary", "replacement_history": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Old task"}]},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Old result"}]},
        ]},
    }
    events = [codex("user", "Current task"), compacted,
              codex("assistant", "Current result", "2026-09-09T10:40:00Z")]
    path = write_transcript(tmp_path, events)
    expected = (["Current task"], ["Current result"])
    assert codex_transcript.is_human_prompt(compacted) is False
    assert summary.is_human_prompt(compacted) is False
    assert len(summary.read_human_message_timestamps(path)) == 1
    assert codex_transcript.read_messages(path) == expected
    assert estimator.read_transcript(path) == expected
    users, assistants, context = daily_input(estimator, events)
    assert (users, assistants) == expected
    assert "Old" not in context
