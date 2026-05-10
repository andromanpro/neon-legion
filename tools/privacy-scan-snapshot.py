#!/usr/bin/env python
"""Scan the public WordPress snapshot for obvious private strings.

This is a guardrail, not a substitute for human review. It checks for local
paths, user names, chat ids, API-token shapes, and raw internal ids before a
snapshot is promoted from local preview to a public page.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


DEFAULT_SNAPSHOT = Path(r"H:\wordpress-androman\wp-data\wp-content\uploads\multi-agent\snapshot.json")

PATTERNS = {
    "windows_path": re.compile(r"[A-Za-z]:[\\/][^\"'\s<>]+"),
    "user_profile": re.compile(r"C:[\\/]Users[\\/][^\"'\s<>]+", re.IGNORECASE),
    "nas_drive": re.compile(r"\b[HF]:[\\/][^\"'\s<>]+", re.IGNORECASE),
    "email": re.compile(r"\b[\w.+\-]+@[\w\-]+\.[\w\-.]+\b"),
    "api_token": re.compile(r"\b(sk_|pk_|ghp_|gho_|github_pat_)\w{16,}\b"),
    "telegram_id": re.compile(r"\btelegram:\d+\b|\b422958213\b", re.IGNORECASE),
    "raw_message_id": re.compile(r"\bmsg_[A-Za-z0-9]{12,}\b"),
    "raw_session_id": re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE),
    "private_name": re.compile(r"\b(Roman|Roono|androman)\b", re.IGNORECASE),
    "private_workspace": re.compile(r"\bWorkAI\b", re.IGNORECASE),
}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    return parser.parse_args(argv)


def preview(text: str, start: int, end: int) -> str:
    left = max(0, start - 80)
    right = min(len(text), end + 120)
    return text[left:right].replace("\n", "\\n")


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args(argv)
    text = args.snapshot.read_text(encoding="utf-8")

    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"not valid JSON: {exc}", file=sys.stderr)
        return 2

    findings = []
    for name, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            findings.append((name, match.group(0), preview(text, match.start(), match.end())))

    if not findings:
        print(f"privacy_scan=ok snapshot={args.snapshot}")
        return 0

    print(f"privacy_scan=failed snapshot={args.snapshot}")
    for name, value, context in findings[:50]:
        print(f"- {name}: {value}")
        print(f"  context: {context}")
    if len(findings) > 50:
        print(f"... {len(findings) - 50} more findings")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
