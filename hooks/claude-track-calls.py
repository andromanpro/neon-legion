#!/usr/bin/env python
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Force UTF-8 on stdin/stdout/stderr (Windows default is cp1251).
# Without this, Cyrillic paths in `cwd` come through as mojibake when
# Claude Code's JSON arrives on a Windows console default codepage. Once
# stored in JSONL as UTF-8, the mojibake is permanent (#20). The fallback
# `errors='replace'` ensures malformed input doesn't crash the hook.
try:
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRACKER_DIR = PROJECT_ROOT / "tracker"
EVENTS_FILE = TRACKER_DIR / "claude-events.jsonl"
LAST_UUIDS_FILE = TRACKER_DIR / ".last-uuids.json"
LOCK_FILE = TRACKER_DIR / ".claude-events.lock"

# Pricing as of 2026-05-09. Values are USD per 1M tokens.
OPUS_PRICING = {"in": 15.00, "out": 75.00, "cache_read": 1.50, "cache_write": 18.75}
SONNET_PRICING = {"in": 3.00, "out": 15.00, "cache_read": 0.30, "cache_write": 3.75}
HAIKU_PRICING = {"in": 1.00, "out": 5.00, "cache_read": 0.10, "cache_write": 1.25}


def pricing_for_model(model: str) -> dict | None:
    if not model:
        return None
    if model.startswith("claude-opus-4"):
        return OPUS_PRICING
    if model.startswith("claude-sonnet-4"):
        return SONNET_PRICING
    if model.startswith("claude-haiku-4"):
        return HAIKU_PRICING
    return None


def read_hook_input() -> dict | None:
    raw = sys.stdin.read()
    if not raw.strip():
        return None

    data = json.loads(raw)
    if not isinstance(data, dict):
        return None

    session_id = data.get("session_id")
    transcript_path = data.get("transcript_path")
    working_dir = data.get("cwd")
    if not isinstance(session_id, str) or not isinstance(transcript_path, str) or not isinstance(working_dir, str):
        return None

    return {
        "session_id": session_id,
        "transcript_path": transcript_path,
        "working_dir": working_dir,
    }


def read_latest_assistant(transcript_path: str) -> tuple[dict, dict, dict] | None:
    latest = None
    path = Path(transcript_path)
    if not path.exists() or not path.is_file():
        return None

    with path.open("r", encoding="utf-8") as transcript:
        for line in transcript:
            if not line.strip():
                continue

            event = json.loads(line)
            if not isinstance(event, dict) or event.get("type") != "assistant":
                continue

            message = event.get("message")
            if not isinstance(message, dict):
                continue

            if message.get("model") == "<synthetic>":
                continue

            usage = message.get("usage")
            model = message.get("model")
            if not isinstance(usage, dict) or not isinstance(model, str) or not model:
                continue

            latest = (event, message, usage)

    return latest


def as_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int,
    cache_read_tokens: int,
) -> float | None:
    pricing = pricing_for_model(model)
    if pricing is None:
        return None

    cost = (
        input_tokens * pricing["in"]
        + output_tokens * pricing["out"]
        + cache_read_tokens * pricing["cache_read"]
        + cache_creation_tokens * pricing["cache_write"]
    ) / 1_000_000
    return round(cost, 4)


def build_event(hook_input: dict, assistant_event: dict, message: dict, usage: dict) -> dict:
    content = message.get("content")
    if not isinstance(content, list):
        content = []

    model = str(message.get("model"))
    input_tokens = as_int(usage.get("input_tokens"))
    output_tokens = as_int(usage.get("output_tokens"))
    cache_creation_tokens = as_int(usage.get("cache_creation_input_tokens"))
    cache_read_tokens = as_int(usage.get("cache_read_input_tokens"))

    return {
        "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "session_id": hook_input["session_id"],
        "message_uuid": str(assistant_event.get("uuid", "")),
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cost_estimate_usd": estimate_cost(model, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens),
        "duration_ms": 0,
        "working_dir": hook_input["working_dir"],
        "tool_uses": sum(1 for block in content if isinstance(block, dict) and block.get("type") == "tool_use"),
        "stop_reason": message.get("stop_reason", ""),
    }


def acquire_lock() -> int | None:
    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("ascii"))
        return fd
    except FileExistsError:
        return None


def release_lock(fd: int | None) -> None:
    if fd is not None:
        os.close(fd)
    try:
        LOCK_FILE.unlink()
    except OSError:
        pass


def read_last_uuids() -> dict:
    if not LAST_UUIDS_FILE.exists():
        return {}

    try:
        with LAST_UUIDS_FILE.open("r", encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def atomic_replace_text(path: Path, temp_path: Path, text: str) -> None:
    with temp_path.open("w", encoding="utf-8", newline="\n") as target:
        target.write(text)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temp_path, path)


def append_event(event: dict) -> None:
    TRACKER_DIR.mkdir(parents=True, exist_ok=True)

    fd = acquire_lock()
    if fd is None:
        return

    events_tmp = TRACKER_DIR / f".claude-events.jsonl.tmp.{os.getpid()}"
    uuids_tmp = TRACKER_DIR / f".last-uuids.json.tmp.{os.getpid()}"

    try:
        last_uuids = read_last_uuids()
        session_id = event["session_id"]
        message_uuid = event["message_uuid"]
        if last_uuids.get(session_id) == message_uuid:
            return

        existing = ""
        if EVENTS_FILE.exists():
            with EVENTS_FILE.open("r", encoding="utf-8") as source:
                existing = source.read()

        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        merged = existing
        if merged and not merged.endswith("\n"):
            merged += "\n"
        merged += line
        atomic_replace_text(EVENTS_FILE, events_tmp, merged)

        last_uuids[session_id] = message_uuid
        atomic_replace_text(
            LAST_UUIDS_FILE,
            uuids_tmp,
            json.dumps(last_uuids, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
    finally:
        for temp_path in (events_tmp, uuids_tmp):
            try:
                temp_path.unlink()
            except OSError:
                pass
        release_lock(fd)


def main() -> int:
    try:
        hook_input = read_hook_input()
        if hook_input is None:
            return 0

        latest = read_latest_assistant(hook_input["transcript_path"])
        if latest is None:
            return 0

        assistant_event, message, usage = latest
        event = build_event(hook_input, assistant_event, message, usage)
        if not event["message_uuid"]:
            return 0

        append_event(event)
    except Exception:
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
