#!/usr/bin/env python
"""Controlled bridge from openclaw workspace to F:/WorkAI and Codex CLI.

openclaw runs on the NAS and cannot see the Windows F: drive directly. This
bridge watches a shared NAS folder, processes constrained JSON requests on the
Windows side, can launch Codex CLI jobs, and writes JSON responses back for
openclaw to read.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_WORKAI_ROOT = Path(os.environ.get("WORKAI_ROOT", "F:/WorkAI"))
DEFAULT_BRIDGE_ROOT = Path(
    os.environ.get("OPENCLAW_CODEX_BRIDGE", "H:/openclaw/workspace/codex-bridge")
)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAX_READ_BYTES = 200_000
MAX_RG_LINES = 200
MAX_LIST_ITEMS = 500
MAX_DEPTH = 4
MAX_PROMPT_CHARS = 60_000
MAX_JOB_RESULT_CHARS = 80_000
DEFAULT_CODEX_TIMEOUT_SECONDS = 3600
MAX_CODEX_TIMEOUT_SECONDS = 14_400
ALLOWED_CODEX_SANDBOXES = {"read-only", "workspace-write"}
ALLOWED_REASONING_EFFORTS = {"low", "medium", "high", "xhigh"}
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,120}$")
SAFE_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/+-]{1,120}$")
SENSITIVE_EXACT_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".ssh",
    "id_rsa",
    "id_ed25519",
    "authorized_keys",
}
SENSITIVE_SUFFIXES = {
    ".key",
    ".pem",
    ".pfx",
    ".p12",
    ".sqlite",
    ".sqlite3",
    ".db",
}
RG_DENY_GLOBS = [
    "!.git/**",
    "!**/.git/**",
    "!**/.env",
    "!**/.env.*",
    "!**/*.pem",
    "!**/*.key",
    "!**/*.pfx",
    "!**/*.p12",
    "!**/node_modules/**",
]


class BridgeError(Exception):
    """Expected validation or execution error."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def configure_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass


def ensure_dirs(bridge_root: Path) -> tuple[Path, Path, Path]:
    inbox = bridge_root / "inbox"
    outbox = bridge_root / "outbox"
    archive = bridge_root / "archive"
    jobs = bridge_root / "jobs"
    for path in (inbox, outbox, archive, jobs):
        path.mkdir(parents=True, exist_ok=True)
    return inbox, outbox, archive


def safe_request_id(raw: Any, fallback: str) -> str:
    request_id = str(raw or fallback).strip()
    if not SAFE_ID_RE.match(request_id):
        raise BridgeError(
            "Invalid id. Use 1-121 chars: letters, digits, underscore, dot, dash."
        )
    return request_id


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    return bool(value)


def is_sensitive_path(path: Path) -> bool:
    parts = {part.lower() for part in path.parts}
    if parts & SENSITIVE_EXACT_NAMES:
        return True
    return path.suffix.lower() in SENSITIVE_SUFFIXES


def resolve_under_root(workai_root: Path, raw_path: Any, *, allow_root: bool) -> Path:
    if raw_path in (None, ""):
        if allow_root:
            return workai_root
        raise BridgeError("path is required")

    candidate = Path(str(raw_path))
    if candidate.is_absolute():
        target = candidate.resolve()
    else:
        target = (workai_root / candidate).resolve()

    root = workai_root.resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise BridgeError(f"path escapes WORKAI_ROOT: {raw_path}") from exc
    return target


