#!/usr/bin/env python
import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import http.server
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from tools import config as cfg  # noqa: E402
from backend import readmodel  # noqa: E402

DASHBOARD_DIR = PROJECT_ROOT / "dashboard"
sys.path.insert(0, str(PROJECT_ROOT / "tracker"))
import summary  # noqa: E402


# Post-May 2026 community-observed lower bounds (Anthropic не публикует точные цифры).
# Sources: 9to5google.com, techcrunch.com, portkey.ai
ESTIMATED_LIMIT_MAX5X = 440_000
ESTIMATED_LIMIT_MAX20X = 1_700_000
DEFAULT_DAYS = 1
DEFAULT_SESSION_LIMIT = 20

# Snapshot writer defaults — safe fallback path inside repo if user hasn't mounted H:/.
SNAPSHOT_DEFAULT_INTERVAL = cfg.get("backend.snapshot_interval_seconds", 900, int)  # 15 minutes
SNAPSHOT_DEFAULT_DAYS = cfg.get("backend.snapshot_period_days", 62, int)
SNAPSHOT_DEFAULT_SESSIONS = 8
SNAPSHOT_DEFAULT_PATH = cfg.get("paths.snapshot_output", None, str)
SALT_FILE_DEFAULT = cfg.get("paths.salt_file", str(Path.home() / ".multi-agent-snapshot-salt"), str)
CUSTOMERS_BLOCKLIST_DEFAULT = cfg.get("paths.customers_blocklist", None, str)
TRACKER_DIR = PROJECT_ROOT / "tracker"
_READMODEL = None
_READMODEL_META = None
_USE_SLOW_READMODEL = False
_USE_LOOP_SUMMARY = False


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


class BadRequest(ValueError):
    pass


def positive_int(value):
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer")
    if v <= 0:
        raise argparse.ArgumentTypeError(f"{value!r} must be > 0")
    return v


def parse_args():
    parser = argparse.ArgumentParser(description="Multi-agent tracker backend API")
    parser.add_argument("--port", type=int, default=cfg.get("backend.port", 8089, int))
    parser.add_argument("--host", default=cfg.get("backend.host", "127.0.0.1", str))
    parser.add_argument(
        "--snapshot-path",
        default=SNAPSHOT_DEFAULT_PATH,
        help="Write WP-shaped JSON snapshot here every --snapshot-interval seconds. "
             "Disabled if empty.",
    )
    parser.add_argument(
        "--snapshot-interval",
        type=positive_int,
        default=SNAPSHOT_DEFAULT_INTERVAL,
        help=f"Seconds between snapshot writes (default {SNAPSHOT_DEFAULT_INTERVAL}).",
    )
    parser.add_argument(
        "--snapshot-days",
        type=positive_int,
        default=SNAPSHOT_DEFAULT_DAYS,
        help=f"Period covered by snapshot (default {SNAPSHOT_DEFAULT_DAYS} days).",
    )
    parser.add_argument(
        "--snapshot-once",
        action="store_true",
        help="Write snapshot once and exit (for testing / cron).",
    )
    parser.add_argument(
        "--public",
        action="store_true",
        help="Enable privacy hardening: hash session_id, scrub paths/emails/customer names. "
             "Use for snapshots intended for public publishing.",
    )
    parser.add_argument(
        "--salt-file",
        default=SALT_FILE_DEFAULT,
        help="Path to file with salt for session_id hashing. Auto-generated (32 random bytes) "
             "if missing. Required when --public is set.",
    )
    parser.add_argument(
        "--customers-blocklist",
        default=CUSTOMERS_BLOCKLIST_DEFAULT,
        help="Optional path to file listing customer names to scrub from desc/top_session "
             "(one name per line, # comments allowed). Only used with --public.",
    )
    parser.add_argument(
        "--no-readmodel",
        action="store_true",
        help="Disable the in-memory SQLite read-model and read canonical JSONL directly.",
    )
    parser.add_argument(
        "--use-slow-readmodel",
        action="store_true",
        help="Use the raw_json-decoding SQLite read-model path instead of the column fast path.",
    )
    parser.add_argument(
        "--use-loop-summary",
        action="store_true",
        help="Use read_events + Python summary loop instead of SQL aggregate summary.",
    )
    return parser.parse_args()


def now_local():
    return datetime.now().astimezone()


def parse_positive_int(query, name, default):
    values = query.get(name)
    if not values:
        return default

    value = values[0]
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise BadRequest(f"{name} must be an integer") from exc

    if parsed <= 0:
        raise BadRequest(f"{name} must be greater than 0")
    return parsed


def parse_days(query):
    return parse_positive_int(query, "days", DEFAULT_DAYS)


def period_for_days(days):
    end = now_local().date()
    start = end - timedelta(days=days - 1)
    return start, end


def period_payload(start, end):
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": (end - start).days + 1,
    }


def rounded(value, digits=4):
    if value is None:
        return 0.0
    return round(float(value), digits)


def static_content_type(file_path):
    suffix = file_path.suffix.lower()
    if suffix == ".html":
        return "text/html; charset=utf-8"
    if suffix == ".css":
        return "text/css"
    if suffix == ".js":
        return "application/javascript"
    return "application/octet-stream"


def path_is_relative_to(path, directory):
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def dashboard_static_path(path):
    if path in {"/", "/dashboard", "/dashboard/"}:
        return DASHBOARD_DIR / "index.html"
    if path.startswith("/dashboard/"):
        return DASHBOARD_DIR / path[len("/dashboard/"):]
    return None


def stats_payload(stats):
    cost_usd = rounded(stats.get("cost_estimate_usd", 0.0))
    return {
        "calls": summary.as_int(stats.get("calls")),
        "input_tokens": summary.as_int(stats.get("input_tokens")),
        "output_tokens": summary.as_int(stats.get("output_tokens")),
        "cache_read_tokens": summary.as_int(stats.get("cache_read_tokens")),
        "cost_usd": cost_usd,
        "api_equivalent_cost_usd": cost_usd,
    }


def model_payload(model, stats):
    payload = stats_payload(stats)
    payload["model"] = model
    payload["provider"] = stats.get("provider") or "anthropic"
    payload["cache_hit_percent"] = summary.cache_hit_percent(stats)
    payload["unknown_pricing_events"] = summary.as_int(stats.get("unknown_pricing_events"))
    return payload


def provider_payload(stats):
    payload = stats_payload(stats)
    models = stats.get("models") if isinstance(stats, dict) else None
    if not isinstance(models, dict):
        models = {}
    origins = stats.get("origins") if isinstance(stats, dict) else None
    if not isinstance(origins, dict):
        origins = {}
    payload["models"] = [
        {"model": model, "calls": summary.as_int(calls)}
        for model, calls in sorted(models.items(), key=lambda item: (-summary.as_int(item[1]), item[0]))
    ]
    payload["origins"] = [
        {
            "origin": origin,
            **stats_payload(origin_stats),
        }
        for origin, origin_stats in sorted(
            origins.items(),
            key=lambda item: (
                -summary.as_float(item[1].get("cost_estimate_usd")),
                -summary.as_int(item[1].get("calls")),
                item[0],
            ),
        )
    ]
    return payload


def providers_payload(events):
    by_provider = summary.summarize_by_provider(events)
    return {
        "anthropic_claude": provider_payload(by_provider.get("anthropic_claude", summary.empty_stats())),
        "openai_codex": provider_payload(by_provider.get("openai_codex", summary.empty_stats())),
        "openrouter_openclaw": provider_payload(by_provider.get("openrouter_openclaw", summary.empty_stats())),
        "opencode_openrouter": provider_payload(by_provider.get("opencode_openrouter", summary.empty_stats())),
    }


