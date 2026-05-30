#!/usr/bin/env python
import argparse
import json
import math
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from tools import config as cfg  # noqa: E402

CLAUDE_EVENTS_FILE = PROJECT_ROOT / "tracker" / "claude-events.jsonl"
CODEX_EVENTS_FILE = PROJECT_ROOT / "tracker" / "codex-events.jsonl"
OPENCLAW_EVENTS_FILE = PROJECT_ROOT / "tracker" / "openclaw-events.jsonl"
OPENCODE_EVENTS_FILE = PROJECT_ROOT / "tracker" / "opencode-events.jsonl"
EVENTS_FILE = CLAUDE_EVENTS_FILE
TASKS_FILE = PROJECT_ROOT / "tracker" / "tasks.json"
PRODUCTIVITY_UNIT = os.environ.get("PRODUCTIVITY_UNIT", "session")
if PRODUCTIVITY_UNIT not in {"session", "chunk"}:
    PRODUCTIVITY_UNIT = "session"


def env_float(name: str, default: float) -> float:
    return cfg.get_legacy_env(name, default, float)


CLAUDE_MONTHLY_SUBSCRIPTION_USD = env_float("CLAUDE_MONTHLY_SUBSCRIPTION_USD", 200.0)
# ChatGPT Pro $200/month by default. Override with
# OPENAI_MONTHLY_SUBSCRIPTION_USD=20 for Plus or 100 for Pro $100.
OPENAI_MONTHLY_SUBSCRIPTION_USD = env_float("OPENAI_MONTHLY_SUBSCRIPTION_USD", 200.0)
OPENROUTER_MONTHLY_SUBSCRIPTION_USD = env_float("OPENROUTER_MONTHLY_SUBSCRIPTION_USD", 0.0)
OPENCODE_MONTHLY_SUBSCRIPTION_USD = env_float("OPENCODE_MONTHLY_SUBSCRIPTION_USD", 0.0)
MONTHLY_SUBSCRIPTION_USD = CLAUDE_MONTHLY_SUBSCRIPTION_USD
PRORATE_DAYS = 30.0
PROVIDER_KEYS = {
    "anthropic": "anthropic_claude",
    "openai": "openai_codex",
    "openrouter": "openrouter_openclaw",
    "opencode": "opencode_openrouter",
}
SENTIMENT_KEYS = {
    "profanity_count",
    "frustration_score",
    "appreciation_score",
    "mood_arc",
    "sentiment_intensity",
}
# Calibration: smallest legit session = 655 events; garbage cluster <=3 events.
TRIVIAL_EVENT_MAX = 5
# Calibration: observed garbage active time <=65 seconds.
TRIVIAL_ACTIVE_MAX_HOURS = 0.05
# Calibration: keep <=1h stub estimates as estimator noise, not numerator damage.
TRIVIAL_MIN_BASELINE_HOURS = 1.0
# Defense-in-depth headroom: 1.0 sits between legit max 0.15 h/event
# and garbage min 9 h/event (deliberately loose; trivial guard is primary).
PER_EVENT_CEILING_HOURS = 1.0
# Calibration: band floor keeps real small dense sessions out of the ceiling.
BAND_MIN_HOURS = 6.0

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


def chunk_date(ts: datetime) -> str:
    return ts.date().isoformat()


def productivity_unit() -> str:
    return PRODUCTIVITY_UNIT if PRODUCTIVITY_UNIT in {"session", "chunk"} else "session"


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
    provider = str(event.get("provider") or "").lower()
    if provider in {"openai", "openai_codex", "codex"}:
        return "openai"
    if provider in {"openrouter", "openrouter_openclaw", "openclaw"}:
        return "openrouter"
    if provider in {"opencode", "opencode_openrouter", "openrouter_opencode"}:
        return "opencode"
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
    prefix_by_provider = {
        "anthropic": "anthropic",
        "openai": "openai",
        "openrouter": "openrouter",
        "opencode": "opencode",
    }
    prefix = prefix_by_provider.get(provider, provider)
    if lower.startswith(f"{prefix}/"):
        return model
    return f"{prefix}/{model}"


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


