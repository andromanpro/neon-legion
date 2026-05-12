#!/usr/bin/env python
"""Live OpenCode usage tracking (#22): poll-based watcher over the SQLite DB.

OpenCode persists each session in a SQLite DB. We cannot tail it (the DB is
locked while opencode runs) — we poll it on a timer. The existing backfill
(`tracker/backfill-opencode-sessions.py`) is already idempotent: it dedupes
new events by `event_id` and a semantic key (provider/session/message/model/
tokens), so running it on a tight cadence is safe.

This watcher imports the backfill logic as a module and calls it in a loop:

    python tracker/opencode-watch.py                # poll every 30s
    python tracker/opencode-watch.py --interval 5   # tighter
    python tracker/opencode-watch.py --once         # one tick, exit (for cron)

Stop with Ctrl+C — exits 0. SIGTERM also handled cleanly.

The watcher logs each non-empty tick to stdout:
    [tick] 2026-05-12T01:23:45 new=2 skipped=14 zero_usage=0

Idle ticks (no new events) suppress output unless --verbose.
"""

from __future__ import annotations

import argparse
import importlib.util
import signal
import sys
import time
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from tools import config as cfg  # noqa: E402

# Import backfill module (filename contains a dash, so import normally fails).
_BACKFILL_PATH = Path(__file__).parent / "backfill-opencode-sessions.py"
_spec = importlib.util.spec_from_file_location("backfill_opencode_sessions", _BACKFILL_PATH)
_backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_backfill)


_STOP = False


def _request_stop(_signum, _frame) -> None:
    global _STOP
    _STOP = True


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--interval",
        type=float,
        default=float(cfg.get_legacy_env("OPENCODE_WATCH_INTERVAL", 30.0, float)),
        help="Seconds between polls (default 30; env: OPENCODE_WATCH_INTERVAL).",
    )
    parser.add_argument("--db-path", type=Path, default=_backfill.DEFAULT_DB_PATH)
    parser.add_argument("--events-file", type=Path, default=_backfill.EVENTS_FILE)
    parser.add_argument("--since", help="On first tick, only import events at/after YYYY-MM-DD.")
    parser.add_argument("--once", action="store_true", help="Run a single tick and exit.")
    parser.add_argument("--verbose", action="store_true", help="Log every tick, even empty ones.")
    return parser.parse_args(argv)


def tick(args: argparse.Namespace, since=None) -> tuple[int, int, int]:
    """Run one polling tick. Returns (new_events, skipped_existing, skipped_zero_usage).

    Mirrors the body of `backfill-opencode-sessions.main()` but returns counts
    instead of printing. The first tick respects `--since`; subsequent ticks
    pass `since=None` because dedup-by-event-id already filters old events.
    """
    if not args.db_path.exists():
        return 0, 0, 0
    existing_event_ids, existing_semantic = _backfill.existing_keys(args.events_file)
    events, _scanned, skipped_zero_usage = _backfill.read_events_from_db(args.db_path, since)
    new_events: list[dict] = []
    skipped_existing = 0
    for event in events:
        event_id = str(event.get("event_id") or "")
        sem_key = _backfill.semantic_key(event)
        if event_id in existing_event_ids or sem_key in existing_semantic:
            skipped_existing += 1
            continue
        existing_event_ids.add(event_id)
        existing_semantic.add(sem_key)
        new_events.append(event)
    if new_events:
        _backfill.append_events(args.events_file, new_events)
    return len(new_events), skipped_existing, skipped_zero_usage


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    args = parse_args(sys.argv[1:] if argv is None else argv)
    signal.signal(signal.SIGINT, _request_stop)
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, _request_stop)
        except (OSError, ValueError):
            pass

    if not args.db_path.exists():
        print(f"OpenCode database not found: {args.db_path}", file=sys.stderr)
        return 2

    since = _backfill.parse_since(args.since) if args.since else None
    first = True
    interval = max(1.0, args.interval)
    print(f"[opencode-watch] db={args.db_path} interval={interval}s once={args.once}", file=sys.stderr)

    while not _STOP:
        try:
            new, skipped, zero = tick(args, since if first else None)
        except Exception as exc:  # never die on a malformed row — log and keep going
            print(f"[tick] ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
            new = skipped = zero = -1
        first = False
        if new > 0 or args.verbose:
            stamp = datetime.now().isoformat(timespec="seconds")
            print(f"[tick] {stamp} new={new} skipped={skipped} zero_usage={zero}", flush=True)
        if args.once:
            break
        # Sleep in 1s chunks so Ctrl+C exits within ~1s
        slept = 0.0
        while slept < interval and not _STOP:
            time.sleep(min(1.0, interval - slept))
            slept += 1.0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
