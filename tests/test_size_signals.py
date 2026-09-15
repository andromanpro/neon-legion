"""SESSION SIZE signals must be counted the way the transcript is read.

Found in review of phase 1a (2026-09-11): the oracle's size block (user and
assistant message counts, tool calls) still derived the role with the
Claude-only reader. A Codex day therefore reached the oracle with real
conversation text next to "0 messages, 0 tool calls", and Claude compaction
summaries were still counted as user messages.

Claude semantics are otherwise pinned by tests/test_session_metrics.py (a
tool_result line keeps counting as a user line there) and must not change.
"""
import ast
import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tracker"))

T = "2026-09-09T10:{:02d}:00Z"


@pytest.fixture
def estimator():
    spec = importlib.util.spec_from_file_location("size_estimator", ROOT / "tracker/estimate-task.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def daily_metrics(estimator, events):
    """Run the production day-loop statements up to the oracle prompt.

    Same AST probe as tests/test_transcript_inputs.py: no oracle call and no
    task writes; returns the chunk_metrics the oracle would have received.
    Since phase 1c those statements live in build_day_input, which both the day
    loop and `--day` call; the AST path remains for runs against pre-1c code.
    """
    tree = ast.parse(Path(estimator.__file__).read_text(encoding="utf-8"))
    if hasattr(estimator, "build_day_input"):
        def calls_in(function_name):
            function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function_name)
            return {n.func.id for n in ast.walk(function)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "build_day_entry" in calls_in("estimate_session")
        assert "build_day_input" in calls_in("build_day_entry")
        pairs = [(event, estimator.parse_transcript_ts(event["timestamp"])) for event in events]
        return estimator.build_day_input(pairs)[2]
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
    wrapper.body[0].body = statements + ast.parse("return chunk_metrics").body
    ast.fix_missing_locations(wrapper)
    namespace = vars(estimator).copy()
    exec(compile(wrapper, estimator.__file__, "exec"), namespace)
    pairs = [(event, estimator.parse_transcript_ts(event["timestamp"])) for event in events]
    return namespace["probe"]({"day": pairs}, "day")


def write(tmp_path, events):
    path = tmp_path / "transcript.jsonl"
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    return path


def signals(metrics):
    return (metrics["user_message_count"], metrics["assistant_message_count"], metrics["tool_call_count"])


def codex_record(minute, payload):
    return {"type": "response_item", "timestamp": T.format(minute), "payload": payload}


def codex_message(minute, role, text):
    kind = "output_text" if role == "assistant" else "input_text"
    return codex_record(minute, {"type": "message", "role": role,
                                 "content": [{"type": kind, "text": text}]})


CODEX_EVENTS = [
    {"type": "session_meta", "timestamp": T.format(0), "payload": {"session_id": "s"}},
    codex_message(1, "user", "<environment_context>\n  <cwd>F:/x</cwd>\n</environment_context>"),
    codex_message(2, "developer", "App scaffolding"),
    codex_message(3, "user", "Fix the parser"),
    codex_record(4, {"type": "reasoning", "summary": [], "content": None}),
    codex_record(5, {"type": "function_call", "name": "shell", "arguments": "{}", "call_id": "c1"}),
    codex_record(6, {"type": "function_call_output", "call_id": "c1", "output": "ok"}),
    codex_record(7, {"type": "custom_tool_call", "name": "apply_patch", "input": "*** patch", "call_id": "c2"}),
    codex_record(8, {"type": "custom_tool_call_output", "call_id": "c2", "output": "done"}),
    codex_record(9, {"type": "web_search_call", "action": {"type": "search"}, "status": "completed"}),
    codex_message(10, "assistant", "Parser fixed"),
    {"type": "event_msg", "timestamp": T.format(11), "payload": {"type": "token_count", "info": {}}},
    {"type": "compacted", "timestamp": T.format(12), "payload": {
        "message": "",
        "replacement_history": [{"type": "message", "role": "user",
                                 "content": [{"type": "input_text", "text": "old ask"}]}],
    }},
]


def claude_event(minute, role, content, **flags):
    return {"type": role, "timestamp": T.format(minute),
            "message": {"role": role, "content": content}, **flags}


CLAUDE_EVENTS = [
    claude_event(0, "user", "This session is being continued from a previous conversation...",
                 isCompactSummary=True, isVisibleInTranscriptOnly=True),
    claude_event(1, "user", "Real task"),
    claude_event(2, "assistant", [{"type": "text", "text": "On it"},
                                  {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]),
]


def test_session_metrics_count_codex_messages_and_tool_calls(estimator, tmp_path):
    # One genuine user message (injected context and developer scaffolding are
    # not the human), one assistant reply, three tool invocations (outputs are
    # not calls), and compacted history is not replayed as messages.
    metrics = estimator.compute_session_metrics(write(tmp_path, CODEX_EVENTS))
    assert signals(metrics) == (1, 1, 3)
    assert metrics["event_count"] == len(CODEX_EVENTS)


def test_daily_metrics_count_codex_messages_and_tool_calls(estimator):
    metrics = daily_metrics(estimator, CODEX_EVENTS)
    assert signals(metrics) == (1, 1, 3)
    assert metrics["event_count"] == len(CODEX_EVENTS)


def test_compaction_summary_not_counted_as_user_message(estimator, tmp_path):
    session = estimator.compute_session_metrics(write(tmp_path, CLAUDE_EVENTS))
    assert signals(session) == (1, 1, 1)
    assert signals(daily_metrics(estimator, CLAUDE_EVENTS)) == (1, 1, 1)
