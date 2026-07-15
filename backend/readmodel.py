#!/usr/bin/env python
"""In-memory SQLite read-model for dashboard event queries.

JSONL files under tracker/ remain canonical. This module rebuilds an in-memory
SQLite cache on each backend start and uses it only to accelerate event-window
reads. Bus worker state transitions hydrate `bus_tasks` from the local
bus-events JSONL stream.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path


PROVIDER_FILES = {
    "claude": ("claude-events.jsonl", "anthropic"),
    "codex": ("codex-events.jsonl", "openai"),
    "openclaw": ("openclaw-events.jsonl", "openrouter"),
    "opencode": ("opencode-events.jsonl", "opencode"),
    # Direct ds-call.py DeepSeek calls — same openrouter provider as openclaw,
    # so they aggregate under the shared openrouter/deepseek/deepseek-v4-pro key.
    "dscall": ("dscall-events.jsonl", "openrouter"),
}
PROVIDER_ALIASES = {
    "anthropic": "claude",
    "openai": "codex",
    "openrouter": "openclaw",
}
PROVIDER_KEYS = {
    "anthropic": "anthropic_claude",
    "openai": "openai_codex",
    "openrouter": "openrouter_openclaw",
    "opencode": "opencode_openrouter",
}


def aggregate_by_model(
    conn: sqlite3.Connection,
    start: date,
    end: date,
    providers: list[str] | None = None,
) -> tuple[dict[str, dict], dict]:
    """SQL-level GROUP BY model.

    Returns (by_model, totals) matching summary.summarize_by_model's output
    shape for read_events_fast() over the same window.
    """
    if start > end:
        return {}, _empty_stats()

    provider_keys = _provider_keys(providers) if providers else None
    cte, params = _deduped_events_cte(start, end, provider_keys)
    sql = cte + """
        SELECT
            provider_norm,
            model_name,
            COUNT(*) AS calls,
            SUM(input_tokens_norm) AS input_tokens,
            SUM(output_tokens_norm) AS output_tokens,
            SUM(cache_read_tokens_norm) AS cache_read_tokens,
            SUM(cache_creation_tokens_norm) AS cache_creation_tokens,
            SUM(cached_input_tokens_norm) AS cached_input_tokens,
            SUM(reasoning_tokens_norm) AS reasoning_tokens,
            SUM(total_tokens_norm) AS total_tokens,
            SUM(cost_estimate_usd_norm) AS cost_estimate_usd,
            SUM(unknown_pricing_flag) AS unknown_pricing_events
        FROM deduped
        GROUP BY provider_norm, model_name
    """

    by_model: dict[str, dict] = {}
    total = _empty_stats()
    for row in conn.execute(sql, params):
        (
            provider,
            model,
            calls,
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_creation_tokens,
            cached_input_tokens,
            reasoning_tokens,
            total_tokens,
            cost_estimate_usd,
            unknown_pricing_events,
        ) = row
        stats = _stats_from_aggregate_row(
            calls,
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_creation_tokens,
            cached_input_tokens,
            reasoning_tokens,
            total_tokens,
            cost_estimate_usd,
            unknown_pricing_events,
        )
        stats["provider"] = provider
        stats["model"] = model
        by_model[_provider_model_key(str(provider), str(model))] = stats
        _add_stats(total, stats)

    return by_model, total


def aggregate_totals(
    conn: sqlite3.Connection,
    start: date,
    end: date,
    providers: list[str] | None = None,
) -> dict:
    """SQL-level totals matching summary.summarize_by_model(...)[1]."""
    if start > end:
        return _empty_stats()

    provider_keys = _provider_keys(providers) if providers else None
    cte, params = _deduped_events_cte(start, end, provider_keys)
    sql = cte + """
        SELECT
            COUNT(*) AS calls,
            SUM(input_tokens_norm) AS input_tokens,
            SUM(output_tokens_norm) AS output_tokens,
            SUM(cache_read_tokens_norm) AS cache_read_tokens,
            SUM(cache_creation_tokens_norm) AS cache_creation_tokens,
            SUM(cached_input_tokens_norm) AS cached_input_tokens,
            SUM(reasoning_tokens_norm) AS reasoning_tokens,
            SUM(total_tokens_norm) AS total_tokens,
            SUM(cost_estimate_usd_norm) AS cost_estimate_usd,
            SUM(unknown_pricing_flag) AS unknown_pricing_events
        FROM deduped
    """
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return _empty_stats()
    return _stats_from_aggregate_row(*row)


def aggregate_by_provider(
    conn: sqlite3.Connection,
    start: date,
    end: date,
) -> dict[str, dict]:
    """SQL-level GROUP BY provider. Returns dict[provider] -> stats dict."""
    if start > end:
        return {}

    cte, params = _deduped_events_cte(start, end, None)
    sql = cte + """
        SELECT
            provider_norm,
            model_name,
            COUNT(*) AS calls,
            SUM(input_tokens_norm) AS input_tokens,
            SUM(output_tokens_norm) AS output_tokens,
            SUM(cache_read_tokens_norm) AS cache_read_tokens,
            SUM(cache_creation_tokens_norm) AS cache_creation_tokens,
            SUM(cached_input_tokens_norm) AS cached_input_tokens,
            SUM(reasoning_tokens_norm) AS reasoning_tokens,
            SUM(total_tokens_norm) AS total_tokens,
            SUM(cost_estimate_usd_norm) AS cost_estimate_usd,
            SUM(unknown_pricing_flag) AS unknown_pricing_events
        FROM deduped
        GROUP BY provider_norm, model_name
    """

    by_provider: dict[str, dict] = {}
    for row in conn.execute(sql, params):
        (
            provider,
            model,
            calls,
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_creation_tokens,
            cached_input_tokens,
            reasoning_tokens,
            total_tokens,
            cost_estimate_usd,
            unknown_pricing_events,
        ) = row
        key = PROVIDER_KEYS.get(str(provider), str(provider))
        if key not in by_provider:
            by_provider[key] = _empty_stats()
            by_provider[key]["provider"] = provider
            by_provider[key]["models"] = {}
            by_provider[key]["origins"] = {}
        stats = _stats_from_aggregate_row(
            calls,
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_creation_tokens,
            cached_input_tokens,
            reasoning_tokens,
            total_tokens,
            cost_estimate_usd,
            unknown_pricing_events,
        )
        _add_stats(by_provider[key], stats)
        by_provider[key]["models"][model] = by_provider[key]["models"].get(model, 0) + _as_int(calls)

        origin = _aggregate_origin(str(provider))
        if origin is not None:
            origins = by_provider[key]["origins"]
            if origin not in origins:
                origins[origin] = _empty_stats()
            _add_stats(origins[origin], stats)

    return by_provider


def build(events_dir: Path, *, providers: list[str] | None = None) -> sqlite3.Connection:
    """Build an in-memory SQLite cache from JSONL events + tasks.json."""
    conn, _meta = build_with_meta(events_dir, providers=providers)
    return conn


def build_with_meta(
    events_dir: Path,
    *,
    providers: list[str] | None = None,
) -> tuple[sqlite3.Connection, dict]:
    """Same as build(), plus counts and timestamp metadata."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    _create_schema(conn)

    provider_keys = _provider_keys(providers)
    events_count = 0
    for provider_key in provider_keys:
        filename, default_provider = PROVIDER_FILES[provider_key]
        events_count += _load_event_file(
            conn,
            events_dir / filename,
            provider_key,
            default_provider,
        )

    tasks_count = _load_tasks(conn, events_dir / "tasks.json")
    bus_tasks_count = _load_bus_events(conn, events_dir / "bus-events.jsonl")
    conn.commit()
    return conn, {
        "events": events_count,
        "tasks": tasks_count,
        "bus_tasks": bus_tasks_count,
        "built_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def read_events(
    conn: sqlite3.Connection,
    start: date,
    end: date,
    providers: list[str] | None = None,
) -> list[dict]:
    """Mirror of tracker.summary.read_events() against the SQLite cache."""
    if start > end:
        return []

    provider_keys = _provider_keys(providers) if providers else None
    where = []
    params: list[object] = []

    lower = _safe_date_offset(start, -1).isoformat()
    upper = _safe_date_offset(end, 2).isoformat()
    where.append("ts >= ? AND ts < ?")
    params.extend([lower, upper])

    if provider_keys:
        placeholders = ",".join("?" for _ in provider_keys)
        where.append(f"provider IN ({placeholders})")
        params.extend(provider_keys)

    sql = "SELECT provider, raw_json FROM events"
    if where:
        sql += " WHERE " + " AND ".join(where)

    rows: list[tuple[dict, str]] = []
    for provider_key, raw_json in conn.execute(sql, params):
        try:
            event = json.loads(raw_json)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        ts = _parse_event_ts(event.get("ts"))
        if ts is None or not (start <= ts.astimezone().date() <= end):
            continue
        rows.append((event, str(provider_key)))

    events = _dedupe_events(rows)
    events.sort(key=lambda item: _event_sort_ts(item[0]))
    return [event for event, _provider_key in events]


