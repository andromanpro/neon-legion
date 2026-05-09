#!/usr/bin/env python
import argparse
import json
import sys
import http.server
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "tracker"))
import summary  # noqa: E402


ESTIMATED_LIMIT_MAX5X = 88_000
ESTIMATED_LIMIT_MAX20X = 220_000
DEFAULT_DAYS = 1
DEFAULT_SESSION_LIMIT = 20


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


class BadRequest(ValueError):
    pass


def parse_args():
    parser = argparse.ArgumentParser(description="Multi-agent tracker backend API")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1")
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
    return round(float(value), digits)


def stats_payload(stats):
    return {
        "calls": summary.as_int(stats.get("calls")),
        "input_tokens": summary.as_int(stats.get("input_tokens")),
        "output_tokens": summary.as_int(stats.get("output_tokens")),
        "cache_read_tokens": summary.as_int(stats.get("cache_read_tokens")),
        "cost_usd": rounded(stats.get("cost_estimate_usd", 0.0)),
    }


def model_payload(model, stats):
    payload = stats_payload(stats)
    payload["model"] = model
    payload["cache_hit_percent"] = summary.cache_hit_percent(stats)
    payload["unknown_pricing_events"] = summary.as_int(stats.get("unknown_pricing_events"))
    return payload


def event_ts_local(event):
    ts = summary.parse_event_ts(event.get("ts"))
    if ts is None:
        return None
    return ts.astimezone()


def read_all_events():
    return summary.read_events(date.min, date.max)


def read_recent_events(since, now):
    events = summary.read_events(since.date(), now.date())
    recent = []
    for event in events:
        ts = event_ts_local(event)
        if ts is not None and since <= ts <= now:
            recent.append(event)
    return recent


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
    productivity = summary.summarize_productivity(events, gap_minutes)
    if productivity is None:
        active_hours = summary.active_time_hours(events, gap_minutes)
        calendar_hours = calendar_span_hours(events)
        hours_without_ai = 0.0
        sessions_covered = 0
        sessions_total = len(session_ranges(events))
    else:
        active_hours = summary.as_float(productivity.get("active_hours_with_ai"))
        calendar_hours = summary.as_float(productivity.get("calendar_hours_with_ai"))
        hours_without_ai = summary.as_float(productivity.get("hours_without_ai"))
        sessions_covered = summary.as_int(productivity.get("sessions_covered"))
        sessions_total = summary.as_int(productivity.get("sessions_total"))

    hours_saved = hours_without_ai - active_hours
    multiplier = hours_without_ai / active_hours if active_hours > 0 else 0.0

    return {
        "active_hours": rounded(active_hours),
        "calendar_span_hours": rounded(calendar_hours),
        "hours_without_ai_estimate": rounded(hours_without_ai),
        "hours_saved": rounded(hours_saved),
        "multiplier": rounded(multiplier),
        "sessions_covered": sessions_covered,
        "sessions_total": sessions_total,
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
    mood_arcs_top = [
        {"arc": arc, "count": count}
        for arc, count in sorted(
            sentiment.get("mood_counts", {}).items(),
            key=lambda item: (-item[1], item[0]),
        )[:3]
    ]

    return {
        "profanity_total": summary.as_int(sentiment.get("profanity_total")),
        "frustration_avg": rounded(sentiment.get("frustration_avg", 0.0)),
        "appreciation_avg": rounded(sentiment.get("appreciation_avg", 0.0)),
        "stress_trend": normalize_stress_trend(sentiment.get("stress_trend")),
        "top_day": {
            "date": str(top_day),
            "profanity": summary.as_int(top_day_stats.get("profanity")),
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
    events = summary.read_events(start, end)
    by_model, total = summary.summarize_by_model(events)

    subscription = summary.MONTHLY_SUBSCRIPTION_USD / summary.PRORATE_DAYS * days
    api_cost = summary.as_float(total.get("cost_estimate_usd"))
    totals = stats_payload(total)
    totals["subscription_usd"] = rounded(subscription)
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
    events = summary.read_events(start, end)
    return productivity_payload(events, gap_minutes)


def build_sentiment(query):
    days = parse_days(query)
    start, end = period_for_days(days)
    events = summary.read_events(start, end)
    sentiment = summary.summarize_sentiment(events, start, end)
    if sentiment is None:
        return None
    return sentiment_payload(sentiment)


def build_budget():
    current = now_local()
    since_5h = current - timedelta(hours=5)
    since_24h = current - timedelta(hours=24)

    events_5h = read_recent_events(since_5h, current)
    events_24h = read_recent_events(since_24h, current)
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
    events = summary.read_events(start, end)
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
    events = read_all_events()
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
    return {
        "status": "ok",
        "timestamp": now_local().isoformat(timespec="seconds"),
        "events_total": len(read_all_events()),
        "tasks_total": len(summary.read_tasks()),
    }


class APIHandler(http.server.BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
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

    def send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

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
    args = parse_args()
    server = http.server.ThreadingHTTPServer((args.host, args.port), APIHandler)
    print(f"Backend running on http://{args.host}:{args.port}")
    print(
        "Endpoints: /api/health, /api/summary, /api/productivity, "
        "/api/sentiment, /api/budget, /api/timeseries, /api/sessions"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
