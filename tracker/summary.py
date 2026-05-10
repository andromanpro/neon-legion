#!/usr/bin/env python
import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLAUDE_EVENTS_FILE = PROJECT_ROOT / "tracker" / "claude-events.jsonl"
CODEX_EVENTS_FILE = PROJECT_ROOT / "tracker" / "codex-events.jsonl"
EVENTS_FILE = CLAUDE_EVENTS_FILE
TASKS_FILE = PROJECT_ROOT / "tracker" / "tasks.json"


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


CLAUDE_MONTHLY_SUBSCRIPTION_USD = env_float("CLAUDE_MONTHLY_SUBSCRIPTION_USD", 200.0)
# ChatGPT Pro $200/month by default. Override with
# OPENAI_MONTHLY_SUBSCRIPTION_USD=20 for Plus or 100 for Pro $100.
OPENAI_MONTHLY_SUBSCRIPTION_USD = env_float("OPENAI_MONTHLY_SUBSCRIPTION_USD", 200.0)
MONTHLY_SUBSCRIPTION_USD = CLAUDE_MONTHLY_SUBSCRIPTION_USD
PRORATE_DAYS = 30.0
PROVIDER_KEYS = {
    "anthropic": "anthropic_claude",
    "openai": "openai_codex",
}
SENTIMENT_KEYS = {
    "profanity_count",
    "frustration_score",
    "appreciation_score",
    "mood_arc",
    "sentiment_intensity",
}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize Claude Code tracking events.")
    parser.add_argument("--days", type=int, default=1, help="Number of local calendar days to include.")
    parser.add_argument("--from", dest="from_date", help="Start date, inclusive, in YYYY-MM-DD format.")
    parser.add_argument("--to", dest="to_date", help="End date, inclusive, in YYYY-MM-DD format.")
    parser.add_argument(
        "--gap-minutes",
        type=int,
        default=2,
        help="Maximum gap between consecutive turns counted as active AI time.",
    )
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


def clamp_score(value: object) -> float:
    return max(0.0, min(1.0, as_float(value)))


def event_provider(event: dict) -> str:
    provider = event.get("provider")
    if provider in {"openai", "openai_codex", "codex"}:
        return "openai"
    return "anthropic"


def is_synthetic_event(event: dict) -> bool:
    return event.get("model") == "<synthetic>"


def provider_key(provider: str) -> str:
    return PROVIDER_KEYS.get(provider, provider)


def codex_origin(event: dict) -> str:
    origin = event.get("codex_origin")
    if isinstance(origin, str) and origin:
        return origin

    model = str(event.get("model") or "").lower()
    originator = str(event.get("originator") or "").lower()
    source = str(event.get("codex_source") or event.get("source") or "").lower()
    if model == "codex-auto-review" or "subagent" in source:
        return "auto_review"
    if originator == "codex_exec" or source == "exec":
        return "headless"
    if originator == "codex-tui" or source == "cli":
        return "tui"
    if originator == "codex desktop" or source in {"vscode", "desktop"}:
        return "desktop"
    if event_provider(event) == "openai":
        return "headless"
    return "unknown"


def provider_model_key(provider: str, model: str) -> str:
    lower = model.lower()
    if lower.startswith(("anthropic/", "openai/")):
        return model
    return f"{provider}/{model}"


