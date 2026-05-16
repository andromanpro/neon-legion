import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ESTIMATOR_PATH = ROOT / "tracker" / "estimate-task.py"


def load_estimator():
    spec = importlib.util.spec_from_file_location("estimate_task", ESTIMATOR_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


estimate_task = load_estimator()


def ts(minutes: float) -> str:
    value = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)
    return value.isoformat().replace("+00:00", "Z")


def write_jsonl(path: Path, events: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as target:
        for event in events:
            target.write(json.dumps(event) + "\n")


def test_compute_session_metrics_counts_roles_tools_span_and_active_hours(tmp_path):
    transcript = tmp_path / "session.jsonl"
    write_jsonl(transcript, [
        {
            "type": "user",
            "timestamp": ts(0),
            "message": {"role": "user", "content": [{"type": "text", "text": "start"}]},
        },
        {
            "type": "assistant",
            "timestamp": ts(1),
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "working"},
                    {"type": "tool_use", "id": "tool-1", "name": "Read"},
                    {"type": "tool_use", "id": "tool-2", "name": "Edit"},
                ],
            },
        },
        {
            "type": "user",
            "timestamp": ts(3),
            "message": {"role": "user", "content": [{"type": "text", "text": "continue"}]},
        },
        {
            "type": "assistant",
            "timestamp": ts(6),
            "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        },
        {
            "type": "queue-operation",
            "timestamp": ts(7),
            "toolUseID": "tool-3",
            "toolUseResult": {"content": "ok"},
        },
    ])

    metrics = estimate_task.compute_session_metrics(transcript)

    assert metrics["event_count"] == 5
    assert metrics["user_message_count"] == 2
    assert metrics["assistant_message_count"] == 2
    assert metrics["tool_call_count"] == 3
    assert metrics["span_hours"] == 7 / 60
    assert metrics["active_hours"] == 4 / 60


def test_compute_session_metrics_ignores_malformed_lines(tmp_path):
    transcript = tmp_path / "malformed.jsonl"
    transcript.write_text(
        json.dumps({
            "type": "user",
            "timestamp": ts(0),
            "message": {"role": "user", "content": [{"type": "text", "text": "ok"}]},
        })
        + "\nnot-json\n\n",
        encoding="utf-8",
    )

    metrics = estimate_task.compute_session_metrics(transcript)

    assert metrics["event_count"] == 1
    assert metrics["user_message_count"] == 1
    assert metrics["assistant_message_count"] == 0
    assert metrics["tool_call_count"] == 0
    assert metrics["span_hours"] == 0.0
    assert metrics["active_hours"] == 0.0
