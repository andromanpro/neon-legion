#!/usr/bin/env python
"""neon-legion orchestrator: runs a task spec through a declarative role flow.

Reads ./roles.toml (gitignored) and a task manifest. Invokes each role in
sequence (or in DAG order if manifest declares dependencies), persists each
role's output as a deliverable file under ./orchestrate-runs/<run-id>/.

Usage:
    py tools/orchestrate.py run prompts/MANIFEST.example.toml
    py tools/orchestrate.py status <run-id>
    py tools/orchestrate.py list

Not a runtime daemon. Each invocation is single-shot. Resumable via run-id.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any

from role_invoke import invoke


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = PROJECT_ROOT / "orchestrate-runs"
EX_CONFIG = 78
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(3)}")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"TOML root must be a table: {path}")
    return data


def load_roles(prefer_example: bool = False) -> tuple[dict[str, Any], Path]:
    example = PROJECT_ROOT / "roles.example.toml"
    local = PROJECT_ROOT / "roles.toml"
    path = example if prefer_example or not local.exists() else local
    data = read_toml(path)
    roles = data.get("role")
    if not isinstance(roles, dict) or not roles:
        raise ValueError(f"No [role.*] tables found in {path}")
    return roles, path


def load_manifest(path: Path) -> dict[str, Any]:
    manifest = read_toml(path)
    task = manifest.get("task")
    if not isinstance(task, dict):
        raise ValueError(f"No [task] table found in {path}")
    flow = task.get("flow")
    if not isinstance(flow, list) or not all(isinstance(item, str) for item in flow):
        raise ValueError("[task].flow must be a list of role names")
    skip = task.get("skip", [])
    if skip and (not isinstance(skip, list) or not all(isinstance(item, str) for item in skip)):
        raise ValueError("[task].skip must be a list of role names")
    return manifest


def planned_flow(manifest: dict[str, Any]) -> list[str]:
    task = manifest["task"]
    skip = set(task.get("skip", []))
    flow = [role for role in task["flow"] if role not in skip]
    dependencies = task.get("dependencies", {})
    if not dependencies:
        return flow
    if not isinstance(dependencies, dict):
        raise ValueError("[task].dependencies must be a table of role = [dependencies]")
    return dag_order(flow, dependencies)


def dag_order(flow: list[str], dependencies: dict[str, Any]) -> list[str]:
    flow_set = set(flow)
    deps: dict[str, set[str]] = {role: set() for role in flow}
    for role, raw_deps in dependencies.items():
        if role not in flow_set:
            continue
        if isinstance(raw_deps, str):
            raw_list = [raw_deps]
        elif isinstance(raw_deps, list) and all(isinstance(item, str) for item in raw_deps):
            raw_list = raw_deps
        else:
            raise ValueError(f"[task.dependencies].{role} must be a string or list of strings")
        deps[role] = {dep for dep in raw_list if dep in flow_set}

    ordered: list[str] = []
    remaining = list(flow)
    while remaining:
        ready = [role for role in remaining if deps[role].issubset(ordered)]
        if not ready:
            raise ValueError("task dependency cycle detected")
        for role in ready:
            ordered.append(role)
            remaining.remove(role)
    return ordered


def safe_role_name(role_name: str) -> str:
    safe = SAFE_NAME_RE.sub("-", role_name).strip(".-")
    return safe or "role"


def run_id() -> str:
    return f"{datetime.now().strftime('%Y%m%dT%H%M')}-{secrets.token_hex(3)}"


def create_run_dir(new_run_id: str) -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RUNS_DIR / f".{new_run_id}.tmp-{os.getpid()}-{secrets.token_hex(3)}"
    final = RUNS_DIR / new_run_id
    tmp.mkdir()
    try:
        os.replace(tmp, final)
    except FileExistsError:
        tmp.rmdir()
        raise
    return final


def resolve_context_path(raw_path: str) -> Path:
    candidate = Path(raw_path)
    path = candidate if candidate.is_absolute() else PROJECT_ROOT / candidate
    # A1 (DeepSeek audit): on Windows `Path.resolve()` does NOT follow
    # directory junctions, so a junction inside PROJECT_ROOT pointing
    # outside would bypass the relative_to check. `os.path.realpath`
    # resolves junctions on Python 3.8+ and symlinks on all platforms.
    resolved = Path(os.path.realpath(path))
    project_real = Path(os.path.realpath(PROJECT_ROOT))
    try:
        resolved.relative_to(project_real)
    except ValueError as exc:
        raise ValueError(f"context file escapes project root: {raw_path}") from exc
    return resolved


def read_context_files(manifest: dict[str, Any]) -> str:
    files = manifest["task"].get("context_files", [])
    if not files:
        return "No extra context files."
    if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
        raise ValueError("[task].context_files must be a list of paths")
    blocks: list[str] = []
    for raw in files:
        path = resolve_context_path(raw)
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        if not path.exists():
            blocks.append(f"----- BEGIN {rel} -----\nMISSING CONTEXT FILE\n----- END {rel} -----")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        blocks.append(f"----- BEGIN {rel} -----\n{text}\n----- END {rel} -----")
    return "\n\n".join(blocks)


def build_prompt(
    role_name: str,
    role_config: dict[str, Any],
    manifest: dict[str, Any],
    manifest_path: Path,
    prior_deliverables: list[Path],
) -> str:
    task = manifest["task"]
    acceptance = task.get("acceptance", {})
    criteria = acceptance.get("criteria", "") if isinstance(acceptance, dict) else ""
    prior_blocks = []
    for path in prior_deliverables:
        text = path.read_text(encoding="utf-8", errors="replace")
        prior_blocks.append(f"----- BEGIN {path.name} -----\n{text}\n----- END {path.name} -----")
    prior_text = "\n\n".join(prior_blocks) if prior_blocks else "No prior role deliverables."
    deliverables = role_config.get("deliverables", [])
    if isinstance(deliverables, list):
        deliverables_text = ", ".join(str(item) for item in deliverables) or "Not specified."
    else:
        deliverables_text = str(deliverables)
    return f"""# neon-legion role invocation