def subscription_from_by_model(by_model, days):
    providers = {
        stats.get("provider")
        for stats in by_model.values()
        if isinstance(stats, dict)
    }
    monthly = 0.0
    if "anthropic" in providers:
        monthly += summary.CLAUDE_MONTHLY_SUBSCRIPTION_USD
    if "openai" in providers:
        monthly += summary.OPENAI_MONTHLY_SUBSCRIPTION_USD
    if "openrouter" in providers:
        monthly += summary.OPENROUTER_MONTHLY_SUBSCRIPTION_USD
    if "opencode" in providers:
        monthly += summary.OPENCODE_MONTHLY_SUBSCRIPTION_USD
    return monthly / summary.PRORATE_DAYS * days


def event_ts_local(event):
    ts = summary.parse_event_ts(event.get("ts"))
    if ts is None:
        return None
    return ts.astimezone()


def read_all_events():
    return _read_events_dispatch(date.min, date.max)


def read_recent_events(since, now):
    events = _read_events_dispatch(since.date(), now.date())
    recent = []
    for event in events:
        ts = event_ts_local(event)
        if ts is not None and since <= ts <= now:
            recent.append(event)
    return recent


def _read_events_dispatch(start, end, providers=None):
    if _READMODEL is not None:
        if _USE_SLOW_READMODEL:
            return readmodel.read_events(_READMODEL, start, end, providers=providers)
        return readmodel.read_events_fast(_READMODEL, start, end, providers=providers)
    return summary.read_events(start, end)


def total_tokens(stats):
    # Only input+output count toward rate-limit window.
    # cache_read_tokens — отдельный pool (cache hits, дешёвые, не считаются в Max 5h budget).
    # Если включить cache_read — получаем 100M+ tokens за 5h окно, что absurd для Max 5x (88k limit).
    return (
        summary.as_int(stats.get("input_tokens"))
        + summary.as_int(stats.get("output_tokens"))
    )


def cache_tokens(stats):
    return (
        summary.as_int(stats.get("cache_read_tokens"))
        + summary.as_int(stats.get("cache_creation_tokens"))
    )


def percent(used, limit):
    if limit <= 0:
        return 0.0
    return rounded(used / limit * 100)


def session_ranges(events):
    return summary.session_timestamp_ranges(events)


def calendar_span_hours(events):
    ranges = session_ranges(events)
    return summary.merged_interval_hours(list(ranges.values()))


def productivity_payload(events, gap_minutes):
    unit = summary.productivity_unit()
    productivity = summary.summarize_productivity(events, gap_minutes)
    if productivity is None:
        active_hours = summary.active_time_hours_merged(events, gap_minutes)
        active_hours_per_session_sum = summary.active_time_hours(events, gap_minutes)
        calendar_hours = calendar_span_hours(events)
        hours_without_ai = 0.0
        baseline_floor_clamped = 0
        hours_floor_added = 0.0
        baseline_ceiling_clamped = 0
        hours_ceiling_removed = 0.0
        baseline_per_event_p95 = 0.0
        sessions_covered = 0
        # Codex-audit HIGH: these must exist in BOTH branches or the multiplier
        # block below raises UnboundLocalError on empty/uncovered periods.
        human_attention_hours = 0.0
        human_attention_fallbacks = 0
        distinct_sessions_covered = 0
        sessions_total = len(session_ranges(events))
    else:
        active_hours = summary.as_float(productivity.get("active_hours_with_ai"))
        active_hours_per_session_sum = summary.as_float(productivity.get("active_hours_per_session_sum"))
        human_attention_hours = summary.as_float(productivity.get("human_attention_hours_with_ai"))
        human_attention_fallbacks = summary.as_int(productivity.get("human_attention_fallbacks"))
        calendar_hours = summary.as_float(productivity.get("calendar_hours_with_ai"))
        hours_without_ai = summary.as_float(productivity.get("hours_without_ai"))
        baseline_floor_clamped = summary.as_int(productivity.get("baseline_floor_clamped"))
        hours_floor_added = summary.as_float(productivity.get("hours_floor_added"))
        baseline_ceiling_clamped = summary.as_int(productivity.get("baseline_ceiling_clamped"))
        hours_ceiling_removed = summary.as_float(productivity.get("hours_ceiling_removed"))
        baseline_per_event_p95 = summary.as_float(productivity.get("baseline_per_event_p95"))
        sessions_covered = summary.as_int(productivity.get("sessions_covered"))
        # Floor divides by DISTINCT sessions, not chunks (chunk mode inflates
        # sessions_covered to per-day chunk count). Falls back to sessions_covered
        # for any producer that doesn't emit the distinct count.
        distinct_sessions_covered = summary.as_int(
            productivity.get("distinct_sessions_covered", sessions_covered)
        )
        sessions_total = summary.as_int(productivity.get("sessions_total"))
        unit = productivity.get("unit") or unit

    # Headline multiplier uses HUMAN-ATTENTION time (denominator = time the human
    # spent, not time the AI was busy). Falls back to AI-active wall-clock only if
    # human attention is unavailable (e.g. all transcripts rotated). AI-active is
    # kept as a secondary diagnostic field.
    # Floor against divide-by-near-zero (DeepSeek Q3): each covered session implies
    # at least HUMAN_ATTENTION_FLOOR_MIN_PER_SESSION of human cost, so a lone tiny
    # prompt can't explode the multiplier.
    # Codex-audit HIGH: apply the floor whenever we have covered sessions, even if
    # measured human attention is exactly 0 (sparse prompts >gap apart). Falling
    # back to AI-active wall-clock there would re-inject autonomous runtime into the
    # denominator — the very thing this metric was changed to exclude. AI-active is
    # only used when there are no covered sessions at all (no floor basis).
    floor_hours = distinct_sessions_covered * (summary.HUMAN_ATTENTION_FLOOR_MIN_PER_SESSION / 60.0)
    if distinct_sessions_covered > 0:
        denom_hours = max(human_attention_hours, floor_hours)
    else:
        denom_hours = active_hours
    hours_saved = hours_without_ai - denom_hours
    multiplier = hours_without_ai / denom_hours if denom_hours > 0 else 0.0

    # When the floor WINS, the denominator is a safety constant, not a
    # measurement — the ratio is then bounded only by the baseline estimate and
    # means nothing. On 2026-08-18 the 7d window had 4.7 minutes of measured
    # attention across 2 covered sessions and published x144. Flag it so
    # consumers can suppress the number instead of printing a fake record.
    floor_driven = distinct_sessions_covered > 0 and floor_hours > human_attention_hours

    return {
        "active_hours": rounded(denom_hours),
        "denominator_is_floor": floor_driven,
        "human_attention_hours": rounded(human_attention_hours),
        "human_attention_fallbacks": human_attention_fallbacks,
        "ai_active_wall_clock_hours": rounded(active_hours),
        "active_hours_per_session_sum": rounded(active_hours_per_session_sum),
        "calendar_span_hours": rounded(calendar_hours),
        "hours_without_ai_estimate": rounded(hours_without_ai),
        "hours_saved": rounded(hours_saved),
        "multiplier": rounded(multiplier),
        "baseline_floor_clamped": baseline_floor_clamped,
        "hours_floor_added": rounded(hours_floor_added),
        "baseline_ceiling_clamped": baseline_ceiling_clamped,
        "hours_ceiling_removed": rounded(hours_ceiling_removed),
        "baseline_per_event_p95": rounded(baseline_per_event_p95),
        "sessions_covered": sessions_covered,
        "sessions_total": sessions_total,
        "unit": unit,
    }


def normalize_stress_trend(value):
    text = str(value or "").lower()
    if "improving" in text:
        return "improving"
    if "worsening" in text:
        return "worsening"
    if "stable" in text:
        return "stable"
    return "stable"