def read_events_fast(
    conn: sqlite3.Connection,
    start: date | None = None,
    end: date | None = None,
    providers: list[str] | None = None,
) -> list[dict]:
    """Fast path: assemble event dicts from column values directly.

    Returns dicts with the documented schema fields (no raw_json). For callers
    that need full event payload, use read_events() which decodes raw_json.
    """
    if start is not None and end is not None and start > end:
        return []

    provider_keys = _provider_keys(providers) if providers else None
    where = []
    params: list[object] = []

    if start is not None:
        where.append("ts >= ?")
        params.append(_safe_date_offset(start, -1).isoformat())
    if end is not None:
        where.append("ts < ?")
        params.append(_safe_date_offset(end, 2).isoformat())

    if provider_keys:
        placeholders = ",".join("?" for _ in provider_keys)
        where.append(f"provider IN ({placeholders})")
        params.extend(provider_keys)

    sql = """
        SELECT
            provider, ts, session_id, message_uuid, model, input_tokens,
            output_tokens, cache_read_tokens, cache_creation_tokens,
            total_tokens, cost_estimate_usd, duration_ms, working_dir,
            tool_uses, stop_reason,
            event_id, tracking_run_id, cached_input_tokens, reasoning_tokens,
            exit_code, json_provider
        FROM events
    """
    if where:
        sql += " WHERE " + " AND ".join(where)

    rows: list[tuple[dict, str, float]] = []
    for row in conn.execute(sql, params):
        (
            provider_key,
            ts,
            session_id,
            message_uuid,
            model,
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_creation_tokens,
            total_tokens,
            cost_estimate_usd,
            duration_ms,
            working_dir,
            tool_uses,
            stop_reason,
            event_id,
            tracking_run_id,
            cached_input_tokens,
            reasoning_tokens,
            exit_code,
            json_provider,
        ) = row

        parsed_ts = _parse_event_ts(ts)
        if parsed_ts is None:
            continue
        event_date = parsed_ts.astimezone().date()
        if start is not None and event_date < start:
            continue
        if end is not None and event_date > end:
            continue

        # DeepSeek audit MED #3: surface the JSONL "provider" verbatim so
        # event["provider"] matches what the slow path returns. Falls back
        # to the file's default mapping when the JSONL omits the key.
        provider_default = PROVIDER_FILES.get(str(provider_key), ("", "anthropic"))[1]
        event = {
            "provider": json_provider or provider_default,
            "ts": ts,
            "session_id": session_id,
            "message_uuid": message_uuid,
            "model": model,
            "input_tokens": input_tokens if input_tokens is not None else 0,
            "output_tokens": output_tokens if output_tokens is not None else 0,
            "cache_read_tokens": cache_read_tokens if cache_read_tokens is not None else 0,
            "cache_creation_tokens": cache_creation_tokens if cache_creation_tokens is not None else 0,
            "total_tokens": total_tokens if total_tokens is not None else 0,
            # Preserve NULL → None semantic for cost so summarize can detect
            # missing-pricing events (DeepSeek HIGH #1 on PR #81). Consumers
            # using .get() with default still work for the zero-cost path.
            "cost_estimate_usd": cost_estimate_usd,
            "duration_ms": duration_ms if duration_ms is not None else 0,
            "working_dir": working_dir,
            "tool_uses": tool_uses if tool_uses is not None else 0,
            "stop_reason": stop_reason,
            # DeepSeek audit HIGH #1+2: fields slow-path dedup compares.
            # These were silently absent in the fast-path dict, so events
            # differing only in these fields collapsed to one row, and
            # events sharing event_id/tracking_run_id were treated as
            # distinct. Both behaviours diverged from the slow path.
            "event_id": event_id,
            "tracking_run_id": tracking_run_id,
            "cached_input_tokens": cached_input_tokens if cached_input_tokens is not None else 0,
            "reasoning_tokens": reasoning_tokens if reasoning_tokens is not None else 0,
            "exit_code": exit_code,
        }
        rows.append((event, str(provider_key), parsed_ts.timestamp()))

    events = _dedupe_fast_events(rows)
    events.sort(key=lambda item: item[2])
    return [event for event, _provider_key, _sort_ts in events]


