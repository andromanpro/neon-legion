#!/usr/bin/env python
"""Retroactively add schema_version=1 to persisted neon-legion records.

Contract:
    This one-off backfill scans tracker/*-events.jsonl and
    orchestrate-runs/*/state.json. Records that already have schema_version
    are left byte-for-byte untouched in JSONL files; records missing it are
    rewritten with schema_version as the first key. Rewrites are atomic:
    temporary file, fsync, then os.replace.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


CURRENT_SCHEMA_VERSION = 1
PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRACKER_DIR = PROJECT_ROOT / "tracker"
RUNS_DIR = PROJECT_ROOT / "orchestrate-runs"


def with_schema_version(record: dict) -> dict:
    if "schema_version" in record:
        return record
    return {"schema_version": CURRENT_SCHEMA_VERSION, **record}


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def backfill_jsonl(path: Path, dry_run: bool) -> tuple[int, int, int]:
    total = 0
    changed = 0
    malformed = 0
    new_lines: list[str] = []

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                new_lines.append(line)
                continue
            total += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                new_lines.append(line)
                continue
            if not isinstance(record, dict):
                malformed += 1
                new_lines.append(line)
                continue
            if "schema_version" in record:
                new_lines.append(line)
                continue
            changed += 1
            new_lines.append(
                json.dumps(with_schema_version(record), ensure_ascii=False, separators=(",", ":")) + "\n"
            )

    if changed and not dry_run:
        atomic_write_text(path, "".join(new_lines))
    return total, changed, malformed


def backfill_state_json(path: Path, dry_run: bool) -> tuple[int, int, int]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return 0, 0, 1
    if not isinstance(payload, dict):
        return 0, 0, 1
    if "schema_version" in payload:
        return 1, 0, 0
    if not dry_run:
        text = json.dumps(with_schema_version(payload), ensure_ascii=False, indent=2) + "\n"
        atomic_write_text(path, text)
    return 1, 1, 0


def candidate_files() -> tuple[list[Path], list[Path]]:
    event_files = sorted(TRACKER_DIR.glob("*-events.jsonl")) if TRACKER_DIR.exists() else []
    state_files = sorted(RUNS_DIR.glob("*/state.json")) if RUNS_DIR.exists() else []
    return event_files, state_files


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report changes without rewriting files.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    event_files, state_files = candidate_files()
    total_records = 0
    changed_records = 0
    malformed_records = 0

    for path in event_files:
        total, changed, malformed = backfill_jsonl(path, args.dry_run)
        total_records += total
        changed_records += changed
        malformed_records += malformed
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        print(f"{rel}: records={total} changed={changed} malformed={malformed}")

    for path in state_files:
        total, changed, malformed = backfill_state_json(path, args.dry_run)
        total_records += total
        changed_records += changed
        malformed_records += malformed
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        print(f"{rel}: records={total} changed={changed} malformed={malformed}")

    print(
        f"summary: files={len(event_files) + len(state_files)} "
        f"records={total_records} changed={changed_records} malformed={malformed_records} "
        f"dry_run={str(args.dry_run).lower()}"
    )
    return 1 if malformed_records else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