def decode_text(data: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8-replace"


def action_ping(
    _request: dict[str, Any],
    workai_root: Path,
    _bridge_root: Path | None = None,
) -> dict[str, Any]:
    return {
        "message": "pong",
        "workai_root": str(workai_root),
        "bridge": "openclaw-codex",
    }


def action_list(
    request: dict[str, Any],
    workai_root: Path,
    _bridge_root: Path | None = None,
) -> dict[str, Any]:
    target = resolve_under_root(workai_root, request.get("path"), allow_root=True)
    if is_sensitive_path(target):
        raise BridgeError("Refusing to list sensitive path")
    if not target.exists():
        raise BridgeError(f"path does not exist: {target}")
    if not target.is_dir():
        raise BridgeError(f"path is not a directory: {target}")

    depth = min(max(int(request.get("depth", 1)), 0), MAX_DEPTH)
    show_hidden = as_bool(request.get("show_hidden"), default=False)
    items: list[dict[str, Any]] = []

    def walk(path: Path, level: int) -> None:
        if len(items) >= MAX_LIST_ITEMS:
            return
        try:
            children = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            items.append({"path": str(path.relative_to(workai_root)), "error": str(exc)})
            return

        for child in children:
            if len(items) >= MAX_LIST_ITEMS:
                return
            if not show_hidden and child.name.startswith("."):
                continue
            if is_sensitive_path(child):
                continue
            rel = child.relative_to(workai_root).as_posix()
            item = {
                "path": rel,
                "type": "dir" if child.is_dir() else "file",
            }
            if child.is_file():
                try:
                    item["size"] = child.stat().st_size
                except OSError:
                    pass
            items.append(item)
            if child.is_dir() and level < depth:
                walk(child, level + 1)

    walk(target, 0)
    return {
        "path": target.relative_to(workai_root).as_posix() if target != workai_root else ".",
        "depth": depth,
        "truncated": len(items) >= MAX_LIST_ITEMS,
        "items": items,
    }


def action_read(
    request: dict[str, Any],
    workai_root: Path,
    _bridge_root: Path | None = None,
) -> dict[str, Any]:
    target = resolve_under_root(workai_root, request.get("path"), allow_root=False)
    if is_sensitive_path(target):
        raise BridgeError("Refusing to read sensitive path")
    if not target.exists():
        raise BridgeError(f"path does not exist: {target}")
    if not target.is_file():
        raise BridgeError(f"path is not a file: {target}")

    max_bytes = min(max(int(request.get("max_bytes", MAX_READ_BYTES)), 1), MAX_READ_BYTES)
    size = target.stat().st_size
    with target.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    truncated = len(data) > max_bytes or size > max_bytes
    content, encoding = decode_text(data[:max_bytes])
    return {
        "path": target.relative_to(workai_root).as_posix(),
        "size": size,
        "encoding": encoding,
        "truncated": truncated,
        "content": content,
    }


def find_rg() -> str:
    explicit = os.environ.get("RG_EXE")
    if explicit:
        return explicit
    found = shutil.which("rg")
    if found:
        return found
    bundled = Path("C:/Users/Roono/AppData/Local/OpenAI/Codex/bin/rg.exe")
    if bundled.exists():
        return str(bundled)
    raise BridgeError("rg executable not found")


def normalize_globs(raw: Any) -> list[str]:
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item).strip()]
    raise BridgeError("glob must be string or list")


def action_rg(
    request: dict[str, Any],
    workai_root: Path,
    _bridge_root: Path | None = None,
) -> dict[str, Any]:
    pattern = str(request.get("pattern") or "")
    if not pattern:
        raise BridgeError("pattern is required")
    target = resolve_under_root(workai_root, request.get("path"), allow_root=True)
    if is_sensitive_path(target):
        raise BridgeError("Refusing to search sensitive path")
    if not target.exists():
        raise BridgeError(f"path does not exist: {target}")

    max_lines = min(max(int(request.get("max_lines", MAX_RG_LINES)), 1), MAX_RG_LINES)
    timeout = min(max(float(request.get("timeout_seconds", 20)), 1), 60)
    cmd = [
        find_rg(),
        "--line-number",
        "--column",
        "--no-heading",
        "--color",
        "never",
    ]
    if as_bool(request.get("literal"), default=False):
        cmd.append("-F")
    if not as_bool(request.get("case_sensitive"), default=True):
        cmd.append("-i")
    for deny_glob in RG_DENY_GLOBS:
        cmd.extend(["--glob", deny_glob])
    for user_glob in normalize_globs(request.get("glob")):
        cmd.extend(["--glob", user_glob])
    cmd.extend([pattern, str(target)])

    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if completed.returncode not in (0, 1):
        raise BridgeError(completed.stderr.strip() or f"rg failed: {completed.returncode}")

    lines = completed.stdout.splitlines()
    return {
        "path": target.relative_to(workai_root).as_posix() if target != workai_root else ".",
        "pattern": pattern,
        "match_count_returned": min(len(lines), max_lines),
        "truncated": len(lines) > max_lines,
        "matches": lines[:max_lines],
        "stderr": completed.stderr.strip(),
    }