def _deduped_events_cte(
    start: date,
    end: date,
    provider_keys: list[str] | None,
) -> tuple[str, list[object]]:
    where = ["event_date >= ? AND event_date <= ?"]
    params: list[object] = [start.isoformat(), end.isoformat()]
    if provider_keys:
        placeholders = ",".join("?" for _ in provider_keys)
        where.append(f"provider IN ({placeholders})")
        params.extend(provider_keys)

    # DeepSeek audit #60 follow-up: dedupe before GROUP BY so aggregate
    # counts match read_events_fast() + summary.summarize_by_model().
    cte = f"""
        WITH filtered AS (
            SELECT
                id,
                provider_norm,
                model_name,
                COALESCE(input_tokens, 0) AS input_tokens_norm,
                COALESCE(output_tokens, 0) AS output_tokens_norm,
                COALESCE(aggregate_cache_read_tokens, 0) AS cache_read_tokens_norm,
                COALESCE(cache_creation_tokens, 0) AS cache_creation_tokens_norm,
                COALESCE(cached_input_tokens, 0) AS cached_input_tokens_norm,
                COALESCE(reasoning_tokens, 0) AS reasoning_tokens_norm,
                COALESCE(aggregate_total_tokens, 0) AS total_tokens_norm,
                COALESCE(cost_estimate_usd, 0.0) AS cost_estimate_usd_norm,
                -- DeepSeek HIGH #1 on PR #81: NULL cost = missing pricing in source.
                CASE WHEN cost_estimate_usd IS NULL THEN 1 ELSE 0 END
                    AS unknown_pricing_flag,
                dedupe_group
            FROM events
            WHERE {" AND ".join(where)}
        ),
        keep AS (
            SELECT MIN(id) AS id
            FROM filtered
            GROUP BY dedupe_group
        ),
        deduped AS (
            SELECT filtered.*
            FROM filtered
            JOIN keep USING (id)
        )
    """
    return cte, params


