#!/usr/bin/env python
"""One-off retroactive fix for cp1251 → UTF-8 mojibake in events (#20).

Pattern of corruption: original path was UTF-8-encoded bytes, but at the
Windows stdin layer they got decoded as cp1251 (the default Russian
Windows codepage), then re-encoded back to UTF-8 when written to JSONL.
This produces e.g. "Р—Р°РєР°Р·С‡РёРєСѓ" where the original was "Заказчику".

To reverse: encode the corrupted string as cp1251 (recovering the
original UTF-8 byte sequence), then decode as UTF-8 (getting back the
correct Russian text).

Idempotent — running twice is a no-op because fixed paths don't contain
the cp1251-byte-pattern that we look for.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRACKER_DIR = PROJECT_ROOT / "tracker"
EVENTS_FILES = [
    TRACKER_DIR / "claude-events.jsonl",
    TRACKER_DIR / "codex-events.jsonl",
    TRACKER_DIR / "openclaw-events.jsonl",
    TRACKER_DIR / "opencode-events.jsonl",
]

# Cyrillic upper/lower letter range in cp1251 (decoded after corruption).
# These chars appear in mojibake strings: "Р" (U+0420), "С" (U+0421), "Ð"
# (U+00D0), em-dash "—" (U+2014) used by cp1251 0x97.
MOJIBAKE_MARKERS = ("Р", "С", "Ð", "вЂ")


def looks_corrupted(text: str) -> bool:
    """Heuristic: contains characters that only appear in cp1251→utf8 mojibake."""
    if not text:
        return False
    # If string has any cyrillic character that's part of mojibake patterns,
    # AND lacks normal Russian word characters (а-я that aren't markers).
    has_marker = any(m in text for m in MOJIBAKE_MARKERS)
    if not has_marker:
        return False
    # Sanity: try the fix and see if result is more "natural" Russian.
    fixed = try_fix(text)
    return fixed != text and is_more_natural(fixed, text)


def try_fix(text: str) -> str:
    """Reverse cp1251-as-UTF-8 corruption. Returns original on failure."""
    try:
        return text.encode("cp1251").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def is_more_natural(candidate: str, original: str) -> bool:
    """Crude check: candidate has fewer 'Р'/'С' markers than original."""
    orig_markers = sum(original.count(m) for m in MOJIBAKE_MARKERS)
    cand_markers = sum(candidate.count(m) for m in MOJIBAKE_MARKERS)
    return cand_markers < orig_markers


def fix_event(event: dict) -> tuple[dict, bool]:
    """Return (event, changed). Only working_dir is fixed for now."""
    changed = False
    for key in ("working_dir", "cwd"):
        value = event.get(key)
        if isinstance(value, str) and looks_corrupted(value):
            fixed = try_fix(value)
            if fixed != value:
                event[key] = fixed
                changed = True
    return event, changed


def with_schema_version(event: dict) -> dict:
    if "schema_version" in event:
        return event
    return {"schema_version": 1, **event}


def atomic_rewrite(path: Path, new_lines: list[str]) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(3)}")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.writelines(new_lines)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def process_file(path: Path) -> tuple[int, int]:
    """Return (events_total, events_fixed)."""
    if not path.exists():
        return 0, 0
    new_lines: list[str] = []
    total = 0
    fixed = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.rstrip("\n")
            if not stripped:
                new_lines.append(line)
                continue
            total += 1
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                new_lines.append(line)
                continue
            if not isinstance(event, dict):
                new_lines.append(line)
                continue
            event, changed = fix_event(event)
            if changed:
                fixed += 1
                event = with_schema_version(event)
                new_lines.append(json.dumps(event, ensure_ascii=False) + "\n")
            else:
                new_lines.append(line)
    if fixed:
        atomic_rewrite(path, new_lines)
    return total, fixed


def main() -> int:
    total_fixed = 0
    for path in EVENTS_FILES:
        total, fixed = process_file(path)
        rel = path.relative_to(PROJECT_ROOT)
        print(f"{rel}: total={total}, fixed={fixed}")
        total_fixed += fixed
    print(f"Total fixed across all event files: {total_fixed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
