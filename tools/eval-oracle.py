#!/usr/bin/env python
"""Sample old-vs-new oracle estimates for #106-B validation.

This tool calls `codex exec` through tracker/estimate-task.py::run_oracle,
so running it may make outbound model calls. It is intended for the
architect/human validation gate, not the sandbox implementation step.
"""
import argparse
import importlib.util
import json
import os
import statistics
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ESTIMATOR_PATH = PROJECT_ROOT / "tracker" / "estimate-task.py"
TASKS_FILE = PROJECT_ROOT / "tracker" / "tasks.json"

BUCKET_ORDER = ["stub", "small", "medium", "large", "marathon"]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate size-aware oracle estimates against stored old baselines.")
    parser.add_argument("--sample-per-bucket", type=int, default=3)
    parser.add_argument("--out", help="Markdown output path.")
    return parser.parse_args(argv)


def load_estimator():
    spec = importlib.util.spec_from_file_location("estimate_task_eval", ESTIMATOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load estimator from {ESTIMATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def read_tasks() -> dict:
    try:
        with TASKS_FILE.open("r", encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def as_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def metric_int(metrics: dict, key: str) -> int:
    try:
        return int(metrics.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def metric_float(metrics: dict, key: str) -> float:
    try:
        return float(metrics.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def resolve_transcript_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def bucket_for_event_count(event_count: int) -> str:
    if event_count <= 3:
        return "stub"
    if event_count <= 50:
        return "small"
    if event_count <= 500:
        return "medium"
    if event_count <= 1500:
        return "large"
    return "marathon"


def collect_samples(estimator, sample_per_bucket: int) -> tuple[list[dict], list[str]]:
    notes = []
    bucketed = {name: [] for name in BUCKET_ORDER}
    tasks = read_tasks()

    for sid in sorted(tasks):
        entry = tasks.get(sid)
        if not isinstance(entry, dict):
            continue

        old_baseline = as_float(entry.get("ai_baseline_hours"))
        if old_baseline is None:
            continue

        transcript_raw = entry.get("transcript_path")
        if not isinstance(transcript_raw, str) or not transcript_raw:
            continue

        transcript_path = resolve_transcript_path(transcript_raw)
        if not transcript_path.exists():
            notes.append(f"- skipped {sid[:8]}: transcript not found: {transcript_path}")
            continue

        try:
            metrics = estimator.compute_session_metrics(transcript_path)
        except Exception as exc:
            notes.append(f"- skipped {sid[:8]}: metrics failed: {exc}")
            continue

        event_count = metric_int(metrics, "event_count")
        bucket = bucket_for_event_count(event_count)
        bucketed[bucket].append({
            "sid": sid,
            "bucket": bucket,
            "path": transcript_path,
            "metrics": metrics,
            "old_baseline": old_baseline,
        })

    selected = []
    limit = max(int(sample_per_bucket), 0)
    for bucket in BUCKET_ORDER:
        bucketed[bucket].sort(key=lambda item: item["sid"])
        selected.extend(bucketed[bucket][:limit])

    return selected, notes


def synthetic_stub_item(estimator) -> dict:
    """No live stub transcript survives (Claude Code prunes them), so the
    oracle's stub->~0 rule can't be validated from tasks.json. Inject one
    synthetic 2-event / ~30s aborted session mirroring the ee73792d garbage
    (36h baseline assigned to a 2-event stub) so the gate still proves it."""
    import json as _json
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)
    path = Path(os.environ.get("TEMP", "/tmp")) / "eval-synth-stub.jsonl"
    events = [
        {"type": "user", "timestamp": base.isoformat().replace("+00:00", "Z"),
         "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
        {"type": "assistant",
         "timestamp": (base + timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
         "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}},
    ]
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for ev in events:
            fh.write(_json.dumps(ev) + "\n")
    metrics = estimator.compute_session_metrics(path)
    return {
        "sid": "SYNTH-stub",
        "bucket": "stub",
        "path": path,
        "metrics": metrics,
        "old_baseline": 36.0,  # representative ee73792d-class garbage
    }


def run_new_oracle(estimator, item: dict) -> tuple[float | None, str | None]:
    try:
        user_messages, assistant_messages = estimator.read_transcript(item["path"])
        context = estimator.build_truncated_context_from_messages(user_messages, assistant_messages)
        prompt = estimator.build_estimation_prompt(context, item["metrics"])
        entry = estimator.run_oracle(prompt)
        new_baseline = as_float(entry.get("ai_baseline_hours"))
        if new_baseline is None:
            return None, "oracle returned non-numeric ai_baseline_hours"
        return new_baseline, None
    except Exception as exc:
        return None, str(exc)


def load_old_prompt() -> str | None:
    """The pre-#106-B prompt (no SESSION SIZE block) from main, so the eval
    re-runs OLD in the SAME pass — a true paired before/after that controls
    for LLM run-to-run variance (DeepSeek method-confound nit)."""
    import subprocess
    try:
        out = subprocess.run(
            ["git", "show", "main:tracker/oracle-prompt.txt"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True, encoding="utf-8",
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        return out.stdout
    except Exception:
        return None


def run_old_oracle(estimator, item: dict, old_prompt: str) -> tuple[float | None, str | None]:
    """OLD assembly = old prompt + transcript marker + context (NO size block)."""
    try:
        user_messages, assistant_messages = estimator.read_transcript(item["path"])
        context = estimator.build_truncated_context_from_messages(user_messages, assistant_messages)
        prompt = old_prompt + "\n\n=== TRANSCRIPT (truncated) ===\n" + context
        entry = estimator.run_oracle(prompt)
        return as_float(entry.get("ai_baseline_hours")), None
    except Exception as exc:
        return None, str(exc)


def evaluate_rows(rows: list[dict]) -> tuple[list[str], dict[str, float]]:
    reasons = []
    medians = {}

    for row in rows:
        new_baseline = row.get("new_baseline")
        if new_baseline is None:
            continue
        if row["bucket"] == "stub" and new_baseline > 0.5:
            reasons.append(f"stub {row['sid'][:8]} new_base {new_baseline:.2f} > 0.5")
        if row["bucket"] == "marathon":
            old_baseline = row["old_baseline"]
            if new_baseline < old_baseline:
                reasons.append(
                    f"marathon {row['sid'][:8]} new_base {new_baseline:.2f} < old_base {old_baseline:.2f}"
                )
            if new_baseline < 1.0:
                reasons.append(f"marathon {row['sid'][:8]} new_base {new_baseline:.2f} < 1.0")

    for bucket in BUCKET_ORDER:
        values = [
            row["new_baseline"]
            for row in rows
            if row["bucket"] == bucket and row.get("new_baseline") is not None
        ]
        if values:
            medians[bucket] = statistics.median(values)

    previous_bucket = None
    previous_value = None
    for bucket in BUCKET_ORDER:
        if bucket not in medians:
            continue
        value = medians[bucket]
        if previous_value is not None and value < previous_value:
            reasons.append(
                f"median new_base decreases {previous_bucket}->{bucket}: {previous_value:.2f} > {value:.2f}"
            )
        previous_bucket = bucket
        previous_value = value

    return reasons, medians


def hours_per_event(hours: float | None, events: int) -> str:
    if hours is None or events <= 0:
        return "n/a"
    return f"{hours / events:.4f}"


def fmt_hours(value: float | None) -> str:
    if value is None:
        return "ERR"
    return f"{value:.2f}"


def fmt_metric(value: float) -> str:
    return f"{value:.3f}"


def render_markdown(rows: list[dict], notes: list[str], sample_per_bucket: int) -> str:
    reasons, medians = evaluate_rows(rows)
    lines = [
        "# Oracle Size-Aware Eval",
        "",
        f"Sample per bucket: {sample_per_bucket}",
        "",
        "| sid | events | active_h | span_h | stored_old | fresh_old | new_base | new_h/ev |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]

    for row in sorted(rows, key=lambda item: (metric_int(item["metrics"], "event_count"), item["sid"])):
        metrics = row["metrics"]
        events = metric_int(metrics, "event_count")
        old_baseline = row["old_baseline"]
        new_baseline = row.get("new_baseline")
        lines.append(
            "| "
            + " | ".join([
                row["sid"][:8],
                str(events),
                fmt_metric(metric_float(metrics, "active_hours")),
                fmt_metric(metric_float(metrics, "span_hours")),
                fmt_hours(old_baseline),
                fmt_hours(row.get("fresh_old_baseline")),
                fmt_hours(new_baseline),
                hours_per_event(new_baseline, events),
            ])
            + " |"
        )

    lines.extend(["", "## Heuristics"])
    if medians:
        median_text = ", ".join(f"{bucket}={value:.2f}" for bucket, value in medians.items())
        lines.append(f"- Median new_base by bucket: {median_text}")
    else:
        lines.append("- Median new_base by bucket: n/a")

    # Noise/drift baseline: fresh_old vs stored_old on the SAME (old) prompt
    # isolates LLM run-to-run variance + temporal drift from the treatment.
    drift = [
        abs(r["fresh_old_baseline"] - r["old_baseline"])
        for r in rows
        if r.get("fresh_old_baseline") is not None and r.get("old_baseline")
        and r["bucket"] != "stub"
    ]
    if drift:
        lines.append(
            f"- Old-prompt noise/drift (|fresh_old - stored_old|, non-stub): "
            f"median {statistics.median(drift):.2f}h, max {max(drift):.2f}h"
        )
    # Paired treatment effect: new vs fresh_old, same eval run (controls variance).
    paired = [
        (r["new_baseline"], r["fresh_old_baseline"], r["sid"][:8], r["bucket"])
        for r in rows
        if r.get("new_baseline") is not None and r.get("fresh_old_baseline") is not None
    ]
    if paired:
        treat = [n - fo for n, fo, _, _ in paired]
        lines.append(
            f"- Paired treatment (new - fresh_old): median {statistics.median(treat):+.2f}h "
            f"over {len(paired)} sessions"
        )
    # Overshoot WARN (not FAIL — de-saturation is intended; surface extremes).
    over = [
        f"{r['sid'][:8]} new {r['new_baseline']:.0f} vs stored_old {r['old_baseline']:.0f}"
        for r in rows
        if r.get("new_baseline") is not None and r.get("old_baseline")
        and r["old_baseline"] > 0 and r["new_baseline"] > r["old_baseline"] * 50
    ]
    if over:
        lines.append(f"- WARN extreme jump (new > stored_old x50, manual check): {'; '.join(over)}")

    if notes:
        lines.extend(["", "## Notes"])
        lines.extend(notes)

    if reasons:
        lines.extend(["", "EVAL: FAIL (" + "; ".join(reasons) + ")"])
    else:
        lines.extend(["", "EVAL: PASS"])

    return "\n".join(lines) + "\n"


def write_output(path: str, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(f".{target.name}.tmp.{os.getpid()}")
    with temp_path.open("w", encoding="utf-8", newline="\n") as target_file:
        target_file.write(text)
        target_file.flush()
        os.fsync(target_file.fileno())
    os.replace(temp_path, target)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    estimator = load_estimator()
    selected, notes = collect_samples(estimator, args.sample_per_bucket)
    try:
        selected.insert(0, synthetic_stub_item(estimator))
    except Exception as exc:
        notes.append(f"- synthetic stub injection failed: {exc}")

    old_prompt = load_old_prompt()
    if old_prompt is None:
        notes.append("- old-prompt paired run skipped: could not load main:tracker/oracle-prompt.txt")

    rows = []
    for item in selected:
        row = dict(item)
        new_baseline, error = run_new_oracle(estimator, item)
        row["new_baseline"] = new_baseline
        if error is not None:
            notes.append(f"- skipped new oracle for {item['sid'][:8]}: {error}")
        if old_prompt is not None:
            fresh_old, old_err = run_old_oracle(estimator, item, old_prompt)
            row["fresh_old_baseline"] = fresh_old
            if old_err is not None:
                notes.append(f"- skipped old-prompt run for {item['sid'][:8]}: {old_err}")
        else:
            row["fresh_old_baseline"] = None
        rows.append(row)

    output = render_markdown(rows, notes, args.sample_per_bucket)
    if args.out:
        write_output(args.out, output)
    print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
