#!/usr/bin/env python3
"""Paired eval: OLD (AI-active) vs NEW (human-attention) productivity multiplier.

Reads the real tracker data and prints both denominators + multipliers for
all / 30d / 7d windows. Read-only — does NOT write or deploy anything.

    py -3.14 tools/eval-human-attention-productivity.py
"""

import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("summary_eval", str(ROOT / "tracker" / "summary.py"))
summary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(summary)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def line(label, events):
    p = summary.summarize_productivity(events, gap_minutes=2)
    if not p:
        print(f"  {label:6}: no covered sessions")
        return
    ai = p["active_hours_with_ai"]
    hu = p["human_attention_hours_with_ai"]
    base = p["hours_without_ai"]
    fb = p["human_attention_fallbacks"]
    old = base / ai if ai > 0 else 0.0
    new = base / hu if hu > 0 else 0.0
    print(
        f"  {label:6}: OLD x{old:5.2f} (AI {ai:7.1f}h)  "
        f"NEW x{new:5.2f} (human {hu:7.1f}h)  base {base:7.0f}h  fallbacks {fb}"
    )


def main():
    end = date.today()
    print("Paired productivity eval — OLD AI-active denom vs NEW human-attention denom")
    print(f"(unit={summary.productivity_unit()}, human gap={summary.HUMAN_ATTENTION_GAP_MINUTES} min)\n")
    line("all", summary.read_events(end - timedelta(days=400), end))
    for days in (30, 7):
        line(f"{days}d", summary.read_events(end - timedelta(days=days - 1), end))


if __name__ == "__main__":
    main()
