#!/usr/bin/env python
"""Backfill direct DeepSeek calls (tools/scripts/ds-call.py) into the tracker.

ds-call.py hits OpenRouter directly (bypassing the opencode CLI) and logs each
call to its own file: F:/temp/deepseek-log/calls.jsonl. Nothing ingested that
log, so all direct-DeepSeek usage was invisible on the dashboard. This backfill
reads that log and appends normalized events to tracker/dscall-events.jsonl,
tagged provider=openrouter / model=deepseek/deepseek-v4-pro so they aggregate
under the same `openrouter/deepseek/deepseek-v4-pro` key as the openclaw calls.

Idempotent: event_id is a content hash of the log line, so re-runs add only
genuinely new calls. Cost comes straight from the log (ds-call already computed
the real OpenRouter cost) — no repricing.

Stdlib only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from tools import config as cfg  # noqa: E402

TRACKER_DIR = PROJECT_ROOT / "tracker"
EVENTS_FILE = TRACKER_DIR / "dscall-events.jsonl"
DEFAULT_LOG_PATH = Path(
    cfg.get_legacy_env("DSCALL_LOG_PATH", "F:/temp/deepseek-log/calls.jsonl", str)
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


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


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-path", type=Path, default=DEFAULT_LOG_PATH)
    parser.add_argument("--events-file", type=Path, default=EVENTS_FILE)
    parser.add_argument("--dry-run", action="store_true", help="Report counts without writing.")
    return parser.parse_args(argv)


def existing_event_ids(events_file: Path) -> set[str]:
    seen: set[str] = set()
    if not events_file.exists():
        return seen
    with events_file.open("r", encoding="utf-8") as source:
        for line in source:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            eid = event.get("event_id")
            if isinstance(eid, str) and eid:
                seen.add(eid)
    return seen


def normalize_ts(raw: object) -> str | None:
    """ds-call writes naive local ISO timestamps. Attach the system-local
    offset so tracker events stay uniformly tz-aware."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.isoformat(timespec="seconds")


def build_event(line: str, seq: int) -> dict | None:
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return None
    ts = normalize_ts(entry.get("ts"))
    if ts is None:
        return None
    model = entry.get("model")
    if not isinstance(model, str) or not model:
        model = "deepseek/deepseek-v4-pro"

    # Content-hash id → idempotent across re-runs of the same log.
    event_id = "dscall-" + hashlib.sha1(line.strip().encode("utf-8")).hexdigest()[:16]
    # One "session" per calendar day of direct-DeepSeek consulting, so the
    # provider's session count on the dashboard stays meaningful (not 1/call).
    session_id = "dscall-" + ts[:10]

    input_tokens = as_int(entry.get("prompt_tokens"))
    output_tokens = as_int(entry.get("completion_tokens"))
    reasoning_tokens = as_int(entry.get("reasoning_tokens"))
    total_tokens = input_tokens + output_tokens + reasoning_tokens

    return {
        "schema_version": 1,
        "event_id": event_id,
        "tracking_run_id": session_id,
        "sequence_no": seq,
        "ts": ts,
        "session_id": session_id,
        "provider": "openrouter",
        "model": model,
        "input_tokens": input_tokens,
        "cached_input_tokens": 0,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
        "duration_ms": 0,
        # ds-call already computed the real OpenRouter cost — use it verbatim.
        "cost_estimate_usd": round(as_float(entry.get("cost_usd")), 6),
        "exit_code": 0,
        "partial": False,
        "usage_captured": True,
        "source": "ds-call",
        "effort": entry.get("effort"),
    }


def append_events(events_file: Path, events: list[dict]) -> None:
    events_file.parent.mkdir(parents=True, exist_ok=True)
    with events_file.open("a", encoding="utf-8", newline="\n") as target:
        for event in events:
            target.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    log_path = args.log_path
    if not log_path.exists():
        print(f"[dscall-backfill] log not found (nothing to do): {log_path}")
        return 0

    seen = existing_event_ids(args.events_file)
    new_events: list[dict] = []
    total = 0
    with log_path.open("r", encoding="utf-8") as source:
        for seq, line in enumerate(source, start=1):
            line = line.strip()
            if not line:
                continue
            total += 1
            event = build_event(line, seq)
            if event is None:
                continue
            if event["event_id"] in seen:
                continue
            seen.add(event["event_id"])
            new_events.append(event)

    if not args.dry_run and new_events:
        append_events(args.events_file, new_events)

    added = 0 if args.dry_run else len(new_events)
    cost = round(sum(e["cost_estimate_usd"] for e in new_events), 4)
    print(
        f"[dscall-backfill] log_lines={total} new_events={len(new_events)} "
        f"{'(dry-run) ' if args.dry_run else ''}appended={added} new_cost=${cost}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