def action_git_status(
    request: dict[str, Any],
    workai_root: Path,
    _bridge_root: Path | None = None,
) -> dict[str, Any]:
    target = resolve_under_root(workai_root, request.get("path"), allow_root=True)
    if not target.exists():
        raise BridgeError(f"path does not exist: {target}")
    if target.is_file():
        target = target.parent

    git = shutil.which("git") or "git"
    completed = subprocess.run(
        [git, "-C", str(target), "status", "--short", "--branch"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        raise BridgeError(completed.stderr.strip() or "git status failed")
    return {
        "path": target.relative_to(workai_root).as_posix() if target != workai_root else ".",
        "status": completed.stdout.splitlines(),
    }


def action_handoff_to_codex(
    request: dict[str, Any],
    _workai_root: Path,
    _bridge_root: Path | None = None,
) -> dict[str, Any]:
    message = str(request.get("message") or "").strip()
    if not message:
        raise BridgeError("message is required")
    if len(message) > 20_000:
        raise BridgeError("message is too long; keep it under 20000 chars")

    context = request.get("context")
    if context is not None and not isinstance(context, (dict, list, str)):
        raise BridgeError("context must be an object, list, or string")

    return {
        "status": "queued_for_human_relay_to_codex",
        "message": message,
        "context": context,
        "note": "Tell Codex the request id and ask it to inspect bridge archive/outbox.",
    }


def jobs_root(bridge_root: Path) -> Path:
    root = bridge_root / "jobs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def job_state_path(job_dir: Path) -> Path:
    return job_dir / "state.json"


def write_job_state(job_dir: Path, status: str, **updates: Any) -> dict[str, Any]:
    state_path = job_state_path(job_dir)
    state: dict[str, Any] = {}
    if state_path.exists():
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state = loaded
        except (OSError, json.JSONDecodeError):
            state = {}
    state.update(updates)
    state["status"] = status
    state["updated_at_utc"] = utc_now()
    write_json_atomic(state_path, state)
    return state


def read_job_state(job_dir: Path) -> dict[str, Any]:
    state_path = job_state_path(job_dir)
    if not state_path.exists():
        raise BridgeError(f"job state not found: {job_dir.name}")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BridgeError(f"job state is not valid JSON: {job_dir.name}") from exc
    if not isinstance(state, dict):
        raise BridgeError(f"job state root is not an object: {job_dir.name}")
    return state


def safe_job_id(request: dict[str, Any]) -> str:
    return safe_request_id(request.get("job_id") or request.get("id"), "codex-job")


def validate_codex_request(request: dict[str, Any], workai_root: Path) -> tuple[str, Path, str]:
    job_id = safe_job_id(request)
    prompt = str(request.get("prompt") or request.get("message") or "").strip()
    if not prompt:
        raise BridgeError("prompt is required for codex_exec")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise BridgeError(f"prompt is too long; keep it under {MAX_PROMPT_CHARS} chars")

    cwd = resolve_under_root(workai_root, request.get("path") or ".", allow_root=True)
    if not cwd.exists() or not cwd.is_dir():
        raise BridgeError(f"path must be an existing directory: {cwd}")

    sandbox = str(request.get("sandbox") or "read-only")
    if sandbox not in ALLOWED_CODEX_SANDBOXES:
        raise BridgeError(
            "sandbox must be read-only or workspace-write; danger-full-access is not allowed"
        )
    if sandbox == "workspace-write":
        allow_write = (
            as_bool(request.get("allow_workspace_write"), default=False)
            or as_bool(request.get("allow_write"), default=False)
        )
        if not allow_write:
            raise BridgeError(
                "workspace-write requires allow_workspace_write=true; use read-only for inspection"
            )
    return job_id, cwd, sandbox


def action_codex_exec(
    request: dict[str, Any],
    workai_root: Path,
    bridge_root: Path | None = None,
) -> dict[str, Any]:
    if bridge_root is None:
        raise BridgeError("bridge_root is required for codex_exec")

    job_id, cwd, sandbox = validate_codex_request(request, workai_root)
    job_dir = jobs_root(bridge_root) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    state_path = job_state_path(job_dir)
    if state_path.exists():
        state = read_job_state(job_dir)
        if state.get("status") in {"queued", "running"}:
            raise BridgeError(f"job already exists and is {state.get('status')}: {job_id}")

    request_payload = dict(request)
    request_payload["job_id"] = job_id
    request_payload["resolved_cwd"] = str(cwd)
    request_payload["sandbox"] = sandbox
    write_json_atomic(job_dir / "request.json", request_payload)
    (job_dir / "prompt.md").write_text(
        str(request.get("prompt") or request.get("message") or ""),
        encoding="utf-8",
    )
    write_job_state(
        job_dir,
        "queued",
        job_id=job_id,
        created_at_utc=utc_now(),
        cwd=str(cwd),
        sandbox=sandbox,
        prompt_file=str(job_dir / "prompt.md"),
    )

    cmd = [
        sys.executable,
        "-u",
        str(Path(__file__).resolve()),
        "--workai-root",
        str(workai_root),
        "--bridge-root",
        str(bridge_root),
        "--run-codex-job",
        str(job_dir),
    ]
    runner_log = job_dir / "runner.log"
    runner_err = job_dir / "runner.err.log"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with runner_log.open("a", encoding="utf-8") as stdout, runner_err.open("a", encoding="utf-8") as stderr:
        proc = subprocess.Popen(
            cmd,
            cwd=str(workai_root),
            stdout=stdout,
            stderr=stderr,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )

    write_job_state(
        job_dir,
        "running",
        runner_pid=proc.pid,
        started_at_utc=utc_now(),
        runner_log=str(runner_log),
        runner_err=str(runner_err),
    )
    return {
        "job_id": job_id,
        "status": "running",
        "runner_pid": proc.pid,
        "job_dir": f"/workspace/codex-bridge/jobs/{job_id}",
        "status_request": {
            "id": f"{job_id}-status",
            "action": "codex_status",
            "job_id": job_id,
        },
    }


def action_codex_status(
    request: dict[str, Any],
    _workai_root: Path,
    bridge_root: Path | None = None,
) -> dict[str, Any]:
    if bridge_root is None:
        raise BridgeError("bridge_root is required for codex_status")
    job_id = safe_job_id(request)
    job_dir = jobs_root(bridge_root) / job_id
    state = read_job_state(job_dir)
    result_file = job_dir / "result.md"
    if result_file.exists() and as_bool(request.get("include_result"), default=True):
        data = result_file.read_text(encoding="utf-8", errors="replace")
        state["result"] = data[-MAX_JOB_RESULT_CHARS:]
        state["result_truncated"] = len(data) > MAX_JOB_RESULT_CHARS
    return state


def action_codex_cancel(
    request: dict[str, Any],
    _workai_root: Path,
    bridge_root: Path | None = None,
) -> dict[str, Any]:
    if bridge_root is None:
        raise BridgeError("bridge_root is required for codex_cancel")
    job_id = safe_job_id(request)
    job_dir = jobs_root(bridge_root) / job_id
    state = read_job_state(job_dir)
    pid = state.get("runner_pid")
    if not isinstance(pid, int) or pid <= 0:
        raise BridgeError(f"job has no runner_pid: {job_id}")
    completed = subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    write_job_state(
        job_dir,
        "cancelled",
        ended_at_utc=utc_now(),
        cancel_stdout=completed.stdout.strip(),
        cancel_stderr=completed.stderr.strip(),
    )
    return read_job_state(job_dir)


ACTIONS = {
    "ping": action_ping,
    "list": action_list,
    "read": action_read,
    "rg": action_rg,
    "git_status": action_git_status,
    "handoff_to_codex": action_handoff_to_codex,
    "codex_exec": action_codex_exec,
    "codex_status": action_codex_status,
    "codex_cancel": action_codex_cancel,
}


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    try:
        tmp.replace(path)
    except PermissionError:
        # Some SMB shares deny atomic replace over an existing file. The bridge
        # protocol tolerates a normal overwrite because each response is JSON
        # and readers retry by id.
        path.write_text(data, encoding="utf-8")
        try:
            tmp.unlink()
        except OSError:
            pass


def archive_request(request_file: Path, archive_dir: Path) -> None:
    destination = archive_dir / request_file.name
    if destination.exists():
        suffix = datetime.now().strftime("%Y%m%d-%H%M%S")
        destination = archive_dir / f"{request_file.stem}.{suffix}{request_file.suffix}"
    request_file.replace(destination)


def process_request_file(
    request_file: Path,
    *,
    workai_root: Path,
    bridge_root: Path,
    outbox: Path,
    archive: Path,
) -> bool:
    fallback_id = request_file.stem
    processed_at = utc_now()
    try:
        raw = request_file.read_text(encoding="utf-8-sig")
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise BridgeError("Request root must be a JSON object")
        request_id = safe_request_id(request.get("id"), fallback_id)
        action_name = str(request.get("action") or "").strip()
        if action_name not in ACTIONS:
            raise BridgeError(f"Unknown action: {action_name!r}")
        result = ACTIONS[action_name](request, workai_root, bridge_root)
        payload = {
            "id": request_id,
            "ok": True,
            "action": action_name,
            "processed_at_utc": processed_at,
            "result": result,
        }
    except Exception as exc:
        try:
            request_id = safe_request_id(fallback_id, "request")
        except BridgeError:
            request_id = "request-error"
        payload = {
            "id": request_id,
            "ok": False,
            "processed_at_utc": processed_at,
            "error": str(exc),
            "error_type": type(exc).__name__,
        }

    write_json_atomic(outbox / f"{payload['id']}.json", payload)
    archive_request(request_file, archive)
    print(f"{processed_at} processed {request_file.name} -> {payload['id']}.json")
    return payload.get("ok", False)


def process_once(workai_root: Path, bridge_root: Path) -> int:
    inbox, outbox, archive = ensure_dirs(bridge_root)
    files = sorted(inbox.glob("*.json"), key=lambda path: path.name.lower())
    processed = 0
    for request_file in files:
        process_request_file(
            request_file,
            workai_root=workai_root.resolve(),
            bridge_root=bridge_root.resolve(),
            outbox=outbox,
            archive=archive,
        )
        processed += 1
    return processed


def write_state(bridge_root: Path, processed_total: int) -> None:
    payload = {
        "pid": os.getpid(),
        "updated_at_utc": utc_now(),
        "processed_total": processed_total,
    }
    write_json_atomic(bridge_root / "bridge-state.json", payload)


def bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, minimum), maximum)


