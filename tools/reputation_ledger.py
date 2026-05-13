#!/usr/bin/env python
"""Build an observation-only reputation ledger from orchestrator run history."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from tools import config as cfg  # noqa: E402


RUNS_DIR = PROJECT_ROOT / "orchestrate-runs"
REPUTATION_PATH = PROJECT_ROOT / "tracker" / "reputation.json"
SUGGESTED_PATH = PROJECT_ROOT / "orchestrate-runs" / "roles.suggested.toml"
HEADER_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
SLUG_RE = re.compile(r"[^A-Za-z0-9]+")


def build_ledger(
    orchestrate_runs_dir: Path,
    *,
    lookback_days: int = 30,
    min_runs: int = 1,
    now: datetime | None = None,
) -> dict:
    """Read state.json files, return reputation.json payload."""
    current = _aware(now or datetime.now().astimezone())
    cutoff = current - timedelta(days=max(1, int(lookback_days)))
    buckets: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {"runs": 0, "successes": 0, "durations": [], "last": None, "fingerprint": "unknown"}
    )
    scanned = 0

    for run_dir, state in _iter_states(orchestrate_runs_dir):
        if _run_at(run_dir / "state.json", state, current.tzinfo) < cutoff:
            continue
        scanned += 1
        roles = _read_roles(run_dir / "roles.used.toml")
        for step in state.get("steps", []):
            if not isinstance(step, dict) or not isinstance(step.get("status"), str):
                continue
            status = step["status"]
            # DeepSeek MED #3 on PR #85: cancelled/expired/timed-out steps
            # must count in the runs denominator (they're attempts that
            # didn't succeed) — otherwise success_rate is inflated by
            # silently dropping every non-success path. Only skip steps
            # with no status at all.
            if status not in {"completed", "failed", "cancelled", "expired", "timed_out"}:
                continue
            role = step.get("role") if isinstance(step.get("role"), str) else "unknown"
            agent = _agent(roles.get(role, {}))
            item = buckets[(role, agent)]
            item["runs"] += 1
            result = step.get("result") if isinstance(step.get("result"), dict) else None
            if result is not None and result.get("ok") is True and status == "completed":
                item["successes"] += 1
            else:
                item.setdefault("non_success_statuses", {}).setdefault(status, 0)
                item["non_success_statuses"][status] += 1
            if result is None:
                continue
            if isinstance(result.get("duration_ms"), (int, float)):
                item["durations"].append(float(result["duration_ms"]))
            started = _step_at(step, current.tzinfo)
            if started and (item["last"] is None or started > item["last"]):
                item["last"] = started
            if item["fingerprint"] == "unknown":
                item["fingerprint"] = _fingerprint(run_dir, step)

    ledger = []
    for (role, agent), item in sorted(buckets.items()):
        runs = item["runs"]
        durations = item["durations"]
        non_success = item.get("non_success_statuses", {})
        ledger.append(
            {
                "role": role,
                "agent": agent,
                "runs": runs,
                "successes": item["successes"],
                "success_rate": item["successes"] / runs if runs else 0.0,
                "mean_duration_ms": round(sum(durations) / len(durations)) if durations else None,
                "mean_cost_usd": None,
                "task_fingerprint": item["fingerprint"],
                "last_run_at": item["last"].isoformat(timespec="seconds") if item["last"] else None,
                # DeepSeek MED #3 on PR #85: surface non-success status breakdown so
                # consumers can distinguish a flaky agent (cancelled/timed_out) from
                # a buggy one (failed) without re-reading state.json.
                "non_success_counts": dict(sorted(non_success.items())),
            }
        )

    return {
        "schema_version": 1,
        "generated_at": current.isoformat(timespec="seconds"),
        "config": {"lookback_days": int(lookback_days), "min_runs": int(min_runs)},
        "ledger": ledger,
        "summary": {
            "total_runs_scanned": scanned,
            "ledger_entries": len(ledger),
            "sparse": scanned == 0 or scanned < int(min_runs),
        },
    }


def write_reputation(payload: dict, output_path: Path) -> None:
    """Atomic write."""
    _atomic_write(_path(output_path), json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def emit_roles_suggested(payload: dict, current_roles_toml_path: Path | None, output_path: Path) -> None:
    """Write roles.suggested.toml from the ledger payload."""
    roles = _read_roles(current_roles_toml_path) if current_roles_toml_path else {}
    summary = payload.get("summary", {})
    lines = [
        f"# Generated by tools/reputation_ledger.py at {payload.get('generated_at')}.",
        "# Never auto-applied - human reviews and copies edits into roles.toml.",
        "# Sample size: {n} runs. Marked `sparse=true` if N < min_runs. sparse={s}".format(
            n=summary.get("total_runs_scanned", 0),
            s=str(bool(summary.get("sparse", True))).lower(),
        ),
    ]
    if not roles or int(summary.get("total_runs_scanned", 0)) == 0:
        lines += ["# No historical runs found, suggestions unavailable yet.", ""]
        _atomic_write(_path(output_path), "\n".join(lines))
        return

    best = _best_by_role(payload.get("ledger", []))
    for role in sorted(roles):
        cfg_role = roles[role]
        current = _agent(cfg_role)
        entry = best.get(role)
        lines += ["", f"[role.{role}]"]
        if entry:
            lines.append(
                "# Score: success_rate={rate} over {runs} run{plural}, mean_duration={duration}".format(
                    rate=f"{round(float(entry['success_rate']) * 100):.0f}%",
                    runs=entry["runs"],
                    plural="" if entry["runs"] == 1 else "s",
                    duration=f"{entry['mean_duration_ms']}ms" if entry.get("mean_duration_ms") is not None else "unknown",
                )
            )
            lines.append(f"# Sample size: {entry['runs']} observations for this role")
            action = f"keep current ({current})" if entry["agent"] == current else f"switch to {entry['agent']}. Current={current}"
            lines.append(f"# Suggestion: {action}. Confidence: {_confidence(int(entry['runs']))}.")
        else:
            lines += ["# Score: no observations yet", "# Sample size: 0 observations for this role", f"# Suggestion: keep current ({current}). Confidence: none."]
        for key in ("provider", "model", "invocation", "sandbox"):
            if isinstance(cfg_role.get(key), str):
                lines.append(f"{key} = {json.dumps(cfg_role[key], ensure_ascii=False)}")
    _atomic_write(_path(output_path), "\n".join(lines) + "\n")


def main() -> int:
    """CLI entrypoint."""
    args = _parse_args()
    lookback = args.lookback_days if args.lookback_days is not None else cfg.get("reputation.lookback_days", 30, int)
    min_runs = args.min_runs if args.min_runs is not None else cfg.get("reputation.min_runs", 1, int)
    runs_dir = _path(args.runs_dir or cfg.get("reputation.runs_dir", str(RUNS_DIR), str))
    out_json = _path(args.output_reputation or cfg.get("reputation.output_reputation_path", str(REPUTATION_PATH), str))
    out_roles = _path(args.output_roles or cfg.get("reputation.output_roles_suggested_path", str(SUGGESTED_PATH), str))
    payload = build_ledger(runs_dir, lookback_days=lookback, min_runs=min_runs)
    write_reputation(payload, out_json)
    emit_roles_suggested(payload, _current_roles(runs_dir), out_roles)
    print(f"[reputation-ledger] wrote {out_json}", file=sys.stderr)
    print(f"[reputation-ledger] wrote {out_roles}", file=sys.stderr)
    print(f"[reputation-ledger] ledger_entries={payload['summary']['ledger_entries']} sparse={payload['summary']['sparse']}", file=sys.stderr)
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the agent reputation ledger from orchestrator history.")
    parser.add_argument("--runs-dir", type=Path, help="Directory containing orchestrate-runs/*/state.json.")
    parser.add_argument("--output-reputation", type=Path, help="Path to write reputation.json.")
    parser.add_argument("--output-roles", type=Path, help="Path to write roles.suggested.toml.")
    parser.add_argument("--lookback-days", type=int, help="How many days of orchestrator history to scan.")
    parser.add_argument("--min-runs", type=int, help="Minimum runs before sparse=false.")
    return parser.parse_args()


def _iter_states(runs_dir: Path) -> list[tuple[Path, dict]]:
    if not runs_dir.exists():
        return []
    states = []
    for run_dir in sorted(p for p in runs_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
        try:
            states.append((run_dir, json.loads((run_dir / "state.json").read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError):
            pass
    return states


def _read_roles(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            roles = tomllib.load(handle).get("role", {})
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return {str(k): v for k, v in roles.items() if isinstance(v, dict)} if isinstance(roles, dict) else {}


def _agent(role: dict[str, Any]) -> str:
    """Identify the agent backing a role from invocation/model strings.

    DeepSeek MED #2 on PR #85: don't conflate OpenAI API with Codex CLI;
    `openai` substring used to collapse to `codex`, which is wrong — they're
    distinct delivery systems. `openai` now stays as its own agent key; only
    `codex` substring routes to `codex`.
    """
    provider = role.get("provider") if isinstance(role.get("provider"), str) else ""
    text = " ".join(str(role.get(k, "")) for k in ("invocation", "model")).lower()
    for marker, agent in (
        ("claude", "claude"),
        ("anthropic", "claude"),
        ("codex", "codex"),       # Codex CLI specifically
        ("opencode", "opencode"),
        ("deepseek", "opencode"),
        ("openclaw", "openclaw"),
        ("openai", "openai"),     # OpenAI API direct — distinct from Codex CLI
        ("human", "human"),
    ):
        if marker in text:
            return agent
    return provider or "unknown"


def _wilson_lower_bound(successes: int, runs: int, z: float = 1.96) -> float:
    """95% Wilson score lower bound for a binomial proportion.

    DeepSeek HIGH #1 on PR #85: previous scoring tuple `(success_rate, runs,
    -duration)` made a 1-run 100% agent outrank a 10-run 90% agent. Wilson
    lower bound returns ~0.21 for 1/1, ~0.55 for 9/10, ~0.82 for 90/100,
    ~0.96 for 1000/1000 — small samples carry uncertainty in the score
    itself, not just in a confidence label.
    """
    if runs <= 0:
        return 0.0
    import math
    p = successes / runs
    n = runs
    numerator = p + z * z / (2 * n) - z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    denominator = 1 + z * z / n
    return max(0.0, numerator / denominator)


def _best_by_role(ledger: Any) -> dict[str, dict[str, Any]]:
    """Pick the best (role, agent) candidate per role.

    Scoring: Wilson 95% lower bound on success_rate as primary axis (DeepSeek
    HIGH #1 fix), with sample-size and lower-mean-duration as tie-breakers.
    Sample-size weighting is now intrinsic to the primary score, not just a
    secondary key.
    """
    best = {}
    for entry in ledger if isinstance(ledger, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("role"), str):
            continue
        successes = int(entry.get("successes") or 0)
        runs = int(entry.get("runs") or 0)
        wilson = _wilson_lower_bound(successes, runs)
        score = (wilson, runs, -(entry.get("mean_duration_ms") or 10**18))
        if entry["role"] not in best or score > best[entry["role"]][0]:
            best[entry["role"]] = (score, entry)
    return {role: entry for role, (_score, entry) in best.items()}


def _fingerprint(run_dir: Path, step: dict[str, Any]) -> str:
    for key in ("prompt_path", "output_path"):
        raw = step.get(key)
        path = Path(raw) if isinstance(raw, str) else None
        if path and not path.is_absolute():
            path = run_dir / path
        header = _first_header(path) if path else None
        if header:
            return SLUG_RE.sub("-", header.lower()).strip("-") or "unknown"
    return "unknown"


def _first_header(path: Path) -> str | None:
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = HEADER_RE.match(line)
            if match:
                return match.group(1).strip()
    except OSError:
        return None
    return None


def _run_at(state_path: Path, state: dict, tz: Any) -> datetime:
    dates = [_step_at(step, tz) for step in state.get("steps", []) if isinstance(step, dict)]
    dates += [_parse_dt(state.get(key), tz) for key in ("created_at", "updated_at")]
    dates = [date for date in dates if date is not None]
    if dates:
        return max(dates)
    return datetime.fromtimestamp(state_path.stat().st_mtime, tz=tz)


def _step_at(step: dict[str, Any], tz: Any) -> datetime | None:
    raw = step.get("started_at")
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw, tz=tz)
    return _parse_dt(raw, tz)


def _parse_dt(raw: Any, tz: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return _aware(datetime.fromisoformat(raw), tz)
    except ValueError:
        return None


def _aware(value: datetime, tz: Any = None) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=tz or datetime.now().astimezone().tzinfo)
    return value.astimezone(tz) if tz else value.astimezone()


def _current_roles(runs_dir: Path) -> Path | None:
    if (PROJECT_ROOT / "roles.toml").exists():
        return PROJECT_ROOT / "roles.toml"
    paths = list(runs_dir.glob("*/roles.used.toml")) if runs_dir.exists() else []
    return max(paths, key=lambda p: p.stat().st_mtime) if paths else (PROJECT_ROOT / "roles.example.toml")


def _confidence(runs: int) -> str:
    if runs <= 1:
        return "low (single-run sample)"
    return "low (small sample)" if runs < 5 else "medium" if runs < 10 else "high"


def _path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if os.name == "nt" and path.root and not path.drive:
        return Path(f"{Path.cwd().drive}{path}")
    return path


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
