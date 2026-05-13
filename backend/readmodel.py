#!/usr/bin/env python
"""In-memory SQLite read-model for dashboard event queries.

JSONL files under tracker/ remain canonical. This module rebuilds an in-memory
SQLite cache on each backend start and uses it only to accelerate event-window
reads. A future `bus_tasks` table belongs here once bus events are written to a
local JSONL stream.
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
}
PROVIDER_ALIASES = {
    "anthropic": "claude",
    "openai": "codex",
    "openrouter": "openclaw",
}


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
    conn.commit()
    return conn, {
        "events": events_count,
        "tasks": tasks_count,
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
            "cost_estimate_usd": cost_estimate_usd if cost_estimate_usd is not None else 0.0,
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


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE events (
            id INTEGER PRIMARY KEY,
            provider TEXT NOT NULL,
            ts TEXT NOT NULL,
            session_id TEXT,
            message_uuid TEXT,
            model TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_creation_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            cost_estimate_usd REAL DEFAULT 0,
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
            raw_json TEXT NOT NULL
        );
        CREATE INDEX idx_events_ts ON events(ts);
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
            conn.execute(
                """
                INSERT INTO events (
                    provider, ts, session_id, message_uuid, model, input_tokens,
                    output_tokens, cache_read_tokens, cache_creation_tokens,
                    total_tokens, cost_estimate_usd, duration_ms, working_dir,
                    tool_uses, stop_reason,
                    event_id, tracking_run_id, cached_input_tokens,
                    reasoning_tokens, exit_code, json_provider,
                    raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?,
                          ?)
                """,
                (
                    provider_key,
                    event["ts"],
                    event.get("session_id"),
                    event.get("message_uuid"),
                    event.get("model"),
                    _as_int(event.get("input_tokens")),
                    _as_int(event.get("output_tokens")),
                    _as_int(event.get("cache_read_tokens")),
                    _as_int(event.get("cache_creation_tokens")),
                    _as_int(event.get("total_tokens")),
                    _as_float(event.get("cost_estimate_usd")),
                    _as_int(event.get("duration_ms")),
                    event.get("working_dir"),
                    _as_int(event.get("tool_uses")),
                    event.get("stop_reason"),
                    event.get("event_id") if isinstance(event.get("event_id"), str) else None,
                    event.get("tracking_run_id") if isinstance(event.get("tracking_run_id"), str) else None,
                    _as_int(event.get("cached_input_tokens")),
                    _as_int(event.get("reasoning_tokens")),
                    exit_code_int,
                    json_provider,
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
