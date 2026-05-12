#!/usr/bin/env python
"""Generate deterministic demo telemetry for a clean neon-legion checkout.

The generator writes only local tracker demo data and never calls the network.
It refuses to overwrite non-empty tracker files unless --force is passed.
"""

import argparse
import json
import os
from pathlib import Path
import random
import secrets
import sys
from datetime import datetime, time, timedelta


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TRACKER_DIR = PROJECT_ROOT / "tracker"
SEED = 20260512

PROVIDERS = {
    "claude": {
        "file": "claude-events.jsonl",
        "provider": "anthropic",
        "weight": 0.38,
        "models": ["claude-sonnet-4-20250514", "claude-opus-4-20250514"],
        "total_tokens": (30_000, 100_000),
        "cost": (0.30, 3.00),
        "calls": (2, 5),
    },
    "codex": {
        "file": "codex-events.jsonl",
        "provider": "openai",
        "weight": 0.34,
        "models": ["gpt-5.5-codex", "gpt-5.4-codex"],
        "total_tokens": (5_000, 30_000),
        "cost": (0.05, 0.40),
        "calls": (2, 6),
    },
    "openclaw": {
        "file": "openclaw-events.jsonl",
        "provider": "openrouter",
        "weight": 0.13,
        "models": ["openrouter/anthropic/claude-sonnet-4", "openrouter/qwen/qwen3-coder"],
        "total_tokens": (8_000, 40_000),
        "cost": (0.02, 0.22),
        "calls": (2, 5),
    },
    "opencode": {
        "file": "opencode-events.jsonl",
        "provider": "opencode",
        "weight": 0.15,
        "models": ["deepseek/deepseek-v4-pro", "deepseek/deepseek-chat"],
        "total_tokens": (3_000, 25_000),
        "cost": (0.005, 0.05),
        "calls": (2, 5),
    },
}

WORKING_DIRS = [
    "/projects/neon-dashboard",
    "/projects/agent-orchestrator",
    "/projects/demo-lab",
    "/projects/local-tracker",
    "/projects/release-tools",
    "/projects/usage-analytics",
    "/projects/snapshot-pipeline",
    "/projects/cli-adapters",
]

TASK_SUMMARIES = [
    "Implemented dashboard snapshot smoke checks and fixed stale provider totals.",
    "Refined orchestrator state handling for resumable role execution.",
    "Added privacy gate fixtures for public release validation.",
    "Investigated CLI accounting drift and documented the corrected flow.",
    "Built a local demo dataset for first-run dashboard onboarding.",
    "Reviewed adapter error handling and tightened retry notes.",
    "Updated snapshot publication notes with safer public-mode guidance.",
    "Validated token aggregation across delegated coding sessions.",
    "Prepared a sanitized sample run for documentation readers.",
    "Simplified demo setup so a clean clone can render metrics quickly.",
    "Audited provider origin labels and normalized compact dashboard output.",
    "Checked task estimate coverage against active-time calculations.",
]

MOOD_ARCS = ["calm", "focused", "mixed", "blocked->clear", "curious->confident"]
SENTIMENTS = ["low", "low", "medium", "medium", "high"]


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=positive_int, default=7, help="Days of demo history to generate.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite non-empty demo targets. Intended only for disposable demo checkouts.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print planned counts without writing files.")
    parser.add_argument(
        "--tracker-dir",
        type=Path,
        default=DEFAULT_TRACKER_DIR,
        help=argparse.SUPPRESS,
    )
    return parser.parse_args()


