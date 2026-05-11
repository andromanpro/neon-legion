#!/usr/bin/env python
"""Backfill OpenClaw session usage into tracker/openclaw-events.jsonl.

OpenClaw session JSONL files already contain provider/model usage and exact
OpenRouter costs. This importer copies only accounting metadata, not prompts or
assistant text, so OpenClaw's own reasoning can be tracked separately from any
Codex jobs it launches through the bridge.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from tools import config as cfg  # noqa: E402

TRACKER_DIR = PROJECT_ROOT / "tracker"
EVENTS_FILE = TRACKER_DIR / "openclaw-events.jsonl"
def _default_openclaw_sessions() -> Path:
    """OpenClaw session-store default. Override via OPENCLAW_SESSIONS_ROOT.

    OpenClaw stores per-agent session JSONLs under:
        <openclaw-data>/.openclaw/agents/<agent-name>/sessions/

    If you run OpenClaw in Docker and mount its data dir on a SMB share,
    set OPENCLAW_SESSIONS_ROOT to the mounted path. Otherwise this script
    looks for a local fallback at ~/.openclaw/agents/main/sessions.
    """
    env = cfg.get_legacy_env("OPENCLAW_SESSIONS_ROOT")
    if env:
        return Path(env)
    return Path.home() / ".openclaw" / "agents" / "main" / "sessions"


DEFAULT_SESSIONS_ROOT = _default_openclaw_sessions()
BASE_SESSION_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\.jsonl$"
)


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions-root", type=Path, default=DEFAULT_SESSIONS_ROOT)
    parser.add_argument("--events-file", type=Path, default=EVENTS_FILE)
    parser.add_argument("--since", help="Only import events at/after YYYY-MM-DD.")
    parser.add_argument("--dry-run", action="store_true", help="Show counts without writing.")
    return parser.parse_args(argv)


def parse_ts(value: object) -> datetime | None:
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


def normalize_provider(value: object, fallback: str = "openrouter") -> str:
    provider = str(value or "").strip().lower()
    if provider in {"openrouter", "openrouter_openclaw", "openclaw"}:
        return "openrouter"
    return provider or fallback


def usage_cost_total(cost: object) -> float:
    if isinstance(cost, dict):
        return round(as_float(cost.get("total")), 10)
    return 0.0


def usage_numbers(usage: dict) -> dict[str, int | float]:
    input_tokens = as_int(usage.get("input"))
    output_tokens = as_int(usage.get("output"))
    cache_read_tokens = as_int(usage.get("cacheRead"))
    cache_creation_tokens = as_int(usage.get("cacheWrite"))
    total_tokens = as_int(usage.get("totalTokens"))
    if total_tokens <= 0:
        total_tokens = input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "total_tokens": total_tokens,
        "cost_estimate_usd": usage_cost_total(usage.get("cost")),
    }


def has_billable_or_token_usage(numbers: dict[str, int | float]) -> bool:
    return (
        as_int(numbers.get("input_tokens"))
        + as_int(numbers.get("output_tokens"))
        + as_int(numbers.get("cache_read_tokens"))
        + as_int(numbers.get("cache_creation_tokens"))
        + as_int(numbers.get("total_tokens"))
    ) > 0 or as_float(numbers.get("cost_estimate_usd")) > 0


def event_status(stop_reason: object) -> tuple[int, bool]:
    reason = str(stop_reason or "").lower()
    if reason in {"error", "aborted", "cancelled", "canceled"}:
        return 1, True
    return 0, False


def event_source_from_runtime_context(content: object) -> str:
    text = content if isinstance(content, str) else ""
    if "telegram:" in text:
        return "telegram"
    if "discord:" in text:
        return "discord"
    if "whatsapp:" in text:
        return "whatsapp"
    return "workspace"


def existing_keys(path: Path) -> tuple[set[str], set[tuple]]:
    event_ids: set[str] = set()
    semantic: set[tuple] = set()
    if not path.exists():
        return event_ids, semantic

    with path.open("r", encoding="utf-8", errors="replace") as source:
        for line in source:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event_id = event.get("event_id") or event.get("tracking_run_id")
            if isinstance(event_id, str) and event_id:
                event_ids.add(event_id)
            semantic.add(semantic_key(event))
    return event_ids, semantic


def semantic_key(event: dict) -> tuple:
    return (
        event.get("provider") or "openrouter",
        event.get("session_id"),
        event.get("response_id"),
        event.get("model"),
        event.get("input_tokens"),
        event.get("cache_read_tokens"),
        event.get("cache_creation_tokens"),
        event.get("output_tokens"),
        event.get("total_tokens"),
    )


def openclaw_session_files(root: Path) -> list[Path]:
    return [
        path
        for path in sorted(root.glob("*.jsonl"))
        if BASE_SESSION_RE.fullmatch(path.name)
    ]


def read_session_events(path: Path, since: datetime | None) -> tuple[list[dict], int]:
    session_id = path.stem
    cwd = ""
    provider = "openrouter"
    model = "unknown"
    source = "workspace"
    events: list[dict] = []
    skipped_zero_usage = 0

    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line_no, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue

            typ = item.get("type")
            if typ == "session":
                session_id = str(item.get("id") or session_id)
                cwd = str(item.get("cwd") or cwd)
                continue

            if typ == "model_change":
                provider = normalize_provider(item.get("provider"), provider)
                model = str(item.get("modelId") or item.get("model") or model)
                continue

            if typ == "custom_message" and item.get("customType") == "openclaw.runtime-context":
                source = event_source_from_runtime_context(item.get("content"))
                continue

            if typ != "message":
                continue

            message = item.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue

            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue

            ts = parse_ts(item.get("timestamp") or message.get("timestamp"))
            if ts is None:
                continue
            if since is not None and ts < since:
                continue

            provider = normalize_provider(message.get("provider") or provider, provider)
            model = str(message.get("model") or message.get("responseModel") or model)
            numbers = usage_numbers(usage)
            if not has_billable_or_token_usage(numbers):
                skipped_zero_usage += 1
                continue

            msg_id = str(item.get("id") or message.get("responseId") or line_no)
            exit_code, partial = event_status(message.get("stopReason"))
            event = {
                "schema_version": 1,
                "event_id": f"openclaw-session-{session_id}-{msg_id}",
                "tracking_run_id": f"openclaw-session-{session_id}",
                "sequence_no": line_no,
                "ts": ts.isoformat().replace("+00:00", "Z"),
                "session_id": session_id,
                "model": model,
                "input_tokens": numbers["input_tokens"],
                "output_tokens": numbers["output_tokens"],
                "cache_read_tokens": numbers["cache_read_tokens"],
                "cache_creation_tokens": numbers["cache_creation_tokens"],
                "total_tokens": numbers["total_tokens"],
                "duration_ms": 0,
                "cost_estimate_usd": numbers["cost_estimate_usd"],
                "exit_code": exit_code,
                "partial": partial,
                "usage_captured": True,
                "working_dir": cwd,
                "provider": provider,
                "openclaw_origin": "openclaw",
                "openclaw_source": source,
                "source": "openclaw-session-backfill",
                "response_id": message.get("responseId") or "",
                "stop_reason": message.get("stopReason") or "",
                "session_file": str(path),
            }
            events.append(event)

    return events, skipped_zero_usage


def append_events(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as target:
        for event in events:
            target.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        target.flush()
        os.fsync(target.fileno())


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not args.sessions_root.exists():
        print(f"sessions root not found: {args.sessions_root}", file=sys.stderr)
        return 2

    since = None
    if args.since:
        since = datetime.fromisoformat(args.since).astimezone()

    existing_event_ids, existing_semantic = existing_keys(args.events_file)
    new_events: list[dict] = []
    scanned_files = 0
    scanned_usage_events = 0
    skipped_existing = 0
    skipped_zero_usage = 0

    for path in openclaw_session_files(args.sessions_root):
        scanned_files += 1
        events, file_skipped_zero = read_session_events(path, since)
        skipped_zero_usage += file_skipped_zero
        for event in events:
            scanned_usage_events += 1
            event_id = str(event.get("event_id") or "")
            sem_key = semantic_key(event)
            if event_id in existing_event_ids or sem_key in existing_semantic:
                skipped_existing += 1
                continue
            existing_event_ids.add(event_id)
            existing_semantic.add(sem_key)
            new_events.append(event)

    print(f"scanned_files={scanned_files}")
    print(f"usage_events={scanned_usage_events}")
    print(f"new_events={len(new_events)}")
    print(f"skipped_existing={skipped_existing}")
    print(f"skipped_zero_usage={skipped_zero_usage}")
    if not args.dry_run and new_events:
        append_events(args.events_file, new_events)
        print(f"appended={len(new_events)} to {args.events_file}")
    elif args.dry_run:
        print("dry_run=true")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
