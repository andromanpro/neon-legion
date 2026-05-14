#!/usr/bin/env python
"""Propose reviewer-step adjustments to an orchestrator manifest.

Before running a manifest, look at each step's `(role, agent)` reputation
from `tracker/reputation.json`. High-risk steps get a `reviewer` injected
after them; low-risk repetitive steps may have a redundant trailing
reviewer suggested for removal.

**Default mode is `propose`**: writes `<manifest>.proposed.toml` next to
the original with a reasoning log embedded as TOML comments. The
original manifest is never modified. `auto` mode is out of MVP scope —
the issue acceptance explicitly demands proposal-only.

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from tools import config as cfg  # noqa: E402


DEFAULT_HIGH_RISK = 0.70
DEFAULT_MIN_RUNS_FOR_CONFIDENCE = 5
DEFAULT_TIMEOUT_RISK_RATIO = 0.20


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", help="Path to the orchestrator manifest TOML.")
    parser.add_argument(
        "--reputation",
        default=str(PROJECT_ROOT / "tracker" / "reputation.json"),
        help="Path to reputation.json produced by tools/reputation_ledger.py.",
    )
    parser.add_argument(
        "--roles",
        default=None,
        help="Path to roles.toml (defines role->agent assignment). "
             "Defaults to orchestrate-runs/roles.suggested.toml if present.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Override output path. Default: <manifest>.proposed.toml.",
    )
    parser.add_argument(
        "--high-risk-threshold",
        type=float,
        default=None,
        help="success_rate < threshold → HIGH risk. (config: disagreement_router.high_risk_threshold)",
    )
    parser.add_argument(
        "--min-runs",
        type=int,
        default=None,
        help="runs < min_runs → sparse / uncertain → MED risk.",
    )
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest)
    reputation_path = Path(args.reputation)
    if not manifest_path.exists():
        print(f"[disagreement-router] manifest not found: {manifest_path}", file=sys.stderr)
        return 2
    if not reputation_path.exists():
        print(f"[disagreement-router] reputation not found: {reputation_path}", file=sys.stderr)
        return 2

    high_risk = args.high_risk_threshold if args.high_risk_threshold is not None else cfg.get(
        "disagreement_router.high_risk_threshold", DEFAULT_HIGH_RISK, float
    )
    min_runs = args.min_runs if args.min_runs is not None else cfg.get(
        "disagreement_router.min_runs_for_confidence", DEFAULT_MIN_RUNS_FOR_CONFIDENCE, int
    )

    with manifest_path.open("rb") as f:
        manifest = tomllib.load(f)
    reputation = json.loads(reputation_path.read_text(encoding="utf-8"))
    roles_path = _resolve_roles_path(args.roles)
    role_assignments = _read_role_assignments(roles_path) if roles_path and roles_path.exists() else {}

    flow = list((manifest.get("task") or {}).get("flow") or [])
    if not flow:
        print("[disagreement-router] manifest has no task.flow; nothing to route.", file=sys.stderr)
        return 2

    decisions = score_flow(
        flow=flow,
        role_assignments=role_assignments,
        reputation_ledger=reputation.get("ledger") or [],
        high_risk_threshold=high_risk,
        min_runs_for_confidence=min_runs,
    )
    new_flow = apply_decisions(flow, decisions)

    output = Path(args.output) if args.output else manifest_path.with_suffix(".proposed.toml")
    text = render_proposed_manifest(
        original=manifest,
        new_flow=new_flow,
        decisions=decisions,
        manifest_path=manifest_path,
        config_used={
            "high_risk_threshold": high_risk,
            "min_runs_for_confidence": min_runs,
        },
    )
    atomic_write(output, text)
    print(f"[disagreement-router] wrote {output}")
    print(
        f"[disagreement-router] original_steps={len(flow)} "
        f"proposed_steps={len(new_flow)} "
        f"high_risk={sum(1 for d in decisions if d['risk'] == 'HIGH')} "
        f"med_risk={sum(1 for d in decisions if d['risk'] == 'MED')} "
        f"low_risk={sum(1 for d in decisions if d['risk'] == 'LOW')}"
    )
    return 0


def score_flow(
    *,
    flow: list[str],
    role_assignments: dict[str, str],
    reputation_ledger: list[dict],
    high_risk_threshold: float = DEFAULT_HIGH_RISK,
    min_runs_for_confidence: int = DEFAULT_MIN_RUNS_FOR_CONFIDENCE,
) -> list[dict]:
    """Return per-step risk decisions including reasoning.

    Each item: {
        "index": int,
        "role": str,
        "agent": str,        # may be "unknown" if not in roles_path
        "risk": "HIGH" | "MED" | "LOW",
        "reason": str,
        "action": "inject_reviewer" | "trim_reviewer" | "keep",
    }
    """
    ledger_by_pair = _ledger_by_pair(reputation_ledger)
    decisions: list[dict] = []
    # Cap consecutive reviewer injections — without this an all-unknown flow
    # of N steps gets N reviewers injected (e.g. ["a","r","b","r","c","r"]),
    # which is more cost than signal (DeepSeek MED).
    just_injected = False
    for idx, role in enumerate(flow):
        agent = role_assignments.get(role, "unknown")
        entry = ledger_by_pair.get((role, agent))
        risk, reason = _classify_risk(
            entry,
            high_risk_threshold=high_risk_threshold,
            min_runs_for_confidence=min_runs_for_confidence,
        )
        # Decide action: inject reviewer after HIGH/MED if there's no reviewer
        # already covering this step; trim trailing reviewer for LOW if redundant.
        next_role = flow[idx + 1] if idx + 1 < len(flow) else None
        if role == "reviewer":
            action = "keep"
            just_injected = False  # explicit reviewer in flow resets the cap
        elif risk in ("HIGH", "MED") and next_role != "reviewer":
            if just_injected:
                action = "keep"
                reason = f"{reason} (skipped — previous step already gets a reviewer; chain cap)"
                just_injected = False
            else:
                action = "inject_reviewer"
                just_injected = True
        elif risk == "LOW" and next_role == "reviewer":
            action = "trim_reviewer"
            just_injected = False
        else:
            action = "keep"
            just_injected = False
        decisions.append({
            "index": idx,
            "role": role,
            "agent": agent,
            "risk": risk,
            "reason": reason,
            "action": action,
        })
    return decisions


def apply_decisions(flow: list[str], decisions: list[dict]) -> list[str]:
    """Apply inject/trim decisions to produce a new flow."""
    skip_next_reviewer = set()
    for d in decisions:
        if d["action"] == "trim_reviewer":
            # Mark the index of the reviewer step to be skipped.
            skip_next_reviewer.add(d["index"] + 1)

    new_flow: list[str] = []
    for idx, role in enumerate(flow):
        if idx in skip_next_reviewer:
            continue
        new_flow.append(role)
        dec = next((d for d in decisions if d["index"] == idx), None)
        if dec and dec["action"] == "inject_reviewer":
            new_flow.append("reviewer")
    return new_flow


def render_proposed_manifest(
    *,
    original: dict,
    new_flow: list[str],
    decisions: list[dict],
    manifest_path: Path,
    config_used: dict,
) -> str:
    """Render the proposed manifest as TOML with reasoning log on top."""
    lines: list[str] = []
    lines.append(f"# Proposed manifest from tools/disagreement_router.py")
    lines.append(f"# Source: {manifest_path.as_posix()}")
    lines.append(f"# Generated: {datetime.now().astimezone().isoformat(timespec='seconds')}")
    lines.append(
        f"# Config: high_risk_threshold={config_used['high_risk_threshold']} "
        f"min_runs_for_confidence={config_used['min_runs_for_confidence']}"
    )
    lines.append("#")
    lines.append("# Reasoning log (per-step risk):")
    for d in decisions:
        lines.append(
            f"#   [{d['index']}] role={d['role']} agent={d['agent']} "
            f"risk={d['risk']} action={d['action']} — {d['reason']}"
        )
    lines.append("#")
    lines.append("# This file is a PROPOSAL. roles.toml is never modified.")
    lines.append("# Diff this against the original before adopting.")
    lines.append("")

    task = original.get("task") or {}
    lines.append("[task]")
    if "title" in task:
        lines.append(f"title = {_toml_str(task['title'])}")
    if "description" in task:
        lines.append(f'description = """\n{task["description"]}"""')
    flow_repr = ", ".join(_toml_str(r) for r in new_flow)
    lines.append(f"flow = [{flow_repr}]")
    if "context_files" in task:
        ctx = ", ".join(_toml_str(p) for p in task["context_files"])
        lines.append(f"context_files = [{ctx}]")
    if "acceptance" in task:
        accept = task["acceptance"]
        lines.append("")
        lines.append("[task.acceptance]")
        if "criteria" in accept:
            lines.append(f'criteria = """\n{accept["criteria"]}"""')

    return "\n".join(lines) + "\n"


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(content, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


# --- internals ----------------------------------------------------------------


def _ledger_by_pair(ledger: list[dict]) -> dict[tuple[str, str], dict]:
    """Index ledger entries by (role, agent). If multiple task-fingerprints exist
    for the same pair, the one with most runs wins (most-data-most-weight)."""
    by_pair: dict[tuple[str, str], dict] = {}
    for entry in ledger:
        key = (entry.get("role", "?"), entry.get("agent", "?"))
        existing = by_pair.get(key)
        if existing is None or (entry.get("runs") or 0) > (existing.get("runs") or 0):
            by_pair[key] = entry
    return by_pair


def _classify_risk(
    entry: dict | None,
    *,
    high_risk_threshold: float,
    min_runs_for_confidence: int,
) -> tuple[str, str]:
    """Risk + human reason. Conservative default when entry is missing."""
    if entry is None:
        return ("MED", "no reputation data for this (role, agent) pair")
    runs = int(entry.get("runs") or 0)
    successes = int(entry.get("successes") or 0)
    success_rate = float(entry.get("success_rate") or 0)
    failures = entry.get("non_success_counts") or {}
    timeout_count = int(failures.get("timeout") or 0)

    if runs == 0:
        return ("MED", "zero runs recorded — treat as uncertain")
    if runs < min_runs_for_confidence:
        return (
            "MED",
            f"sparse sample ({runs} runs < {min_runs_for_confidence}); raise threshold or accept proposal",
        )
    if success_rate < high_risk_threshold:
        return (
            "HIGH",
            f"success_rate={success_rate:.2f} below {high_risk_threshold} over {runs} runs",
        )
    if timeout_count >= 1 and (timeout_count / max(runs, 1)) >= DEFAULT_TIMEOUT_RISK_RATIO:
        return (
            "MED",
            f"timeouts={timeout_count}/{runs} ≥ {DEFAULT_TIMEOUT_RISK_RATIO * 100:.0f}% rate",
        )
    return (
        "LOW",
        f"success_rate={success_rate:.2f} over {runs} runs, no concerning failure modes",
    )


def _resolve_roles_path(explicit: str | None) -> Path | None:
    if explicit:
        return Path(explicit)
    default = PROJECT_ROOT / "orchestrate-runs" / "roles.suggested.toml"
    return default if default.exists() else None


def _read_role_assignments(roles_path: Path) -> dict[str, str]:
    """Thin wrapper around `tools.config.read_role_providers`.

    Centralized in `tools/config.py` to handle ALL TOML quote styles
    (DeepSeek MED — the old in-file regex only matched `"double"` quotes,
    silently giving agent="unknown" for `provider = 'single-quoted'` roles,
    which then defaulted to MED risk → inject_reviewer spam).
    """
    return cfg.read_role_providers(roles_path)


def _toml_str(value: str) -> str:
    """Render a TOML basic string literal."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


if __name__ == "__main__":
    raise SystemExit(main())
