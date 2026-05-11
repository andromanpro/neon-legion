#!/usr/bin/env python
"""Schema migration dispatcher for neon-legion data files.

Usage:
    python tools/schema_migrate.py --check
    python tools/schema_migrate.py --target 2

Contract:
    The checker is read-only. It scans persisted event/state/snapshot records,
    reports schema coverage, and exits 0 only when all present records are at
    CURRENT_SCHEMA_VERSION. Actual migrations are intentionally unimplemented
    until a schema newer than v1 exists.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


CURRENT_SCHEMA_VERSION = 1
MIGRATIONS = {
    # (from_version, to_version): callable(path, dry_run) -> bool
    # e.g. (1, 2): migrate_v1_to_v2,
}

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRACKER_DIR = PROJECT_ROOT / "tracker"
RUNS_DIR = PROJECT_ROOT / "orchestrate-runs"
DASHBOARD_DIR = PROJECT_ROOT / "dashboard"


def _record_version(record: dict) -> tuple[int | None, str | None]:
    if "schema_version" not in record:
        return None, None
    value = record.get("schema_version")
    if isinstance(value, bool) or not isinstance(value, int):
        return None, f"invalid schema_version: {value!r}"
    return value, None


def _empty_report(path: Path, kind: str) -> dict:
    return {
        "path": path,
        "kind": kind,
        "total": 0,
        "with_schema": 0,
        "without_schema": 0,
        "max_version": None,
        "error": None,
    }


def _add_record(report: dict, record: dict, label: str) -> None:
    report["total"] += 1
    version, error = _record_version(record)
    if error is not None:
        report["error"] = f"{label}: {error}"
        return
    if version is None:
        report["without_schema"] += 1
        return
    report["with_schema"] += 1
    current = report["max_version"]
    if current is None or version > current:
        report["max_version"] = version


def scan_file(path: Path) -> dict:
    kind = "jsonl" if path.suffix == ".jsonl" else "json"
    report = _empty_report(path, kind)
    if not path.exists():
        return report

    try:
        if kind == "jsonl":
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line_no, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as exc:
                        report["error"] = f"line {line_no}: {exc}"
                        break
                    if not isinstance(record, dict):
                        report["error"] = f"line {line_no}: JSONL record is not an object"
                        break
                    _add_record(report, record, f"line {line_no}")
                    if report["error"] is not None:
                        break
            return report

        with path.open("r", encoding="utf-8", errors="replace") as handle:
            payload = json.load(handle)
        if isinstance(payload, dict):
            _add_record(report, payload, "root")
        elif isinstance(payload, list):
            for index, record in enumerate(payload):
                if not isinstance(record, dict):
                    report["error"] = f"item {index}: JSON record is not an object"
                    break
                _add_record(report, record, f"item {index}")
                if report["error"] is not None:
                    break
        else:
            report["error"] = "JSON root is not an object or list"
    except OSError as exc:
        report["error"] = str(exc)
    except json.JSONDecodeError as exc:
        report["error"] = str(exc)

    return report


def check_file(path: Path) -> tuple[int | None, str | None]:
    """Return (max_version_found, error or None). Handles .jsonl and .json."""
    report = scan_file(path)
    return report["max_version"], report["error"]


def candidate_files() -> list[Path]:
    paths: list[Path] = []
    if TRACKER_DIR.exists():
        paths.extend(sorted(TRACKER_DIR.glob("*.jsonl")))
    if RUNS_DIR.exists():
        paths.extend(sorted(RUNS_DIR.glob("*/state.json")))
    if DASHBOARD_DIR.exists():
        paths.extend(sorted(DASHBOARD_DIR.glob("*snapshot*.json")))
        snapshot = DASHBOARD_DIR / "snapshot.json"
        if snapshot.exists() and snapshot not in paths:
            paths.append(snapshot)
    return sorted(set(paths), key=lambda path: path.relative_to(PROJECT_ROOT).as_posix())


def report_status(report: dict) -> str:
    if report["error"] is not None:
        return "error"
    if report["total"] == 0:
        return "empty"
    if report["with_schema"] == 0:
        return "legacy"
    if report["without_schema"]:
        return "mixed"
    max_version = report["max_version"]
    if max_version == CURRENT_SCHEMA_VERSION:
        return "current"
    if isinstance(max_version, int) and max_version > CURRENT_SCHEMA_VERSION:
        return "newer"
    return "old"


def command_check() -> int:
    paths = candidate_files()
    reports = [scan_file(path) for path in paths]
    counts = {
        "current": 0,
        "legacy": 0,
        "mixed": 0,
        "old": 0,
        "newer": 0,
        "empty": 0,
        "error": 0,
    }

    print(f"current_schema_version={CURRENT_SCHEMA_VERSION}")
    print(f"files_scanned={len(reports)}")
    if not reports:
        print("status=ok")
        return 0

    for report in reports:
        status = report_status(report)
        counts[status] += 1
        rel = report["path"].relative_to(PROJECT_ROOT).as_posix()
        detail = (
            f"{rel}: status={status} records={report['total']} "
            f"with_schema={report['with_schema']} without_schema={report['without_schema']} "
            f"max_version={report['max_version']}"
        )
        if report["error"] is not None:
            detail += f" error={report['error']}"
        print(detail)

    print(
        "summary="
        + " ".join(f"{name}={counts[name]}" for name in sorted(counts))
    )
    ok = counts["legacy"] == counts["mixed"] == counts["old"] == counts["newer"] == counts["error"] == 0
    print("status=ok" if ok else "status=needs_migration")
    return 0 if ok else 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="Report schema coverage without modifying files.")
    action.add_argument("--target", type=int, help="Target schema version for migration.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.check:
        return command_check()

    print(
        f"schema migrations are not implemented yet (current={CURRENT_SCHEMA_VERSION}, target={args.target})",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
