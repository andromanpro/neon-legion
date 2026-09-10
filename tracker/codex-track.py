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
from datetime import date, datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from tools import config as cfg  # noqa: E402

TRACKER_DIR = PROJECT_ROOT / "tracker"
EVENTS_FILE = TRACKER_DIR / "codex-events.jsonl"
LOCK_FILE = TRACKER_DIR / ".codex-events.lock"

# Pricing per MILLION tokens — "what the API would have cost" if the user paid
# OpenAI per token instead of via a ChatGPT subscription.
#
# Source of truth: OpenAI's own rate card and changelog
# (developers.openai.com/api/docs/pricing, .../models/gpt-6-astra,
# .../changelog), verified 2026-09-10. Third-party price blogs contradicted
# each other on exactly these numbers — do not update them from blogs.
#
# MODEL_PRICING holds each model's LAUNCH rate; PRICE_CHANGES holds later
# official cuts with their effective date, so an event is billed at the rate in
# force on its UTC date and history before a cut keeps the old rate:
#   gpt-5.6-terra  $2.50 / $15  -> $2 / $12       from 2026-07-30 (-20%)
#   gpt-5.6-luna   $1 / $6      -> $0.20 / $1.20  from 2026-07-30 (-80%)
#   gpt-5.6-sol    $5 / $30     -> $4 / $20       from 2026-08-21
#   gpt-6-astra    $10 / $50    launched 2026-09-03, long-context tier below
#   gpt-5.5        $5 / $30     gpt-5.4 $2.50 / $15 (unchanged)
# cached_input = 10% of input; reasoning tokens are part of output and billed
# once, at the output rate. The global OPENAI_TOKEN_PRICE_* env vars still
# override the DEFAULT tier (unrecognized model strings) for back-compat.
def _rate(inp: float, cached: float, out: float, reasoning: float) -> dict:
    return {
        "input": inp / 1_000_000,
        "cached_input": cached / 1_000_000,
        "output": out / 1_000_000,
        "reasoning": reasoning / 1_000_000,
    }


MODEL_PRICING = {
    "gpt-6-astra": _rate(10.0, 1.0, 50.0, 50.0),
    "gpt-5.6-sol": _rate(5.0, 0.5, 30.0, 30.0),
    "gpt-5.6-terra": _rate(2.5, 0.25, 15.0, 15.0),
    "gpt-5.6-luna": _rate(1.0, 0.1, 6.0, 6.0),
    "gpt-5.5": _rate(5.0, 0.5, 30.0, 30.0),
    "gpt-5.4": _rate(2.5, 0.25, 15.0, 15.0),
}

# (effective-from UTC date, rate), ascending. Found 2026-09-10: the ledger was
# still billing Sol at $5/$30 three weeks after OpenAI cut it, and gpt-6-astra
# was absent entirely — silently billed at the Sol fallback, 2.6x too low.
PRICE_CHANGES = {
    "gpt-5.6-terra": [(date(2026, 7, 30), _rate(2.0, 0.2, 12.0, 12.0))],
    "gpt-5.6-luna": [(date(2026, 7, 30), _rate(0.2, 0.02, 1.2, 1.2))],
    "gpt-5.6-sol": [(date(2026, 8, 21), _rate(4.0, 0.4, 20.0, 20.0))],
}

# Long-context tier: a request with MORE than `threshold` input tokens is billed
# at the multiplied rates for the WHOLE request. Official wording for
# gpt-6-astra: "more than 272K input tokens are priced at 2x input and cache
# rates and 1.5x output for the full request". Only models whose threshold is
# confirmed on their OpenAI model page are listed here.
LONG_CONTEXT = {
    "gpt-6-astra": {"threshold": 272_000, "input": 2.0, "cached_input": 2.0, "output": 1.5},
}