def run_codex_job(job_dir: Path, workai_root: Path, bridge_root: Path) -> int:
    request_path = job_dir / "request.json"
    if not request_path.exists():
        write_job_state(job_dir, "failed", error="request.json not found")
        return 2

    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        if not isinstance(request, dict):
            raise BridgeError("request.json root must be an object")
        job_id, cwd, sandbox = validate_codex_request(request, workai_root)
        prompt = str(request.get("prompt") or request.get("message") or "").strip()
        timeout = bounded_int(
            request.get("timeout_seconds"),
            DEFAULT_CODEX_TIMEOUT_SECONDS,
            30,
            MAX_CODEX_TIMEOUT_SECONDS,
        )
        model = str(request.get("model") or "").strip()
        reasoning_effort = str(request.get("reasoning_effort") or "").strip()
        persist_session = as_bool(request.get("persist_session"), default=False)
    except Exception as exc:
        write_job_state(job_dir, "failed", error=str(exc), error_type=type(exc).__name__)
        return 2

    stdout_log = job_dir / "codex.stdout.jsonl"
    stderr_log = job_dir / "codex.stderr.log"
    result_file = job_dir / "result.md"
    tracker = PROJECT_ROOT / "tracker" / "codex-track.py"

    cmd = [
        sys.executable,
        str(tracker),
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        sandbox,
        "--cd",
        str(cwd),
        "--output-last-message",
        str(result_file),
    ]
    if not persist_session:
        cmd.append("--ephemeral")
    if model:
        if not SAFE_MODEL_RE.match(model):
            write_job_state(job_dir, "failed", error="invalid model value")
            return 2
        cmd.extend(["--model", model])
    if reasoning_effort:
        if reasoning_effort not in ALLOWED_REASONING_EFFORTS:
            write_job_state(job_dir, "failed", error="invalid reasoning_effort value")
            return 2
        cmd.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
    cmd.append(prompt)

    started = time.time()
    write_job_state(
        job_dir,
        "running",
        codex_started_at_utc=utc_now(),
        cwd=str(cwd),
        sandbox=sandbox,
        timeout_seconds=timeout,
        stdout_log=str(stdout_log),
        stderr_log=str(stderr_log),
        result_file=str(result_file),
    )

    exit_code = 1
    timed_out = False
    try:
        with stdout_log.open("w", encoding="utf-8") as stdout, stderr_log.open("w", encoding="utf-8") as stderr:
            completed = subprocess.run(
                cmd,
                cwd=str(cwd),
                stdout=stdout,
                stderr=stderr,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
            exit_code = completed.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        exit_code = 124
    except Exception as exc:
        write_job_state(
            job_dir,
            "failed",
            ended_at_utc=utc_now(),
            duration_ms=int((time.time() - started) * 1000),
            exit_code=1,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return 1

    duration_ms = int((time.time() - started) * 1000)
    status = "timed_out" if timed_out else ("completed" if exit_code == 0 else "failed")
    result_text = ""
    if result_file.exists():
        result_text = result_file.read_text(encoding="utf-8", errors="replace")

    state = write_job_state(
        job_dir,
        status,
        ended_at_utc=utc_now(),
        duration_ms=duration_ms,
        exit_code=exit_code,
        timed_out=timed_out,
        result_preview=result_text[-MAX_JOB_RESULT_CHARS:],
        result_truncated=len(result_text) > MAX_JOB_RESULT_CHARS,
    )
    write_json_atomic(
        bridge_root / "outbox" / f"{job_id}.codex-result.json",
        {
            "id": f"{job_id}.codex-result",
            "ok": exit_code == 0,
            "action": "codex_result",
            "processed_at_utc": utc_now(),
            "result": state,
        },
    )
    return exit_code


def main(argv: list[str] | None = None) -> int:
    configure_stdout()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workai-root", type=Path, default=DEFAULT_WORKAI_ROOT)
    parser.add_argument("--bridge-root", type=Path, default=DEFAULT_BRIDGE_ROOT)
    parser.add_argument("--once", action="store_true", help="process inbox once")
    parser.add_argument("--watch", action="store_true", help="poll inbox continuously")
    parser.add_argument("--sleep", type=float, default=2.0, help="watch sleep seconds")
    parser.add_argument("--run-codex-job", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    workai_root = args.workai_root.resolve()
    bridge_root = args.bridge_root.resolve()
    if not workai_root.exists():
        raise SystemExit(f"WORKAI_ROOT does not exist: {workai_root}")
    ensure_dirs(bridge_root)

    if args.run_codex_job:
        return run_codex_job(args.run_codex_job.resolve(), workai_root, bridge_root)

    if args.watch:
        print(f"Watching {bridge_root} for requests against {workai_root}")
        processed_total = 0
        while True:
            processed_total += process_once(workai_root, bridge_root)
            write_state(bridge_root, processed_total)
            time.sleep(max(args.sleep, 0.5))

    process_once(workai_root, bridge_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
