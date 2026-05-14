#!/usr/bin/env python
"""Refresh the `<!-- START_STATS --> ... <!-- END_STATS -->` block in README.md
from the same snapshot.json the dashboard reads.

Idempotent: re-running with the same snapshot does not change the file.
--check mode: exit 0 if the README is already up to date, exit 1 otherwise
(suitable for CI / pre-commit). No writes outside the marker block.

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

START_MARKER = "<!-- START_STATS -->"
END_MARKER = "<!-- END_STATS -->"

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def format_stats_block(snapshot: dict) -> str:
    """Return the marker block content (without the marker lines themselves)."""
    totals = snapshot.get("totals") or {}
    productivity = snapshot.get("productivity") or {}
    sentiment = snapshot.get("sentiment") or {}

    days = _safe_int(totals.get("days"))
    calls = _safe_int(totals.get("calls"))
    cost = _safe_float(totals.get("cost_usd"))
    saved = _safe_float(totals.get("savings_usd"))
    multiplier = _safe_float(productivity.get("multiplier"))
    hours_saved = _safe_float(productivity.get("hours_saved"))

    top_day = sentiment.get("top_day") or {}
    top_date = top_day.get("date") or ""
    top_appr = sentiment.get("top_appreciation_day") or {}
    top_appr_date = top_appr.get("date") or ""

    profanity_total = _safe_int(sentiment.get("profanity_total"))
    appreciation_total = _safe_int(sentiment.get("appreciation_total"))

    period_start = totals.get("period_start") or ""
    period_end = totals.get("period_end") or ""

    lines = [
        f"**Past {days} days from the author's local instance** "
        f"(`{period_start}` → `{period_end}`)",
        "",
        f"- **{calls:,} AI calls** across Claude Code + Codex CLI + OpenClaw + OpenCode",
        f"- **${saved:,.0f} saved** vs equivalent API rate "
        f"(API would cost ${cost:,.0f}, subscriptions cost a fraction)",
        f"- **×{multiplier:.2f} productivity multiplier** "
        f"— {hours_saved:,.0f} human-hours of work compressed",
    ]
    if appreciation_total or profanity_total:
        ratio_note = ""
        if profanity_total > 0:
            ratio = appreciation_total / profanity_total
            ratio_note = f" — ratio {ratio:.0f}:1, mostly happy"
        elif appreciation_total > 0:
            ratio_note = " — zero profanity this window"
        lines.append(
            f"- **Sentiment markers:** {appreciation_total:,} thanks / "
            f"{profanity_total:,} swears{ratio_note}"
        )
    if top_date:
        top_profanity = _safe_int(top_day.get("profanity"))
        if top_profanity > 0:
            lines.append(
                f"- **Most stressed day:** {top_date} "
                f"({top_profanity} frustrated mentions — yes, we count them)"
            )
    if top_appr_date:
        top_appr_count = _safe_int(top_appr.get("appreciation"))
        if top_appr_count > 0:
            lines.append(
                f"- **Most grateful day:** {top_appr_date} "
                f"({top_appr_count} positive markers — we count those too)"
            )
    lines.append("")
    lines.append(
        "_Numbers refresh whenever the snapshot writer runs. Your mileage will "
        "vary; see the [dashboard](docs/screenshots/hero.png) for what it looks "
        "like locally._"
    )
    return "\n".join(lines)


def replace_block(readme_text: str, new_block: str) -> tuple[str, bool, str]:
    """Return (updated_text, changed, reason).

    `changed` is True iff the file content would differ. `reason` is a short
    diagnostic string suitable for --check output.

    Scans line-by-line tracking fenced-code-block state so markers that
    appear INSIDE a code block (e.g. a tutorial showing the marker syntax)
    are ignored. Only markers at non-fenced positions count (DeepSeek MED —
    naive `.index()` could eat content between a fence-embedded marker and
    the real one).
    """
    start_idx = _find_marker_outside_fences(readme_text, START_MARKER)
    if start_idx is None:
        return readme_text, False, "markers missing"
    end_idx = _find_marker_outside_fences(readme_text, END_MARKER, after=start_idx)
    if end_idx is None:
        return readme_text, False, "markers missing"
    if end_idx < start_idx:
        return readme_text, False, "end-marker precedes start-marker"

    # Preserve the marker lines verbatim. Place new content between them with
    # blank lines around the body for Markdown readability.
    head = readme_text[: start_idx + len(START_MARKER)]
    tail = readme_text[end_idx:]
    new_section = head + "\n\n" + new_block.rstrip() + "\n\n" + tail
    changed = new_section != readme_text
    reason = "block updated" if changed else "block already current"
    return new_section, changed, reason


def _find_marker_outside_fences(text: str, marker: str, *, after: int = 0) -> int | None:
    """Return the absolute index of `marker` ignoring matches inside ``` or ~~~ fences.

    A fence opens / closes when a line (after lstrip) starts with three or
    more backticks OR three or more tildes. Marker matches inside an open
    fence are skipped. Returns None if no eligible match found.
    """
    in_fence = False
    offset = 0
    cursor = after
    for line in text.splitlines(keepends=True):
        line_end = offset + len(line)
        if line_end <= cursor:
            # Still scanning fence state up to the start cursor.
            stripped = line.lstrip()
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_fence = not in_fence
            offset = line_end
            continue

        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            offset = line_end
            continue
        if not in_fence:
            local_idx = line.find(marker)
            if local_idx >= 0:
                absolute = offset + local_idx
                if absolute >= cursor:
                    return absolute
        offset = line_end
    return None


def atomic_write(path: Path, content: str) -> None:
    """Atomic write: unique tmp + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(content, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot",
        default=str(PROJECT_ROOT / "dashboard" / "snapshot.json"),
        help="Path to snapshot.json produced by backend/server.py",
    )
    parser.add_argument(
        "--readme",
        default=str(PROJECT_ROOT / "README.md"),
        help="Path to the README to update.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Dry-run: exit 0 if README is current, exit 1 if it would change.",
    )
    args = parser.parse_args(argv)

    snapshot_path = Path(args.snapshot)
    readme_path = Path(args.readme)

    if not snapshot_path.exists():
        print(f"[auto-readme] snapshot not found: {snapshot_path}", file=sys.stderr)
        return 2
    if not readme_path.exists():
        print(f"[auto-readme] readme not found: {readme_path}", file=sys.stderr)
        return 2

    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    readme_text = readme_path.read_text(encoding="utf-8")
    new_block = format_stats_block(snapshot)
    updated_text, changed, reason = replace_block(readme_text, new_block)

    if reason == "markers missing":
        print(f"[auto-readme] {reason}", file=sys.stderr)
        return 2
    if reason == "end-marker precedes start-marker":
        print(f"[auto-readme] {reason}", file=sys.stderr)
        return 2

    if args.check:
        print(f"[auto-readme] check: {reason}")
        return 1 if changed else 0

    if not changed:
        print(f"[auto-readme] {reason}")
        return 0

    atomic_write(readme_path, updated_text)
    print(f"[auto-readme] wrote {readme_path} ({reason})")
    return 0


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


if __name__ == "__main__":
    raise SystemExit(main())
