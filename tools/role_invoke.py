#!/usr/bin/env python
"""Invocation adapters for neon-legion declarative roles."""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(3)}")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _command(name: str) -> str:
    return shutil.which(name) or name


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
        atomic_write_text(output_path, completed.stdout)
    error = None if ok else "Claude CLI failed. If this is an OAuth refresh issue, re-authenticate Claude Code and retry. " + _error_from(completed, "claude")
    return _result(ok=ok, exit_code=completed.returncode, started=started, output_path=output_path, error=error)


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
            atomic_write_text(output_path, completed.stdout)
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
        atomic_write_text(output_path, completed.stdout)
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
