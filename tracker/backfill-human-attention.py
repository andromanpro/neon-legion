#!/usr/bin/env python
"""Freeze per-session human-prompt timestamps while transcripts still exist.

Claude Code rotates transcripts out of ~/.claude/projects. Once a transcript is
gone, summarize_productivity had no way to know how long the HUMAN actually
spent in that session, so it fell back to AI-event timestamps — i.e. AI-busy
time, the very thing the human-attention denominator replaced. Effect: the
all-time multiplier drifted DOWN on its own as history aged out, and the
all-time number stopped being comparable to the 7d/today ones.

This backfill scans every live transcript, extracts genuine human-prompt
timestamps per session, and freezes them into tracker/human-attention.json.
summary.read_human_attention_cache() then serves those sessions forever, so a
session measured once keeps measuring human time after its transcript is gone.

Sessions whose transcript is still present are re-captured each run (a live
session grows), so the frozen value tracks the transcript until it disappears.
Entries whose transcript is gone are preserved untouched.

Only timestamps (epoch seconds) are stored — never prompt text.

Stdlib only.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "tracker"))

import summary  # noqa: E402

DEFAULT_SCAN_DIR = Path.home() / ".claude" / "projects"
CACHE_FILE = summary.HUMAN_ATTENTION_CACHE_FILE

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan-dir", type=Path, default=DEFAULT_SCAN_DIR)
    parser.add_argument("--cache-file", type=Path, default=CACHE_FILE)
    parser.add_argument("--dry-run", action="store_true", help="Report without writing.")
    return parser.parse_args(argv)


def load_raw_cache(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def scan_live_transcripts(scan_dir: Path) -> dict[str, list[float]]:
    """session_id -> sorted epoch seconds of genuine human prompts."""
    by_session: dict[str, set[float]] = {}
    pattern = str(Path(scan_dir).expanduser() / "*" / "*.jsonl")
    for path_str in glob.glob(pattern):
        # The transcript filename is the session id for Claude Code, but trust
        # the per-event session_id when present.
        fallback_sid = Path(path_str).stem
        try:
            with open(path_str, encoding="utf-8") as source:
                for line in source:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not summary.is_human_prompt(event):
                        continue
                    ts = summary.parse_event_ts(event.get("timestamp") or event.get("ts"))
                    if ts is None:
                        continue
                    sid = event.get("session_id")
                    if not isinstance(sid, str) or not sid:
                        sid = fallback_sid
                    by_session.setdefault(sid, set()).add(ts.timestamp())
        except (OSError, UnicodeDecodeError):
            continue
    return {sid: sorted(stamps) for sid, stamps in by_session.items()}


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    scan_dir = Path(args.scan_dir).expanduser()
    if not scan_dir.is_dir():
        print(f"[human-attention] scan dir not found (nothing to freeze): {scan_dir}")
        return 0

    existing = load_raw_cache(Path(args.cache_file))
    live = scan_live_transcripts(scan_dir)

    captured_at = datetime.now().astimezone().isoformat(timespec="seconds")
    merged = dict(existing)
    new_sessions = 0
    refreshed = 0
    for sid, stamps in live.items():
        if sid in merged:
            prev = merged[sid].get("ts") if isinstance(merged[sid], dict) else None
            if isinstance(prev, list) and len(prev) == len(stamps):
                continue  # unchanged — don't churn the file
            refreshed += 1
        else:
            new_sessions += 1
        merged[sid] = {"ts": stamps, "prompts": len(stamps), "captured_at": captured_at}

    kept = len(existing) - refreshed
    if not args.dry_run and (new_sessions or refreshed):
        _atomic_write_json(Path(args.cache_file), merged)

    print(
        f"[human-attention] live_sessions={len(live)} new={new_sessions} refreshed={refreshed} "
        f"kept_frozen={max(kept, 0)} total_cached={len(merged)}"
        f"{' (dry-run)' if args.dry_run else ''}"
    )
    return 0


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as target:
            json.dump(payload, target, ensure_ascii=False, separators=(",", ":"))
            target.flush()
            os.fsync(target.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