def _empty_stats() -> dict:
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


def _stats_from_aggregate_row(
    calls: object,
    input_tokens: object,
    output_tokens: object,
    cache_read_tokens: object,
    cache_creation_tokens: object,
    cached_input_tokens: object,
    reasoning_tokens: object,
    total_tokens: object,
    cost_estimate_usd: object,
    unknown_pricing_events: object = 0,
) -> dict:
    stats = _empty_stats()
    stats["calls"] = _as_int(calls)
    stats["input_tokens"] = _as_int(input_tokens)
    stats["output_tokens"] = _as_int(output_tokens)
    stats["cache_read_tokens"] = _as_int(cache_read_tokens)
    stats["cache_creation_tokens"] = _as_int(cache_creation_tokens)
    stats["cached_input_tokens"] = _as_int(cached_input_tokens)
    stats["reasoning_tokens"] = _as_int(reasoning_tokens)
    stats["total_tokens"] = _as_int(total_tokens)
    stats["cost_estimate_usd"] = _as_float(cost_estimate_usd)
    stats["api_equivalent_cost_usd"] = stats["cost_estimate_usd"]
    # DeepSeek HIGH #1 on PR #81: count events whose JSONL source had no
    # cost_estimate_usd key. summarize_by_model's add_event uses this to
    # flag pricing-table gaps; the aggregate path must report the same.
    stats["unknown_pricing_events"] = _as_int(unknown_pricing_events)
    return stats


