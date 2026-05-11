#!/usr/bin/env python
"""Invocation adapters for neon-legion declarative roles."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

# ANSI escape sequence regex (color codes, cursor moves, screen clears).
# OpenCode and some CLIs emit these even when piped to non-tty; they corrupt
# the deliverable files and pollute downstream role prompts.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text) if text else text


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(3)}")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _command(name: str) -> str:
    """Resolve a CLI tool path; helpful error if not installed.

    Falls back to the bare name so the OSError from subprocess.run still
    has a recognizable command in its message — but in practice the caller
    catches OSError and we want a clearer signal than "[WinError 2]".
    """
    found = shutil.which(name)
    if found is None:
        raise FileNotFoundError(
            f"{name!r} not found on PATH. "
            f"Install it or update your role's invocation in roles.toml."
        )
    return found


def _error_from(completed: subprocess.CompletedProcess[str], prefix: str) -> str:
    detail = (completed.stderr or completed.stdout or "").strip()
    if not detail:
        detail = f"{prefix} exited with code {completed.returncode}"
    return detail[:8000]


def _result(
    *,
    ok: bool,
    exit_code: int,
    started: float,
    output_path: Path,
    error: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": ok,
        "exit_code": exit_code,
        "duration_ms": int((time.time() - started) * 1000),
        "output_path": str(output_path),
        "error": error,
    }
    payload.update(extra)
    return payload


def _invoke_claude(prompt: str, output_path: Path) -> dict[str, Any]:
    started = time.time()
    try:
        completed = subprocess.run(
            [_command("claude"), "-p", "--bare", "--output-format", "json"],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _result(
            ok=False,
            exit_code=124,
            started=started,
            output_path=output_path,
            error="claude-cli-headless timed out after 300s",
        )
    except OSError as exc:
        return _result(ok=False, exit_code=127, started=started, output_path=output_path, error=str(exc))

    ok = completed.returncode == 0
    if ok:
        # B1 (DeepSeek audit): `claude -p --output-format json` returns
        # {"response": "...", "cost_usd": ..., "usage": ...}. We must extract
        # the response text — writing the raw JSON would pollute every
        # downstream role's prompt context with metadata noise.
        body = _extract_claude_response(completed.stdout)
        atomic_write_text(output_path, body)
    error = None if ok else "Claude CLI failed. If this is an OAuth refresh issue, re-authenticate Claude Code and retry. " + _error_from(completed, "claude")
    return _result(ok=ok, exit_code=completed.returncode, started=started, output_path=output_path, error=error)


def _extract_claude_response(stdout: str) -> str:
    """Extract response text from `claude -p --output-format json` wrapper.

    Format observed: {"response": "...", "result": "...", "content": [...], ...}.
    We try several keys in order. Falls back to raw stdout if parse fails so
    the user never silently loses data.
    """
    if not stdout:
        return ""
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return stdout  # not JSON, treat as plain text
    if isinstance(data, dict):
        # Try common response keys
        for key in ("response", "result", "text", "output", "content"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
            # Anthropic-style: content is a list of {type, text} blocks
            if isinstance(value, list):
                parts = [b.get("text", "") for b in value if isinstance(b, dict)]
                joined = "".join(parts).strip()
                if joined:
                    return joined
    return stdout  # unrecognized JSON shape, keep raw


def _invoke_codex(role_config: dict[str, Any], prompt: str, output_path: Path) -> dict[str, Any]:
    started = time.time()
    sandbox = str(role_config.get("sandbox") or "read-only")
    tmp_output = output_path.with_name(f".{output_path.name}.codex-{os.getpid()}-{secrets.token_hex(3)}.tmp")
    try:
        completed = subprocess.run(
            [
                _command("codex"),
                "exec",
                "--sandbox",
                sandbox,
                "--skip-git-repo-check",
                "--output-last-message",
                str(tmp_output),
            ],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _result(
            ok=False,
            exit_code=124,
            started=started,
            output_path=output_path,
            error="codex-exec timed out after 600s",
        )
    except OSError as exc:
        return _result(ok=False, exit_code=127, started=started, output_path=output_path, error=str(exc))

    ok = completed.returncode == 0
    if ok:
        if tmp_output.exists():
            os.replace(tmp_output, output_path)
        else:
            # C2 (DeepSeek audit): Codex CLI stdout has ANSI + spinner chars.
            # When --output-last-message file is missing (codex misbehaved),
            # we fall back to stdout — but strip ANSI first so the deliverable
            # is at least readable to downstream roles.
            atomic_write_text(output_path, _strip_ansi(completed.stdout))
    elif tmp_output.exists():
        tmp_output.unlink(missing_ok=True)
    return _result(
        ok=ok,
        exit_code=completed.returncode,
        started=started,
        output_path=output_path,
        error=None if ok else _error_from(completed, "codex"),
    )


def _openrouter_key_from_git() -> str | None:
    git = shutil.which("git")
    if not git:
        return None
    try:
        completed = subprocess.run(
            [git, "config", "--global", "openrouter.apiKey"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    key = completed.stdout.strip()
    return key if completed.returncode == 0 and key else None


def _invoke_opencode(role_config: dict[str, Any], prompt: str, output_path: Path) -> dict[str, Any]:
    started = time.time()
    env = os.environ.copy()
    key = _openrouter_key_from_git()
    if key:
        env["OPENROUTER_API_KEY"] = key
    try:
        completed = subprocess.run(
            [
                _command("opencode"),
                "run",
                "-m",
                str(role_config.get("model") or ""),
                "--format",
                "default",
                "--pure",
            ],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=900,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _result(
            ok=False,
            exit_code=124,
            started=started,
            output_path=output_path,
            error="opencode-run timed out after 900s",
        )
    except OSError as exc:
        return _result(ok=False, exit_code=127, started=started, output_path=output_path, error=str(exc))

    ok = completed.returncode == 0
    if ok:
        # B2 (DeepSeek audit): OpenCode emits ANSI escape sequences (\x1b[0m,
        # \x1b[33m, ...) even when stdout is piped. They corrupt the
        # deliverable file and pollute downstream role prompts.
        atomic_write_text(output_path, _strip_ansi(completed.stdout))
    return _result(
        ok=ok,
        exit_code=completed.returncode,
        started=started,
        output_path=output_path,
        error=None if ok else _error_from(completed, "opencode"),
    )


def _invoke_human(prompt: str, output_path: Path) -> dict[str, Any]:
    started = time.time()
    prompt_path = output_path.with_name(f"{output_path.stem}-PROMPT{output_path.suffix}")
    atomic_write_text(prompt_path, prompt)
    print(f"WAITING FOR HUMAN: please put response into {output_path}")
    return _result(
        ok=True,
        exit_code=0,
        started=started,
        output_path=prompt_path,
        error=None,
        waiting_for_human=True,
        response_path=str(output_path),
    )


def invoke(role_config: dict[str, Any], prompt: str, output_path: Path) -> dict[str, Any]:
    """Invoke a role's underlying CLI with the prompt; capture output.

    Returns:
        {"ok": bool, "exit_code": int, "duration_ms": int,
         "output_path": str, "error": str | None}
    """
    invocation = str(role_config.get("invocation") or "").strip()
    if invocation == "claude-cli-headless":
        return _invoke_claude(prompt, output_path)
    if invocation == "codex-exec":
        return _invoke_codex(role_config, prompt, output_path)
    if invocation == "opencode-run":
        return _invoke_opencode(role_config, prompt, output_path)
    if invocation == "human-relay":
        return _invoke_human(prompt, output_path)
    return {
        "ok": False,
        "exit_code": 2,
        "duration_ms": 0,
        "output_path": str(output_path),
        "error": f"unsupported invocation: {invocation}",
    }
