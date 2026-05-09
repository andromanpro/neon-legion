#!/usr/bin/env python
import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRACKER_DIR = PROJECT_ROOT / "tracker"
EVENTS_FILE = TRACKER_DIR / "claude-events.jsonl"

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
    parser = argparse.ArgumentParser(description="Recompute cost_estimate_usd in tracker/claude-events.jsonl.")
    parser.add_argument("--dry-run", action="store_true", help="Report only; do not rewrite the event log.")
    parser.add_argument(
        "--cleanup-synthetic",
        action="store_true",
        help="Remove existing <synthetic> events while rewriting the event log.",
    )
    return parser.parse_args(argv)


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


def estimate_event_cost(event: dict) -> float | None:
    model = event.get("model")
    if not isinstance(model, str) or HOOK.pricing_for_model(model) is None:
        return None

    return HOOK.estimate_cost(
        model,
        as_int(event.get("input_tokens")),
        as_int(event.get("output_tokens")),
        as_int(event.get("cache_creation_tokens")),
        as_int(event.get("cache_read_tokens")),
    )


def costs_equal(left: object, right: object) -> bool:
    if left is None or right is None:
        return left is None and right is None
    try:
        return float(left) == float(right)
    except (TypeError, ValueError):
        return left == right


def atomic_write_text(path: Path, text: str) -> None:
    TRACKER_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    with temp_path.open("w", encoding="utf-8", newline="\n") as target:
        target.write(text)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temp_path, path)


def recost_events(cleanup_synthetic: bool) -> dict:
    report = {
        "total_events": 0,
        "cost_changed": 0,
        "synthetic_skipped": 0,
        "synthetic_removed": 0,
        "malformed_lines": 0,
        "old_total_cost": 0.0,
        "new_total_cost": 0.0,
        "rewritten_lines": [],
    }

    if not EVENTS_FILE.exists():
        return report

    with EVENTS_FILE.open("r", encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                report["rewritten_lines"].append(line)
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                report["malformed_lines"] += 1
                report["rewritten_lines"].append(line)
                continue

            if not isinstance(event, dict):
                report["malformed_lines"] += 1
                report["rewritten_lines"].append(line)
                continue

            report["total_events"] += 1
            old_has_cost = "cost_estimate_usd" in event
            old_cost = event.get("cost_estimate_usd")
            report["old_total_cost"] += as_float(old_cost)

            if event.get("model") == "<synthetic>":
                if cleanup_synthetic:
                    report["synthetic_removed"] += 1
                    continue

                report["synthetic_skipped"] += 1
                report["new_total_cost"] += as_float(old_cost)
                report["rewritten_lines"].append(line)
                continue

            new_cost = estimate_event_cost(event)
            report["new_total_cost"] += as_float(new_cost)
            if not old_has_cost or not costs_equal(old_cost, new_cost):
                event["cost_estimate_usd"] = new_cost
                report["cost_changed"] += 1

            report["rewritten_lines"].append(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")

    return report


def print_report(report: dict, dry_run: bool, cleanup_synthetic: bool) -> None:
    print("## Recost report")
    print()
    print(f"- **Total events**: {report['total_events']:,}")
    print(f"- **Cost changed**: {report['cost_changed']:,}")
    print(f"- **Old total cost**: ${report['old_total_cost']:.2f}")
    print(f"- **New total cost**: ${report['new_total_cost']:.2f}")
    print(f"- **Synthetic events skipped**: {report['synthetic_skipped']:,}")
    if cleanup_synthetic:
        print(f"- **Synthetic events removed**: {report['synthetic_removed']:,}")
    if report["malformed_lines"]:
        print(f"- **Malformed lines preserved**: {report['malformed_lines']:,}")
    if dry_run:
        print()
        print("Dry run: no files modified.")


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    lock_fd = None
    if not args.dry_run:
        lock_fd = HOOK.acquire_lock()
        if lock_fd is None:
            print("Tracker is locked by another writer; retry recost later.", file=sys.stderr)
            return 1

    try:
        report = recost_events(args.cleanup_synthetic)
        if not args.dry_run and (report["cost_changed"] or report["synthetic_removed"]):
            atomic_write_text(EVENTS_FILE, "".join(report["rewritten_lines"]))
    finally:
        if lock_fd is not None:
            HOOK.release_lock(lock_fd)

    print_report(report, args.dry_run, args.cleanup_synthetic)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
