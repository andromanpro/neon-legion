#!/usr/bin/env python
"""Backfill Codex Desktop/TUI session usage into tracker/codex-events.jsonl.

Codex Desktop writes local session JSONL files with token_count events. The
Phase 1.1 wrapper only captures future `codex exec` calls that go through
tracker/codex-track.py, so this script imports historical desktop usage without
copying prompt/response text into the tracker.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from tools import config as cfg  # noqa: E402

TRACKER_DIR = PROJECT_ROOT / "tracker"
EVENTS_FILE = TRACKER_DIR / "codex-events.jsonl"
LOCK_FILE = TRACKER_DIR / ".codex-events.lock"
DEFAULT_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"

PRICING = {
    "input": 10.0 / 1_000_000,
    "cached_input": 2.5 / 1_000_000,
    "output": 30.0 / 1_000_000,
    "reasoning": 30.0 / 1_000_000,
}


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions-root", type=Path, default=DEFAULT_SESSIONS_ROOT)
    parser.add_argument("--events-file", type=Path, default=EVENTS_FILE)
    parser.add_argument("--since", help="Only import events at/after YYYY-MM-DD.")
    parser.add_argument("--dry-run", action="store_true", help="Show counts without writing.")
    return parser.parse_args(argv)


def as_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def estimate_cost(
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    reasoning_tokens: int,
) -> float:
    cost = (
        input_tokens * PRICING["input"]
        + cached_input_tokens * PRICING["cached_input"]
        + output_tokens * PRICING["output"]
        + reasoning_tokens * PRICING["reasoning"]
    )
    return round(cost, 6)


def parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def subscription_type() -> str:
    if cfg.get_legacy_env("OPENAI_API_KEY") or cfg.get_legacy_env("ANTHROPIC_API_KEY"):
        return "api-key"
    return "chatgpt-pro"


def source_to_string(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def classify_codex_origin(originator: str, source: str, model: str) -> str:
    lower_originator = originator.lower()
    lower_source = source.lower()
    lower_model = model.lower()

    if lower_model == "codex-auto-review" or "subagent" in lower_source:
        return "auto_review"
    if lower_originator == "codex_exec" or lower_source == "exec":
        return "headless"
    if lower_originator == "codex-tui" or lower_source == "cli":
        return "tui"
    if lower_originator == "codex desktop" or lower_source in {"vscode", "desktop"}:
        return "desktop"
    return "unknown"


def usage_from_payload(payload: dict) -> dict | None:
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    usage = info.get("last_token_usage")
    if not isinstance(usage, dict):
        return None
    return usage


def usage_value(usage: dict, *names: str) -> int:
    for name in names:
        if name in usage:
            return as_int(usage.get(name))
    return 0


def existing_keys(path: Path) -> tuple[set[str], set[tuple]]:
    event_ids: set[str] = set()
    semantic: set[tuple] = set()
    if not path.exists():
        return event_ids, semantic

    with path.open("r", encoding="utf-8", errors="replace") as source:
        for line in source:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event_id = event.get("event_id") or event.get("tracking_run_id")
            if isinstance(event_id, str) and event_id:
                event_ids.add(event_id)
            semantic.add(semantic_key(event))
    return event_ids, semantic


def semantic_key(event: dict) -> tuple:
    return (
        event.get("provider") or "openai",
        event.get("session_id"),
        event.get("model"),
        event.get("input_tokens"),
        event.get("cached_input_tokens"),
        event.get("output_tokens"),
        event.get("reasoning_tokens"),
        event.get("total_tokens"),
    )


def read_session_events(path: Path, since: datetime | None) -> list[dict]:
    session_id = ""
    cwd = ""
    originator = ""
    source = ""
    current_model = "unknown"
    approval_mode = "unknown"
    sandbox_mode = "unknown"
    events: list[dict] = []

    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line_no, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue

            typ = item.get("type")
            payload = item.get("payload")
            if not isinstance(payload, dict):
                continue

            if typ == "session_meta":
                session_id = str(payload.get("id") or session_id or path.stem)
                cwd = str(payload.get("cwd") or cwd)
                originator = str(payload.get("originator") or originator)
                source = source_to_string(payload.get("source") or source)
                continue

            if typ == "turn_context":
                current_model = str(payload.get("model") or current_model)
                cwd = str(payload.get("cwd") or cwd)
                approval_mode = str(payload.get("approval_policy") or approval_mode)
                sandbox = payload.get("sandbox_policy")
                if isinstance(sandbox, dict):
                    sandbox_mode = str(sandbox.get("type") or sandbox_mode)
                continue

            if typ != "event_msg" or payload.get("type") != "token_count":
                continue

            usage = usage_from_payload(payload)
            if usage is None:
                continue

            ts = parse_ts(item.get("timestamp"))
            if ts is None:
                continue
            if since is not None and ts < since:
                continue

            input_tokens = usage_value(usage, "input_tokens", "prompt_tokens")
            cached_input_tokens = usage_value(
                usage,
                "cached_input_tokens",
                "cache_read_input_tokens",
                "cached_tokens",
            )
            output_tokens = usage_value(usage, "output_tokens", "completion_tokens")
            reasoning_tokens = usage_value(
                usage,
                "reasoning_tokens",
                "reasoning_output_tokens",
                "output_reasoning_tokens",
            )
            total_tokens = usage_value(usage, "total_tokens")
            if total_tokens <= 0:
                total_tokens = input_tokens + cached_input_tokens + output_tokens + reasoning_tokens
            if input_tokens <= 0 and cached_input_tokens <= 0 and output_tokens <= 0 and reasoning_tokens <= 0:
                continue
            if total_tokens <= 0:
                continue

            sid = session_id or path.stem
            event = {
                "schema_version": 1,
                "event_id": f"codex-session-{sid}-{line_no}",
                "tracking_run_id": f"codex-session-{sid}",
                "sequence_no": line_no,
                "ts": item.get("timestamp"),
                "session_id": sid,
                "model": current_model,
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_input_tokens,
                "output_tokens": output_tokens,
                "reasoning_tokens": reasoning_tokens,
                "total_tokens": total_tokens,
                "duration_ms": 0,
                "cost_estimate_usd": estimate_cost(
                    input_tokens,
                    cached_input_tokens,
                    output_tokens,
                    reasoning_tokens,
                ),
                "exit_code": 0,
                "partial": False,
                "usage_captured": True,
                "codex_json_events": 0,
                "working_dir": cwd.replace("\\", "/") if cwd else "",
                "subscription_type": subscription_type(),
                "approval_mode": approval_mode,
                "sandbox_mode": sandbox_mode,
                "provider": "openai",
                "codex_origin": classify_codex_origin(originator, source, current_model),
                "source": "codex-session-backfill",
                "originator": originator,
                "codex_source": source,
            }
            events.append(event)

    return events


def append_events(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as target:
        for event in events:
            target.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        target.flush()
        os.fsync(target.fileno())


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not args.sessions_root.exists():
        print(f"sessions root not found: {args.sessions_root}", file=sys.stderr)
        return 2

    since = None
    if args.since:
        since = datetime.fromisoformat(args.since).astimezone()

    existing_event_ids, existing_semantic = existing_keys(args.events_file)
    new_events: list[dict] = []
    scanned_files = 0
    scanned_usage_events = 0
    skipped_existing = 0

    for path in sorted(args.sessions_root.rglob("*.jsonl")):
        scanned_files += 1
        for event in read_session_events(path, since):
            scanned_usage_events += 1
            event_id = str(event.get("event_id") or "")
            sem_key = semantic_key(event)
            if event_id in existing_event_ids or sem_key in existing_semantic:
                skipped_existing += 1
                continue
            existing_event_ids.add(event_id)
            new_events.append(event)

    print(f"scanned_files={scanned_files}")
    print(f"usage_events={scanned_usage_events}")
    print(f"new_events={len(new_events)}")
    print(f"skipped_existing={skipped_existing}")
    if not args.dry_run and new_events:
        append_events(args.events_file, new_events)
        print(f"appended={len(new_events)} to {args.events_file}")
    elif args.dry_run:
        print("dry_run=true")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
