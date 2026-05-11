#!/usr/bin/env python
"""Codex CLI tracking wrapper - Phase 1.1.

Wraps `codex` invocations, captures usage stats from `codex exec --json`,
and appends JSONL events to tracker/codex-events.jsonl. Stdout/stderr/stdin
are forwarded so the wrapped command behaves like native Codex CLI.

Usage:
    py -3.14 tracker/codex-track.py <codex args>
    py -3.14 tracker/codex-track.py exec --sandbox read-only "prompt"
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from tools import config as cfg  # noqa: E402

TRACKER_DIR = PROJECT_ROOT / "tracker"
EVENTS_FILE = TRACKER_DIR / "codex-events.jsonl"
LOCK_FILE = TRACKER_DIR / ".codex-events.lock"

# Pricing per million tokens — used to compute "what API would have cost" if
# the user paid OpenAI per token instead of via a ChatGPT subscription. These
# defaults track GPT-5.5 (gpt-5.5) rates as of 2026-Q2:
#   input          $10 / 1M
#   cached_input   $2.50 / 1M  (90% cache discount)
#   output         $30 / 1M
#   reasoning      $30 / 1M    (charged at output rate)
# Source: OpenAI pricing page, capture from 2026-05. Override per environment
# by setting OPENAI_TOKEN_PRICE_{INPUT,CACHED_INPUT,OUTPUT,REASONING} (USD per
# million tokens) before launching the wrapper. See `_load_pricing()` below.
PRICING = {
    "input": cfg.get_legacy_env("OPENAI_TOKEN_PRICE_INPUT", 10.0, float) / 1_000_000,
    "cached_input": cfg.get_legacy_env("OPENAI_TOKEN_PRICE_CACHED_INPUT", 2.5, float) / 1_000_000,
    "output": cfg.get_legacy_env("OPENAI_TOKEN_PRICE_OUTPUT", 30.0, float) / 1_000_000,
    "reasoning": cfg.get_legacy_env("OPENAI_TOKEN_PRICE_REASONING", 30.0, float) / 1_000_000,
}

LOCK_TIMEOUT_SECONDS = 10.0
STALE_LOCK_SECONDS = 120.0


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def as_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def estimate_cost(
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    reasoning_tokens: int,
) -> float:
    cost = (
        input_tokens * PRICING["input"]
        + cached_input_tokens * PRICING["cached_input"]
        + output_tokens * PRICING["output"]
        + reasoning_tokens * PRICING["reasoning"]
    )
    return round(cost, 6)


def find_codex_command() -> str | None:
    for name in ("codex.cmd", "codex.exe", "codex"):
        path = shutil.which(name)
        if path:
            return path
    return None


def has_json_flag(args: list[str]) -> bool:
    return any(arg == "--json" or arg.startswith("--json=") for arg in args)


def args_for_codex(argv: list[str]) -> list[str]:
    args = list(argv)
    if args and args[0] == "exec" and not has_json_flag(args):
        args.insert(1, "--json")
    return args


def parse_option(args: list[str], long_name: str, short_name: str | None = None) -> str | None:
    prefixes = [f"{long_name}="]
    for index, arg in enumerate(args):
        if arg == long_name and index + 1 < len(args):
            return args[index + 1]
        if short_name is not None and arg == short_name and index + 1 < len(args):
            return args[index + 1]
        for prefix in prefixes:
            if arg.startswith(prefix):
                return arg[len(prefix):]
    return None


def parse_config_value(key: str) -> str | None:
    config = Path.home() / ".codex" / "config.toml"
    if not config.exists():
        return None

    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(['\"]?)(.*?)\1\s*$")
    try:
        with config.open("r", encoding="utf-8") as source:
            for line in source:
                stripped = line.strip()
                if stripped.startswith("["):
                    break
                match = pattern.match(stripped)
                if match:
                    return match.group(2).strip()
    except OSError:
        return None
    return None


def resolve_model(args: list[str], events: list[dict]) -> str:
    for event in events:
        model = event.get("model")
        if isinstance(model, str) and model:
            return model
        item = event.get("item")
        if isinstance(item, dict):
            model = item.get("model")
            if isinstance(model, str) and model:
                return model

    arg_model = parse_option(args, "--model", "-m")
    if arg_model:
        return arg_model

    env_model = cfg.get_legacy_env("CODEX_MODEL")
    if env_model:
        return env_model

    config_model = parse_config_value("model")
    return config_model or "unknown"


def resolve_sandbox_mode(args: list[str]) -> str:
    if "--dangerously-bypass-approvals-and-sandbox" in args:
        return "danger-full-access"

    sandbox = parse_option(args, "--sandbox", "-s")
    if sandbox:
        return sandbox

    config_sandbox = parse_config_value("sandbox_mode") or parse_config_value("sandbox")
    return config_sandbox or "unknown"


def resolve_approval_mode(args: list[str]) -> str:
    approval = (
        parse_option(args, "--approval-mode")
        or parse_option(args, "--ask-for-approval")
        or cfg.get_legacy_env("CODEX_APPROVAL_MODE")
        or parse_config_value("approval_mode")
        or parse_config_value("approval_policy")
    )
    return approval or "unknown"


def subscription_type() -> str:
    if cfg.get_legacy_env("OPENAI_API_KEY") or cfg.get_legacy_env("ANTHROPIC_API_KEY"):
        return "api-key"
    return "chatgpt-pro"


def parse_json_line(line: str) -> dict | None:
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def usage_value(usage: dict, *names: str) -> int:
    for name in names:
        if name in usage:
            return as_int(usage.get(name))
    return 0


def build_event_from_codex_events(
    events: list[dict],
    exit_code: int,
    duration_ms: int,
    cwd: str,
    args: list[str],
    run_id: str,
    interrupted: bool = False,
) -> dict | None:
    if not args or args[0] != "exec":
        return None

    session_id = ""
    usage = None
    for event in events:
        for key in ("session_id", "thread_id"):
            value = event.get(key)
            if isinstance(value, str) and value:
                session_id = value
                break
        if session_id:
            break

    for event in events:
        candidate = event.get("usage")
        if isinstance(candidate, dict):
            usage = candidate

    usage_captured = usage is not None
    if not session_id:
        session_id = f"codex-missing-thread-{int(time.time() * 1000)}-{os.getpid()}"
    if usage is None:
        usage = {}

    input_tokens = usage_value(usage, "input_tokens", "prompt_tokens")
    cached_input_tokens = usage_value(
        usage,
        "cached_input_tokens",
        "cache_read_input_tokens",
        "cached_tokens",
    )
    output_tokens = usage_value(usage, "output_tokens", "completion_tokens")
    reasoning_tokens = usage_value(
        usage,
        "reasoning_tokens",
        "reasoning_output_tokens",
        "output_reasoning_tokens",
    )
    total_tokens = usage_value(usage, "total_tokens")
    if total_tokens <= 0:
        total_tokens = input_tokens + cached_input_tokens + output_tokens + reasoning_tokens

    partial = interrupted or exit_code != 0 or not usage_captured

    return {
        "schema_version": 1,
        "event_id": run_id,
        "tracking_run_id": run_id,
        "sequence_no": 1,
        "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "session_id": session_id,
        "model": resolve_model(args, events),
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
        "duration_ms": duration_ms,
        "cost_estimate_usd": estimate_cost(
            input_tokens,
            cached_input_tokens,
            output_tokens,
            reasoning_tokens,
        ),
        "exit_code": exit_code,
        "partial": partial,
        "usage_captured": usage_captured,
        "codex_json_events": len(events),
        "working_dir": cwd.replace("\\", "/"),
        "subscription_type": subscription_type(),
        "approval_mode": resolve_approval_mode(args),
        "sandbox_mode": resolve_sandbox_mode(args),
        "provider": "openai",
        "codex_origin": "headless",
    }


def acquire_lock() -> int | None:
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            return fd
        except FileExistsError:
            try:
                age = time.time() - LOCK_FILE.stat().st_mtime
                if age > STALE_LOCK_SECONDS:
                    LOCK_FILE.unlink()
                    continue
            except OSError:
                pass

            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)


def release_lock(fd: int | None) -> None:
    if fd is not None:
        os.close(fd)
    try:
        LOCK_FILE.unlink()
    except OSError:
        pass


def append_jsonl_atomic(path: Path, event: dict) -> bool:
    TRACKER_DIR.mkdir(parents=True, exist_ok=True)
    fd = acquire_lock()
    if fd is None:
        return False

    try:
        with path.open("a", encoding="utf-8", newline="\n") as target:
            target.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
            target.flush()
            os.fsync(target.fileno())
        return True
    finally:
        release_lock(fd)


def main(argv: list[str]) -> int:
    codex_cmd = find_codex_command()
    if not codex_cmd:
        sys.stderr.write("codex CLI not found in PATH\n")
        return 127

    args = args_for_codex(argv)
    cwd = os.getcwd()
    start_ts = time.time()
    run_id = f"codex-{int(start_ts * 1000)}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    parsed_events: list[dict] = []
    interrupted = False

    proc = subprocess.Popen(
        [codex_cmd] + args,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
        stdin=None,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    try:
        if proc.stdout is not None:
            for line in proc.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                event = parse_json_line(line)
                if event is not None:
                    parsed_events.append(event)
    except KeyboardInterrupt:
        interrupted = True
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    if proc.poll() is None:
        # DeepSeek audit C2: bounded wait — if the wrapped codex process hangs
        # past 10s after stdout closes, kill it rather than block forever.
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass  # last-ditch: leak the child rather than hang the wrapper
    duration_ms = int((time.time() - start_ts) * 1000)
    exit_code = 130 if interrupted and proc.returncode == 0 else proc.returncode

    event = build_event_from_codex_events(
        parsed_events,
        exit_code,
        duration_ms,
        cwd,
        args,
        run_id,
        interrupted=interrupted,
    )
    if event is not None and not append_jsonl_atomic(EVENTS_FILE, event):
        sys.stderr.write("codex tracking event skipped: tracker lock timeout\n")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