def sentiment_payload(sentiment):
    top_day, top_day_stats = sentiment["top_day"]
    top_appr_raw = sentiment.get("top_appreciation_day")
    if top_appr_raw and isinstance(top_appr_raw, tuple) and len(top_appr_raw) == 2:
        top_appr_day, top_appr_stats = top_appr_raw
    else:
        top_appr_day, top_appr_stats = "", {}
    mood_arcs_top = [
        {"arc": arc, "count": count}
        for arc, count in sorted(
            sentiment.get("mood_counts", {}).items(),
            key=lambda item: (-item[1], item[0]),
        )[:3]
    ]

    return {
        "profanity_total": summary.as_int(sentiment.get("profanity_total")),
        "appreciation_total": summary.as_int(sentiment.get("appreciation_total")),
        "frustration_avg": rounded(sentiment.get("frustration_avg", 0.0)),
        "appreciation_avg": rounded(sentiment.get("appreciation_avg", 0.0)),
        "stress_trend": normalize_stress_trend(sentiment.get("stress_trend")),
        "top_day": {
            "date": str(top_day),
            "profanity": summary.as_int(top_day_stats.get("profanity")),
        },
        "top_appreciation_day": {
            "date": str(top_appr_day) if top_appr_day else "",
            "appreciation": summary.as_int(top_appr_stats.get("appreciation")) if top_appr_stats else 0,
        },
        "mood_arcs_top": mood_arcs_top,
        "sessions_covered": summary.as_int(sentiment.get("sessions_covered")),
    }


def task_estimated_hours(entry):
    if not isinstance(entry, dict):
        return None
    hours = summary.effective_task_hours(entry)
    return rounded(hours) if hours is not None else None


def build_summary(query):
    days = parse_days(query)
    start, end = period_for_days(days)
    if _READMODEL is not None and not _USE_SLOW_READMODEL and not _USE_LOOP_SUMMARY:
        # DeepSeek audit #60 follow-up: keep the hot summary path in SQL so
        # dashboard requests do not materialize every event dict in Python.
        by_model, total = readmodel.aggregate_by_model(_READMODEL, start, end)
        subscription = subscription_from_by_model(by_model, days)
    else:
        events = _read_events_dispatch(start, end)
        by_model, total = summary.summarize_by_model(events)
        subscription = summary.subscription_prorated_usd(events, days)

    api_cost = summary.as_float(total.get("cost_estimate_usd"))
    totals = stats_payload(total)
    totals["api_equivalent_cost_usd"] = rounded(api_cost)
    totals["subscription_usd"] = rounded(subscription)
    totals["subscription_cost_usd"] = rounded(subscription)
    totals["savings_usd"] = rounded(api_cost - subscription)

    return {
        "period": period_payload(start, end),
        "totals": totals,
        "by_model": [
            model_payload(model, by_model[model])
            for model in sorted(by_model)
        ],
    }


def build_productivity(query):
    days = parse_days(query)
    gap_minutes = parse_positive_int(query, "gap_minutes", 2)
    start, end = period_for_days(days)
    events = _read_events_dispatch(start, end)
    return productivity_payload(events, gap_minutes)


def build_sentiment(query):
    days = parse_days(query)
    start, end = period_for_days(days)
    events = _read_events_dispatch(start, end)
    sentiment = summary.summarize_sentiment(events, start, end)
    if sentiment is None:
        return None
    return sentiment_payload(sentiment)


def build_budget():
    current = now_local()
    since_5h = current - timedelta(hours=5)
    since_24h = current - timedelta(hours=24)

    events_5h = summary.events_for_provider(read_recent_events(since_5h, current), "anthropic")
    events_24h = summary.events_for_provider(read_recent_events(since_24h, current), "anthropic")
    _by_model_5h, total_5h = summary.summarize_by_model(events_5h)
    _by_model_24h, total_24h = summary.summarize_by_model(events_24h)

    tokens_5h = total_tokens(total_5h)
    tokens_24h = total_tokens(total_24h)

    cache_5h = cache_tokens(total_5h)
    cache_24h = cache_tokens(total_24h)

    return {
        "window_5h": {
            "since": since_5h.isoformat(timespec="seconds"),
            "tokens_used": tokens_5h,
            "cache_tokens": cache_5h,
            "estimated_limit_max5x": ESTIMATED_LIMIT_MAX5X,
            "estimated_limit_max20x": ESTIMATED_LIMIT_MAX20X,
            "percent_max5x": percent(tokens_5h, ESTIMATED_LIMIT_MAX5X),
            "percent_max20x": percent(tokens_5h, ESTIMATED_LIMIT_MAX20X),
        },
        "window_24h": {
            "tokens_used": tokens_24h,
            "cache_tokens": cache_24h,
            "calls": summary.as_int(total_24h.get("calls")),
            "cost_usd": rounded(total_24h.get("cost_estimate_usd", 0.0)),
        },
    }


def build_numeric_timeseries(metric, start, end, events):
    by_day = summary.summarize_by_day(events)
    data = []
    current = start
    while current <= end:
        key = current.isoformat()
        stats = by_day.get(key, summary.empty_stats())
        if metric == "cost":
            value = rounded(stats.get("cost_estimate_usd", 0.0))
        elif metric == "calls":
            value = summary.as_int(stats.get("calls"))
        elif metric == "input_tokens":
            value = summary.as_int(stats.get("input_tokens"))
        elif metric == "output_tokens":
            value = summary.as_int(stats.get("output_tokens"))
        else:
            value = 0
        data.append({"date": key, "value": value})
        current += timedelta(days=1)
    return data


def build_sentiment_timeseries(metric, start, end, events):
    tasks = summary.read_tasks()
    ranges = session_ranges(events)
    per_day = {}

    for session_id, (first_ts, _last_ts) in ranges.items():
        entry = tasks.get(session_id)
        if not isinstance(entry, dict) or not summary.has_sentiment_data(entry):
            continue

        day = datetime.fromtimestamp(first_ts).astimezone().date().isoformat()
        day_entry = per_day.setdefault(day, {"profanity": 0, "frustration": 0.0, "frustration_count": 0})
        day_entry["profanity"] += max(summary.as_int(entry.get("profanity_count")), 0)
        day_entry["frustration"] += summary.clamp_score(entry.get("frustration_score"))
        day_entry["frustration_count"] += 1

    data = []
    current = start
    while current <= end:
        key = current.isoformat()
        day_entry = per_day.get(key, {})
        if metric == "profanity":
            value = summary.as_int(day_entry.get("profanity"))
        else:
            count = summary.as_int(day_entry.get("frustration_count"))
            value = rounded(day_entry.get("frustration", 0.0) / count) if count else 0.0
        data.append({"date": key, "value": value})
        current += timedelta(days=1)
    return data


def build_timeseries(query):
    metric = query.get("metric", ["cost"])[0]
    allowed = {"cost", "calls", "input_tokens", "output_tokens", "profanity", "frustration"}
    if metric not in allowed:
        raise BadRequest("metric must be one of: cost, calls, input_tokens, output_tokens, profanity, frustration")

    days = parse_days(query)
    start, end = period_for_days(days)
    events = _read_events_dispatch(start, end)
    if metric in {"profanity", "frustration"}:
        data = build_sentiment_timeseries(metric, start, end, events)
    else:
        data = build_numeric_timeseries(metric, start, end, events)

    return {
        "metric": metric,
        "period": period_payload(start, end),
        "data": data,
    }


