#!/usr/bin/env python
"""Render a consensus matrix for an orchestrate run.

Reads three role deliverables (`01-architect.md`, `02-developer.md`,
`03-reviewer.md`) from a run directory, extracts candidate findings from each
(H2/H3 headings + top-level bullet points), then renders a markdown table:

    | # | Finding                | Architect | Developer | Reviewer |

Cells: ✅ raised the finding, 🤝 corroborated (high token overlap with a
finding raised by another role), · silent.

Each row carries a stable 8-char `fid:` prefix so future runs can re-check
"aged well / aged badly" by matching the same id (hash of normalized text).

Reads, never writes back into role files. Output goes to `consensus.md` in
the same directory. Stdlib only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROLES = (
    ("Architect", "01-architect.md"),
    ("Developer", "02-developer.md"),
    ("Reviewer", "03-reviewer.md"),
)

# Tokens that carry no semantic signal for agreement detection.
STOP_TOKENS = frozenset(
    """
    the a an and or but if then else not no yes is are was were be been being
    of to in on at by for from with into onto off out over under above below
    this that these those it its as such so than too very can may might shall
    should would could will need needs has have had do does did done done
    we us our you your they them their he she his her i me my mine ours yours
    here there where when why how what who which whose
    also still yet just only even both either neither
    most some many few all any each every other another
    using use used uses using via per about across between within among
    sample run runs ran section sections page pages line lines item items
    """.split()
)

WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9_]+")
# Picked empirically: ≥0.4 catches "atomic writes missing on snapshot writer"
# vs "atomic writes still inconsistent across some snapshot helpers" (Jaccard
# 0.428 after stopword removal). Higher thresholds (0.55) split clearly-related
# pairs; lower thresholds (0.3) start collapsing unrelated mentions of common
# domain nouns like "snapshot" or "config".
JACCARD_THRESHOLD = 0.4


@dataclass(frozen=True)
class Finding:
    """A single candidate finding extracted from a role file."""

    fid: str          # stable 8-char id (hash of normalized tokens)
    role: str         # which role raised it
    title: str        # display text (the heading / bullet line)
    tokens: frozenset[str]  # normalized token set for similarity


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", help="Directory containing 01-architect.md, 02-developer.md, 03-reviewer.md.")
    parser.add_argument(
        "--output",
        default=None,
        help="Override output path (defaults to <run_dir>/consensus.md).",
    )
    parser.add_argument(
        "--state",
        default=None,
        help="Optional path to an orchestrate run state.json. Recorded in the report for traceability.",
    )
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        print(f"[consensus] not a directory: {run_dir}", file=sys.stderr)
        return 2

    findings_by_role: dict[str, list[Finding]] = {}
    missing: list[str] = []
    for role, filename in ROLES:
        path = run_dir / filename
        if not path.exists():
            missing.append(filename)
            findings_by_role[role] = []
            continue
        findings_by_role[role] = extract_findings(role, path.read_text(encoding="utf-8"))

    matrix = build_matrix(findings_by_role)
    state_meta = _load_state(args.state or (run_dir / "state.json"))
    report = render_report(
        run_dir=run_dir,
        matrix=matrix,
        findings_by_role=findings_by_role,
        missing=missing,
        state_meta=state_meta,
    )

    output = Path(args.output) if args.output else run_dir / "consensus.md"
    atomic_write(output, report)
    print(f"[consensus] wrote {output}")
    print(
        f"[consensus] roles={len(findings_by_role)} "
        f"findings={sum(len(v) for v in findings_by_role.values())} "
        f"rows={len(matrix)}"
    )
    return 0


def extract_findings(role: str, text: str) -> list[Finding]:
    """Extract candidate findings (H2/H3 headings + top-level bullets)."""
    findings: list[Finding] = []
    in_code = False
    for raw in text.splitlines():
        line = raw.rstrip()
        # CommonMark fences come in two flavors: ` ``` ` and `~~~`. Either
        # opens/closes a code block. Indented fences also count — strip
        # leading whitespace before the check (DeepSeek MED).
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code = not in_code
            continue
        if in_code:
            continue
        title = _candidate_title(line)
        if title is None:
            continue
        tokens = _normalize_tokens(title)
        if not tokens:
            continue
        fid = _finding_id(tokens)
        findings.append(Finding(fid=fid, role=role, title=title, tokens=frozenset(tokens)))
    return _dedupe(findings)


def build_matrix(findings_by_role: dict[str, list[Finding]]) -> list[dict]:
    """Build a row-per-canonical-finding matrix.

    Each row picks the first occurrence as the canonical text, then marks
    every role as raised / corroborated / silent based on token overlap.
    """
    canonical: list[Finding] = []
    canonical_ids: set[str] = set()
    flat: list[Finding] = []
    for role, items in findings_by_role.items():
        flat.extend(items)

    # Stable pass: first time we see a finding, it becomes canonical. Later
    # findings get merged in by Jaccard similarity.
    for finding in flat:
        if finding.fid in canonical_ids:
            continue
        if any(_jaccard(finding.tokens, c.tokens) >= JACCARD_THRESHOLD for c in canonical):
            continue
        canonical.append(finding)
        canonical_ids.add(finding.fid)

    rows = []
    for finding in canonical:
        row = {"fid": finding.fid, "title": finding.title, "cells": {}}
        for role in findings_by_role:
            row["cells"][role] = _classify(finding, findings_by_role[role])
        rows.append(row)
    return rows


def render_report(
    *,
    run_dir: Path,
    matrix: list[dict],
    findings_by_role: dict[str, list[Finding]],
    missing: list[str],
    state_meta: dict,
) -> str:
    """Render the consensus.md markdown report."""
    role_names = list(findings_by_role.keys())
    lines: list[str] = []
    lines.append("# Consensus Matrix")
    lines.append("")
    lines.append(f"_Run dir: `{run_dir.as_posix()}`_  ")
    lines.append(f"_Generated: {datetime.now().astimezone().isoformat(timespec='seconds')}_  ")
    if state_meta:
        run_id = state_meta.get("run_id") or state_meta.get("id") or "unknown"
        lines.append(f"_Run id: `{run_id}`_  ")
    lines.append("")

    if missing:
        lines.append(f"> **Note**: missing role file(s): {', '.join(missing)}. "
                     f"Their column appears empty.")
        lines.append("")

    if not matrix:
        lines.append("_No findings detected. Role files contain no H2/H3 headings or "
                     "top-level bullets to extract._")
        lines.append("")
        lines.append("Tip: structure each role deliverable with `## Findings` or "
                     "`- bullet` lines so the matrix has rows to render.")
        return "\n".join(lines) + "\n"

    # Counts row
    counts = {r: len(v) for r, v in findings_by_role.items()}
    counts_line = " · ".join(f"{r}: {counts[r]}" for r in role_names)
    lines.append(f"**Raw finding counts** — {counts_line}")
    lines.append("")

    header = "| # | fid | Finding | " + " | ".join(role_names) + " |"
    sep = "|---|---|---|" + "|".join(["---"] * len(role_names)) + "|"
    lines.append(header)
    lines.append(sep)
    for idx, row in enumerate(matrix, 1):
        cells = " | ".join(_cell_glyph(row["cells"][r]) for r in role_names)
        title = row["title"].replace("|", "\\|")
        lines.append(f"| {idx} | `{row['fid']}` | {title} | {cells} |")

    lines.append("")
    lines.append("Legend: ✅ raised · 🤝 corroborated (high token overlap) · · silent")
    lines.append("")
    lines.append("**Disagreements** (one role silent while ≥1 other raised) and "
                 "**solo findings** can be re-checked in future runs — the `fid` "
                 "is a stable hash of normalized text.")
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


def _candidate_title(line: str) -> str | None:
    stripped = line.strip()
    if not stripped:
        return None
    # H2 / H3 headings
    if stripped.startswith("###"):
        return stripped.lstrip("# ").rstrip()
    if stripped.startswith("##") and not stripped.startswith("###"):
        return stripped.lstrip("# ").rstrip()
    # Top-level unordered bullets (`- `, `* `, `+ `) — at the start of a line,
    # not in the middle of a paragraph. Nested bullets ignored.
    m = re.match(r"^[\-*+]\s+(.+)", line)
    if m:
        return m.group(1).rstrip()
    # Numbered list items at column 0
    m = re.match(r"^\d+\.\s+(.+)", line)
    if m:
        return m.group(1).rstrip()
    return None


def _normalize_tokens(text: str) -> list[str]:
    return [
        t.lower()
        for t in WORD_RE.findall(text)
        if t.lower() not in STOP_TOKENS and len(t) > 1
    ]


def _finding_id(tokens: list[str]) -> str:
    canonical = " ".join(sorted(set(tokens)))
    return hashlib.blake2b(canonical.encode("utf-8"), digest_size=4).hexdigest()


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _classify(canonical: Finding, role_findings: list[Finding]) -> str:
    """Return one of: 'raised' | 'corroborated' | 'silent'."""
    if not role_findings:
        return "silent"
    if any(f.fid == canonical.fid for f in role_findings):
        return "raised"
    if any(_jaccard(canonical.tokens, f.tokens) >= JACCARD_THRESHOLD for f in role_findings):
        return "corroborated"
    return "silent"


def _cell_glyph(state: str) -> str:
    return {"raised": "✅", "corroborated": "🤝", "silent": "·"}.get(state, "·")


def _dedupe(findings: list[Finding]) -> list[Finding]:
    seen: set[str] = set()
    out: list[Finding] = []
    for f in findings:
        if f.fid in seen:
            continue
        seen.add(f.fid)
        out.append(f)
    return out


def _load_state(path: Path | str | None) -> dict:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


if __name__ == "__main__":
    raise SystemExit(main())
