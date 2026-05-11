#!/usr/bin/env python
"""Backfill OpenCode SQLite usage into tracker/opencode-events.jsonl.

OpenCode stores assistant accounting in its local SQLite database. This importer
copies only structured usage metadata (provider, model, tokens, cost, status),
not prompts or assistant text.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRACKER_DIR = PROJECT_ROOT / "tracker"
EVENTS_FILE = TRACKER_DIR / "opencode-events.jsonl"
def _default_opencode_db() -> Path:
    """OpenCode default DB path per OS. Override via OPENCODE_DB_PATH env var."""
    env = os.environ.get("OPENCODE_DB_PATH")
    if env:
        return Path(env)
    home = Path.home()
    # Linux/macOS: XDG ~/.local/share/opencode/opencode.db
    # Windows: %APPDATA%\opencode\opencode.db (recent) or %LOCALAPPDATA%\opencode\opencode.db
    # Fall back to ~/.local/share which OpenCode also creates on cross-platform installs.
    if os.name == "nt":
        appdata = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if appdata:
            candidate = Path(appdata) / "opencode" / "opencode.db"
            if candidate.exists():
                return candidate
    return home / ".local" / "share" / "opencode" / "opencode.db"


DEFAULT_DB_PATH = _default_opencode_db()


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--events-file", type=Path, default=EVENTS_FILE)
    parser.add_argument("--since", help="Only import events at/after YYYY-MM-DD.")
    parser.add_argument("--dry-run", action="store_true", help="Show counts without writing.")
    return parser.parse_args(argv)


def parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def ms_to_datetime(value: object) -> datetime | None:
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def ms_to_iso(value: object) -> str | None:
    ts = ms_to_datetime(value)
    if ts is None:
        return None
    return ts.isoformat().replace("+00:00", "Z")


def as_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def as_float(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def token_cache(tokens: dict) -> dict:
    cache = tokens.get("cache")
    return cache if isinstance(cache, dict) else {}


def usage_numbers(tokens: dict, cost: object) -> dict[str, int | float]:
    cache = token_cache(tokens)
    input_tokens = as_int(tokens.get("input"))
    output_tokens = as_int(tokens.get("output"))
    reasoning_tokens = as_int(tokens.get("reasoning"))
    cache_read_tokens = as_int(cache.get("read"))
    cache_creation_tokens = as_int(cache.get("write"))
    total_tokens = as_int(tokens.get("total"))
    if total_tokens <= 0:
        total_tokens = (
            input_tokens
            + output_tokens
            + reasoning_tokens
            + cache_read_tokens
            + cache_creation_tokens
        )
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "total_tokens": total_tokens,
        "cost_estimate_usd": round(as_float(cost), 10),
    }


def has_billable_or_token_usage(numbers: dict[str, int | float]) -> bool:
    token_sum = (
        as_int(numbers.get("input_tokens"))
        + as_int(numbers.get("output_tokens"))
        + as_int(numbers.get("reasoning_tokens"))
        + as_int(numbers.get("cache_read_tokens"))
        + as_int(numbers.get("cache_creation_tokens"))
        + as_int(numbers.get("total_tokens"))
    )
    return token_sum > 0 or as_float(numbers.get("cost_estimate_usd")) > 0


def event_status(finish: object) -> tuple[int, bool]:
    reason = str(finish or "").lower()
    if reason in {"error", "aborted", "cancelled", "canceled"}:
        return 1, True
    return 0, False


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
        event.get("provider") or "opencode",
        event.get("session_id"),
        event.get("message_id"),
        event.get("model"),
        event.get("input_tokens"),
        event.get("cache_read_tokens"),
        event.get("cache_creation_tokens"),
        event.get("output_tokens"),
        event.get("reasoning_tokens"),
        event.get("total_tokens"),
    )


def connect_readonly(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def read_events_from_db(path: Path, since: datetime | None) -> tuple[list[dict], int, int]:
    events: list[dict] = []
    scanned_assistant = 0
    skipped_zero_usage = 0

    with connect_readonly(path) as con:
        rows = con.execute(
            """
            select
                m.id as message_id,
                m.session_id as session_id,
                m.time_created as message_time_created,
                m.time_updated as message_time_updated,
                m.data as message_data,
                s.directory as session_directory,
                s.title as session_title,
                s.version as opencode_version,
                s.path as session_path
            from message m
            left join session s on s.id = m.session_id
            order by m.time_created, m.id
            """
        )
        for sequence_no, row in enumerate(rows, 1):
            try:
                data = json.loads(row["message_data"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict) or data.get("role") != "assistant":
                continue
            scanned_assistant += 1

            tokens = data.get("tokens")
            if not isinstance(tokens, dict):
                continue

            time_data = data.get("time")
            if not isinstance(time_data, dict):
                time_data = {}
            event_time_ms = (
                time_data.get("completed")
                or row["message_time_updated"]
                or time_data.get("created")
                or row["message_time_created"]
            )
            event_dt = ms_to_datetime(event_time_ms)
            if event_dt is None:
                continue
            if since is not None and event_dt < since:
                continue

            numbers = usage_numbers(tokens, data.get("cost"))
            if not has_billable_or_token_usage(numbers):
                skipped_zero_usage += 1
                continue

            session_id = str(row["session_id"] or "")
            message_id = str(row["message_id"] or sequence_no)
            model = str(data.get("modelID") or "unknown")
            router_provider = str(data.get("providerID") or "unknown")
            exit_code, partial = event_status(data.get("finish"))
            path_data = data.get("path")
            working_dir = ""
            if isinstance(path_data, dict):
                working_dir = str(path_data.get("cwd") or "")
            if not working_dir:
                working_dir = str(row["session_directory"] or "")

            events.append({
                "event_id": f"opencode-session-{session_id}-{message_id}",
                "tracking_run_id": f"opencode-session-{session_id}",
                "sequence_no": sequence_no,
                "ts": event_dt.isoformat().replace("+00:00", "Z"),
                "session_id": session_id,
                "message_id": message_id,
                "model": model,
                "input_tokens": numbers["input_tokens"],
                "output_tokens": numbers["output_tokens"],
                "reasoning_tokens": numbers["reasoning_tokens"],
                "cache_read_tokens": numbers["cache_read_tokens"],
                "cache_creation_tokens": numbers["cache_creation_tokens"],
                "total_tokens": numbers["total_tokens"],
                "duration_ms": max(
                    0,
                    as_int(time_data.get("completed")) - as_int(time_data.get("created")),
                ),
                "cost_estimate_usd": numbers["cost_estimate_usd"],
                "exit_code": exit_code,
                "partial": partial,
                "usage_captured": True,
                "working_dir": working_dir,
                "provider": "opencode",
                "opencode_provider_id": router_provider,
                "opencode_agent": str(data.get("agent") or ""),
                "opencode_mode": str(data.get("mode") or ""),
                "opencode_version": str(row["opencode_version"] or ""),
                "source": "opencode-db-backfill",
                "stop_reason": data.get("finish") or "",
            })

    return events, scanned_assistant, skipped_zero_usage


def append_events(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as target:
        for event in events:
            target.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        target.flush()
        os.fsync(target.fileno())


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not args.db_path.exists():
        print(f"OpenCode database not found: {args.db_path}", file=sys.stderr)
        return 2

    since = parse_since(args.since)
    existing_event_ids, existing_semantic = existing_keys(args.events_file)
    events, scanned_assistant, skipped_zero_usage = read_events_from_db(args.db_path, since)
    new_events: list[dict] = []
    skipped_existing = 0

    for event in events:
        event_id = str(event.get("event_id") or "")
        sem_key = semantic_key(event)
        if event_id in existing_event_ids or sem_key in existing_semantic:
            skipped_existing += 1
            continue
        existing_event_ids.add(event_id)
        existing_semantic.add(sem_key)
        new_events.append(event)

    print(f"scanned_assistant_messages={scanned_assistant}")
    print(f"usage_events={len(events)}")
    print(f"new_events={len(new_events)}")
    print(f"skipped_existing={skipped_existing}")
    print(f"skipped_zero_usage={skipped_zero_usage}")
    if not args.dry_run and new_events:
        append_events(args.events_file, new_events)
        print(f"appended={len(new_events)} to {args.events_file}")
    elif args.dry_run:
        print("dry_run=true")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
