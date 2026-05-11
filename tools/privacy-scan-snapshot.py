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


# Default snapshot location. Override via --snapshot if your WordPress install
# uses a different uploads directory.
DEFAULT_SNAPSHOT = Path("dashboard/snapshot.json")

# Generic privacy patterns — apply to any snapshot. Personal terms (specific
# usernames, customer codenames, internal IDs) belong in your own private
# blocklist loaded via --extra-terms; this file MUST NOT bake them in.
PATTERNS = {
    "windows_path": re.compile(r"[A-Za-z]:[\\/][^\"'\s<>]+"),
    "user_profile": re.compile(r"C:[\\/]Users[\\/][^\"'\s<>]+", re.IGNORECASE),
    "unix_home": re.compile(r"/(?:home|users)/[\w\-+./]+", re.IGNORECASE),
    "email": re.compile(r"\b[\w.+\-]+@[\w\-]+\.[\w\-.]+\b"),
    "api_token": re.compile(r"\b(sk_|pk_|ghp_|gho_|github_pat_)\w{16,}\b"),
    "telegram_id": re.compile(r"\btelegram:\d+\b"),
    "raw_message_id": re.compile(r"\bmsg_[A-Za-z0-9]{12,}\b"),
    "raw_session_id": re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE),
    "private_lan_ip": re.compile(r"\b(?:10|172\.(?:1[6-9]|2[0-9]|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b"),
}


def load_extra_patterns(path: Path | None) -> dict:
    """Load user-specific terms from a blocklist file (one literal per line,
    # comments allowed). Compiled as case-insensitive whole-word patterns."""
    if path is None or not path.exists():
        return {}
    extra: dict = {}
    raw = path.read_text(encoding="utf-8")
    for i, line in enumerate(raw.splitlines(), start=1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        # Whole-word, Cyrillic-aware
        extra[f"private_term_{i}"] = re.compile(
            r"(?<![\wЀ-ӿ])" + re.escape(s) + r"(?![\wЀ-ӿ])",
            re.IGNORECASE,
        )
    return extra


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument(
        "--extra-terms",
        type=Path,
        default=None,
        help="Optional blocklist file (one literal term per line). Compiled as "
             "case-insensitive whole-word patterns. Inflected languages (RU, DE, "
             "FI, ...) need each form listed explicitly — the matcher does no "
             "lemmatization.",
    )
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

    all_patterns = dict(PATTERNS)
    all_patterns.update(load_extra_patterns(args.extra_terms))

    findings = []
    for name, pattern in all_patterns.items():
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