def primary_model(model_counts):
    if not model_counts:
        return "unknown"
    return sorted(model_counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def task_payload(entry):
    if not isinstance(entry, dict):
        return None
    description = entry.get("brief_description")
    mood_arc = entry.get("mood_arc")
    return {
        "brief_description": description if isinstance(description, str) else "",
        "ai_baseline_hours": task_estimated_hours(entry),
        "profanity_count": max(summary.as_int(entry.get("profanity_count")), 0),
        "mood_arc": mood_arc if isinstance(mood_arc, str) else "",
    }


def build_sessions(query):
    limit = parse_positive_int(query, "limit", DEFAULT_SESSION_LIMIT)
    events = summary.events_for_task_metrics(read_all_events())
    tasks = summary.read_tasks()
    sessions = {}

    for event in events:
        session_id = event.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            continue

        ts = event_ts_local(event)
        if ts is None:
            continue

        session = sessions.setdefault(
            session_id,
            {
                "first_ts": ts,
                "last_ts": ts,
                "calls": 0,
                "cost_usd": 0.0,
                "models": {},
            },
        )
        if ts < session["first_ts"]:
            session["first_ts"] = ts
        if ts > session["last_ts"]:
            session["last_ts"] = ts
        session["calls"] += 1
        session["cost_usd"] += summary.as_float(event.get("cost_estimate_usd"))

        model = event.get("model")
        if not isinstance(model, str) or not model:
            model = "unknown"
        session["models"][model] = session["models"].get(model, 0) + 1

    rows = []
    for session_id, session in sessions.items():
        rows.append(
            {
                "session_id": session_id,
                "first_ts": session["first_ts"].isoformat(timespec="seconds"),
                "last_ts": session["last_ts"].isoformat(timespec="seconds"),
                "calls": session["calls"],
                "cost_usd": rounded(session["cost_usd"]),
                "model_primary": primary_model(session["models"]),
                "task": task_payload(tasks.get(session_id)),
            }
        )

    rows.sort(key=lambda row: row["last_ts"], reverse=True)
    return {"sessions": rows[:limit]}


def build_health():
    bus_tasks_total = 0
    if isinstance(_READMODEL_META, dict):
        bus_tasks_total = summary.as_int(_READMODEL_META.get("bus_tasks"))
    return {
        "status": "ok",
        "timestamp": now_local().isoformat(timespec="seconds"),
        "events_total": len(read_all_events()),
        "tasks_total": len(summary.read_tasks()),
        "bus_tasks_total": bus_tasks_total,
        "readmodel": _READMODEL_META,
    }


# === WP snapshot ============================================================
# Composite payload tailored for the WordPress page-multi-agent.php template.
# WP fetches this JSON and overrides PHP-baked mock values via JS.
# Atomic write: tmp + os.replace to avoid the WP page seeing a half-written file.

# Generic placeholder for `today.top_session` when no real description is
# available. Must not reference any internal phase / project codename — this
# string appears in the public snapshot under --public mode.
_TASK_DESCRIPTION_FALLBACK = "AI coding session"
# Lowercased — comparison happens case-insensitively in _models_with_pct.
_SYNTHETIC_MODELS = {"<synthetic>", "synthetic", "unknown", ""}
_DESC_MAX_LEN = 120
_PATTERN_PATH_WIN = re.compile(r'(?<![\w/])[A-Za-z]:[\\/][\w\\/.\-+~]*', re.IGNORECASE)
_PATTERN_PATH_UNIX = re.compile(r'(?<![\w/])(?:~|/(?:home|usr|opt|var|etc)(?:/[\w\-+./]*)?)', re.IGNORECASE)
_PATTERN_EMAIL = re.compile(r'\b[\w.+\-]+@[\w\-]+\.[\w\-.]+\b')
_PATTERN_TOKEN = re.compile(r'\b(sk_|pk_|ghp_|gho_|github_pat_)\w{16,}\b')
# Personal-identifier scrub defaults are intentionally empty for OSS users:
# add your own via --customers-blocklist (one entry per line). For Cyrillic /
# accent inflections, list each form explicitly — the regex matches whole-word
# case-insensitive without lemmatization.
#
# Example: if you want every mention of "AcmeCorp" or "acme-corp" in a sessions
# description scrubbed to <client>, write a line `AcmeCorp` AND `acme-corp` in
# the blocklist file.
_PUBLIC_TERM_REPLACEMENTS: tuple = ()


def load_or_create_salt(path):
    """Read salt from file, generating 32 random bytes if file doesn't exist.

    Returns bytes. Atomic create (tmp + replace) to avoid readers observing a
    partial file. Permissions: 0600 on POSIX; on Windows best-effort chmod.
    """
    p = Path(path).expanduser()
    if p.exists():
        data = p.read_bytes().strip()
        if len(data) >= 16:
            return data

    p.parent.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_bytes(32)
    tmp = p.with_suffix(p.suffix + f".tmp.{os.getpid()}")
    tmp.write_bytes(salt)
    if hasattr(os, "chmod"):
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, p)
    return salt


def _hash_session_id(uuid_str, salt):
    """Stable 8-hex-char hash of session_id with local salt."""
    if not isinstance(uuid_str, str) or not uuid_str:
        return ""
    h = hashlib.blake2b(salt + uuid_str.encode("utf-8"), digest_size=4)
    return h.hexdigest()


def _build_customer_pattern(blocklist_path):
    """Read customer names and return a case-insensitive Cyrillic-aware regex."""
    if not blocklist_path:
        return None
    try:
        text = Path(blocklist_path).read_text(encoding="utf-8-sig")
    except OSError:
        return None
    names = [
        line.strip() for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not names:
        return None

    names.sort(key=len, reverse=True)
    escaped = [re.escape(name) for name in names]
    return re.compile(
        r'(?<![\wЀ-ӿ])(?:' + '|'.join(escaped) + r')(?![\wЀ-ӿ])',
        re.IGNORECASE,
    )


def _scrub_for_public(text, customer_pattern=None):
    """Strip absolute paths, emails, secret tokens, customer names. Idempotent."""
    if not isinstance(text, str) or not text:
        return ""
    text = _PATTERN_PATH_WIN.sub("[path]", text)
    text = _PATTERN_PATH_UNIX.sub("[path]", text)
    text = _PATTERN_EMAIL.sub("[email]", text)
    text = _PATTERN_TOKEN.sub("[token]", text)
    for pattern, replacement in _PUBLIC_TERM_REPLACEMENTS:
        text = pattern.sub(replacement, text)
    if customer_pattern is not None:
        text = customer_pattern.sub("[client]", text)
    return text


def _sanitize_desc(text):
    """Drop estimation-failure error strings before exposing to the WP page.
    Strip control/bidi characters that could spoof rendering. Cap length."""
    if not isinstance(text, str):
        return ""
    lowered = text.lower()
    if any(marker in lowered for marker in ("estimation failed", "[winerror", "[errno", "traceback")):
        return ""
    cleaned = "".join(
        ch for ch in text
        if ch.isprintable() and ch not in ("‪", "‫", "‬", "‭", "‮", "‎", "‏", "​")
    ).strip()
    if len(cleaned) > _DESC_MAX_LEN:
        cleaned = cleaned[: _DESC_MAX_LEN - 1].rstrip() + "…"
    return cleaned


def _sessions_within_24h(events_24h, all_sessions):
    """Return only sessions whose session_id appears in last-24h events.

    Note: this is constrained by `all_sessions` (the UI list, capped at
    SNAPSHOT_DEFAULT_SESSIONS = 8). For aggregations over today's full set
    of sessions (profanity, estimated hours), prefer
    `_today_session_ids_from_events()` which is uncapped.
    """
    ids = {ev.get("session_id") for ev in events_24h if isinstance(ev.get("session_id"), str)}
    return [s for s in all_sessions if s.get("session_id") in ids]


def _today_session_ids_from_events(events_24h):
    """Uncapped set of session_ids active in the today window.

    Codex review A1: Aggregations over today (profanity sum, estimated hours
    sum) must include ALL sessions active today, not just the capped UI list.
    """
    ids = []
    seen = set()
    # Stable ordering by first-seen in events list (latest events come last in
    # JSONL after read_events sorts; still produce a deterministic list).
    for ev in events_24h:
        sid = ev.get("session_id")
        if isinstance(sid, str) and sid and sid not in seen:
            seen.add(sid)
            ids.append(sid)
    return ids


_PROFANITY_PATTERNS_CACHE = None


def _load_profanity_patterns():
    """Import ALL_PROFANITY from tracker/estimate-task.py once. The dashed
    filename prevents normal `import`, so use importlib."""
    global _PROFANITY_PATTERNS_CACHE
    if _PROFANITY_PATTERNS_CACHE is not None:
        return _PROFANITY_PATTERNS_CACHE
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "estimate_task", PROJECT_ROOT / "tracker" / "estimate-task.py"
    )
    et = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(et)
    _PROFANITY_PATTERNS_CACHE = et.ALL_PROFANITY
    return _PROFANITY_PATTERNS_CACHE