def read_openclaw_events(start: date, end: date) -> list[dict]:
    return read_event_file(OPENCLAW_EVENTS_FILE, start, end, "openrouter")


def read_opencode_events(start: date, end: date) -> list[dict]:
    return read_event_file(OPENCODE_EVENTS_FILE, start, end, "opencode")


def event_sort_ts(event: dict) -> float:
    ts = parse_event_ts(event.get("ts"))
    return ts.timestamp() if ts is not None else 0.0


def read_events(start: date, end: date) -> list[dict]:
    events = (
        read_claude_events(start, end)
        + read_codex_events(start, end)
        + read_openclaw_events(start, end)
        + read_opencode_events(start, end)
    )
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
    # tasks.json is keyed by Claude Code sessions. Codex/OpenClaw/OpenCode calls are
    # counted in usage/cost, but not in task/productivity metrics to avoid
    # double counting work that was already estimated from the Claude
    # orchestrator session.
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
    if "openrouter" in providers:
        monthly += OPENROUTER_MONTHLY_SUBSCRIPTION_USD
    if "opencode" in providers:
        monthly += OPENCODE_MONTHLY_SUBSCRIPTION_USD
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


def summarize_openclaw_by_model(events: list[dict]) -> tuple[dict[str, dict], dict]:
    return summarize_by_model(events_for_provider(events, "openrouter"))


def summarize_opencode_by_model(events: list[dict]) -> tuple[dict[str, dict], dict]:
    return summarize_by_model(events_for_provider(events, "opencode"))


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
        elif provider == "openrouter":
            origin = str(event.get("openclaw_source") or event.get("source") or "openclaw")
            origins = by_provider[key]["origins"]
            if origin not in origins:
                origins[origin] = empty_stats()
            add_event(origins[origin], event)
        elif provider == "opencode":
            origin = str(event.get("opencode_agent") or event.get("opencode_provider_id") or "opencode")
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


def effective_session_hours(
    baseline_hours: float,
    session_active_hours: float,
    event_count: int,
) -> tuple[float, str]:
    """Returns (effective_hours, kind) where kind is one of:
    "normal", "floor", "ceiling_trivial", "ceiling_band".
    Floor (#108) and ceiling (#106-A) are mutually exclusive per session.
    """
    baseline = float(baseline_hours)
    active = float(session_active_hours)
    events = int(event_count or 0)

    eff = max(baseline, active)
    kind = "floor" if baseline < active else "normal"

    if (
        events <= TRIVIAL_EVENT_MAX
        and active <= TRIVIAL_ACTIVE_MAX_HOURS
        and baseline > TRIVIAL_MIN_BASELINE_HOURS
    ):
        return active, "ceiling_trivial"

    ceiling = max(BAND_MIN_HOURS, PER_EVENT_CEILING_HOURS * events)
    if eff > ceiling:
        return ceiling, "ceiling_band"

    return eff, kind


def percentile(values: list[float], percentile_rank: float) -> float:
    if not values:
        return 0.0

    ordered = sorted(values)
    if percentile_rank <= 0:
        return ordered[0]
    if percentile_rank >= 100:
        return ordered[-1]

    index = math.ceil(len(ordered) * percentile_rank / 100) - 1
    index = max(0, min(index, len(ordered) - 1))
    return ordered[index]


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


def _active_time_hours_for_timestamps(timestamps: list[datetime], gap_minutes: int) -> float:
    if len(timestamps) < 2:
        return 0.0

    max_gap = timedelta(minutes=gap_minutes)
    timestamps = sorted(timestamps)
    total_seconds = 0.0
    previous = timestamps[0]
    for current in timestamps[1:]:
        gap = current - previous
        if gap <= max_gap:
            total_seconds += gap.total_seconds()
        previous = current

    return total_seconds / 3600


def active_time_hours_merged(events: list[dict], gap_minutes: int = 2) -> float:
    if gap_minutes <= 0:
        raise ValueError("gap_minutes must be greater than 0")

    timestamps: list[datetime] = []
    for event in events:
        session_id = event.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            continue

        ts = parse_event_ts(event.get("ts"))
        if ts is None:
            continue

        timestamps.append(ts)

    return _active_time_hours_for_timestamps(timestamps, gap_minutes)