def _add_stats(target: dict, source: dict) -> None:
    for key in (
        "calls",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "cached_input_tokens",
        "reasoning_tokens",
        "total_tokens",
        "unknown_pricing_events",
    ):
        target[key] += _as_int(source.get(key))
    cost = _as_float(source.get("cost_estimate_usd"))
    target["cost_estimate_usd"] += cost
    target["api_equivalent_cost_usd"] += cost


def _provider_model_key(provider: str, model: str) -> str:
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


def _aggregate_origin(provider: str) -> str | None:
    if provider == "openai":
        return "headless"
    if provider == "openrouter":
        return "openclaw"
    if provider == "opencode":
        return "opencode"
    return None


def _normalize_event_provider(provider: str | None) -> str:
    value = str(provider or "").lower()
    if value in {"openai", "openai_codex", "codex"}:
        return "openai"
    if value in {"openrouter", "openrouter_openclaw", "openclaw"}:
        return "openrouter"
    if value in {"opencode", "opencode_openrouter", "openrouter_opencode"}:
        return "opencode"
    return "anthropic"


def _normalize_dedupe_provider(json_provider: str | None, default_provider: str) -> str:
    value = str(json_provider or "").lower()
    if value in {"openai", "openai_codex", "codex"}:
        return "openai"
    if value in {"openrouter", "openrouter_openclaw", "openclaw"}:
        return "openrouter"
    if value in {"opencode", "opencode_openrouter", "openrouter_opencode"}:
        return "opencode"
    return default_provider


def _model_name(model: object) -> str:
    if isinstance(model, str) and model:
        return model
    return "unknown"