# Fallback for unrecognized models (e.g. "codex-auto-review", future variants):
# flagship Sol/5.5 launch rates, env-overridable for back-compat.
DEFAULT_PRICING = _rate(
    cfg.get_legacy_env("OPENAI_TOKEN_PRICE_INPUT", 5.0, float),
    cfg.get_legacy_env("OPENAI_TOKEN_PRICE_CACHED_INPUT", 0.5, float),
    cfg.get_legacy_env("OPENAI_TOKEN_PRICE_OUTPUT", 30.0, float),
    cfg.get_legacy_env("OPENAI_TOKEN_PRICE_REASONING", 30.0, float),
)


def _model_key(model: object) -> str | None:
    """MODEL_PRICING key for a model string (exact match, then prefix), else None.

    Sub-variant tolerant: "gpt-5.6-sol-ultra" or "gpt-5.6-sol (2026-07)" resolve
    to the sol tier.
    """
    name = str(model or "").lower().strip()
    if name in MODEL_PRICING:
        return name
    for key in MODEL_PRICING:
        if name.startswith(key):
            return key
    return None


def _price_date(at: object) -> date | None:
    """UTC calendar date for a pricing timestamp; None means "now".

    Accepts a date, a datetime (naive = UTC) or an ISO-8601 string. An
    unparseable value prices at the current rate, exactly like a live event.
    """
    if at is None:
        return None
    if isinstance(at, datetime):  # before `date`: datetime subclasses date
        moment = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).date()
    if isinstance(at, date):
        return at
    try:
        parsed = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        return None
    return _price_date(parsed)


def pricing_for_model(model: object, at: object = None) -> dict:
    """Per-token price dict for an OpenAI/Codex model at a point in time.

    `at` (the event timestamp) selects the rate in force on that UTC date;
    None means the current rate. Unknown model → DEFAULT_PRICING (never None,
    so Codex cost is always populated — OpenAI is pay-per-token, no free tier).
    """
    key = _model_key(model)
    if key is None:
        return DEFAULT_PRICING
    rate = MODEL_PRICING[key]
    when = _price_date(at)
    for effective_from, changed in PRICE_CHANGES.get(key, []):
        if when is None or when >= effective_from:
            rate = changed
    return rate

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
    model: object,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    reasoning_tokens: int,
    at: object = None,
) -> float:
    """OpenAI usage semantics: cached_input is a SUBSET of input_tokens, and
    reasoning is a SUBSET of output_tokens (verified empirically on 38k ledger
    events: cached<=input and reasoning<=output hold on every one). The old
    formula billed all four fields additively, double-charging cache and
    reasoning — the ledger carried $28.3k of Codex cost where the real figure
    was ~$3.4k. Bill the uncached remainder at the input rate, the cached part
    at the cache rate, and output once (reasoning is already inside it).
    reasoning_tokens stays in the signature: it's recorded in events for
    diagnostics, it just must not be priced separately.

    `at` is the event timestamp: the rate in force on that date applies (see
    PRICE_CHANGES), and a request above a model's LONG_CONTEXT threshold is
    billed at the multiplied rates for the whole request.
    """
    pricing = pricing_for_model(model, at)
    input_rate = pricing["input"]
    cached_rate = pricing["cached_input"]
    output_rate = pricing["output"]
    key = _model_key(model)
    long_context = LONG_CONTEXT.get(key) if key else None
    if long_context and input_tokens > long_context["threshold"]:
        input_rate *= long_context["input"]
        cached_rate *= long_context["cached_input"]
        output_rate *= long_context["output"]
    uncached_input = max(0, input_tokens - cached_input_tokens)
    cost = (
        uncached_input * input_rate
        + cached_input_tokens * cached_rate
        + output_tokens * output_rate
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
    model = resolve_model(args, events)

    return {
        "schema_version": 1,
        "event_id": run_id,
        "tracking_run_id": run_id,
        "sequence_no": 1,
        "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "session_id": session_id,
        "model": model,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
        "duration_ms": duration_ms,
        "cost_estimate_usd": estimate_cost(
            model,
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