## Role
Name: {role_name}
Provider: {role_config.get("provider", "unknown")}
Model: {role_config.get("model", "unknown")}
Sandbox: {role_config.get("sandbox", "read-only")}
Expected deliverables: {deliverables_text}

## Backstory
{role_config.get("backstory", "")}

## Goal
{role_config.get("goal", "")}

## Canonical Task
Manifest: {manifest_path}
Title: {task.get("title", "")}

{task.get("description", "")}

## Acceptance Criteria
{criteria or "Not specified."}

## Extra Context
{read_context_files(manifest)}

## Prior Deliverables
{prior_text}

## Instructions
Produce the deliverable for this role only. Cite concrete files and commands
when relevant. Keep the output in Markdown.
"""


def init_state(new_run_id: str, manifest_path: Path, roles_path: Path, flow: list[str]) -> dict[str, Any]:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    return {
        "run_id": new_run_id,
        "status": "running",
        "created_at": now,
        "updated_at": now,
        "manifest_path": str(manifest_path),
        "roles_path": str(roles_path),
        "flow": flow,
        "next_index": 0,
        "steps": [
            {"index": index, "role": role, "status": "pending"}
            for index, role in enumerate(flow)
        ],
    }


def update_state(run_dir: Path, state: dict[str, Any], status: str | None = None) -> None:
    if status is not None:
        state["status"] = status
    state["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    atomic_write_json(run_dir / "state.json", state)


def output_path_for(run_dir: Path, index: int, role_name: str) -> Path:
    return run_dir / f"{index + 1:02d}-{safe_role_name(role_name)}.md"


def write_error_file(run_dir: Path, index: int, role_name: str, result: dict[str, Any]) -> Path:
    path = run_dir / f"{index + 1:02d}-{safe_role_name(role_name)}.error.md"
    body = f"""# {role_name} failed

- exit_code: {result.get("exit_code")}
- duration_ms: {result.get("duration_ms")}
- output_path: {result.get("output_path")}