def _event_dedupe_group(
    event: dict,
    default_provider: str,
    json_provider: str | None,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int,
    reasoning_tokens: int,
    total_tokens: int,
    exit_code: int | None,
) -> str:
    event_id = event.get("event_id") or event.get("tracking_run_id")
    if isinstance(event_id, str) and event_id:
        return "event_id:" + event_id

    key = (
        _normalize_dedupe_provider(json_provider, default_provider),
        event.get("session_id"),
        event.get("ts"),
        event.get("model"),
        input_tokens,
        cached_input_tokens,
        output_tokens,
        reasoning_tokens,
        total_tokens,
        exit_code,
    )
    return "legacy:" + json.dumps(key, ensure_ascii=False, separators=(",", ":"))


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE events (
            id INTEGER PRIMARY KEY,
            provider TEXT NOT NULL,
            ts TEXT NOT NULL,
            event_date TEXT NOT NULL,
            session_id TEXT,
            message_uuid TEXT,
            model TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_creation_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            -- DeepSeek audit HIGH #1 on PR #81: nullable. NULL means the
            -- source JSONL did not include cost_estimate_usd at all, which
            -- is the signal summarize uses to increment unknown_pricing.
            cost_estimate_usd REAL,
            duration_ms INTEGER DEFAULT 0,
            working_dir TEXT,
            tool_uses INTEGER DEFAULT 0,
            stop_reason TEXT,
            -- DeepSeek audit on #60 (HIGH 1+2): dedup parity with slow path.
            -- event_id/tracking_run_id are the primary dedup keys; the legacy
            -- token/exit_code fields participate in the fallback key when
            -- event_id is absent.
            event_id TEXT,
            tracking_run_id TEXT,
            cached_input_tokens INTEGER DEFAULT 0,
            reasoning_tokens INTEGER DEFAULT 0,
            exit_code INTEGER,
            -- The provider field stored on disk in the JSONL (vs. provider_key
            -- which is the file tag). Slow path returns this verbatim; fast
            -- path now does too. Falls back to default_provider when absent.
            json_provider TEXT,
            provider_norm TEXT NOT NULL,
            model_name TEXT NOT NULL,
            aggregate_cache_read_tokens INTEGER DEFAULT 0,
            aggregate_total_tokens INTEGER DEFAULT 0,
            dedupe_group TEXT NOT NULL,
            raw_json TEXT NOT NULL
        );
        CREATE INDEX idx_events_ts ON events(ts);
        CREATE INDEX idx_events_date ON events(event_date);
        CREATE INDEX idx_events_date_group ON events(event_date, dedupe_group, id);
        CREATE INDEX idx_events_session ON events(session_id);
        CREATE INDEX idx_events_provider ON events(provider);

        CREATE TABLE tasks (
            session_id TEXT PRIMARY KEY,
            brief_description TEXT,
            ai_baseline_hours REAL,
            human_corrected_hours REAL,
            estimation_confidence TEXT,
            needs_manual_review INTEGER,
            profanity_count INTEGER,
            mood_score REAL,
            estimated_at TEXT,
            transcript_path TEXT,
            raw_json TEXT NOT NULL
        );

        CREATE TABLE bus_tasks (
            id INTEGER PRIMARY KEY,
            ts TEXT NOT NULL,
            task_id TEXT,
            kind TEXT,
            transition TEXT NOT NULL,
            exec_id TEXT,
            target_host TEXT,
            issue_number INTEGER,
            lease_seconds INTEGER DEFAULT 0,
            raw_json TEXT NOT NULL
        );
        CREATE INDEX idx_bus_tasks_task ON bus_tasks(task_id);
        CREATE INDEX idx_bus_tasks_ts ON bus_tasks(ts);
        """
    )


def _load_event_file(
    conn: sqlite3.Connection,
    path: Path,
    provider_key: str,
    default_provider: str,
) -> int:
    if not path.exists():
        return 0

    count = 0
    with path.open("r", encoding="utf-8") as source:
        for line_no, line in enumerate(source, 1):
            raw = line.rstrip("\n")
            if not raw.strip():
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError as exc:
                _log(f"{path.name}:{line_no}: corrupt JSON skipped: {exc}")
                continue
            if not isinstance(event, dict) or event.get("model") == "<synthetic>":
                continue
            ts = _parse_event_ts(event.get("ts"))
            if ts is None:
                continue

            event_for_columns = dict(event)
            event_for_columns.setdefault("provider", default_provider)
            # DeepSeek audit on #60: project the fields slow-path dedup uses
            # into columns so fast-path dedup can mirror it without raw_json.
            exit_code = event.get("exit_code")
            exit_code_int = _as_int(exit_code) if exit_code is not None else None
            json_provider = event.get("provider") if isinstance(event.get("provider"), str) else None
            input_tokens = _as_int(event.get("input_tokens"))
            output_tokens = _as_int(event.get("output_tokens"))
            cache_read_tokens = _as_int(event.get("cache_read_tokens"))
            cached_input_tokens = _as_int(event.get("cached_input_tokens"))
            reasoning_tokens = _as_int(event.get("reasoning_tokens"))
            total_tokens = _as_int(event.get("total_tokens"))
            provider_norm = _normalize_event_provider(json_provider or default_provider)
            model_name = _model_name(event.get("model"))
            aggregate_cache_read_tokens = cache_read_tokens + cached_input_tokens
            aggregate_total_tokens = total_tokens or (
                input_tokens + cached_input_tokens + output_tokens + reasoning_tokens
            )
            dedupe_group = _event_dedupe_group(
                event,
                default_provider,
                json_provider,
                input_tokens,
                output_tokens,
                cached_input_tokens,
                reasoning_tokens,
                total_tokens,
                exit_code_int,
            )
            conn.execute(
                """
                INSERT INTO events (
                    provider, ts, event_date, session_id, message_uuid, model, input_tokens,
                    output_tokens, cache_read_tokens, cache_creation_tokens,
                    total_tokens, cost_estimate_usd, duration_ms, working_dir,
                    tool_uses, stop_reason,
                    event_id, tracking_run_id, cached_input_tokens,
                    reasoning_tokens, exit_code, json_provider,
                    provider_norm, model_name, aggregate_cache_read_tokens,
                    aggregate_total_tokens, dedupe_group,
                    raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?)
                """,
                (
                    provider_key,
                    event["ts"],
                    ts.astimezone().date().isoformat(),
                    event.get("session_id"),
                    event.get("message_uuid"),
                    event.get("model"),
                    input_tokens,
                    output_tokens,
                    cache_read_tokens,
                    _as_int(event.get("cache_creation_tokens")),
                    total_tokens,
                    # DeepSeek audit HIGH #1 on PR #81: preserve NULL when the
                    # source JSONL has no cost_estimate_usd key, so summarize
                    # can distinguish "missing pricing" (→ unknown_pricing
                    # increment) from "zero cost" (e.g. cache-only tokens at 0
                    # rate). The raw_json loop path sees a missing key as None
                    # via dict.get; mirror that semantic here.
                    (_as_float(event["cost_estimate_usd"])
                     if event.get("cost_estimate_usd") is not None
                     else None),
                    _as_int(event.get("duration_ms")),
                    event.get("working_dir"),
                    _as_int(event.get("tool_uses")),
                    event.get("stop_reason"),
                    event.get("event_id") if isinstance(event.get("event_id"), str) else None,
                    event.get("tracking_run_id") if isinstance(event.get("tracking_run_id"), str) else None,
                    cached_input_tokens,
                    reasoning_tokens,
                    exit_code_int,
                    json_provider,
                    provider_norm,
                    model_name,
                    aggregate_cache_read_tokens,
                    aggregate_total_tokens,
                    dedupe_group,
                    raw,
                ),
            )
            count += 1
    return count


def _load_tasks(conn: sqlite3.Connection, path: Path) -> int:
    if not path.exists():
        return 0
    try:
        with path.open("r", encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, json.JSONDecodeError) as exc:
        _log(f"{path.name}: corrupt JSON skipped: {exc}")
        return 0
    if not isinstance(data, dict):
        return 0

    count = 0
    for session_id, entry in data.items():
        if not isinstance(session_id, str) or not isinstance(entry, dict):
            continue
        raw_json = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        conn.execute(
            """
            INSERT OR REPLACE INTO tasks (
                session_id, brief_description, ai_baseline_hours,
                human_corrected_hours, estimation_confidence,
                needs_manual_review, profanity_count, mood_score,
                estimated_at, transcript_path, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                entry.get("brief_description"),
                _as_float_or_none(entry.get("ai_baseline_hours")),
                _as_float_or_none(entry.get("human_corrected_hours")),
                entry.get("estimation_confidence"),
                _bool_int_or_none(entry.get("needs_manual_review")),
                _as_int_or_none(entry.get("profanity_count")),
                _as_float_or_none(entry.get("mood_score")),
                entry.get("estimated_at"),
                entry.get("transcript_path"),
                raw_json,
            ),
        )
        count += 1
    return count