def positive_int(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer")
    if parsed <= 0:
        raise argparse.ArgumentTypeError("--days must be greater than zero")
    return parsed


def safe_round(value, places):
    return round(float(value), places)


def split_integer(total, count, rng):
    if count <= 1:
        return [total]
    weights = [rng.randint(3, 13) for _ in range(count)]
    weight_sum = sum(weights)
    parts = []
    used = 0
    for weight in weights[:-1]:
        part = max(1, int(total * weight / weight_sum))
        parts.append(part)
        used += part
    parts.append(max(1, total - used))
    return parts


def split_cost(total, token_parts):
    token_sum = sum(token_parts) or 1
    parts = []
    used = 0.0
    for tokens in token_parts[:-1]:
        part = safe_round(total * tokens / token_sum, 6)
        parts.append(part)
        used += part
    parts.append(safe_round(max(0.0, total - used), 6))
    return parts


def choose_provider(rng):
    marker = rng.random()
    cumulative = 0.0
    for name, cfg in PROVIDERS.items():
        cumulative += cfg["weight"]
        if marker <= cumulative:
            return name
    return "opencode"


def provider_sequence(total, rng):
    names = [choose_provider(rng) for _ in range(total)]
    for index, provider in enumerate(PROVIDERS):
        if provider not in names:
            names[index % len(names)] = provider
    rng.shuffle(names)
    return names


def sessions_for_day(day, rng):
    if day.weekday() >= 5:
        return rng.randint(30, 45)
    return rng.randint(52, 78)


def session_start(day, now, rng):
    today = now.date()
    if day == today:
        max_start = max(1, now.hour * 60 + now.minute - 12)
        if max_start >= 9 * 60:
            start_minute = int(rng.triangular(8 * 60, max_start, min(13 * 60, max_start)))
        else:
            start_minute = rng.randint(1, max_start)
    else:
        weekend = day.weekday() >= 5
        if rng.random() < (0.12 if weekend else 0.18):
            ranges = [(7 * 60, 9 * 60), (18 * 60, 23 * 60)]
            lo, hi = ranges[rng.randrange(len(ranges))]
            start_minute = rng.randint(lo, hi)
        elif weekend:
            start_minute = int(rng.triangular(10 * 60, 17 * 60, 13 * 60))
        else:
            start_minute = int(rng.triangular(8 * 60, 18 * 60, 13 * 60))

    hour = min(start_minute // 60, 23)
    minute = min(start_minute % 60, 59)
    return datetime.combine(day, time(hour, minute), tzinfo=now.tzinfo)


def token_shape(provider, total, rng):
    if provider == "claude":
        cache_read = int(total * rng.uniform(0.30, 0.55))
        cache_creation = int(total * rng.uniform(0.03, 0.09))
        output = int(total * rng.uniform(0.06, 0.14))
        input_tokens = max(1, total - cache_read - cache_creation - output)
        return input_tokens, output, 0, cache_read, cache_creation
    if provider == "codex":
        cached = int(total * rng.uniform(0.08, 0.22))
        reasoning = int(total * rng.uniform(0.10, 0.28))
        output = int(total * rng.uniform(0.08, 0.20))
        input_tokens = max(1, total - cached - reasoning - output)
        return input_tokens, output, reasoning, cached, 0
    if provider == "opencode":
        cache_read = int(total * rng.uniform(0.04, 0.14))
        cache_creation = int(total * rng.uniform(0.01, 0.05))
        reasoning = int(total * rng.uniform(0.06, 0.22))
        output = int(total * rng.uniform(0.08, 0.18))
        input_tokens = max(1, total - cache_read - cache_creation - reasoning - output)
        return input_tokens, output, reasoning, cache_read, cache_creation
    cache_read = int(total * rng.uniform(0.04, 0.18))
    cache_creation = int(total * rng.uniform(0.01, 0.06))
    output = int(total * rng.uniform(0.08, 0.22))
    input_tokens = max(1, total - cache_read - cache_creation - output)
    return input_tokens, output, 0, cache_read, cache_creation


def base_event(provider, session_id, event_id, sequence_no, ts, model, tokens, cost, working_dir):
    input_tokens, output_tokens, reasoning_tokens, cache_read, cache_creation = tokens
    total_tokens = input_tokens + output_tokens + reasoning_tokens + cache_read + cache_creation
    cfg = PROVIDERS[provider]
    event = {
        "schema_version": 1,
        "event_id": event_id,
        "tracking_run_id": session_id,
        "sequence_no": sequence_no,
        "ts": ts.isoformat(timespec="milliseconds"),
        "session_id": session_id,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "duration_ms": 0,
        "cost_estimate_usd": cost,
        "exit_code": 0,
        "partial": False,
        "usage_captured": True,
        "working_dir": working_dir,
        "provider": cfg["provider"],
        "source": "demo-fake-events",
    }
    if reasoning_tokens:
        event["reasoning_tokens"] = reasoning_tokens
    if provider == "codex":
        event.update(
            {
                "cached_input_tokens": cache_read,
                "codex_json_events": 0,
                "subscription_type": "chatgpt-pro",
                "approval_mode": "on-request",
                "sandbox_mode": "workspace-write",
                "codex_origin": ["headless", "tui", "desktop"][sequence_no % 3],
                "originator": "codex_exec",
                "codex_source": "exec",
            }
        )
    elif provider == "claude":
        event.update(
            {
                "message_uuid": f"msg-{event_id}",
                "cache_read_tokens": cache_read,
                "cache_creation_tokens": cache_creation,
                "tool_uses": sequence_no % 4,
                "stop_reason": "end_turn",
            }
        )
    elif provider == "openclaw":
        event.update(
            {
                "cache_read_tokens": cache_read,
                "cache_creation_tokens": cache_creation,
                "openclaw_origin": "openclaw",
                "openclaw_source": ["workspace", "bridge", "review"][sequence_no % 3],
                "response_id": f"resp-{event_id}",
                "stop_reason": "end_turn",
            }
        )
    else:
        event.update(
            {
                "cache_read_tokens": cache_read,
                "cache_creation_tokens": cache_creation,
                "opencode_provider_id": "openrouter",
                "opencode_agent": ["reviewer", "builder", "planner"][sequence_no % 3],
                "opencode_mode": "build",
                "opencode_version": "demo",
                "stop_reason": "stop",
            }
        )
    return event


def task_for_session(session_id, first_ts, last_ts, rng):
    summary = TASK_SUMMARIES[rng.randrange(len(TASK_SUMMARIES))]
    baseline = safe_round(rng.uniform(0.8, 8.0), 2)
    frustration = safe_round(rng.uniform(0.0, 0.55), 2)
    appreciation = safe_round(rng.uniform(0.0, 0.35), 2)
    return {
        "ai_baseline_hours": baseline,
        "appreciation_score": appreciation,
        "brief_description": summary,
        "estimated_at": (last_ts + timedelta(minutes=5)).isoformat(timespec="seconds"),
        "estimation_confidence": ["high", "medium", "medium"][rng.randrange(3)],
        "first_ts": first_ts.isoformat(timespec="seconds"),
        "frustration_score": frustration,
        "human_corrected_hours": None,
        "last_ts": last_ts.isoformat(timespec="seconds"),
        "mood_arc": MOOD_ARCS[rng.randrange(len(MOOD_ARCS))],
        "needs_manual_review": False,
        "profanity_count": 0,
        "sentiment_intensity": SENTIMENTS[rng.randrange(len(SENTIMENTS))],
        "transcript_path": "",
    }


def generate(days):
    rng = random.Random(SEED + days)
    actual_now = datetime.now().astimezone()
    schedule_now = datetime.combine(actual_now.date(), time(10, 0), tzinfo=actual_now.tzinfo)
    start = actual_now.date() - timedelta(days=days - 1)
    events = {name: [] for name in PROVIDERS}
    tasks = {}
    daily = []
    session_index = 0

    for offset in range(days):
        day = start + timedelta(days=offset)
        session_total = sessions_for_day(day, rng)
        providers = provider_sequence(session_total, rng)
        daily.append({"date": day.isoformat(), "sessions": session_total})

        for provider in providers:
            session_index += 1
            cfg = PROVIDERS[provider]
            started = session_start(day, schedule_now, rng)
            call_count = rng.randint(cfg["calls"][0], cfg["calls"][1])
            session_tokens = rng.randint(cfg["total_tokens"][0], cfg["total_tokens"][1])
            session_cost = safe_round(rng.uniform(cfg["cost"][0], cfg["cost"][1]), 6)
            token_parts = split_integer(session_tokens, call_count, rng)
            cost_parts = split_cost(session_cost, token_parts)
            session_id = f"demo-{provider}-{day.strftime('%Y%m%d')}-{session_index:04d}"
            model = cfg["models"][rng.randrange(len(cfg["models"]))]
            working_dir = WORKING_DIRS[rng.randrange(len(WORKING_DIRS))]
            first_ts = started
            last_ts = started

            for sequence_no, (tokens_total, cost) in enumerate(zip(token_parts, cost_parts), start=1):
                ts = started + timedelta(seconds=(sequence_no - 1) * rng.randint(35, 95))
                last_ts = ts
                event_id = f"{session_id}-{sequence_no:02d}"
                event = base_event(
                    provider,
                    session_id,
                    event_id,
                    sequence_no,
                    ts,
                    model,
                    token_shape(provider, tokens_total, rng),
                    cost,
                    working_dir,
                )
                events[provider].append(event)

            if provider == "claude":
                tasks[session_id] = task_for_session(session_id, first_ts, last_ts, rng)

    for provider_events in events.values():
        provider_events.sort(key=lambda item: (item["ts"], item["event_id"]))
    return events, tasks, daily


def path_nonempty(path):
    try:
        return path.exists() and path.stat().st_size > 0
    except OSError:
        return False


def atomic_write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def jsonl_text(rows):
    return "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows)


def print_stats(events, tasks, daily, tracker_dir, dry_run):
    total_sessions = sum(item["sessions"] for item in daily)
    total_events = sum(len(rows) for rows in events.values())
    total_cost = sum(float(row.get("cost_estimate_usd") or 0) for rows in events.values() for row in rows)
    prefix = "dry-run: " if dry_run else ""
    print(
        f"{prefix}planned {len(daily)} days, {total_sessions} sessions, "
        f"{total_events} events, ${total_cost:.2f} estimated API cost"
    )
    for provider, rows in events.items():
        sessions = {row["session_id"] for row in rows}
        tokens = sum(int(row.get("total_tokens") or 0) for row in rows)
        cost = sum(float(row.get("cost_estimate_usd") or 0) for row in rows)
        print(
            f"- {provider}: {len(sessions)} sessions, {len(rows)} events, "
            f"{tokens:,} tokens, ${cost:.2f} -> {tracker_dir / PROVIDERS[provider]['file']}"
        )
    print(f"- tasks: {len(tasks)} Claude session summaries -> {tracker_dir / 'tasks.json'}")


def write_outputs(events, tasks, tracker_dir, force):
    skipped = []
    written = []
    if force:
        print("WARNING: --force is replacing demo tracker targets, including non-empty files.", file=sys.stderr)

    for provider, rows in events.items():
        path = tracker_dir / PROVIDERS[provider]["file"]
        if path_nonempty(path) and not force:
            print(f"refusing to overwrite non-empty {path} ({path.stat().st_size} bytes)", file=sys.stderr)
            skipped.append(str(path))
            continue
        atomic_write_text(path, jsonl_text(rows))
        written.append(str(path))
        print(f"wrote {path} ({len(rows)} events)")

    tasks_path = tracker_dir / "tasks.json"
    if path_nonempty(tasks_path) and not force:
        print(f"refusing to overwrite non-empty {tasks_path} ({tasks_path.stat().st_size} bytes)", file=sys.stderr)
        skipped.append(str(tasks_path))
    else:
        text = json.dumps(tasks, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        atomic_write_text(tasks_path, text)
        written.append(str(tasks_path))
        print(f"wrote {tasks_path} ({len(tasks)} task summaries)")

    return written, skipped


def all_schema_version_one(events):
    for rows in events.values():
        for row in rows:
            if row.get("schema_version") != 1:
                return False
    return True


def main():
    args = parse_args()
    tracker_dir = args.tracker_dir
    if not tracker_dir.is_absolute():
        tracker_dir = PROJECT_ROOT / tracker_dir
    events, tasks, daily = generate(args.days)
    print_stats(events, tasks, daily, tracker_dir, args.dry_run)

    if not all_schema_version_one(events):
        print("internal error: generated event without schema_version=1", file=sys.stderr)
        return 2

    if args.dry_run:
        print("dry-run: no files written")
        return 0

    _written, skipped = write_outputs(events, tasks, tracker_dir, args.force)
    if skipped:
        print("demo data was not fully written; remove real tracker data or use a clean clone", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