def read_event_file(path: Path, start: date, end: date, provider: str) -> list[dict]:
    if not path.exists():
        return []

    events = []
    with path.open("r", encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if is_synthetic_event(event):
                continue

            ts = parse_event_ts(event.get("ts"))
            if ts is None:
                continue

            event_date = ts.astimezone().date()
            if start <= event_date <= end:
                event = dict(event)
                event.setdefault("provider", provider)
                events.append(event)

    return events


def read_claude_events(start: date, end: date) -> list[dict]:
    return read_event_file(CLAUDE_EVENTS_FILE, start, end, "anthropic")


def read_codex_events(start: date, end: date) -> list[dict]:
    return read_event_file(CODEX_EVENTS_FILE, start, end, "openai")


def event_sort_ts(event: dict) -> float:
    ts = parse_event_ts(event.get("ts"))
    return ts.timestamp() if ts is not None else 0.0


def read_events(start: date, end: date) -> list[dict]:
    events = read_claude_events(start, end) + read_codex_events(start, end)
    events = dedupe_events(events)
    events.sort(key=event_sort_ts)
    return events


def event_legacy_dedupe_key(event: dict) -> tuple:
    return (
        event_provider(event),
        event.get("session_id"),
        event.get("ts"),
        event.get("model"),
        event.get("input_tokens"),
        event.get("cached_input_tokens"),
        event.get("output_tokens"),
        event.get("reasoning_tokens"),
        event.get("total_tokens"),
        event.get("exit_code"),
    )


def dedupe_events(events: list[dict]) -> list[dict]:
    deduped = []
    seen_event_ids = set()
    seen_legacy = set()
    for event in events:
        event_id = event.get("event_id") or event.get("tracking_run_id")
        if isinstance(event_id, str) and event_id:
            if event_id in seen_event_ids:
                continue
            seen_event_ids.add(event_id)
        else:
            legacy_key = event_legacy_dedupe_key(event)
            if legacy_key in seen_legacy:
                continue
            seen_legacy.add(legacy_key)
        deduped.append(event)
    return deduped


def events_for_provider(events: list[dict], provider: str) -> list[dict]:
    return [event for event in events if event_provider(event) == provider]


def events_for_task_metrics(events: list[dict]) -> list[dict]:
    # tasks.json is keyed by Claude Code sessions. Codex calls are counted in
    # usage/cost, but not in task/productivity metrics to avoid double counting
    # work that was already estimated from the Claude orchestrator session.
    return events_for_provider(events, "anthropic")


def read_tasks() -> dict:
    if not TASKS_FILE.exists():
        return {}

    try:
        with TASKS_FILE.open("r", encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def empty_stats() -> dict:
    return {
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "cached_input_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "cost_estimate_usd": 0.0,
        "api_equivalent_cost_usd": 0.0,
        "unknown_pricing_events": 0,
    }


def add_event(stats: dict, event: dict) -> None:
    cached_input_tokens = as_int(event.get("cached_input_tokens"))
    cache_read_tokens = as_int(event.get("cache_read_tokens"))
    input_tokens = as_int(event.get("input_tokens"))
    output_tokens = as_int(event.get("output_tokens"))
    reasoning_tokens = as_int(event.get("reasoning_tokens"))
    total_tokens = as_int(event.get("total_tokens"))

    stats["calls"] += 1
    stats["input_tokens"] += input_tokens
    stats["output_tokens"] += output_tokens
    stats["cache_read_tokens"] += cache_read_tokens + cached_input_tokens
    stats["cache_creation_tokens"] += as_int(event.get("cache_creation_tokens"))
    stats["cached_input_tokens"] += cached_input_tokens
    stats["reasoning_tokens"] += reasoning_tokens
    stats["total_tokens"] += total_tokens or (input_tokens + cached_input_tokens + output_tokens + reasoning_tokens)
    api_equivalent_cost = as_float(event.get("cost_estimate_usd"))
    stats["cost_estimate_usd"] += api_equivalent_cost
    stats["api_equivalent_cost_usd"] += api_equivalent_cost
    if event.get("cost_estimate_usd") is None:
        stats["unknown_pricing_events"] += 1


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


def subscription_prorated_usd(events: list[dict], days: int) -> float:
    providers = {event_provider(event) for event in events}
    monthly = 0.0
    if "anthropic" in providers:
        monthly += CLAUDE_MONTHLY_SUBSCRIPTION_USD
    if "openai" in providers:
        monthly += OPENAI_MONTHLY_SUBSCRIPTION_USD
    if monthly <= 0:
        monthly = CLAUDE_MONTHLY_SUBSCRIPTION_USD
    return monthly / PRORATE_DAYS * days


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
        provider = event_provider(event)
        model = event.get("model")
        if not isinstance(model, str) or not model:
            model = "unknown"
        key = provider_model_key(provider, model)
        if key not in by_model:
            by_model[key] = empty_stats()
            by_model[key]["provider"] = provider
            by_model[key]["model"] = model
        add_event(by_model[key], event)
        add_event(total, event)

    return by_model, total


def summarize_codex_by_model(events: list[dict]) -> tuple[dict[str, dict], dict]:
    return summarize_by_model(events_for_provider(events, "openai"))


def summarize_by_provider(events: list[dict]) -> dict[str, dict]:
    by_provider: dict[str, dict] = {}

    for event in events:
        provider = event_provider(event)
        key = provider_key(provider)
        if key not in by_provider:
            by_provider[key] = empty_stats()
            by_provider[key]["provider"] = provider
            by_provider[key]["models"] = {}
            by_provider[key]["origins"] = {}
        add_event(by_provider[key], event)

        model = event.get("model")
        if not isinstance(model, str) or not model:
            model = "unknown"
        models = by_provider[key]["models"]
        models[model] = models.get(model, 0) + 1

        if provider == "openai":
            origin = codex_origin(event)
            origins = by_provider[key]["origins"]
            if origin not in origins:
                origins[origin] = empty_stats()
            add_event(origins[origin], event)

    return by_provider


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


def unknown_pricing_note(by_model: dict[str, dict]) -> str | None:
    unknown_by_model = {
        model: stats["unknown_pricing_events"]
        for model, stats in by_model.items()
        if stats["unknown_pricing_events"] > 0
    }
    if not unknown_by_model:
        return None

    total_unknown = sum(unknown_by_model.values())
    breakdown = ", ".join(f"{model}: {count}" for model, count in sorted(unknown_by_model.items()))
    return f"{total_unknown} events have unknown pricing and are counted as $0 in totals ({breakdown})."


def effective_task_hours(entry: dict) -> float | None:
    corrected = entry.get("human_corrected_hours")
    if corrected is not None:
        try:
            return float(corrected)
        except (TypeError, ValueError):
            return None

    baseline = entry.get("ai_baseline_hours")
    if baseline is None:
        return None
    try:
        return float(baseline)
    except (TypeError, ValueError):
        return None


def merged_interval_hours(intervals: list[tuple[float, float]]) -> float:
    if not intervals:
        return 0.0

    merged: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue

        merged[-1] = (merged[-1][0], max(merged[-1][1], end))

    return sum(max(end - start, 0.0) for start, end in merged) / 3600


def active_time_hours(events: list[dict], gap_minutes: int = 2) -> float:
    if gap_minutes <= 0:
        raise ValueError("gap_minutes must be greater than 0")

    session_timestamps: dict[str, list[datetime]] = {}
    for event in events:
        session_id = event.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            continue

        ts = parse_event_ts(event.get("ts"))
        if ts is None:
            continue

        session_timestamps.setdefault(session_id, []).append(ts)

    max_gap = timedelta(minutes=gap_minutes)
    total_seconds = 0.0
    for timestamps in session_timestamps.values():
        if len(timestamps) < 2:
            continue

        timestamps.sort()
        previous = timestamps[0]
        for current in timestamps[1:]:
            gap = current - previous
            if gap <= max_gap:
                total_seconds += gap.total_seconds()
            previous = current

    return total_seconds / 3600


def summarize_productivity(events: list[dict], gap_minutes: int = 2) -> dict | None:
    events = events_for_task_metrics(events)
    tasks = read_tasks()
    if not tasks:
        return None

    session_ranges: dict[str, tuple[float, float]] = {}
    for event in events:
        session_id = event.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            continue

        ts = parse_event_ts(event.get("ts"))
        if ts is None:
            continue
        event_time = ts.timestamp()

        current = session_ranges.get(session_id)
        if current is None:
            session_ranges[session_id] = (event_time, event_time)
        else:
            session_ranges[session_id] = (min(current[0], event_time), max(current[1], event_time))

    if not session_ranges:
        return None

    # "Covered" = sessions for which we have a real baseline estimate (not None).
    # If we counted sessions whose entry exists in tasks.json but has no baseline
    # (failed estimation), the hours_with_ai vs hours_without_ai comparison would
    # be apples-to-oranges (active hours over all sessions vs baselines over a subset).
    covered_session_ids = []
    hours_without_ai = 0.0
    for session_id in session_ranges:
        entry = tasks.get(session_id)
        if not isinstance(entry, dict):
            continue
        hours = effective_task_hours(entry)
        if hours is None:
            continue
        covered_session_ids.append(session_id)
        hours_without_ai += hours

    if not covered_session_ids:
        return None

    # Only count active/calendar hours over the *same* covered subset, so
    # the multiplier and saved hours are like-with-like.
    covered_id_set = set(covered_session_ids)
    covered_events = [
        ev for ev in events
        if isinstance(ev.get("session_id"), str) and ev.get("session_id") in covered_id_set
    ]
    covered_ranges = [session_ranges[sid] for sid in covered_session_ids]

    return {
        "active_hours_with_ai": active_time_hours(covered_events, gap_minutes),
        "calendar_hours_with_ai": merged_interval_hours(covered_ranges),
        "gap_minutes": gap_minutes,
        "hours_without_ai": hours_without_ai,
        "sessions_covered": len(covered_session_ids),
        "sessions_total": len(session_ranges),
    }


def has_sentiment_data(entry: dict) -> bool:
    return any(key in entry for key in SENTIMENT_KEYS)


def sentiment_label(value: float) -> str:
    if value < 0.2:
        return "low"
    if value < 0.4:
        return "medium-low"
    if value < 0.6:
        return "medium"
    if value < 0.8:
        return "medium-high"
    return "high"


def average(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def session_timestamp_ranges(events: list[dict]) -> dict[str, tuple[float, float]]:
    session_ranges: dict[str, tuple[float, float]] = {}

    for event in events:
        session_id = event.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            continue

        ts = parse_event_ts(event.get("ts"))
        if ts is None:
            continue
        event_time = ts.timestamp()

        current = session_ranges.get(session_id)
        if current is None:
            session_ranges[session_id] = (event_time, event_time)
        else:
            session_ranges[session_id] = (min(current[0], event_time), max(current[1], event_time))

    return session_ranges


def stress_trend(first_half: list[float], second_half: list[float]) -> str:
    if not first_half or not second_half:
        return "n/a (need both halves)"

    delta = average(second_half) - average(first_half)
    if delta <= -0.05:
        return "↘ improving"
    if delta >= 0.05:
        return "↗ worsening"
    return "→ stable"


def summarize_sentiment(events: list[dict], start: date, end: date) -> dict | None:
    events = events_for_task_metrics(events)
    tasks = read_tasks()
    if not tasks:
        return None

    session_ranges = session_timestamp_ranges(events)
    if not session_ranges:
        return None

    covered: list[tuple[str, dict, float]] = []
    for session_id, (first_ts, _last_ts) in session_ranges.items():
        entry = tasks.get(session_id)
        if isinstance(entry, dict) and has_sentiment_data(entry):
            covered.append((session_id, entry, first_ts))

    if not covered:
        return None

    profanity_total = 0
    frustration_values = []
    appreciation_values = []
    first_half_frustration = []
    second_half_frustration = []
    mood_counts: dict[str, int] = {}
    day_stats: dict[str, dict[str, int]] = {}
    days = (end - start).days + 1

    for _session_id, entry, first_ts in covered:
        profanity = max(as_int(entry.get("profanity_count")), 0)
        frustration = clamp_score(entry.get("frustration_score"))
        appreciation = clamp_score(entry.get("appreciation_score"))
        session_day = datetime.fromtimestamp(first_ts).astimezone().date()
        day_key = session_day.isoformat()

        profanity_total += profanity
        frustration_values.append(frustration)
        appreciation_values.append(appreciation)

        day_entry = day_stats.setdefault(day_key, {"profanity": 0, "sessions": 0})
        day_entry["profanity"] += profanity
        day_entry["sessions"] += 1

        if (session_day - start).days < days / 2:
            first_half_frustration.append(frustration)
        else:
            second_half_frustration.append(frustration)

        mood_arc = entry.get("mood_arc")
        if isinstance(mood_arc, str) and mood_arc.strip():
            mood_arc = mood_arc.strip()[:30]
            mood_counts[mood_arc] = mood_counts.get(mood_arc, 0) + 1

    top_day = max(day_stats.items(), key=lambda item: (item[1]["profanity"], item[1]["sessions"], item[0]))

    return {
        "profanity_total": profanity_total,
        "frustration_avg": average(frustration_values),
        "appreciation_avg": average(appreciation_values),
        "stress_trend": stress_trend(first_half_frustration, second_half_frustration),
        "top_day": top_day,
        "mood_counts": mood_counts,
        "sessions_covered": len(covered),
        "sessions_total": len(session_ranges),
    }


def format_mood_counts(mood_counts: dict[str, int]) -> str:
    if not mood_counts:
        return "n/a"

    top_arcs = sorted(mood_counts.items(), key=lambda item: (-item[1], item[0]))[:3]
    return ", ".join(f"'{arc.replace(chr(39), '')}' ({count})" for arc, count in top_arcs)


def print_productivity(productivity: dict | None) -> None:
    if productivity is None:
        return

    active_hours_with_ai = productivity["active_hours_with_ai"]
    calendar_hours_with_ai = productivity["calendar_hours_with_ai"]
    gap_minutes = productivity["gap_minutes"]
    hours_without_ai = productivity["hours_without_ai"]
    hours_saved = hours_without_ai - active_hours_with_ai
    sessions_covered = productivity["sessions_covered"]
    sessions_total = productivity["sessions_total"]
    sessions_pending = sessions_total - sessions_covered

    multiplier = f"×{hours_without_ai / active_hours_with_ai:.1f}" if active_hours_with_ai > 0 else "n/a"
    saved_suffix = " ✅" if hours_saved >= 0 else ""

    print()
    print("## Productivity (Phase 1.3)")
    print()
    print(f"**Hours with AI (active, ≤{gap_minutes}min gaps)**: {active_hours_with_ai:.1f}")
    print(f"**Hours with AI (calendar span)**: {calendar_hours_with_ai:.1f}")
    print(f"**Hours without AI** (estimated): {hours_without_ai:.1f}")
    print(f"**Hours saved**: {hours_saved:.1f}{saved_suffix}")
    print(f"**Productivity multiplier**: {multiplier} (active-based)")
    print()
    print(
        f"Sessions covered: {sessions_covered} of {sessions_total} "
        f"({sessions_pending} pending complexity estimation)"
    )


def print_sentiment(sentiment: dict | None) -> None:
    if sentiment is None:
        return

    profanity_total = sentiment["profanity_total"]
    frustration_avg = sentiment["frustration_avg"]
    appreciation_avg = sentiment["appreciation_avg"]
    sessions_covered = sentiment["sessions_covered"]
    sessions_total = sentiment["sessions_total"]
    sessions_pending = sessions_total - sessions_covered
    avg_profanity = profanity_total / sessions_covered if sessions_covered else 0.0
    top_day, top_day_stats = sentiment["top_day"]

    print()
    print("## Sentiment (Phase 1.4)")
    print()
    print(
        f"**Profanity total**: {profanity_total} across {sessions_covered} "
        f"sessions (avg {avg_profanity:.1f}/session)"
    )
    print(f"**Frustration avg**: {frustration_avg:.2f} ({sentiment_label(frustration_avg)})")
    print(f"**Appreciation avg**: {appreciation_avg:.2f} ({sentiment_label(appreciation_avg)})")
    print(f"**Stress trend**: {sentiment['stress_trend']}")
    print(
        f"**Top day**: {top_day} "
        f"({top_day_stats['profanity']} profanity hits in {top_day_stats['sessions']} sessions)"
    )
    print(f"**Mood arcs (top-3)**: {format_mood_counts(sentiment['mood_counts'])}")
    print()
    print(
        f"Sessions covered: {sessions_covered} of {sessions_total} "
        f"({sessions_pending} pending sentiment estimation)"
    )


def period_title(start: date, end: date) -> str:
    days = (end - start).days + 1
    if start == end:
        return f"## Claude + Codex stats: {start.isoformat()} (1 day)"
    return f"## Claude + Codex stats: {start.isoformat()}..{end.isoformat()} ({days} days)"


def print_summary(start: date, end: date, events: list[dict], gap_minutes: int = 2) -> None:
    if not events:
        print("No events in period")
        return

    by_model, total = summarize_by_model(events)
    days = (end - start).days + 1
    prorated = subscription_prorated_usd(events, days)
    delta = total["cost_estimate_usd"] - prorated

    print(period_title(start, end))
    print()
    print("| Model | Calls | In tok | Out tok | Cache hit % | API cost ($) |")
    print("|---|---|---|---|---|---|")
    for model in sorted(by_model):
        print(stats_row(model, by_model[model]))
    print(stats_row("**Total**", total))
    note = unknown_pricing_note(by_model)
    if note is not None:
        print()
        print(f"Unknown pricing note: {note}")
    print()
    print(f"**Period API cost**: ${fmt_cost(total['cost_estimate_usd'])}")
    print(f"**Subscriptions prorated**: ${fmt_cost(prorated)}")
    if delta >= 0:
        print(f"**Savings vs API rates**: ${fmt_cost(delta)} ✅")
        print(
            f"If paid by API rates would owe ${fmt_cost(total['cost_estimate_usd'])}; "
            f"subscription costs ${fmt_cost(prorated)} for this period; "
            f"saved ${fmt_cost(delta)}."
        )
    else:
        print(f"**Subscription not fully used**: ${fmt_cost(abs(delta))}")
        print(
            f"If paid by API rates would owe ${fmt_cost(total['cost_estimate_usd'])}; "
            f"subscription costs ${fmt_cost(prorated)} for this period; "
            f"unused subscription value ${fmt_cost(abs(delta))}."
        )

    print_productivity(summarize_productivity(events, gap_minutes))
    print_sentiment(summarize_sentiment(events, start, end))

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

    try:
        if args.gap_minutes <= 0:
            raise ValueError("--gap-minutes must be greater than 0")
    except ValueError as exc:
        print(f"Invalid argument: {exc}", file=sys.stderr)
        return 2

    if start > end:
        print("Invalid period: --from must be earlier than or equal to --to", file=sys.stderr)
        return 2

    print_summary(start, end, read_events(start, end), args.gap_minutes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
