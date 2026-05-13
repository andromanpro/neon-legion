#!/usr/bin/env python
"""Benchmark JSONL event reads against the in-memory SQLite read-model.

No outbound network calls. Reads local tracker JSONL files only.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "tracker") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "tracker"))

from backend import readmodel  # noqa: E402
import summary  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark backend read-model reads.")
    parser.add_argument("--days", type=int, default=30, help="Calendar-day window to read.")
    parser.add_argument("--runs", type=int, default=10, help="Timed runs per path.")
    return parser.parse_args()


def median_seconds(func, runs: int) -> float:
    samples = []
    for _ in range(max(runs, 1)):
        started = time.perf_counter()
        func()
        samples.append(time.perf_counter() - started)
    return statistics.median(samples)


def main() -> int:
    args = parse_args()
    today = datetime.now().astimezone().date()
    start = today - timedelta(days=max(args.days, 1) - 1)
    end = today
    events_dir = PROJECT_ROOT / "tracker"

    conn, meta = readmodel.build_with_meta(events_dir)
    try:
        slow = median_seconds(lambda: readmodel.read_events(conn, start, end), args.runs)
        fast = median_seconds(lambda: readmodel.read_events_fast(conn, start, end), args.runs)
        aggregate = median_seconds(lambda: readmodel.aggregate_by_model(conn, start, end), args.runs)
        jsonl = median_seconds(lambda: summary.read_events(start, end), args.runs)
    finally:
        conn.close()

    fast_jsonl_ratio = fast / jsonl if jsonl > 0 else float("inf")
    aggregate_jsonl_ratio = aggregate / jsonl if jsonl > 0 else float("inf")
    fast_slow_ratio = fast / slow if slow > 0 else float("inf")
    print(f"window: {start.isoformat()}..{end.isoformat()} ({args.days} days)")
    print(f"events indexed: {meta['events']} tasks indexed: {meta['tasks']}")
    print(f"readmodel slow median: {slow:.6f}s ({args.runs} runs)")
    print(f"readmodel fast median: {fast:.6f}s ({args.runs} runs)")
    print(f"readmodel aggregate:    {aggregate:.6f}s ({args.runs} runs)")
    print(f"jsonl     median:      {jsonl:.6f}s ({args.runs} runs)")
    print(f"speedup vs jsonl:      {aggregate_jsonl_ratio:.2f}x")
    print(f"fast vs jsonl:         {fast_jsonl_ratio:.2f}x")
    print(f"speedup vs slow:       {fast_slow_ratio:.2f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