def _profanity_since(transcript_path, since_dt):
    """Count profanity in user-messages whose `timestamp` is ≥ `since_dt`.

    Returns:
        int  — count of profanity matches in eligible messages
        None — if the transcript is missing, unreadable, or the path looks
               unsafe (so caller can fallback to per-session count, B2).

    Why: `tasks.json[*].profanity_count` is a per-session counter for the
    *whole* session. A session that started yesterday and bled into today
    keeps yesterday's swears in the per-session total. For today panel we
    need the per-message slice.

    Path safety (C1): the transcript_path comes from `tasks.json` which is
    written by our own scripts but could be tampered with. Validate that
    the path is under `~/.claude/projects/`, has a `.jsonl` suffix, and
    matches a UUID-like name.
    """
    if not transcript_path:
        return None
    try:
        p = Path(transcript_path).resolve(strict=False)
    except (OSError, RuntimeError):
        return None

    # Path validation: must be a `.jsonl` file under ~/.claude/projects/
    claude_projects = (Path.home() / ".claude" / "projects").resolve()
    if p.suffix.lower() != ".jsonl":
        return None
    if not path_is_relative_to(p, claude_projects):
        return None
    if not p.exists() or not p.is_file():
        return None
    # Reject absurdly large files (10 MB cap)
    try:
        if p.stat().st_size > 10 * 1024 * 1024:
            return None
    except OSError:
        return None

    # Normalize since_dt → aware UTC for comparison
    if since_dt.tzinfo is None:
        since_aware = since_dt.replace(tzinfo=timezone.utc)
    else:
        since_aware = since_dt

    patterns = _load_profanity_patterns()
    total = 0
    try:
        with p.open("r", encoding="utf-8") as source:
            for line in source:
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict) or msg.get("type") != "user":
                    continue
                ts_str = msg.get("timestamp")
                if not isinstance(ts_str, str):
                    continue
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except ValueError:
                    continue
                # If transcript timestamp is naive, treat it as UTC (most jsonl
                # have explicit Z suffix, but be defensive).
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < since_aware:
                    continue
                content = ((msg.get("message") or {}).get("content"))
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    parts = []
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            parts.append(item.get("text") or "")
                        elif isinstance(item, str):
                            parts.append(item)
                    text = "\n".join(parts)
                for pattern in patterns:
                    total += len(pattern.findall(text))
    except OSError:
        return None
    return total


def _productivity_block(productivity_data):
    """Sanity-clamp productivity values to 0 if math implies AI is slower than
    manual (multiplier < 1, saved < 0). The summary.summarize_productivity now
    computes everything over the same covered-sessions subset (like-with-like),
    so coverage doesn't need its own threshold — the math is consistent
    regardless of how many sessions have estimates."""
    active = productivity_data.get("active_hours") or 0
    active_per_session_sum = productivity_data.get("active_hours_per_session_sum")
    if active_per_session_sum is None:
        active_per_session_sum = active
    calendar = productivity_data.get("calendar_span_hours") or 0
    multiplier_raw = productivity_data.get("multiplier") or 0
    saved_raw = productivity_data.get("hours_saved") or 0

    # `denominator_is_floor` means the divisor was the per-session safety floor
    # rather than measured attention: the ratio is then just baseline/constant
    # and is not a productivity figure at all (7d read x144 on 2026-08-18 off
    # 4.7 minutes of attention). Suppress like the sub-1 case — the dashboard
    # falls back to its legacy label instead of printing a fabricated record.
    if productivity_data.get("denominator_is_floor"):
        multiplier = 0.0
        saved = 0.0
    elif multiplier_raw < 1 or saved_raw < 0:
        multiplier = 0.0
        saved = 0.0
    else:
        multiplier = rounded(multiplier_raw, 3)
        saved = rounded(saved_raw, 1)

    return {
        "active_hours": rounded(active, 1),
        "estimate_active_hours": rounded(active, 1),
        "active_hours_per_session_sum": rounded(active_per_session_sum, 1),
        "calendar_hours": rounded(calendar, 1),
        "multiplier": multiplier,
        "hours_saved": saved,
        "estimated_hours": rounded(active + saved, 1),
        "baseline_floor_clamped": int(productivity_data.get("baseline_floor_clamped") or 0),
        "hours_floor_added": rounded(productivity_data.get("hours_floor_added") or 0, 1),
        "baseline_ceiling_clamped": int(productivity_data.get("baseline_ceiling_clamped") or 0),
        "hours_ceiling_removed": rounded(productivity_data.get("hours_ceiling_removed") or 0, 1),
        "baseline_per_event_p95": rounded(productivity_data.get("baseline_per_event_p95") or 0, 1),
        "sessions_total": int(productivity_data.get("sessions_total") or 0),
        "sessions_covered": int(productivity_data.get("sessions_covered") or 0),
        "unit": productivity_data.get("unit") or summary.productivity_unit(),
    }


def _today_productivity_block(day_productivity):
    """Productivity shape for the calendar-day Today filter.

    Built from the SAME human-attention pipeline as the other periods (a
    build_productivity run over a 1-day window), so Today's multiplier is
    comparable with 7d/30d. It used to divide task estimates by 2-minute
    AI-active wall-clock — a different metric published under the same label
    (Codex-audit HIGH), and with zero covered sessions it fabricated
    `estimated = active × 7.3` out of thin air.

    Partial-day guard (kept from the original widget, theme v0.8.30): intraday
    denominators are tiny and noisy — 0.1h of attention estimated at 18.5h
    reads ×185. Suppress the multiplier when the day's attention is under 30
    minutes or the ratio exceeds ×50; the dashboard then falls back to its
    legacy label instead of flashing an artefact.
    """
    block = _productivity_block(day_productivity)
    block["days"] = 1
    block["calendar_hours"] = 24.0
    attention = block.get("active_hours") or 0
    if block["multiplier"] > 50 or attention < 0.5:
        block["multiplier"] = 0.0
        block["hours_saved"] = 0.0
        block["estimated_hours"] = block["estimate_active_hours"]
    return block


def _productivity_periods(total_days, base_productivity):
    """Precompute period-specific productivity.

    The dashboard period selector should not pro-rate productivity ratios:
    multiplying both active and saved hours by the same factor makes the
    multiplier artificially constant. This payload lets the UI use real session
    coverage for each period.
    """
    periods = {
        "today": _today_productivity_block(build_productivity({"days": ["1"]})),
        "all": _productivity_block(base_productivity),
    }
    periods["all"]["days"] = int(total_days)

    for key, requested_days in (("7d", 7), ("30d", 30), ("60d", 60)):
        days = min(int(requested_days), int(total_days))
        payload = build_productivity({"days": [str(days)]})
        periods[key] = _productivity_block(payload)
        periods[key]["days"] = days

    return periods