def _load_bus_events(conn: sqlite3.Connection, path: Path) -> int:
    if not path.exists():
        return 0

    count = 0
    with path.open("r", encoding="utf-8") as source:
        for line_no, line in enumerate(source, 1):
            raw = line.rstrip("\n")
            if not raw.strip():
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError as exc:
                _log(f"{path.name}:{line_no}: corrupt JSON skipped: {exc}")
                continue
            if not isinstance(event, dict):
                continue
            ts = _parse_event_ts(event.get("ts"))
            transition = event.get("transition")
            if ts is None or not isinstance(transition, str) or not transition:
                continue
            conn.execute(
                """
                INSERT INTO bus_tasks (
                    ts, task_id, kind, transition, exec_id, target_host,
                    issue_number, lease_seconds, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["ts"],
                    event.get("task_id"),
                    event.get("kind"),
                    transition,
                    event.get("exec_id"),
                    event.get("target_host"),
                    _as_int_or_none(event.get("issue_number")),
                    _as_int(event.get("lease_seconds")),
                    raw,
                ),
            )
            count += 1
    return count


def _provider_keys(providers: list[str] | None) -> list[str]:
    if not providers:
        return list(PROVIDER_FILES)

    keys = []
    for provider in providers:
        key = PROVIDER_ALIASES.get(str(provider).lower(), str(provider).lower())
        if key in PROVIDER_FILES and key not in keys:
            keys.append(key)
    return keys


def _parse_event_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _event_provider(event: dict, provider_key: str) -> str:
    provider = str(event.get("provider") or "").lower()
    if provider in {"openai", "openai_codex", "codex"}:
        return "openai"
    if provider in {"openrouter", "openrouter_openclaw", "openclaw"}:
        return "openrouter"
    if provider in {"opencode", "opencode_openrouter", "openrouter_opencode"}:
        return "opencode"
    return PROVIDER_FILES.get(provider_key, ("", "anthropic"))[1]


def _dedupe_events(rows: list[tuple[dict, str]]) -> list[tuple[dict, str]]:
    deduped = []
    seen_event_ids = set()
    seen_legacy = set()
    for event, provider_key in rows:
        event_id = event.get("event_id") or event.get("tracking_run_id")
        if isinstance(event_id, str) and event_id:
            if event_id in seen_event_ids:
                continue
            seen_event_ids.add(event_id)
        else:
            legacy_key = (
                _event_provider(event, provider_key),
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
            if legacy_key in seen_legacy:
                continue
            seen_legacy.add(legacy_key)
        deduped.append((event, provider_key))
    return deduped


def _dedupe_fast_events(rows: list[tuple[dict, str, float]]) -> list[tuple[dict, str, float]]:
    """Mirror `_dedupe_events` two-tier logic (primary by event_id /
    tracking_run_id, fallback by legacy key). DeepSeek audit HIGH #1+2 on
    PR #68 — the previous single-tier legacy-only dedup diverged from the
    slow path on two fronts: events sharing event_id were not deduped, and
    events differing in cached_input_tokens/reasoning_tokens/exit_code
    incorrectly collapsed because those fields were always None in the
    fast-path dict.
    """
    deduped = []
    seen_event_ids: set[str] = set()
    seen_legacy: set[tuple] = set()
    for event, provider_key, sort_ts in rows:
        event_id = event.get("event_id") or event.get("tracking_run_id")
        if isinstance(event_id, str) and event_id:
            if event_id in seen_event_ids:
                continue
            seen_event_ids.add(event_id)
        else:
            legacy_key = (
                _event_provider(event, provider_key),
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
            if legacy_key in seen_legacy:
                continue
            seen_legacy.add(legacy_key)
        deduped.append((event, provider_key, sort_ts))
    return deduped


def _event_sort_ts(event: dict) -> float:
    ts = _parse_event_ts(event.get("ts"))
    return ts.timestamp() if ts is not None else 0.0


def _safe_date_offset(value: date, days: int) -> date:
    try:
        return value + timedelta(days=days)
    except OverflowError:
        return date.min if days < 0 else date.max


def _as_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_int_or_none(value: object) -> int | None:
    if value is None:
        return None
    return _as_int(value)


def _as_float(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _as_float_or_none(value: object) -> float | None:
    if value is None:
        return None
    return _as_float(value)


def _bool_int_or_none(value: object) -> int | None:
    if value is None:
        return None
    return 1 if bool(value) else 0


def _log(message: str) -> None:
    print(f"[readmodel] {message}", file=sys.stderr)
