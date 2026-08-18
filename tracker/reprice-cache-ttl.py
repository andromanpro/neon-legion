#!/usr/bin/env python
"""Restate historical cost for the cache-write TTL that was never priced.

Anthropic charges a 1-hour cache write at 2x base input and a 5-minute one
at 1.25x. Every event written before 2026-08-18 used the 5-minute rate for
both, while 95% of the tokens written carried the 1-hour TTL — so the money
column is short by about an eighth of itself.

The ledger is append-only, so nothing here rewrites a past event. For each
event whose transcript still exists, this emits a compensating record:

    correction_of = the original event_id
    cost_estimate_usd = the difference, never the new total
    every token field zero

`readmodel` and `summary` count such a record as money only — not as
another call, not as more tokens. Events whose transcript has rotated away
cannot be repriced and are reported as such rather than guessed at.

Safe to re-run: a correction that already exists is skipped.

    py -3.14 tracker/reprice-cache-ttl.py --dry-run
    py -3.14 tracker/reprice-cache-ttl.py
"""
import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRACKER_DIR = PROJECT_ROOT / "tracker"
EVENTS_FILE = TRACKER_DIR / "claude-events.jsonl"
CORRECTION_SUFFIX = "#cache-ttl-correction"
CORRECTION_REASON = "cache write TTL: 1h writes were priced at the 5m rate"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def load_hook_module():
    hook_path = PROJECT_ROOT / "hooks" / "claude-track-calls.py"
    spec = importlib.util.spec_from_file_location("claude_track_calls", hook_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to import the tracking hook from {hook_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HOOK = load_hook_module()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report only; write nothing.")
    parser.add_argument(
        "--scan-dir",
        default=str(Path.home() / ".claude" / "projects"),
        help="Claude projects directory holding the transcripts.",
    )
    parser.add_argument(
        "--min-delta-usd",
        type=float,
        default=0.000001,
        help="Skip corrections smaller than this; float noise is not money.",
    )
    return parser.parse_args(argv)


def read_transcript_usage(scan_dir: Path) -> dict[str, tuple[str, dict]]:
    """message_uuid -> (model, usage) for every transcript still on disk."""
    usage_by_uuid: dict[str, tuple[str, dict]] = {}
    for path in scan_dir.rglob("*.jsonl"):
        try:
            with path.open(encoding="utf-8") as source:
                for line in source:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict) or event.get("type") != "assistant":
                        continue
                    message = event.get("message")
                    if not isinstance(message, dict):
                        continue
                    usage = message.get("usage")
                    model = message.get("model")
                    uuid = event.get("uuid")
                    if isinstance(usage, dict) and isinstance(model, str) and isinstance(uuid, str) and uuid:
                        usage_by_uuid[uuid] = (model, usage)
        except OSError:
            continue
    return usage_by_uuid


def event_identity(event: dict) -> str:
    """The correction's target. Older events predate event_id, so fall back
    to the shape the hook would have stamped."""
    event_id = event.get("event_id")
    if isinstance(event_id, str) and event_id:
        return event_id
    return f"claude:{event.get('session_id')}:{event.get('message_uuid')}"


def build_correction(event: dict, delta: float) -> dict:
    identity = event_identity(event)
    correction = {
        "schema_version": 1,
        "ts": event.get("ts"),
        "session_id": event.get("session_id"),
        "message_uuid": event.get("message_uuid"),
        "event_id": identity + CORRECTION_SUFFIX,
        "correction_of": identity,
        "kind": "cost_correction",
        "reason": CORRECTION_REASON,
        "model": event.get("model"),
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "cost_estimate_usd": round(delta, 10),
        "working_dir": event.get("working_dir"),
        "tool_uses": 0,
        "stop_reason": "",
    }
    if event.get("agent_id"):
        correction["agent_id"] = event["agent_id"]
        correction["is_sidechain"] = True
    return correction


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    scan_dir = Path(args.scan_dir).expanduser()
    if not EVENTS_FILE.exists():
        print("No ledger to reprice.", file=sys.stderr)
        return 1

    usage_by_uuid = read_transcript_usage(scan_dir)
    print(f"Transcript messages available: {len(usage_by_uuid):,}")

    corrections: list[dict] = []
    already_corrected: set[str] = set()
    no_transcript = 0
    unchanged = 0
    recorded_total = 0.0
    corrected_total = 0.0

    with EVENTS_FILE.open(encoding="utf-8") as source:
        events = []
        for line in source:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("correction_of"):
                already_corrected.add(str(event["correction_of"]))
                continue
            events.append(event)

    for event in events:
        identity = event_identity(event)
        if identity in already_corrected:
            continue
        found = usage_by_uuid.get(event.get("message_uuid"))
        if found is None:
            no_transcript += 1
            continue
        model, usage = found
        tokens = HOOK.usage_tokens(usage)
        if not tokens["cache_creation_1h_tokens"]:
            unchanged += 1
            continue
        correct = HOOK.estimate_cost(
            model,
            tokens["input_tokens"],
            tokens["output_tokens"],
            tokens["cache_creation_tokens"],
            tokens["cache_read_tokens"],
            tokens["cache_creation_1h_tokens"],
        )
        recorded = event.get("cost_estimate_usd")
        if correct is None or recorded is None:
            continue
        delta = correct - float(recorded)
        if abs(delta) < args.min_delta_usd:
            unchanged += 1
            continue
        recorded_total += float(recorded)
        corrected_total += correct
        corrections.append(build_correction(event, delta))

    print(f"Events already corrected:      {len(already_corrected):,}")
    print(f"Events with no transcript:     {no_transcript:,}  (cannot be repriced)")
    print(f"Events already priced right:   {unchanged:,}")
    print(f"Corrections to write:          {len(corrections):,}")
    print(f"  recorded:  ${recorded_total:,.2f}")
    print(f"  correct:   ${corrected_total:,.2f}")
    print(f"  delta:     ${corrected_total - recorded_total:,.2f}")

    if args.dry_run:
        print("\nDry run: nothing written.")
        return 0
    if not corrections:
        return 0

    lock_fd = HOOK.acquire_lock()
    if lock_fd is None:
        print("Tracker is locked by another writer; retry later.", file=sys.stderr)
        return 1
    try:
        payload = "".join(
            json.dumps(correction, ensure_ascii=False, separators=(",", ":")) + "\n"
            for correction in corrections
        )
        if HOOK.needs_leading_newline(EVENTS_FILE):
            payload = "\n" + payload
        with EVENTS_FILE.open("a", encoding="utf-8", newline="\n") as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
    finally:
        HOOK.release_lock(lock_fd)

    HOOK.record_ops(
        "cache_ttl_repriced",
        f"corrections={len(corrections)} delta_usd={corrected_total - recorded_total:.2f}",
        component="reprice-cache-ttl",
    )
    print(f"\nAppended {len(corrections):,} corrections.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