## Error
{result.get("error") or "Unknown error"}
"""
    atomic_write_text(path, body)
    return path


def prior_outputs(state: dict[str, Any], run_dir: Path, before_index: int) -> list[Path]:
    paths: list[Path] = []
    for step in state["steps"][:before_index]:
        if step.get("status") != "completed":
            continue
        raw = step.get("output_path")
        if not isinstance(raw, str):
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = run_dir / path
        if path.exists():
            paths.append(path)
    return paths


def execute_flow(
    run_dir: Path,
    state: dict[str, Any],
    roles: dict[str, Any],
    manifest: dict[str, Any],
    manifest_path: Path,
) -> int:
    flow: list[str] = state["flow"]
    for index in range(int(state.get("next_index", 0)), len(flow)):
        role_name = flow[index]
        role_config = dict(roles[role_name])
        role_config["name"] = role_name
        out_path = output_path_for(run_dir, index, role_name)
        step = state["steps"][index]

        if step.get("status") == "waiting_for_human":
            if not out_path.exists():
                print(f"run {state['run_id']} waiting for human response: {out_path}")
                return EX_CONFIG
            step.update({"status": "completed", "output_path": str(out_path)})
            state["next_index"] = index + 1
            update_state(run_dir, state, "running")
            continue

        prompt = build_prompt(role_name, role_config, manifest, manifest_path, prior_outputs(state, run_dir, index))
        print(f"running {index + 1:02d}/{len(flow):02d}: {role_name} via {role_config.get('invocation')}")
        step.update({"status": "running", "output_path": str(out_path), "started_at": time.time()})
        update_state(run_dir, state, "running")
        result = invoke(role_config, prompt, out_path)
        step["result"] = result

        if result.get("waiting_for_human"):
            step.update(
                {
                    "status": "waiting_for_human",
                    "prompt_path": result.get("output_path"),
                    "response_path": result.get("response_path", str(out_path)),
                }
            )
            update_state(run_dir, state, "waiting_for_human")
            print(f"paused run {state['run_id']}; resume with: py tools/orchestrate.py resume {state['run_id']}")
            return EX_CONFIG

        if not result.get("ok"):
            error_path = write_error_file(run_dir, index, role_name, result)
            step.update({"status": "failed", "error_path": str(error_path)})
            update_state(run_dir, state, "failed")
            # E2 (DeepSeek audit): include run_id so user can inspect run dir.
            print(f"failed at role {role_name}; run_id={state['run_id']}; error written to {error_path}")
            return 1

        step.update({"status": "completed", "output_path": str(out_path)})
        state["next_index"] = index + 1
        update_state(run_dir, state, "running")

    update_state(run_dir, state, "completed")
    print_summary(state, run_dir)
    return 0


def print_summary(state: dict[str, Any], run_dir: Path) -> None:
    print(f"run_id={state['run_id']} status={state['status']}")
    print(f"outputs={run_dir}")
    for step in state["steps"]:
        path = step.get("output_path") or step.get("prompt_path") or ""
        print(f"- {step['index'] + 1:02d} {step['role']}: {step['status']} {path}")


def print_roles(roles: dict[str, Any], source: Path) -> None:
    print(f"roles_source={source}")
    for name, cfg in roles.items():
        print(f"- {name}: provider={cfg.get('provider')} model={cfg.get('model', '')} invocation={cfg.get('invocation')}")


def validate_flow(flow: list[str], roles: dict[str, Any]) -> None:
    missing = [role for role in flow if role not in roles]
    if missing:
        raise ValueError("manifest references unknown roles: " + ", ".join(missing))


def command_run(args: argparse.Namespace) -> int:
    roles, roles_path = load_roles()
    if getattr(args, "list_roles", False):
        print_roles(roles, roles_path)
        return 0
    if args.manifest is None:
        # D4 (DeepSeek audit): manifest is required for actual run; only
        # --list-roles is OK without it.
        print("error: manifest path required (or pass --list-roles)", file=sys.stderr)
        return 2
    manifest_path = (PROJECT_ROOT / args.manifest).resolve() if not args.manifest.is_absolute() else args.manifest
    manifest = load_manifest(manifest_path)
    flow = planned_flow(manifest)
    validate_flow(flow, roles)
    if args.dry_run:
        print(f"dry_run=true manifest={manifest_path}")
        print_roles(roles, roles_path)
        print("planned_flow=" + " -> ".join(flow))
        return 0

    for _attempt in range(10):
        new_run_id = run_id()
        final = RUNS_DIR / new_run_id
        if not final.exists():
            try:
                run_dir = create_run_dir(new_run_id)
                break
            except FileExistsError:
                continue
    else:
        raise RuntimeError("could not allocate a unique run id")

    atomic_write_text(run_dir / "manifest.used.toml", manifest_path.read_text(encoding="utf-8"))
    atomic_write_text(run_dir / "roles.used.toml", roles_path.read_text(encoding="utf-8"))
    state = init_state(new_run_id, run_dir / "manifest.used.toml", run_dir / "roles.used.toml", flow)
    update_state(run_dir, state, "running")
    return execute_flow(run_dir, state, roles, manifest, run_dir / "manifest.used.toml")


def command_resume(args: argparse.Namespace) -> int:
    run_dir = RUNS_DIR / args.run_id
    state_path = run_dir / "state.json"
    if not state_path.exists():
        raise FileNotFoundError(f"run state not found: {state_path}")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("status") == "completed":
        print_summary(state, run_dir)
        return 0
    if state.get("status") not in {"waiting_for_human", "running"}:
        print_summary(state, run_dir)
        return 1
    roles = read_toml(run_dir / "roles.used.toml")["role"]
    manifest = load_manifest(run_dir / "manifest.used.toml")
    return execute_flow(run_dir, state, roles, manifest, run_dir / "manifest.used.toml")


def command_status(args: argparse.Namespace) -> int:
    run_dir = RUNS_DIR / args.run_id
    state_path = run_dir / "state.json"
    if not state_path.exists():
        raise FileNotFoundError(f"run state not found: {state_path}")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    print_summary(state, run_dir)
    return 0


def command_list(_args: argparse.Namespace) -> int:
    if not RUNS_DIR.exists():
        print("no runs")
        return 0
    for run_dir in sorted((p for p in RUNS_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")), reverse=True):
        state_path = run_dir / "state.json"
        if not state_path.exists():
            print(f"{run_dir.name} status=unknown")
            continue
        state = json.loads(state_path.read_text(encoding="utf-8"))
        title = ""
        manifest_path = run_dir / "manifest.used.toml"
        if manifest_path.exists():
            try:
                title = read_toml(manifest_path).get("task", {}).get("title", "")
            except Exception:
                title = ""
        print(f"{run_dir.name} status={state.get('status')} updated={state.get('updated_at')} title={title}")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-roles", action="store_true", help="print parsed role definitions and exit")
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="start a new orchestrator run")
    # D4 (DeepSeek audit): manifest is nargs='?' so `run --list-roles` works
    # without requiring a dummy manifest path.
    run.add_argument("manifest", type=Path, nargs="?", default=None)
    run.add_argument("--dry-run", action="store_true", help="print the planned flow without invoking roles")
    run.add_argument("--list-roles", action="store_true", help="print parsed role definitions and exit")
    resume = sub.add_parser("resume", help="resume a paused human-relay run")
    resume.add_argument("run_id")
    status = sub.add_parser("status", help="show one run")
    status.add_argument("run_id")
    sub.add_parser("list", help="list runs")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.list_roles and args.command is None:
            roles, roles_path = load_roles()
            print_roles(roles, roles_path)
            return 0
        if args.command == "run":
            return command_run(args)
        if args.command == "resume":
            return command_resume(args)
        if args.command == "status":
            return command_status(args)
        if args.command == "list":
            return command_list(args)
        raise SystemExit("command required: run, resume, status, list, or --list-roles")
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
