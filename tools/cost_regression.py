#!/usr/bin/env python
"""Detect cost-per-output-token regressions across provider/model pairs."""

from __future__ import annotations

import argparse
import json
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


def detect_regressions(
    events: list[dict],
    *,
    now: datetime | None = None,
    threshold: float = 1.2,
    min_calls: int = 10,
    short_days: int = 7,
    long_days: int = 30,
) -> dict:
    """Return the regressions.json payload dict."""
    current = (now or datetime.now().astimezone()).astimezone()
    today = current.date()
    short_start = today - timedelta(days=max(short_days, 1) - 1)
    long_start = today - timedelta(days=max(long_days, 1) - 1)
    threshold = float(threshold)
    min_calls = int(min_calls)

    buckets: dict[tuple[str, str], dict[date, dict[str, float]]] = defaultdict(
        lambda: defaultdict(lambda: {"cost": 0.0, "output": 0.0})
    )
    for event in events:
        day = _event_date(event)
        if day is None or day < long_start or day > today:
            continue
        provider = summary.event_provider(event)
        model = _event_model(event)
        bucket = buckets[(provider, model)][day]
        bucket["cost"] += summary.as_float(event.get("cost_estimate_usd"))
        bucket["output"] += summary.as_int(event.get("output_tokens"))

    regressions = []
    pairs_with_min_calls = 0
    for (provider, model), by_day in buckets.items():
        short_cost, short_output = _window_totals(by_day, short_start, today)
        long_cost, long_output = _window_totals(by_day, long_start, today)
        if short_output < min_calls:
            continue
        pairs_with_min_calls += 1
        if long_cost == 0 or long_output <= 0 or short_output <= 0:
            continue

        short_rate = short_cost / short_output
        long_rate = long_cost / long_output
        if long_rate <= 0:
            continue
        ratio = short_rate / long_rate
        if ratio <= threshold:
            continue

        window_start, window_end = _narrow_window(by_day, short_start, today, long_rate, threshold)
        regressions.append(
            {
                "provider": provider,
                "model": model,
                "cost_per_otok_7d": short_rate,
                "cost_per_otok_30d": long_rate,
                "ratio": ratio,
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "delta_pct": (ratio - 1.0) * 100.0,
            }
        )

    regressions.sort(key=lambda item: item["ratio"], reverse=True)
    return {
        "schema_version": 1,
        "generated_at": current.isoformat(timespec="seconds"),
        "config": {
            "threshold": threshold,
            "min_calls": min_calls,
            "window_short_days": max(short_days, 1),
            "window_long_days": max(long_days, 1),
        },
        "regressions": regressions,
        "summary": {
            "total_pairs_scanned": len(buckets),
            "pairs_with_min_calls": pairs_with_min_calls,
            "regressions_count": len(regressions),
        },
    }


def write_regressions(payload: dict, output_path: Path) -> None:
    """Atomic write of regressions.json to disk."""
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
    threshold = args.threshold if args.threshold is not None else cfg.get("cost_regression.threshold", 1.2, float)
    min_calls = args.min_calls if args.min_calls is not None else cfg.get("cost_regression.min_calls", 10, int)
    short_days = cfg.get("cost_regression.short_days", 7, int)
    long_days = cfg.get("cost_regression.long_days", 30, int)
    output = args.output or cfg.get("cost_regression.output_path", "tracker/regressions.json", str)

    now = datetime.now().astimezone()
    start = now.date() - timedelta(days=max(long_days, 1) - 1)
    events = summary.read_events(start, now.date())
    payload = detect_regressions(
        events,
        now=now,
        threshold=threshold,
        min_calls=min_calls,
        short_days=short_days,
        long_days=long_days,
    )
    output_path = _output_path(output)
    write_regressions(payload, output_path)
    _log(f"wrote {output_path}")
    _log(f"regressions={payload['summary']['regressions_count']} pairs={payload['summary']['total_pairs_scanned']}")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Detect vendor cost-per-output-token regressions.")
    parser.add_argument("--output", help="Path to write regressions.json.")
    parser.add_argument("--threshold", type=float, help="7d/30d ratio threshold.")
    parser.add_argument("--min-calls", type=int, help="Minimum 7d output tokens for a pair.")
    return parser.parse_args()


def _output_path(value: str) -> Path:
    path = Path(value)
    if os.name == "nt" and path.root and not path.drive:
        return Path(f"{Path.cwd().drive}{value}")
    return path


def _window_totals(by_day: dict[date, dict[str, float]], start: date, end: date) -> tuple[float, float]:
    cost = 0.0
    output = 0.0
    for day, stats in by_day.items():
        if start <= day <= end:
            cost += stats["cost"]
            output += stats["output"]
    return cost, output


def _narrow_window(
    by_day: dict[date, dict[str, float]],
    start: date,
    end: date,
    baseline: float,
    threshold: float,
) -> tuple[date, date]:
    elevated: list[date] = []
    day = start
    while day <= end:
        stats = by_day.get(day, {"cost": 0.0, "output": 0.0})
        output = stats["output"]
        if output > 0 and (stats["cost"] / output) / baseline > threshold:
            elevated.append(day)
        day += timedelta(days=1)

    if not elevated:
        return start, end

    window_end = elevated[-1]
    window_start = window_end
    elevated_set = set(elevated)
    while window_start - timedelta(days=1) in elevated_set:
        window_start -= timedelta(days=1)
    return window_start, window_end


def _event_date(event: dict) -> date | None:
    ts = summary.parse_event_ts(event.get("ts"))
    return ts.astimezone().date() if ts is not None else None


def _event_model(event: dict) -> str:
    model = event.get("model")
    return model if isinstance(model, str) and model else "unknown"


def _log(message: str) -> None:
    print(f"[cost-regression] {message}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