def _model_short(name):
    """Strip vendor prefix and version-day suffix for compact display."""
    if not isinstance(name, str):
        return "unknown"
    bare = name.lower()
    for prefix in ("anthropic/", "openai/", "claude-", "claude/"):
        if bare.startswith(prefix):
            bare = bare[len(prefix):]
    parts = bare.split("-")
    if len(parts) >= 2 and parts[1].isdigit() and len(parts[1]) >= 1:
        return f"{parts[0]} {parts[1]}.{parts[2]}" if len(parts) >= 3 and parts[2].isdigit() else f"{parts[0]} {parts[1]}"
    return bare


def _models_with_pct(by_model_payload, total_cost):
    """Return [{name, calls, cost, pct}] sorted by cost desc, synthetic excluded."""
    rows = []
    for entry in by_model_payload:
        raw_name = (entry.get("model") or "").strip()
        if raw_name.lower() in _SYNTHETIC_MODELS:
            continue
        cost = float(entry.get("cost_usd") or 0)
        rows.append({
            "name": _model_short(raw_name),
            "calls": int(entry.get("calls") or 0),
            "cost": rounded(cost, 2),
            "pct": rounded((cost / total_cost * 100) if total_cost > 0 else 0.0, 1),
        })
    rows.sort(key=lambda r: -r["cost"])
    return rows


def _timeline_weights_for(start, end, events):
    """62-element list of daily call counts (or however many days in window)."""
    series = build_numeric_timeseries("calls", start, end, events)
    return [int(point.get("value") or 0) for point in series]


def _provider_compact_payload(stats):
    return {
        "calls": summary.as_int(stats.get("calls")),
        "cost_usd": rounded(stats.get("cost_estimate_usd", 0.0), 2),
    }


def _timeline_days_for(start, end, events):
    """Per-day tooltip payload aligned with timeline_weights."""
    events_by_day = {}
    for event in events:
        ts = event_ts_local(event)
        if ts is None:
            continue
        events_by_day.setdefault(ts.date().isoformat(), []).append(event)

    rows = []
    current = start
    while current <= end:
        key = current.isoformat()
        day_events = events_by_day.get(key, [])
        _by_model, total = summary.summarize_by_model(day_events)
        by_provider = summary.summarize_by_provider(day_events)
        rows.append({
            "date": key,
            "calls": summary.as_int(total.get("calls")),
            "cost_usd": rounded(total.get("cost_estimate_usd", 0.0), 2),
            "input_tokens": summary.as_int(total.get("input_tokens")),
            "output_tokens": summary.as_int(total.get("output_tokens")),
            "cache_tokens": cache_tokens(total),
            "active_hours": rounded(
                summary.active_time_hours(day_events, gap_minutes=2) if day_events else 0.0,
                1,
            ),
            "providers": {
                "anthropic_claude": _provider_compact_payload(
                    by_provider.get("anthropic_claude", summary.empty_stats())
                ),
                "openai_codex": _provider_compact_payload(
                    by_provider.get("openai_codex", summary.empty_stats())
                ),
                "openrouter_openclaw": _provider_compact_payload(
                    by_provider.get("openrouter_openclaw", summary.empty_stats())
                ),
                "opencode_openrouter": _provider_compact_payload(
                    by_provider.get("opencode_openrouter", summary.empty_stats())
                ),
            },
        })
        current += timedelta(days=1)
    return rows


def _today_payload(events_24h, sessions_recent, tasks, since_dt=None,
                   today_session_ids=None, public_mode=False, customer_pattern=None):
    """Aggregate last 24h into the WP $today shape.

    Productivity math is like-with-like: estimated/saved hours are computed
    from sessions that have an `ai_baseline_hours` estimate, and active hours
    in those numbers cover only the same subset. Calls/cost/profanity remain
    over the full 24h window.
    """
    return _today_payload_unit_aware(
        events_24h,
        sessions_recent,
        tasks,
        since_dt=since_dt,
        today_session_ids=today_session_ids,
        public_mode=public_mode,
        customer_pattern=customer_pattern,
    )