# ── Human-attention denominator ────────────────────────────────────────────
# The productivity multiplier's denominator used to be `active_time_hours_merged`
# — wall-clock during which ANY AI session emitted events (≤2-min bridged). That
# measures *AI busy time*, not *human attention*. When the user fires 3-5 agents
# in parallel and works on something else by hand, autonomous agent runtime
# inflates that denominator → the multiplier reads low on parallel weeks.
#
# This denominator instead counts the *human's* engaged time: timestamps of
# genuine human prompts (NOT tool-result "user" lines, NOT sub-agent sidechain
# turns), pooled across covered sessions, merged with a 5-min gap. Autonomous
# stretches between prompts cost ~0 → parallelism is credited honestly.
HUMAN_ATTENTION_GAP_MINUTES = 5

# Floor against divide-by-near-zero: a single 10-second prompt into a session
# that then ran autonomously would otherwise give human_attention ≈ 0.003h and
# an absurd multiplier. Each covered unit implies at least this much human cost
# (you fire it and glance at the result). Bounds the multiplier without erasing
# the parallelism credit. Applied in backend/server.py against sessions_covered.
HUMAN_ATTENTION_FLOOR_MIN_PER_SESSION = 5


def is_human_prompt(event: dict) -> bool:
    """True only for a genuine human-typed prompt in a Claude transcript line.

    Excludes:
      - sub-agent turns (`isSidechain: true`) — Task-tool children, not the user;
      - tool-result "user" lines — Claude logs tool output as role=user; counting
        them would re-measure AI-busy time (a single session has ~1000 of them vs
        ~80 real prompts).
    A genuine prompt has string content or a non-empty text block.
    """
    if not isinstance(event, dict):
        return False
    message = event.get("message")
    role = message.get("role") if isinstance(message, dict) else None
    # Accept either the top-level type or the message.role (mirrors transcript_role).
    if event.get("type") != "user" and role != "user":
        return False
    if event.get("isSidechain") is True:
        return False
    content = message.get("content") if isinstance(message, dict) else event.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        has_text = any(
            isinstance(b, dict) and b.get("type") == "text" and str(b.get("text", "")).strip()
            for b in content
        )
        has_tool_result = any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
        return has_text and not has_tool_result
    return False


def read_human_message_timestamps(transcript_path) -> list[datetime]:
    """Sorted timestamps of genuine human prompts in a Claude transcript .jsonl.

    Returns [] if the file is missing/unreadable (caller falls back to AI events).
    """
    timestamps: list[datetime] = []
    try:
        with Path(transcript_path).open("r", encoding="utf-8") as source:
            for line in source:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not is_human_prompt(event):
                    continue
                ts = parse_event_ts(event.get("timestamp") or event.get("ts"))
                if ts is not None:
                    timestamps.append(ts)
    except (OSError, UnicodeDecodeError, ValueError):
        return []
    timestamps.sort()
    return timestamps


def resolve_transcript_path(value: object):
    """Return an existing Path for a stored transcript_path, else None."""
    if not isinstance(value, str) or not value:
        return None
    candidate = Path(value)
    try:
        return candidate if candidate.exists() else None
    except OSError:
        return None


def _human_attention_hours_for_units(
    units,
    tasks: dict,
    ai_session_timestamps: dict,
    gap_minutes: int = HUMAN_ATTENTION_GAP_MINUTES,
) -> tuple[float, int]:
    """Core: pool human-prompt timestamps across coverage units → merged hours.

    Each unit is (session_id, date_key | None). When date_key is given (chunk
    mode), the session's human prompts are restricted to that calendar day so the
    denominator matches the per-day baselines in the numerator. None = whole
    session (session mode / fallback un-chunked sessions). Missing transcript →
    fall back to the session's AI-event timestamps (restricted to the day too).
    Returns (hours, fallback_unit_count). Transcripts are read once per session.
    """
    transcript_cache: dict[str, list] = {}

    def human_ts_for(sid):
        if sid not in transcript_cache:
            entry = tasks.get(sid)
            tpath = resolve_transcript_path(entry.get("transcript_path")) if isinstance(entry, dict) else None
            transcript_cache[sid] = read_human_message_timestamps(tpath) if tpath is not None else []
        return transcript_cache[sid]

    pooled: list[datetime] = []
    fallbacks = 0
    for sid, date_key in units:
        human_ts = human_ts_for(sid)
        if date_key is not None:
            human_ts = [ts for ts in human_ts if chunk_date(ts) == date_key]
        if human_ts:
            pooled.extend(human_ts)
        else:
            fallbacks += 1
            ai_ts = ai_session_timestamps.get(sid, [])
            if date_key is not None:
                ai_ts = [ts for ts in ai_ts if chunk_date(ts) == date_key]
            pooled.extend(ai_ts)
    return _active_time_hours_for_timestamps(pooled, gap_minutes), fallbacks


