#!/usr/bin/env python
"""Hindsight Replay; outbound calls happen only through role_invoke adapters."""
from __future__ import annotations
import argparse, json, os, re, secrets, sys, time, tomllib
from datetime import datetime
from pathlib import Path
from typing import Any
try:
    import role_invoke
except ModuleNotFoundError:
    from tools import role_invoke  # type: ignore
PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = PROJECT_ROOT / "orchestrate-runs"
EVENTS_FILE = PROJECT_ROOT / "tracker" / "hindsight-events.jsonl"
SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")
CRITIC_BY_INVOCATION = {"codex-exec": "opencode-run", "opencode-run": "codex-exec",
                        "claude-cli-headless": "opencode-run", "human-relay": "opencode-run"}
def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(3)}")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text); handle.flush(); os.fsync(handle.fileno())
    os.replace(tmp, path)
def atomic_append_jsonl(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line); handle.flush(); os.fsync(handle.fileno())
def read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return data if isinstance(data, dict) else {}
def load_roles() -> tuple[dict[str, Any], Path]:
    path = PROJECT_ROOT / "roles.toml"
    path = path if path.exists() else PROJECT_ROOT / "roles.example.toml"
    roles = read_toml(path).get("role", {})
    return (roles if isinstance(roles, dict) else {}), path