def _today_payload_unit_aware(events_24h, sessions_recent, tasks, since_dt=None,
                              today_session_ids=None, public_mode=False, customer_pattern=None):
    by_model_24h, total_24h = summary.summarize_by_model(events_24h)
    calls = summary.as_int(total_24h.get("calls"))
    cost = rounded(total_24h.get("cost_estimate_usd", 0.0), 2)
    unit = summary.productivity_unit()

    active_hours_24h_full = summary.active_time_hours_merged(events_24h, gap_minutes=2) if events_24h else 0.0
    active_hours_24h_per_session_sum = summary.active_time_hours(events_24h, gap_minutes=2) if events_24h else 0.0

    if today_session_ids is None:
        today_session_ids = _today_session_ids_from_events(events_24h)

    top_session = ""
    if sessions_recent:
        top = sessions_recent[0]
        top_task = top.get("task") or {}
        top_session = _sanitize_desc(top_task.get("brief_description")) or _TASK_DESCRIPTION_FALLBACK
        if public_mode:
            top_session = _scrub_for_public(top_session, customer_pattern) or _TASK_DESCRIPTION_FALLBACK

    today_profanity = 0
    for sid in today_session_ids:
        entry = tasks.get(sid) if isinstance(sid, str) else None
        transcript = entry.get("transcript_path") if isinstance(entry, dict) else None
        if since_dt is not None and transcript:
            counted = _profanity_since(transcript, since_dt)
            if counted is None:
                today_profanity += int((entry or {}).get("profanity_count") or 0)
            else:
                today_profanity += counted
        else:
            today_profanity += int((entry or {}).get("profanity_count") or 0) if entry else 0

    events_by_session = {}
    for ev in events_24h:
        sid = ev.get("session_id")
        if isinstance(sid, str) and sid:
            events_by_session.setdefault(sid, []).append(ev)

    estimated_hours_sum = 0.0
    estimated_session_ids = []
    estimated_chunk_keys = []
    baseline_floor_clamped = 0
    hours_floor_added = 0.0
    baseline_ceiling_clamped = 0
    hours_ceiling_removed = 0.0
    baseline_per_event_values = []
    active_hours_per_unit_sum = 0.0
    total_units = len(today_session_ids)

    def record_estimated_unit(baseline_hours, unit_events, covered_sid=None, covered_chunk_key=None):
        nonlocal estimated_hours_sum
        nonlocal active_hours_per_unit_sum
        nonlocal baseline_floor_clamped
        nonlocal hours_floor_added
        nonlocal baseline_ceiling_clamped
        nonlocal hours_ceiling_removed

        timestamps = []
        for event in unit_events:
            ts = summary.parse_event_ts(event.get("ts"))
            if ts is not None:
                timestamps.append(ts)
        unit_active_hours = summary._active_time_hours_for_timestamps(timestamps, gap_minutes=2)
        event_count = len(unit_events)
        effective_hours, kind = summary.effective_session_hours(
            float(baseline_hours),
            unit_active_hours,
            event_count,
        )
        if kind == "floor":
            baseline_floor_clamped += 1
            hours_floor_added += effective_hours - float(baseline_hours)
        elif kind.startswith("ceiling"):
            baseline_ceiling_clamped += 1
            hours_ceiling_removed += float(baseline_hours) - effective_hours
        if event_count > 0:
            baseline_per_event_values.append(float(baseline_hours) / event_count)
        estimated_hours_sum += effective_hours
        active_hours_per_unit_sum += unit_active_hours
        if covered_sid is not None:
            estimated_session_ids.append(covered_sid)
        if covered_chunk_key is not None:
            estimated_chunk_keys.append(covered_chunk_key)

    if unit == "session":
        for sid in today_session_ids:
            entry = tasks.get(sid) if isinstance(sid, str) else None
            if not isinstance(entry, dict):
                continue
            hours = summary.effective_task_hours(entry)
            if hours is None:
                continue
            record_estimated_unit(hours, events_by_session.get(sid, []), covered_sid=sid)
    else:
        events_by_chunk = {}
        for sid, session_events in events_by_session.items():
            for ev in session_events:
                ts = summary.parse_event_ts(ev.get("ts"))
                if ts is None:
                    continue
                date_key = summary.chunk_date(ts)
                events_by_chunk.setdefault((sid, date_key), []).append(ev)

        total_units = 0
        for sid in today_session_ids:
            if not isinstance(sid, str):
                continue
            dates = sorted(
                date_key
                for chunk_sid, date_key in events_by_chunk
                if chunk_sid == sid
            )
            if not dates:
                continue

            chunk_task_keys = [f"{sid}:{date_key}" for date_key in dates]
            present_chunk_keys = [key for key in chunk_task_keys if key in tasks]
            session_entry = tasks.get(sid)
            session_hours = (
                summary.effective_task_hours(session_entry)
                if isinstance(session_entry, dict)
                else None
            )

            if not present_chunk_keys:
                total_units += 1
                if session_hours is None:
                    continue
                record_estimated_unit(
                    session_hours,
                    events_by_session.get(sid, []),
                    covered_sid=sid,
                )
                continue

            if len(present_chunk_keys) < len(chunk_task_keys) and session_hours is not None:
                total_units += 1
                record_estimated_unit(
                    session_hours,
                    events_by_session.get(sid, []),
                    covered_sid=sid,
                )
                continue

            total_units += len(dates)
            for date_key in dates:
                task_key = f"{sid}:{date_key}"
                entry = tasks.get(task_key)
                if not isinstance(entry, dict):
                    continue
                hours = summary.effective_task_hours(entry)
                if hours is None:
                    continue
                chunk_key = (sid, date_key)
                record_estimated_unit(
                    hours,
                    events_by_chunk.get(chunk_key, []),
                    covered_chunk_key=chunk_key,
                )

    if estimated_session_ids or estimated_chunk_keys:
        covered_ids = set(estimated_session_ids)
        covered_chunks = set(estimated_chunk_keys)
        covered_events = []
        for ev in events_24h:
            sid = ev.get("session_id")
            if not isinstance(sid, str) or not sid:
                continue
            if sid in covered_ids:
                covered_events.append(ev)
                continue
            ts = summary.parse_event_ts(ev.get("ts"))
            if ts is None:
                continue
            if (sid, summary.chunk_date(ts)) in covered_chunks:
                covered_events.append(ev)
        active_hours_for_estimate = summary.active_time_hours_merged(covered_events, gap_minutes=2)
        if unit == "session":
            active_hours_per_session_sum = summary.active_time_hours(covered_events, gap_minutes=2)
        else:
            active_hours_per_session_sum = active_hours_per_unit_sum
    else:
        active_hours_for_estimate = active_hours_24h_full
        active_hours_per_session_sum = active_hours_24h_per_session_sum
        # No covered estimates today → no estimate. The old code fabricated
        # `active × 7.3` here, publishing an invented multiplier for any day
        # with ≥0.5h of AI activity and zero estimated sessions (Codex-audit
        # HIGH). Zero means "nothing to claim yet", and the dashboard's
        # fallback label handles it honestly.
        estimated_hours_sum = 0.0

    hours_saved_today = rounded(max(0.0, estimated_hours_sum - active_hours_for_estimate), 1)

    return {
        "calls": calls,
        "cost_usd": cost,
        "active_hours": rounded(active_hours_24h_full, 1),
        "active_hours_for_estimate": rounded(active_hours_for_estimate, 1),
        "active_hours_per_session_sum": rounded(active_hours_per_session_sum, 1),
        "estimated_hours": rounded(estimated_hours_sum, 1),
        "hours_saved": hours_saved_today,
        "baseline_floor_clamped": baseline_floor_clamped,
        "hours_floor_added": rounded(hours_floor_added, 1),
        "baseline_ceiling_clamped": baseline_ceiling_clamped,
        "hours_ceiling_removed": rounded(hours_ceiling_removed, 1),
        "baseline_per_event_p95": rounded(summary.percentile(baseline_per_event_values, 95), 1),
        "sessions_total": total_units,
        "estimated_sessions_covered": len(estimated_session_ids) + len(estimated_chunk_keys),
        "unit": unit,
        "profanity": today_profanity,
        "top_session": top_session or _TASK_DESCRIPTION_FALLBACK,
        "providers": providers_payload(events_24h),
    }


def build_wp_snapshot(
    days=SNAPSHOT_DEFAULT_DAYS,
    sessions_limit=SNAPSHOT_DEFAULT_SESSIONS,
    public_mode=False,
    salt=None,
    customer_pattern=None,
):
    """Build a composite payload matching page-multi-agent.php data shape."""
    query = {"days": [str(days)]}

    summary_data = build_summary(query)
    productivity_data = build_productivity(query)
    sentiment_data = build_sentiment(query) or {}
    budget_data = build_budget()
    sessions_data = build_sessions({"limit": [str(sessions_limit)]})

    period = summary_data["period"]
    start_date = date.fromisoformat(period["start"])
    end_date = date.fromisoformat(period["end"])
    events_window = _read_events_dispatch(start_date, end_date)
    providers = providers_payload(events_window)

    # Build models list with cost share %
    by_model_payload = summary_data["by_model"]
    total_cost = float(summary_data["totals"].get("cost_usd") or 0)
    models_list = _models_with_pct(by_model_payload, total_cost)

    # Today (since local midnight, not 24h sliding) — matches the user's
    # intuition of "today" as a calendar day. Yesterday's late-night sessions
    # don't bleed into today's metrics.
    current = now_local()
    since_today = datetime.combine(current.date(), datetime.min.time(), tzinfo=current.tzinfo)
    events_24h = read_recent_events(since_today, current)
    sessions_24h = _sessions_within_24h(events_24h, sessions_data["sessions"])
    today_session_ids = _today_session_ids_from_events(events_24h)
    today_payload = _today_payload(
        events_24h,
        sessions_24h,
        summary.read_tasks(),
        since_dt=since_today,
        today_session_ids=today_session_ids,
        public_mode=public_mode,
        customer_pattern=customer_pattern,
    )

    # Sessions for the WP list (compact shape)
    sessions_compact = []
    for sess in sessions_data["sessions"]:
        task = sess.get("task") or {}
        sid = sess.get("session_id") or ""
        if public_mode:
            sid_short = _hash_session_id(sid, salt) if salt else ""
            desc = _scrub_for_public(_sanitize_desc(task.get("brief_description")), customer_pattern)
        else:
            sid_short = sid[:8]
            desc = _sanitize_desc(task.get("brief_description"))
        sessions_compact.append({
            "session_id_short": sid_short,
            "first_ts": sess.get("first_ts"),
            "last_ts": sess.get("last_ts"),
            "calls": sess.get("calls"),
            "cost_usd": sess.get("cost_usd"),
            "model_primary": _model_short(sess.get("model_primary")),
            "desc": desc,
            "mood": (task.get("mood_arc") or "").strip(),
            "profanity": int(task.get("profanity_count") or 0),
            "ai_baseline_hours": task.get("ai_baseline_hours"),
        })

    # Compose WP-shaped payload
    return {
        "schema_version": 1,
        "generated_at": current.isoformat(timespec="seconds"),
        "totals": {
            "calls": int(summary_data["totals"].get("calls") or 0),
            "cost_usd": rounded(summary_data["totals"].get("cost_usd"), 2),
            "cost_usd_combined": rounded(summary_data["totals"].get("cost_usd"), 2),
            "savings_usd": rounded(summary_data["totals"].get("savings_usd"), 2),
            "subscription_usd": rounded(summary_data["totals"].get("subscription_usd"), 2),
            "days": period["days"],
            "period_start": period["start"],
            "period_end": period["end"],
        },
        "providers": providers,
        "productivity": _productivity_block(productivity_data),
        "productivity_periods": _productivity_periods(period["days"], productivity_data),
        "budget": {
            "tokens_used": int(budget_data["window_5h"].get("tokens_used") or 0),
            "limit_5h": ESTIMATED_LIMIT_MAX5X,
            "limit_20x": ESTIMATED_LIMIT_MAX20X,
            "percent_5x": int(round(float(budget_data["window_5h"].get("percent_max5x") or 0))),
            "percent_20x": int(round(float(budget_data["window_5h"].get("percent_max20x") or 0))),
            "cache_tokens": int(budget_data["window_5h"].get("cache_tokens") or 0),
        },
        "sentiment": {
            "profanity_total": int(sentiment_data.get("profanity_total") or 0),
            "appreciation_total": int(sentiment_data.get("appreciation_total") or 0),
            "frustration_avg": rounded(sentiment_data.get("frustration_avg"), 2),
            "appreciation_avg": rounded(sentiment_data.get("appreciation_avg"), 2),
            "stress_trend": sentiment_data.get("stress_trend") or "stable",
            "top_day": sentiment_data.get("top_day") or {"date": "", "profanity": 0},
            "top_appreciation_day": sentiment_data.get("top_appreciation_day") or {"date": "", "appreciation": 0},
        },
        "today": today_payload,
        "models": models_list,
        "sessions": sessions_compact,
        "timeline_weights": _timeline_weights_for(start_date, end_date, events_window),
        "timeline_days": _timeline_days_for(start_date, end_date, events_window),
    }


