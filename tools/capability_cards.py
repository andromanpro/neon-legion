#!/usr/bin/env python
"""Render one capability-card markdown file per agent from reputation.json.

`reputation.json` (produced by `tools/reputation_ledger.py`) carries one entry
per (role, agent) pair: runs, successes, success_rate, mean_cost_usd,
mean_duration_ms, non_success_counts, last_run_at. This tool groups those
entries by AGENT and writes a single markdown card per agent into the
configured output directory.

Reads only. Never modifies `roles.toml`. The companion `roles.suggested.toml`
draft is produced by `tools/reputation_ledger.py` (same scoring backend,
different rendering surface) — this module does not duplicate that work.

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        default=str(PROJECT_ROOT / "tracker" / "reputation.json"),
        help="Path to reputation.json (produced by tools/reputation_ledger.py).",
    )
    parser.add_argument(
        "--output-dir",
        default=str(PROJECT_ROOT / "docs" / "capability"),
        help="Directory to write per-agent capability cards.",
    )
    args = parser.parse_args(argv)

    input_path = Path(args.input)
    output_dir = Path(args.output_dir)

    if not input_path.exists():
        print(f"[capability-cards] input not found: {input_path}", file=sys.stderr)
        return 2

    payload = json.loads(input_path.read_text(encoding="utf-8"))
    ledger = payload.get("ledger") or []
    if not ledger:
        print("[capability-cards] ledger empty; nothing to render.", file=sys.stderr)
        return 0

    grouped = group_by_agent(ledger)
    written: list[Path] = []
    for agent, entries in sorted(grouped.items()):
        card = render_card(
            agent=agent,
            entries=entries,
            generated_at=payload.get("generated_at") or _now_iso(),
            global_summary=payload.get("summary") or {},
        )
        target = output_dir / f"{slugify(agent)}.md"
        atomic_write(target, card)
        written.append(target)
        print(f"[capability-cards] wrote {target}")

    print(f"[capability-cards] agents={len(grouped)} cards={len(written)}")
    return 0


def group_by_agent(ledger: list[dict]) -> dict[str, list[dict]]:
    """Bucket ledger entries by agent name."""
    out: dict[str, list[dict]] = defaultdict(list)
    for entry in ledger:
        agent = entry.get("agent") or "unknown"
        out[agent].append(entry)
    return dict(out)


def render_card(
    *,
    agent: str,
    entries: list[dict],
    generated_at: str,
    global_summary: dict,
) -> str:
    """Render a single agent's capability card."""
    total_runs = sum(_safe_int(e.get("runs")) for e in entries)
    total_successes = sum(_safe_int(e.get("successes")) for e in entries)
    success_rate = (total_successes / total_runs) if total_runs > 0 else 0.0
    roles_played = sorted({e.get("role", "?") for e in entries})

    failure_counts: dict[str, int] = defaultdict(int)
    for entry in entries:
        for mode, count in (entry.get("non_success_counts") or {}).items():
            failure_counts[mode] += _safe_int(count)
    top_failures = sorted(failure_counts.items(), key=lambda kv: kv[1], reverse=True)[:3]

    sparse_note = ""
    if global_summary.get("sparse"):
        sparse_note = (
            "_Note: the underlying ledger is **sparse** "
            f"({global_summary.get('ledger_entries')} entries from "
            f"{global_summary.get('total_runs_scanned')} runs). "
            "Treat these numbers as directional, not authoritative._\n\n"
        )

    lines: list[str] = []
    lines.append(f"# Capability card — `{agent}`")
    lines.append("")
    lines.append(f"_Source: `reputation.json` snapshot of {generated_at}_  ")
    lines.append("_Reads `tracker/reputation.json`; never touches `roles.toml`._")
    lines.append("")
    if sparse_note:
        lines.append(sparse_note.rstrip())
        lines.append("")
    lines.append(f"**Roles played:** {', '.join(f'`{r}`' for r in roles_played) or 'none'}")
    lines.append("")
    lines.append(
        f"**Aggregate over all roles:** {total_successes}/{total_runs} runs successful "
        f"({_pct(success_rate)})"
    )
    lines.append("")
    lines.append("## Per-role detail")
    lines.append("")
    lines.append("| Role | Runs | Success rate | Median cost | Median latency | Last run |")
    lines.append("|---|---:|---:|---:|---:|---|")
    for entry in sorted(entries, key=lambda e: (e.get("role", ""), e.get("task_fingerprint", ""))):
        role = entry.get("role", "?")
        runs = _safe_int(entry.get("runs"))
        rate = _safe_float(entry.get("success_rate"))
        cost = entry.get("mean_cost_usd")
        latency_ms = entry.get("mean_duration_ms")
        last_run = entry.get("last_run_at") or ""
        lines.append(
            "| `{role}` | {runs} | {rate} | {cost} | {latency} | {last} |".format(
                role=role,
                runs=runs,
                rate=_pct(rate),
                cost=_fmt_cost(cost),
                latency=_fmt_latency(latency_ms),
                last=last_run[:19] if last_run else "—",
            )
        )
    lines.append("")
    lines.append("## Top failure modes")
    lines.append("")
    if not top_failures:
        lines.append("_None recorded._")
    else:
        for mode, count in top_failures:
            lines.append(f"- `{mode}` × {count}")
    lines.append("")
    lines.append(
        "_For role suggestions across all agents, see "
        "`orchestrate-runs/roles.suggested.toml` "
        "(generated by `tools/reputation_ledger.py`)._"
    )
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


def slugify(value: str) -> str:
    """Filesystem-safe lowercase token for use as a filename stem."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-_.")
    return slug.lower() or "unknown"


# --- internals ----------------------------------------------------------------


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_float(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _pct(value: float) -> str:
    if not isinstance(value, (int, float)) or math.isnan(value):
        return "—"
    return f"{value * 100:.0f}%"


def _fmt_cost(value: object) -> str:
    if value is None:
        return "—"
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"${f:.4f}"


def _fmt_latency(value: object) -> str:
    if value is None:
        return "—"
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return "—"
    if ms < 1000:
        return f"{ms} ms"
    return f"{ms / 1000:.1f} s"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


if __name__ == "__main__":
    raise SystemExit(main())
