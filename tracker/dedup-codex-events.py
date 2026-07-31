"""One-shot semantic dedup of the Codex event ledger.

backfill-codex-sessions.py failed to register the semantic key of accepted
events within a run, so a re-emitted last_token_usage (same session + same
token counters, later line => fresh event_id) slipped past the gate. By
2026-07-31 the ledger held 4,992 duplicated semantic groups / 5,819 extra
billable events. The backfill bug is fixed; this script removes the already
accumulated duplicates.

Like tracker/recost.py, this is a sanctioned exception to the append-only
ledger rule: an atomic in-place rewrite that keeps the FIRST event of every
semantic group (matching the order the backfill would have kept them in) and
drops the rest. Run once, then `recost.py --provider codex` to reprice.

Usage:
    py -3.14 tracker/dedup-codex-events.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVENTS_FILE = PROJECT_ROOT / "tracker" / "codex-events.jsonl"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def semantic_key(event: dict) -> tuple:
    # Mirrors backfill-codex-sessions.semantic_key — keep in sync.
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


def atomic_write(path: Path, text: str) -> None:
    temp_path = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    with temp_path.open("w", encoding="utf-8", newline="\n") as target:
        target.write(text)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temp_path, path)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report only, no rewrite")
    parser.add_argument("--events-file", type=Path, default=EVENTS_FILE)
    args = parser.parse_args(argv)

    if not args.events_file.exists():
        print(f"missing: {args.events_file}")
        return 1

    seen: set[tuple] = set()
    kept_lines: list[str] = []
    total = kept = dropped = malformed = 0
    dropped_cost = 0.0

    with args.events_file.open("r", encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                kept_lines.append(line if line.endswith("\n") else line + "\n")
                continue
            total += 1
            key = semantic_key(event)
            if key in seen:
                dropped += 1
                cost = event.get("cost_estimate_usd")
                if isinstance(cost, (int, float)):
                    dropped_cost += cost
                continue
            seen.add(key)
            kept += 1
            kept_lines.append(line if line.endswith("\n") else line + "\n")

    print("## Codex ledger dedup report")
    print(f"- total events: {total}")
    print(f"- kept: {kept}")
    print(f"- dropped duplicates: {dropped}")
    print(f"- dropped stored cost: ${dropped_cost:,.2f}")
    print(f"- malformed lines preserved: {malformed}")

    if args.dry_run:
        print("dry_run=true (no rewrite)")
        return 0

    atomic_write(args.events_file, "".join(kept_lines))
    print(f"rewritten: {args.events_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