def write_snapshot(path, payload):
    """Atomic write: unique tmp + os.replace.

    Unique suffix lets concurrent writers (e.g. cron + daemon) coexist without
    clobbering each other's tmp file. os.replace on NTFS is atomic for readers.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + f".tmp.{os.getpid()}.{threading.get_ident()}")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, target)
    except Exception:
        # Cleanup tmp if rename failed mid-write
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def snapshot_loop(path, interval, days, stop_event, public_mode=False, salt=None, customer_pattern=None):
    """Background writer loop. Quiet on success, prints on failure."""
    while not stop_event.is_set():
        try:
            payload = build_wp_snapshot(
                days=days,
                public_mode=public_mode,
                salt=salt,
                customer_pattern=customer_pattern,
            )
            write_snapshot(path, payload)
        except Exception as exc:
            print(f"[snapshot] write failed: {exc}", file=sys.stderr)
        if stop_event.wait(interval):
            return


# === API handler ============================================================


class APIHandler(http.server.BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        raw_path = urllib.parse.unquote(parsed.path)
        static_path = dashboard_static_path(raw_path)
        if static_path is not None:
            self.serve_static(static_path)
            return

        path = raw_path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

        try:
            if path == "/api/health":
                payload = build_health()
            elif path == "/api/summary":
                payload = build_summary(query)
            elif path == "/api/productivity":
                payload = build_productivity(query)
            elif path == "/api/sentiment":
                payload = build_sentiment(query)
            elif path == "/api/budget":
                payload = build_budget()
            elif path == "/api/timeseries":
                payload = build_timeseries(query)
            elif path == "/api/sessions":
                payload = build_sessions(query)
            else:
                self.send_json({"error": "not found"}, status=404)
                return
        except BadRequest as exc:
            self.send_json({"error": str(exc)}, status=400)
            return
        except Exception as exc:
            print(f"internal error: {exc}", file=sys.stderr)
            self.send_json({"error": str(exc)}, status=500)
            return

        self.send_json(payload)

    def serve_static(self, file_path):
        dashboard_root = DASHBOARD_DIR.resolve()

        try:
            resolved_path = file_path.resolve(strict=False)
        except (OSError, RuntimeError):
            self.send_error_json(404, "static file not found")
            return

        if resolved_path != dashboard_root and not path_is_relative_to(resolved_path, dashboard_root):
            self.send_error_json(404, "static file not found")
            return

        if not resolved_path.exists() or not resolved_path.is_file():
            self.send_error_json(404, "static file not found")
            return

        try:
            content = resolved_path.read_bytes()
        except OSError as exc:
            print(f"static file read error: {exc}", file=sys.stderr)
            self.send_error_json(500, "static file read failed")
            return

        self.send_response(200)
        self.send_cors_headers()
        self.send_header("Content-Type", static_content_type(resolved_path))
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def send_error_json(self, status, message):
        self.send_json({"error": message}, status=status)

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args, **kwargs):
        return


def main():
    global _READMODEL, _READMODEL_META, _USE_SLOW_READMODEL, _USE_LOOP_SUMMARY
    args = parse_args()
    _USE_SLOW_READMODEL = bool(args.use_slow_readmodel)
    _USE_LOOP_SUMMARY = bool(args.use_loop_summary)

    if args.no_readmodel:
        _READMODEL = None
        _READMODEL_META = None
    else:
        _READMODEL, _READMODEL_META = readmodel.build_with_meta(TRACKER_DIR)
        print(
            f"[readmodel] built events={_READMODEL_META['events']} "
            f"tasks={_READMODEL_META['tasks']} "
            f"bus_tasks={_READMODEL_META['bus_tasks']}",
            file=sys.stderr,
        )

    salt = None
    customer_pattern = None
    if args.public:
        salt = load_or_create_salt(args.salt_file)
        customer_pattern = _build_customer_pattern(args.customers_blocklist)
        print(
            f"[snapshot] PUBLIC mode — salt loaded ({len(salt)} bytes), "
            f"customers blocklist: {args.customers_blocklist or 'none'}"
        )

    # Snapshot-once mode: build, write, exit. Used for cron and tests.
    if args.snapshot_once:
        if not args.snapshot_path:
            print("--snapshot-once requires --snapshot-path", file=sys.stderr)
            sys.exit(2)
        payload = build_wp_snapshot(
            days=args.snapshot_days,
            public_mode=args.public,
            salt=salt,
            customer_pattern=customer_pattern,
        )
        write_snapshot(args.snapshot_path, payload)
        print(f"[snapshot] wrote {args.snapshot_path}")
        return

    # Optional background snapshot writer
    stop_event = None
    if args.snapshot_path:
        stop_event = threading.Event()
        t = threading.Thread(
            target=snapshot_loop,
            args=(
                args.snapshot_path,
                args.snapshot_interval,
                args.snapshot_days,
                stop_event,
                args.public,
                salt,
                customer_pattern,
            ),
            daemon=True,
        )
        t.start()
        print(
            f"[snapshot] writer started — every {args.snapshot_interval}s "
            f"to {args.snapshot_path}"
        )

    server = http.server.ThreadingHTTPServer((args.host, args.port), APIHandler)
    print(f"Backend running on http://{args.host}:{args.port}")
    print(
        "Endpoints: /api/health, /api/summary, /api/productivity, "
        "/api/sentiment, /api/budget, /api/timeseries, /api/sessions"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        if stop_event is not None:
            stop_event.set()
        print("\nShutting down.")


if __name__ == "__main__":
    main()
