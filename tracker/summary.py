#!/usr/bin/env python
import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVENTS_FILE = PROJECT_ROOT / "tracker" / "claude-events.jsonl"
MONTHLY_SUBSCRIPTION_USD = 200.0
PRORATE_DAYS = 30.0

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize Claude Code tracking events.")
    parser.add_argument("--days", type=int, default=1, help="Number of local calendar days to include.")
    parser.add_argument("--from", dest="from_date", help="Start date, inclusive, in YYYY-MM-DD format.")
    parser.add_argument("--to", dest="to_date", help="End date, inclusive, in YYYY-MM-DD format.")
    return parser.parse_args(argv)


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def resolve_period(args: argparse.Namespace) -> tuple[date, date]:
    today = datetime.now().astimezone().date()
    days = max(args.days, 1)

    start = parse_date(args.from_date) if args.from_date else None
    end = parse_date(args.to_date) if args.to_date else None

    if start is not None and end is not None:
        return start, end
    if start is not None:
        return start, today
    if end is not None:
        return end - timedelta(days=days - 1), end

    return today - timedelta(days=days - 1), today


def parse_event_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


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


def read_events(start: date, end: date) -> list[dict]:
    if not EVENTS_FILE.exists():
        return []

    events = []
    with EVENTS_FILE.open("r", encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue

            ts = parse_event_ts(event.get("ts"))
            if ts is None:
                continue

            event_date = ts.astimezone().date()
            if start <= event_date <= end:
                events.append(event)

    return events


def empty_stats() -> dict:
    return {
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cost_estimate_usd": 0.0,
    }


def add_event(stats: dict, event: dict) -> None:
    stats["calls"] += 1
    stats["input_tokens"] += as_int(event.get("input_tokens"))
    stats["output_tokens"] += as_int(event.get("output_tokens"))
    stats["cache_read_tokens"] += as_int(event.get("cache_read_tokens"))
    stats["cost_estimate_usd"] += as_float(event.get("cost_estimate_usd"))


def cache_hit_percent(stats: dict) -> int:
    cache_read = stats["cache_read_tokens"]
    input_tokens = stats["input_tokens"]
    total = cache_read + input_tokens
    if total <= 0:
        return 0
    return round(cache_read / total * 100)


def fmt_int(value: int) -> str:
    return f"{value:,}"


def fmt_cost(value: float) -> str:
    return f"{value:.2f}"


def stats_row(label: str, stats: dict) -> str:
    return (
        f"| {label} | {stats['calls']:,} | {fmt_int(stats['input_tokens'])} | "
        f"{fmt_int(stats['output_tokens'])} | {cache_hit_percent(stats)}% | "
        f"{fmt_cost(stats['cost_estimate_usd'])} |"
    )


def summarize_by_model(events: list[dict]) -> tuple[dict[str, dict], dict]:
    by_model: dict[str, dict] = {}
    total = empty_stats()

    for event in events:
        model = event.get("model")
        if not isinstance(model, str) or not model:
            model = "unknown"
        if model not in by_model:
            by_model[model] = empty_stats()
        add_event(by_model[model], event)
        add_event(total, event)

    return by_model, total


def summarize_by_day(events: list[dict]) -> dict[str, dict]:
    by_day: dict[str, dict] = {}

    for event in events:
        ts = parse_event_ts(event.get("ts"))
        if ts is None:
            continue
        day = ts.astimezone().date().isoformat()
        if day not in by_day:
            by_day[day] = empty_stats()
        add_event(by_day[day], event)

    return by_day


def period_title(start: date, end: date) -> str:
    days = (end - start).days + 1
    if start == end:
        return f"## Claude Code stats: {start.isoformat()} (1 day)"
    return f"## Claude Code stats: {start.isoformat()}..{end.isoformat()} ({days} days)"


def print_summary(start: date, end: date, events: list[dict]) -> None:
    if not events:
        print("No events in period")
        return

    by_model, total = summarize_by_model(events)
    days = (end - start).days + 1
    prorated = MONTHLY_SUBSCRIPTION_USD / PRORATE_DAYS * days
    delta = prorated - total["cost_estimate_usd"]

    print(period_title(start, end))
    print()
    print("| Model | Calls | In tok | Out tok | Cache hit % | API cost ($) |")
    print("|---|---|---|---|---|---|")
    for model in sorted(by_model):
        print(stats_row(model, by_model[model]))
    print(stats_row("**Total**", total))
    print()
    print(f"**Period API cost**: ${fmt_cost(total['cost_estimate_usd'])}")
    print(f"**Max prorated** ($200/mo for this period): ${fmt_cost(prorated)}")
    if delta >= 0:
        print(f"**Savings**: ${fmt_cost(delta)} ✅")
    else:
        print(f"**Доплата**: ${fmt_cost(abs(delta))}")

    if days > 1:
        print()
        print("### Daily aggregates")
        print()
        print("| Date | Calls | In tok | Out tok | Cache hit % | API cost ($) |")
        print("|---|---|---|---|---|---|")
        for day, stats in sorted(summarize_by_day(events).items()):
            print(stats_row(day, stats))


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        start, end = resolve_period(args)
    except ValueError as exc:
        print(f"Invalid date: {exc}", file=sys.stderr)
        return 2

    if start > end:
        print("Invalid period: --from must be earlier than or equal to --to", file=sys.stderr)
        return 2

    print_summary(start, end, read_events(start, end))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