def load_run_roles(run_dir: Path, state: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    paths = [run_dir / "roles.used.toml"]
    raw = state.get("roles_path")
    if isinstance(raw, str) and raw:
        path = Path(raw); paths.append(path if path.is_absolute() else PROJECT_ROOT / path)
    for path in paths:
        if path.exists():
            roles = read_toml(path).get("role", {})
            if isinstance(roles, dict):
                return roles
    return fallback

def safe_name(value: str) -> str:
    return SAFE_RE.sub("-", value).strip(".-") or "task"

def select_critic(original_invocation: str, override: str | None = None) -> str:
    return override or CRITIC_BY_INVOCATION.get(original_invocation, "opencode-run")

def role_for_invocation(roles: dict[str, Any], invocation: str) -> dict[str, Any] | None:
    for name, cfg in roles.items():
        if isinstance(cfg, dict) and str(cfg.get("invocation") or "") == invocation:
            cfg = dict(cfg); cfg["name"] = name
            return cfg
    return None

def load_state(run_id: str) -> tuple[Path, dict[str, Any]]:
    run_dir = RUNS_DIR / run_id
    state_path = run_dir / "state.json"
    if not state_path.exists():
        raise FileNotFoundError(f"run state not found: {state_path}")
    return run_dir, json.loads(state_path.read_text(encoding="utf-8"))

def resolve_run_path(run_dir: Path, raw: str) -> Path:
    path = Path(raw)
    path = path if path.is_absolute() else run_dir / path
    resolved = Path(os.path.realpath(path))
    try:
        resolved.relative_to(Path(os.path.realpath(run_dir)))
    except ValueError as exc:
        raise ValueError(f"deliverable escapes run dir: {raw}") from exc
    return resolved

def completed_steps(state: dict[str, Any], role: str | None = None) -> list[dict[str, Any]]:
    steps = state.get("steps", [])
    if not isinstance(steps, list):
        return []
    done = [s for s in steps if isinstance(s, dict) and s.get("status") == "completed"]
    return [s for s in done if s.get("role") == role] if role else done

def task_description(run_dir: Path, state: dict[str, Any]) -> str:
    for key in ("task_description", "description"):
        if isinstance(state.get(key), str) and state[key].strip():
            return state[key]
    if isinstance(state.get("task"), dict):
        return json.dumps(state["task"], ensure_ascii=False, indent=2)
    paths = [run_dir / "manifest.used.toml"]
    raw = state.get("manifest_path")
    if isinstance(raw, str) and raw:
        path = Path(raw); paths.append(path if path.is_absolute() else PROJECT_ROOT / path)
    for path in paths:
        if not path.exists():
            continue
        task = read_toml(path).get("task", {})
        if isinstance(task, dict):
            parts = [str(task.get("title") or "").strip(), str(task.get("description") or "").strip()]
            acc = task.get("acceptance", {})
            if isinstance(acc, dict) and acc.get("criteria"):
                parts.append("Acceptance criteria:\n" + str(acc["criteria"]).strip())
            text = "\n\n".join(p for p in parts if p)
            if text:
                return text
    return f"Run {state.get('run_id', run_dir.name)}"

def critique_prompt(task_text: str, deliverable: str) -> str:
    return ("You are reviewing work done by another AI agent. Read the task they\n"
            "were given and the deliverable they produced. Write a critique.\n\n"
            f"## Task they were given\n\n{task_text}\n\n## What they produced\n\n{deliverable}\n\n"
            "## What I want from you\n\n1. **Top 3 risks** — bugs, edge cases, security/privacy concerns the\n"
            "   author missed. Concrete file:line if possible.\n2. **Top 3 things they did well** — be honest, not flattering.\n"
            "3. **One non-obvious \"I would have done X instead\"** — only if you can\n   defend it; \"I'd add more tests\" is too generic.\n\n"
            "Constraints:\n- Be terse. Under 600 words total.\n- No preamble like \"Here is my review:\". Just start.\n"
            "- No \"would consider\", \"might want to\" — say what you'd actually do.\n- If the deliverable is empty/trivial, say \"Skipped: trivial deliverable\"\n  and stop.\n")

def event(**fields: Any) -> dict[str, Any]:
    fields = dict(fields); fields.update({"schema_version": 1, "ts": datetime.now().astimezone().isoformat(timespec="seconds")})
    return {"schema_version": fields.pop("schema_version"), "ts": fields.pop("ts"), **fields}

def hindsight_path(run_dir: Path, deliverable: Path, task_name: str) -> Path:
    return run_dir / f"{safe_name(task_name)}.hindsight.md"

def emit(base: dict[str, Any], model: str, ok: bool, duration: int, out: Path | None, **extra: Any) -> None:
    size = out.stat().st_size if out and out.exists() else 0
    atomic_append_jsonl(EVENTS_FILE, event(**base, critic_model=model, duration_ms=duration,
                                           ok=ok, hindsight_bytes=size,
                                           output_path=str(out) if out else "", **extra))

def process_step(run_id: str, run_dir: Path, step: dict[str, Any], task_text: str,
                 run_roles: dict[str, Any], critic_roles: dict[str, Any],
                 critic_override: str | None, dry_run: bool) -> tuple[bool, Path | None]:
    task_name = str(step.get("role") or step.get("task_name") or "task")
    raw = step.get("output_path") if isinstance(step.get("output_path"), str) else f"{safe_name(task_name)}.md"
    deliverable_path = resolve_run_path(run_dir, raw)
    original_cfg = run_roles.get(task_name, {}) if isinstance(run_roles.get(task_name), dict) else {}
    original = str(step.get("invocation") or original_cfg.get("invocation") or "")
    critic = select_critic(original, critic_override)
    out_path = hindsight_path(run_dir, deliverable_path, task_name)
    text = deliverable_path.read_text(encoding="utf-8", errors="replace") if deliverable_path.exists() else ""
    base = {"run_id": run_id, "task_name": task_name, "original_invocation": original,
            "critic_invocation": critic, "deliverable_bytes": len(text.encode("utf-8"))}
    if dry_run:
        print(f"{run_id}:{task_name} {original or '?'} -> {critic} output={out_path}")
        return True, None
    critic_cfg = role_for_invocation(critic_roles, critic)
    if critic_cfg is None:
        atomic_write_text(out_path, f"Skipped: critic not configured: {critic}\n")
        emit(base, "", True, 0, out_path, skipped=True, skip_reason="critic_not_configured")
        print(f"skipped {run_id}:{task_name}; critic not configured: {critic}")
        return True, out_path
    model = str(critic_cfg.get("model") or "")
    if not text.strip():
        atomic_write_text(out_path, "Skipped: trivial deliverable\n")
        emit(base, model, True, 0, out_path, skipped=True, skip_reason="trivial_deliverable")
        return True, out_path
    started = time.time()
    result = role_invoke.invoke(critic_cfg, critique_prompt(task_text, text), out_path)
    if result.get("ok") and not out_path.exists():
        fallback = result.get("output") or result.get("response") or result.get("text")
        atomic_write_text(out_path, "" if fallback is None else str(fallback))
    duration = int(result.get("duration_ms") or ((time.time() - started) * 1000))
    extra = {"error": str(result["error"])} if result.get("error") else {}
    emit(base, model, bool(result.get("ok")), duration, out_path, **extra)
    return bool(result.get("ok")), out_path if out_path.exists() else None

def missing_hindsight_steps(run_dir: Path, state: dict[str, Any]) -> list[dict[str, Any]]:
    missing = []
    for step in completed_steps(state):
        role = str(step.get("role") or "task")
        raw = step.get("output_path") if isinstance(step.get("output_path"), str) else f"{safe_name(role)}.md"
        if not hindsight_path(run_dir, resolve_run_path(run_dir, raw), role).exists():
            missing.append(step)
    return missing

def pending_runs() -> list[str]:
    if not RUNS_DIR.exists():
        return []
    found = []
    for run_dir in sorted(p for p in RUNS_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")):
        state_path = run_dir / "state.json"
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if state.get("status") == "completed" and missing_hindsight_steps(run_dir, state):
                found.append(run_dir.name)
    return found

def run_hindsight(run_id: str, role: str | None, critic: str | None, dry_run: bool) -> int:
    run_dir, state = load_state(run_id)
    if state.get("status") != "completed":
        print(f"error: run {run_id} status is {state.get('status')!r}, not 'completed'", file=sys.stderr)
        return 1
    critic_roles, _ = load_roles()
    steps = completed_steps(state, role)
    if not steps:
        print(f"no completed tasks found for run {run_id}")
        return 0
    ok, written = True, []
    run_roles = load_run_roles(run_dir, state, critic_roles)
    task_text = task_description(run_dir, state)
    for step in steps:
        step_ok, path = process_step(run_id, run_dir, step, task_text, run_roles, critic_roles, critic, dry_run)
        ok = ok and step_ok
        if path:
            written.append(path)
    if not dry_run and written:
        body = written[0].read_text(encoding="utf-8", errors="replace") if len(written) == 1 else "\n\n".join(f"## {p.name}\n\n{p.read_text(encoding='utf-8', errors='replace')}" for p in written)
        atomic_write_text(run_dir / "hindsight.md", body)
    return 0 if ok else 1

def command_list() -> int:
    runs = pending_runs()
    print("\n".join(runs) if runs else "no pending hindsight")
    return 0

def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate second-model critiques for completed orchestrate runs.")
    parser.add_argument("run_id", nargs="?", help="orchestrate-runs/<run_id> to review")
    parser.add_argument("--role", help="only review one completed role/task")
    parser.add_argument("--critic", help="critic invocation override, e.g. opencode-run")
    parser.add_argument("--dry-run", action="store_true", help="print planned actions without writing files")
    parser.add_argument("--list", action="store_true", help="list completed runs missing hindsight")
    parser.add_argument("--all-pending", action="store_true", help="run hindsight for every pending completed run")
    return parser.parse_args(argv)

def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.list:
            return command_list()
        if args.all_pending:
            runs = pending_runs()
            if not runs:
                print("no pending hindsight"); return 0
            return max(run_hindsight(r, args.role, args.critic, args.dry_run) for r in runs)
        if not args.run_id:
            print("error: run_id required unless --list or --all-pending is used", file=sys.stderr)
            return 2
        return run_hindsight(args.run_id, args.role, args.critic, args.dry_run)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
