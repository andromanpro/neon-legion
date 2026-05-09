#!/usr/bin/env python
import argparse
import glob
import importlib.util
import json
import os
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRACKER_DIR = PROJECT_ROOT / "tracker"
EVENTS_FILE = TRACKER_DIR / "claude-events.jsonl"
LAST_UUIDS_FILE = TRACKER_DIR / ".last-uuids.json"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def load_hook_module():
    hook_path = PROJECT_ROOT / "hooks" / "claude-track-calls.py"
    spec = importlib.util.spec_from_file_location("claude_track_calls", hook_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to import pricing hook from {hook_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HOOK = load_hook_module()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill Claude Code usage events from transcript JSONL files.")
    parser.add_argument("--from-date", help="Start date, inclusive, in YYYY-MM-DD format.")
    parser.add_argument("--to-date", help="End date, inclusive, in YYYY-MM-DD format.")
    parser.add_argument("--scan-dir", default=str(Path.home() / ".claude" / "projects"), help="Claude projects directory.")
    parser.add_argument("--dry-run", action="store_true", help="Report only; do not modify tracker files.")
    parser.add_argument("--verbose", action="store_true", help="Log skipped malformed transcript lines to stderr.")
    return parser.parse_args(argv)


def parse_cli_date(value: str | None):
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def parse_event_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def as_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def decode_project_dir(name: str) -> tuple[str, bool]:
    try:
        if "--" in name:
            head, _, tail = name.partition("--")
            return f"{head}:/{tail.replace('-', '/')}", False
        return name.replace("-", "/"), False
    except Exception:
        return name, True


def transcript_files(scan_dir: Path) -> list[Path]:
    pattern = str(scan_dir.expanduser() / "*" / "*.jsonl")
    return [Path(path) for path in glob.glob(pattern)]


def read_last_uuids() -> dict:
    if not LAST_UUIDS_FILE.exists():
        return {}

    try:
        with LAST_UUIDS_FILE.open("r", encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def read_existing_state() -> tuple[set[tuple[str, str]], dict[str, tuple[datetime, str]]]:
    seen = set()
    latest_by_session: dict[str, tuple[datetime, str]] = {}
    if not EVENTS_FILE.exists():
        return seen, latest_by_session

    with EVENTS_FILE.open("r", encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue

            session_id = event.get("session_id")
            message_uuid = event.get("message_uuid")
            if not isinstance(session_id, str) or not session_id:
                continue
            if not isinstance(message_uuid, str) or not message_uuid:
                continue

            seen.add((session_id, message_uuid))
            ts = parse_event_ts(event.get("ts"))
            if ts is None:
                continue

            current = latest_by_session.get(session_id)
            if current is None or ts.timestamp() > current[0].timestamp():
                latest_by_session[session_id] = (ts, message_uuid)

    return seen, latest_by_session


def usage_dict(event: dict) -> tuple[dict, dict] | None:
    if event.get("type") != "assistant":
        return None

    message = event.get("message")
    if not isinstance(message, dict):
        return None

    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None

    return message, usage


def count_tool_uses(content: object) -> int:
    if not isinstance(content, list):
        return 0
    return sum(1 for block in content if isinstance(block, dict) and block.get("type") == "tool_use")


def build_tracker_event(
    session_id: str,
    working_dir: str,
    working_dir_raw: bool,
    transcript_event: dict,
    message: dict,
    usage: dict,
) -> dict | None:
    message_uuid = transcript_event.get("uuid")
    timestamp = transcript_event.get("timestamp")
    model = message.get("model")
    if not isinstance(message_uuid, str) or not message_uuid:
        return None
    if not isinstance(timestamp, str) or not timestamp:
        return None
    if not isinstance(model, str) or not model:
        return None

    input_tokens = as_int(usage.get("input_tokens"))
    output_tokens = as_int(usage.get("output_tokens"))
    cache_creation_tokens = as_int(usage.get("cache_creation_input_tokens"))
    cache_read_tokens = as_int(usage.get("cache_read_input_tokens"))

    event = {
        "ts": timestamp,
        "session_id": session_id,
        "message_uuid": message_uuid,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cost_estimate_usd": HOOK.estimate_cost(
            model,
            input_tokens,
            output_tokens,
            cache_creation_tokens,
            cache_read_tokens,
        ),
        "duration_ms": 0,
        "working_dir": working_dir,
        "tool_uses": count_tool_uses(message.get("content")),
        "stop_reason": message.get("stop_reason", ""),
        "backfilled": True,
    }
    if working_dir_raw:
        event["working_dir_raw"] = True
    return event


def log_verbose(verbose: bool, message: str) -> None:
    if verbose:
        print(message, file=sys.stderr)


def collect_events(
    files: list[Path],
    from_date,
    to_date,
    seen: set[tuple[str, str]],
    verbose: bool,
) -> tuple[list[tuple[datetime, dict]], int, int]:
    candidates: list[tuple[datetime, dict]] = []
    seen_this_run = set(seen)
    total_usage_events = 0
    duplicates = 0

    for transcript_path in files:
        session_id = transcript_path.stem
        working_dir, working_dir_raw = decode_project_dir(transcript_path.parent.name)

        try:
            source = transcript_path.open("r", encoding="utf-8")
        except OSError as exc:
            log_verbose(verbose, f"Skipping unreadable transcript {transcript_path}: {exc}")
            continue

        with source:
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    continue

                try:
                    transcript_event = json.loads(line)
                except json.JSONDecodeError as exc:
                    log_verbose(verbose, f"Skipping malformed JSON in {transcript_path}:{line_number}: {exc}")
                    continue
                if not isinstance(transcript_event, dict):
                    continue

                message_usage = usage_dict(transcript_event)
                if message_usage is None:
                    continue

                message, usage = message_usage
                tracker_event = build_tracker_event(
                    session_id,
                    working_dir,
                    working_dir_raw,
                    transcript_event,
                    message,
                    usage,
                )
                if tracker_event is None:
                    log_verbose(verbose, f"Skipping incomplete assistant usage event in {transcript_path}:{line_number}")
                    continue

                ts = parse_event_ts(tracker_event.get("ts"))
                if ts is None:
                    log_verbose(verbose, f"Skipping assistant usage event with invalid timestamp in {transcript_path}:{line_number}")
                    continue

                event_date = ts.date()
                if from_date is not None and event_date < from_date:
                    continue
                if to_date is not None and event_date > to_date:
                    continue

                total_usage_events += 1
                key = (session_id, tracker_event["message_uuid"])
                if key in seen_this_run:
                    duplicates += 1
                    continue

                seen_this_run.add(key)
                candidates.append((ts, tracker_event))

    return candidates, total_usage_events, duplicates


def atomic_write_text(path: Path, text: str) -> None:
    TRACKER_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    with temp_path.open("w", encoding="utf-8", newline="\n") as target:
        target.write(text)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temp_path, path)


def append_events(events: list[dict]) -> None:
    if not events:
        return

    existing = ""
    if EVENTS_FILE.exists():
        with EVENTS_FILE.open("r", encoding="utf-8") as source:
            existing = source.read()

    merged = existing
    if merged and not merged.endswith("\n"):
        merged += "\n"
    merged += "".join(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n" for event in events)
    atomic_write_text(EVENTS_FILE, merged)


def remove_fresh_duplicates(
    events_with_ts: list[tuple[datetime, dict]],
    seen: set[tuple[str, str]],
) -> tuple[list[tuple[datetime, dict]], int]:
    filtered = []
    duplicates = 0
    for ts, event in events_with_ts:
        key = (event["session_id"], event["message_uuid"])
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        filtered.append((ts, event))
    return filtered, duplicates


def update_last_uuids(events_with_ts: list[tuple[datetime, dict]], existing_latest: dict[str, tuple[datetime, str]]) -> None:
    if not events_with_ts:
        return

    last_uuids = read_last_uuids()
    new_latest: dict[str, tuple[datetime, str]] = {}
    for ts, event in events_with_ts:
        session_id = event["session_id"]
        message_uuid = event["message_uuid"]
        current = new_latest.get(session_id)
        if current is None or ts.timestamp() > current[0].timestamp():
            new_latest[session_id] = (ts, message_uuid)

    changed = False
    for session_id, (new_ts, new_uuid) in new_latest.items():
        existing = existing_latest.get(session_id)
        if existing is not None and existing[0].timestamp() > new_ts.timestamp():
            continue
        if last_uuids.get(session_id) != new_uuid:
            last_uuids[session_id] = new_uuid
            changed = True

    if changed:
        atomic_write_text(LAST_UUIDS_FILE, json.dumps(last_uuids, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def date_span(events_with_ts: list[tuple[datetime, dict]]) -> str:
    if not events_with_ts:
        return "n/a"
    dates = [ts.date() for ts, _event in events_with_ts]
    return f"{min(dates).isoformat()} .. {max(dates).isoformat()}"


def total_cost(events: list[dict]) -> float:
    total = 0.0
    for event in events:
        try:
            total += float(event.get("cost_estimate_usd") or 0)
        except (TypeError, ValueError):
            continue
    return total


def print_report(
    scan_dir: Path,
    scanned_count: int,
    total_usage_events: int,
    duplicates: int,
    events_with_ts: list[tuple[datetime, dict]],
    dry_run: bool,
) -> None:
    events = [event for _ts, event in events_with_ts]
    print("## Backfill report")
    print()
    print(f"- **Scanned**: {scanned_count:,} transcript files in `{scan_dir}`")
    print(f"- **Total assistant events with usage**: {total_usage_events:,}")
    print(f"- **Already in tracker**: {duplicates:,} (skipped as duplicates)")
    print(f"- **New events appended**: {len(events):,}")
    print(f"- **Date span of new events**: {date_span(events_with_ts)}")
    print(f"- **Total cost added (estimated)**: ${total_cost(events):.2f}")
    print(f"- **Sessions touched**: {len({event['session_id'] for event in events}):,}")
    if dry_run:
        print()
        print("Dry run: no files modified.")


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        from_date = parse_cli_date(args.from_date)
        to_date = parse_cli_date(args.to_date)
    except ValueError as exc:
        print(f"Invalid date: {exc}", file=sys.stderr)
        return 2

    if from_date is not None and to_date is not None and from_date > to_date:
        print("Invalid period: --from-date must be earlier than or equal to --to-date", file=sys.stderr)
        return 2

    scan_dir = Path(args.scan_dir).expanduser()
    seen, existing_latest = read_existing_state()
    files = transcript_files(scan_dir)
    events_with_ts, total_usage_events, duplicates = collect_events(files, from_date, to_date, seen, args.verbose)
    events_with_ts.sort(key=lambda item: item[0].timestamp())

    if not args.dry_run:
        lock_fd = HOOK.acquire_lock()
        if lock_fd is None:
            print("Tracker is locked by another writer; retry backfill later.", file=sys.stderr)
            return 1
        try:
            fresh_seen, fresh_existing_latest = read_existing_state()
            events_with_ts, fresh_duplicates = remove_fresh_duplicates(events_with_ts, fresh_seen)
            duplicates += fresh_duplicates
            append_events([event for _ts, event in events_with_ts])
            update_last_uuids(events_with_ts, fresh_existing_latest)
        finally:
            HOOK.release_lock(lock_fd)

    print_report(scan_dir, len(files), total_usage_events, duplicates, events_with_ts, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