def human_attention_hours(
    covered_session_ids,
    tasks: dict,
    ai_session_timestamps: dict,
    gap_minutes: int = HUMAN_ATTENTION_GAP_MINUTES,
) -> tuple[float, int]:
    """Whole-session human-attention hours (session mode). See _..._for_units."""
    units = [(sid, None) for sid in covered_session_ids]
    return _human_attention_hours_for_units(units, tasks, ai_session_timestamps, gap_minutes)


def summarize_productivity(events: list[dict], gap_minutes: int = 2) -> dict | None:
    unit = productivity_unit()
    events = events_for_task_metrics(events)
    tasks = read_tasks()
    if not tasks:
        return None

    session_ranges: dict[str, tuple[float, float]] = {}
    session_timestamps: dict[str, list[datetime]] = {}
    for event in events:
        session_id = event.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            continue

        ts = parse_event_ts(event.get("ts"))
        if ts is None:
            continue
        event_time = ts.timestamp()
        session_timestamps.setdefault(session_id, []).append(ts)

        current = session_ranges.get(session_id)
        if current is None:
            session_ranges[session_id] = (event_time, event_time)
        else:
            session_ranges[session_id] = (min(current[0], event_time), max(current[1], event_time))

    if not session_ranges:
        return None

    if unit == "session":
        # "Covered" = sessions for which we have a real baseline estimate (not None).
        # If we counted sessions whose entry exists in tasks.json but has no baseline
        # (failed estimation), the hours_with_ai vs hours_without_ai comparison would
        # be apples-to-oranges (active hours over all sessions vs baselines over a subset).
        covered_session_ids = []
        hours_without_ai = 0.0
        baseline_floor_clamped = 0
        hours_floor_added = 0.0
        baseline_ceiling_clamped = 0
        hours_ceiling_removed = 0.0
        baseline_per_event_values: list[float] = []
        for session_id in session_ranges:
            entry = tasks.get(session_id)
            if not isinstance(entry, dict):
                continue
            hours = effective_task_hours(entry)
            if hours is None:
                continue
            session_active_hours = _active_time_hours_for_timestamps(
                session_timestamps.get(session_id, []),
                gap_minutes,
            )
            event_count = len(session_timestamps.get(session_id, []))
            effective_hours, kind = effective_session_hours(hours, session_active_hours, event_count)
            if kind == "floor":
                baseline_floor_clamped += 1
                hours_floor_added += effective_hours - hours
            elif kind.startswith("ceiling"):
                baseline_ceiling_clamped += 1
                hours_ceiling_removed += hours - effective_hours
            if event_count > 0:
                baseline_per_event_values.append(hours / event_count)
            covered_session_ids.append(session_id)
            hours_without_ai += effective_hours

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

        human_hours, human_fallbacks = human_attention_hours(
            covered_session_ids, tasks, session_timestamps
        )

        return {
            "active_hours_with_ai": active_time_hours_merged(covered_events, gap_minutes),
            "active_hours_per_session_sum": active_time_hours(covered_events, gap_minutes),
            "human_attention_hours_with_ai": human_hours,
            "human_attention_fallbacks": human_fallbacks,
            "calendar_hours_with_ai": merged_interval_hours(covered_ranges),
            "gap_minutes": gap_minutes,
            "hours_without_ai": hours_without_ai,
            "baseline_floor_clamped": baseline_floor_clamped,
            "hours_floor_added": hours_floor_added,
            "baseline_ceiling_clamped": baseline_ceiling_clamped,
            "hours_ceiling_removed": hours_ceiling_removed,
            "baseline_per_event_p95": percentile(baseline_per_event_values, 95),
            "sessions_covered": len(covered_session_ids),
            "sessions_total": len(session_ranges),
            "unit": unit,
        }

    chunk_timestamps: dict[tuple[str, str], list[datetime]] = {}
    chunk_ranges: dict[tuple[str, str], tuple[float, float]] = {}
    for event in events:
        session_id = event.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            continue

        ts = parse_event_ts(event.get("ts"))
        if ts is None:
            continue

        date_key = chunk_date(ts)
        chunk_key = (session_id, date_key)
        event_time = ts.timestamp()
        chunk_timestamps.setdefault(chunk_key, []).append(ts)
        current = chunk_ranges.get(chunk_key)
        if current is None:
            chunk_ranges[chunk_key] = (event_time, event_time)
        else:
            chunk_ranges[chunk_key] = (min(current[0], event_time), max(current[1], event_time))

    covered_fallback_session_ids = []
    covered_chunk_keys: list[tuple[str, str]] = []
    covered_ranges: list[tuple[float, float]] = []
    hours_without_ai = 0.0
    active_hours_per_unit_sum = 0.0
    baseline_floor_clamped = 0
    hours_floor_added = 0.0
    baseline_ceiling_clamped = 0
    hours_ceiling_removed = 0.0
    baseline_per_event_values: list[float] = []
    total_units = 0

    def record_unit(
        baseline_hours: float,
        timestamps: list[datetime],
        event_range: tuple[float, float],
        covered_chunk_key: tuple[str, str] | None = None,
        covered_session_id: str | None = None,
    ) -> None:
        nonlocal hours_without_ai
        nonlocal active_hours_per_unit_sum
        nonlocal baseline_floor_clamped
        nonlocal hours_floor_added
        nonlocal baseline_ceiling_clamped
        nonlocal hours_ceiling_removed

        unit_active_hours = _active_time_hours_for_timestamps(timestamps, gap_minutes)
        event_count = len(timestamps)
        effective_hours, kind = effective_session_hours(
            baseline_hours,
            unit_active_hours,
            event_count,
        )
        if kind == "floor":
            baseline_floor_clamped += 1
            hours_floor_added += effective_hours - baseline_hours
        elif kind.startswith("ceiling"):
            baseline_ceiling_clamped += 1
            hours_ceiling_removed += baseline_hours - effective_hours
        if event_count > 0:
            baseline_per_event_values.append(baseline_hours / event_count)
        if covered_chunk_key is not None:
            covered_chunk_keys.append(covered_chunk_key)
        if covered_session_id is not None:
            covered_fallback_session_ids.append(covered_session_id)
        covered_ranges.append(event_range)
        active_hours_per_unit_sum += unit_active_hours
        hours_without_ai += effective_hours

    for session_id in session_ranges:
        dates = sorted(
            date_key
            for sid, date_key in chunk_timestamps
            if sid == session_id
        )
        if not dates:
            continue

        chunk_task_keys = [f"{session_id}:{date_key}" for date_key in dates]
        present_chunk_keys = [key for key in chunk_task_keys if key in tasks]
        session_entry = tasks.get(session_id)
        session_hours = (
            effective_task_hours(session_entry)
            if isinstance(session_entry, dict)
            else None
        )

        if not present_chunk_keys:
            total_units += 1
            if session_hours is None:
                continue
            record_unit(
                session_hours,
                session_timestamps.get(session_id, []),
                session_ranges[session_id],
                covered_session_id=session_id,
            )
            continue

        if len(present_chunk_keys) < len(chunk_task_keys) and session_hours is not None:
            total_units += 1
            record_unit(
                session_hours,
                session_timestamps.get(session_id, []),
                session_ranges[session_id],
                covered_session_id=session_id,
            )
            continue

        total_units += len(dates)
        for date_key in dates:
            task_key = f"{session_id}:{date_key}"
            entry = tasks.get(task_key)
            if not isinstance(entry, dict):
                continue
            hours = effective_task_hours(entry)
            if hours is None:
                continue
            key = (session_id, date_key)
            record_unit(
                hours,
                chunk_timestamps.get(key, []),
                chunk_ranges[key],
                covered_chunk_key=key,
            )

    if not covered_fallback_session_ids and not covered_chunk_keys:
        return None

    covered_fallback_id_set = set(covered_fallback_session_ids)
    covered_chunk_key_set = set(covered_chunk_keys)
    covered_events = []
    for ev in events:
        session_id = ev.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            continue
        if session_id in covered_fallback_id_set:
            covered_events.append(ev)
            continue
        ts = parse_event_ts(ev.get("ts"))
        if ts is None:
            continue
        if (session_id, chunk_date(ts)) in covered_chunk_key_set:
            covered_events.append(ev)

    # Human attention, matched to the numerator's coverage units:
    #  - covered day-chunks → (session, date) so prompts are restricted to that
    #    day (numerator has per-day baselines → denominator must too, else a
    #    multi-day session pools all its prompts against one day's baseline);
    #  - un-chunked fallback sessions → (session, None) = whole session.
    human_units = [(key[0], key[1]) for key in covered_chunk_key_set]
    human_units += [(sid, None) for sid in covered_fallback_id_set]
    human_hours, human_fallbacks = _human_attention_hours_for_units(
        human_units, tasks, session_timestamps
    )

    return {
        "active_hours_with_ai": active_time_hours_merged(covered_events, gap_minutes),
        "active_hours_per_session_sum": active_hours_per_unit_sum,
        "human_attention_hours_with_ai": human_hours,
        "human_attention_fallbacks": human_fallbacks,
        "calendar_hours_with_ai": merged_interval_hours(covered_ranges),
        "gap_minutes": gap_minutes,
        "hours_without_ai": hours_without_ai,
        "baseline_floor_clamped": baseline_floor_clamped,
        "hours_floor_added": hours_floor_added,
        "baseline_ceiling_clamped": baseline_ceiling_clamped,
        "hours_ceiling_removed": hours_ceiling_removed,
        "baseline_per_event_p95": percentile(baseline_per_event_values, 95),
        "sessions_covered": len(covered_fallback_session_ids) + len(covered_chunk_keys),
        "sessions_total": total_units,
        "unit": unit,
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
    appreciation_total = 0
    frustration_values = []
    appreciation_values = []
    first_half_frustration = []
    second_half_frustration = []
    mood_counts: dict[str, int] = {}
    day_stats: dict[str, dict[str, int]] = {}
    days = (end - start).days + 1

    for _session_id, entry, first_ts in covered:
        profanity = max(as_int(entry.get("profanity_count")), 0)
        appreciation_count = max(as_int(entry.get("appreciation_count")), 0)
        frustration = clamp_score(entry.get("frustration_score"))
        appreciation = clamp_score(entry.get("appreciation_score"))
        session_day = datetime.fromtimestamp(first_ts).astimezone().date()
        day_key = session_day.isoformat()

        profanity_total += profanity
        appreciation_total += appreciation_count
        frustration_values.append(frustration)
        appreciation_values.append(appreciation)

        day_entry = day_stats.setdefault(
            day_key, {"profanity": 0, "appreciation": 0, "sessions": 0}
        )
        day_entry["profanity"] += profanity
        day_entry["appreciation"] += appreciation_count
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
    top_appreciation_day = max(
        day_stats.items(),
        key=lambda item: (item[1].get("appreciation", 0), item[1]["sessions"], item[0]),
    )

    return {
        "profanity_total": profanity_total,
        "appreciation_total": appreciation_total,
        "frustration_avg": average(frustration_values),
        "appreciation_avg": average(appreciation_values),
        "stress_trend": stress_trend(first_half_frustration, second_half_frustration),
        "top_day": top_day,
        "top_appreciation_day": top_appreciation_day,
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
        return f"## Claude + Codex + OpenClaw + OpenCode stats: {start.isoformat()} (1 day)"
    return f"## Claude + Codex + OpenClaw + OpenCode stats: {start.isoformat()}..{end.isoformat()} ({days} days)"


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
