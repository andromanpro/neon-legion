#!/usr/bin/env python
"""Detect per-task-shape model cost slippage."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tracker"))

from tools import config as cfg  # noqa: E402
import summary  # noqa: E402


def detect_slippages(
    events: list[dict],
    *,
    now: datetime | None = None,
    threshold: float = 1.3,
    min_events: int = 5,
    short_days: int = 7,
    long_days: int = 30,
) -> dict:
    """Return the slippage.json payload."""
    current = (now or datetime.now().astimezone()).astimezone()
    today = current.date()
    short_days = max(int(short_days), 1)
    long_days = max(int(long_days), 1)
    short_start = today - timedelta(days=short_days - 1)
    long_start = today - timedelta(days=long_days - 1)
    threshold = float(threshold)
    min_events = int(min_events)

    buckets: dict[tuple[str, str, str], list[tuple[date, float]]] = defaultdict(list)
    for event in events:
        day = _event_date(event)
        if day is None or day < long_start or day > today:
            continue
        buckets[_fingerprint(event)].append((day, summary.as_float(event.get("cost_estimate_usd"))))

    slippages = []
    baselines = []
    fingerprints_with_min_events = 0
    for (provider, model, prompt_size_bucket), entries in buckets.items():
        short_costs = [cost for day, cost in entries if short_start <= day <= today]
        if len(short_costs) < min_events:
            continue
        fingerprints_with_min_events += 1

        # Landscape row for EVERY qualifying fingerprint (not just drifting
        # ones): "what does this model cost per prompt-size bucket". Lets the
        # dashboard render the cost/prompt-size distribution even when zero
        # drift is detected (the common, healthy case) instead of a blank panel.
        window_costs = [cost for day, cost in entries if long_start <= day <= today]
        baselines.append(
            {
                "provider": provider,
                "model": model,
                "prompt_size_bucket": prompt_size_bucket,
                "events": len(window_costs),
                "events_7d": len(short_costs),
                "median_cost": _median(window_costs),
                "median_cost_7d": _median(short_costs),
                "p95_cost": _p95(window_costs),
            }
        )

        # Baseline must EXCLUDE the recent short window — otherwise a real
        # spike in the last 7d pulls up the 30d median and hides itself.
        # Compare 7d against the preceding (long_days - short_days) days
        # (DeepSeek MED — overlap dampening masks the very slippage the
        # tool was built to detect).
        baseline_costs = [
            cost for day, cost in entries if long_start <= day < short_start
        ]
        # `long_costs` retained in the output for backward compatibility
        # with any downstream that read the total window — but the RATIO
        # is now computed against the clean baseline.
        long_costs = [cost for day, cost in entries if long_start <= day <= today]
        median_baseline = _median(baseline_costs)
        if median_baseline <= 0:
            continue

        median_cost_7d = _median(short_costs)
        ratio = median_cost_7d / median_baseline
        if ratio <= threshold:
            continue

        slippages.append(
            {
                "provider": provider,
                "model": model,
                "prompt_size_bucket": prompt_size_bucket,
                "events_7d": len(short_costs),
                "events_30d": len(long_costs),
                "events_baseline": len(baseline_costs),
                "median_cost_7d": median_cost_7d,
                "median_cost_baseline": median_baseline,
                "median_cost_30d": _median(long_costs),
                "p95_cost_7d": _p95(short_costs),
                "ratio": ratio,
                "delta_pct": (ratio - 1.0) * 100.0,
            }
        )

    slippages.sort(key=lambda item: item["ratio"], reverse=True)
    baselines.sort(key=lambda item: item["median_cost"], reverse=True)
    return {
        "schema_version": 1,
        "generated_at": current.isoformat(timespec="seconds"),
        "config": {
            "threshold": threshold,
            "min_events": min_events,
            "short_days": short_days,
            "long_days": long_days,
        },
        "slippages": slippages,
        "baselines": baselines,
        "summary": {
            "total_fingerprints_scanned": len(buckets),
            "fingerprints_with_min_events": fingerprints_with_min_events,
            "slippages_count": len(slippages),
            "baselines_count": len(baselines),
            "retry_tracking": "deferred - no session-retry column in tracker events",
        },
    }


def write_slippages(payload: dict, output_path: Path) -> None:
    """Atomic write of slippage.json to disk."""
    output_path = output_path.expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_name(f".{output_path.name}.tmp.{os.getpid()}")
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp_path, output_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def main() -> int:
    """CLI entrypoint."""
    args = _parse_args()
    threshold = args.threshold if args.threshold is not None else cfg.get("model_slippage.threshold", 1.3, float)
    min_events = args.min_events if args.min_events is not None else cfg.get("model_slippage.min_events", 5, int)
    short_days = args.short_days if args.short_days is not None else cfg.get("model_slippage.short_days", 7, int)
    long_days = args.long_days if args.long_days is not None else cfg.get("model_slippage.long_days", 30, int)
    output = args.output or cfg.get("model_slippage.output_path", "tracker/slippage.json", str)

    now = datetime.now().astimezone()
    start = now.date() - timedelta(days=max(long_days, 1) - 1)
    events = summary.read_events(start, now.date())
    payload = detect_slippages(
        events,
        now=now,
        threshold=threshold,
        min_events=min_events,
        short_days=short_days,
        long_days=long_days,
    )
    output_path = Path(output)
    write_slippages(payload, output_path)
    _log(f"wrote {output_path}")
    _log(
        "slippages="
        f"{payload['summary']['slippages_count']} "
        f"fingerprints={payload['summary']['total_fingerprints_scanned']}"
    )
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Detect model cost slippage by provider/model/prompt-size bucket.")
    parser.add_argument("--output", help="Path to write slippage.json.")
    parser.add_argument("--threshold", type=float, help="7d/30d median-cost ratio threshold.")
    parser.add_argument("--min-events", type=int, help="Minimum 7d events for a fingerprint to be scanned.")
    parser.add_argument("--short-days", type=int, help="Short comparison window in days.")
    parser.add_argument("--long-days", type=int, help="Long baseline window in days.")
    return parser.parse_args()


def _fingerprint(event: dict) -> tuple[str, str, str]:
    provider = summary.event_provider(event)
    model = _event_model(event)
    input_tokens = summary.as_int(event.get("input_tokens"))
    bucket = _prompt_size_bucket(input_tokens)
    return (provider, model, bucket)


def _prompt_size_bucket(tokens: int) -> str:
    if tokens < 1_000:
        return "xs"
    if tokens < 10_000:
        return "s"
    if tokens < 100_000:
        return "m"
    return "l"


def _event_date(event: dict) -> date | None:
    ts = summary.parse_event_ts(event.get("ts"))
    return ts.astimezone().date() if ts is not None else None


def _event_model(event: dict) -> str:
    model = event.get("model")
    return model if isinstance(model, str) and model else "unknown"


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2.0


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(math.ceil(len(ordered) * 0.95) - 1, 0)
    return ordered[index]


def _log(message: str) -> None:
    print(f"[model-slippage] {message}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
