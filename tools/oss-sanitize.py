#!/usr/bin/env python
"""OSS sanitization bot — strips personal/customer identifiers from tracked files
before public publication.

Run modes:
  --check    Report what would change; non-zero exit if violations remain.
  --apply    Rewrite files in place. Backup originals to .oss-backup/.
  --diff     Show unified diff per file without modifying anything.

Default scope: README*, prompts/**, CLAUDE.md, dashboard/PUBLICATION_NOTES.md,
tracker/README.md, dashboard/README.md, tools/*.py. Excluded by default:
LICENSE, SECURITY.md, CONTRIBUTING.md, config.example.toml, .gitignore.

Substitution philosophy: replace with `<placeholder>` so the file remains
syntactically/grammatically OK. Customer names → `<client>`. Personal IPs/hosts
→ `localhost` (since this code runs locally). Personal paths → `<project_root>`
or `<wp_uploads>` based on context.

Rule scope split (intentional):
  - `GENERIC_RULES` (below) — safe-for-everyone patterns: RFC1918 LAN IPs,
    unix system home dirs. Few false positives.
  - `.oss-sanitize-private.txt` (gitignored, user-editable) — your specific
    drive letters, mDNS hosts, blog domain, GitHub username, customer
    codenames. These MUST be configured per-environment because the
    generic regex either causes false positives (CLAUDE.local.md filename
    matching `.local` rule) or scrubs nothing useful (Windows path
    `C:\\Users\\` is a generic pattern that would scrub any code referencing
    Windows file system).

Important: by default the sanitizer will NOT catch `C:\\Users\\<your-name>`,
`H:\\openclaw`, `nas.local`, or `<your-blog>.example`. You MUST list those in
`.oss-sanitize-private.txt` for your fork. See
`.oss-sanitize-private.example.txt` for the file format.

The bot does NOT scrub:
- File names. If a prompt is named `phase-1-3-codex-tracking-task.md` that
  stays.
- Markdown code blocks that look like shell sessions in the user's own
  environment if marked with `<!-- oss:keep -->` comment line.
- Files under tracker/private/ or prompts/private/ (gitignored anyway).
"""
from __future__ import annotations

import argparse
import difflib
import re
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKUP_DIR = PROJECT_ROOT / ".oss-backup"


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


# Generic rules — safe for OSS, do not embed any specific personal value.
# Personal/private patterns (specific IPs, hostnames, customer codenames,
# usernames, paths) live in a gitignored private config — see
# `_load_private_rules()` below.
GENERIC_RULES: list[tuple[str, str, str]] = [
    # Private LAN IPv4 (RFC1918)
    (r"\b(?:10|172\.(?:1[6-9]|2[0-9]|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b", "localhost", "Private LAN IP (RFC1918)"),
    # Unix user home / system dirs (only when an absolute path)
    (r"(?<![\w/])/(?:home|users)/[\w\-+./]+", "<user_home>", "Unix user home"),
    # NOTE: mDNS `.local` and drive-letter paths are user-specific and prone
    # to false positives (CLAUDE.local.md filename, .env.local, generic F:/
    # references in docs). Put them in `.oss-sanitize-private.txt` if needed
    # for your environment.
]


def _load_private_rules(project_root: Path) -> list[tuple[str, str, str]]:
    """Load user-specific scrub rules from a gitignored file. Format:

        # one rule per line, columns separated by " | "
        # pattern | replacement | description
        my-customer-name | <client> | acme-corp full word

    Returns a list of (pattern, replacement, description). Empty list if
    the file doesn't exist (OSS users get only GENERIC_RULES).
    """
    private_path = project_root / ".oss-sanitize-private.txt"
    if not private_path.exists():
        return []
    rules: list[tuple[str, str, str]] = []
    for i, raw_line in enumerate(private_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            print(f"WARNING: malformed rule at line {i}: {line!r}", file=sys.stderr)
            continue
        pattern, replacement = parts[0], parts[1]
        desc = parts[2] if len(parts) > 2 else f"private rule #{i}"
        rules.append((pattern, replacement, desc))
    return rules


def _all_rules() -> list[tuple[str, str, str]]:
    """Generic + private rules, in that order (generic first matches more
    broadly; private rules handle exact-name overrides)."""
    return GENERIC_RULES + _load_private_rules(PROJECT_ROOT)


DEFAULT_INCLUDE_GLOBS = [
    "README.md",
    "CLAUDE.md",
    "prompts/**/*.md",
    "dashboard/PUBLICATION_NOTES.md",
    "tracker/README.md",
    "dashboard/README.md",
    "tools/openclaw-codex-bridge.py",
    "tracker/run-*.cmd",
    "backend/run-*.cmd",
]
EXCLUDE_PATTERNS = {
    "LICENSE", "SECURITY.md", "CONTRIBUTING.md",
    "config.example.toml", ".gitignore", "tools/oss-sanitize.py",
}
KEEP_OPEN = "<!-- oss:keep -->"
KEEP_CLOSE = "<!-- /oss:keep -->"


def gather_files(root: Path, globs: list[str]) -> list[Path]:
    seen: set[Path] = set()
    for g in globs:
        for p in root.glob(g):
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            if rel in EXCLUDE_PATTERNS:
                continue
            seen.add(p)
    return sorted(seen)


def _keep_segments(text: str) -> list[tuple[bool, str]]:
    """Split text into (is_keep_block, segment) chunks."""
    lines = text.splitlines(keepends=True)
    if not any(KEEP_OPEN in line for line in lines):
        return [(False, text)]

    segments: list[tuple[bool, str]] = []
    buf: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if KEEP_OPEN not in line:
            buf.append(line)
            i += 1
            continue

        if buf:
            segments.append((False, "".join(buf)))
            buf = []

        keep: list[str] = [line]
        i += 1
        fence: str | None = None
        while i < len(lines):
            keep.append(lines[i])
            stripped = lines[i].lstrip()
            if KEEP_CLOSE in lines[i]:
                i += 1
                break
            if fence is None and (stripped.startswith("```") or stripped.startswith("~~~")):
                fence = stripped[:3]
            elif fence is not None and stripped.startswith(fence):
                i += 1
                break
            i += 1
        segments.append((True, "".join(keep)))

    if buf:
        segments.append((False, "".join(buf)))
    return segments


def _apply_rules(text: str, rules: list[tuple[str, str, str]]) -> tuple[str, list[tuple[str, int]]]:
    hits: list[tuple[str, int]] = []
    out = text
    for pat, repl, desc in rules:
        new, n = re.subn(pat, repl, out)
        if n > 0:
            hits.append((desc, n))
        out = new
    return out, hits


def sanitize_text(text: str) -> tuple[str, list[tuple[str, int]]]:
    """Apply rules outside oss:keep blocks. Return (new_text, [(rule_desc, hit_count), ...])."""
    rules = _all_rules()
    pieces: list[str] = []
    hits: list[tuple[str, int]] = []
    for keep, segment in _keep_segments(text):
        if keep:
            pieces.append(segment)
            continue
        new, segment_hits = _apply_rules(segment, rules)
        pieces.append(new)
        hits.extend(segment_hits)
    return "".join(pieces), hits


def backup_file(path: Path) -> None:
    rel = path.relative_to(PROJECT_ROOT)
    dst = BACKUP_DIR / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dst)


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Report violations, exit non-zero if any")
    mode.add_argument("--apply", action="store_true", help="Rewrite files (backup to .oss-backup/)")
    mode.add_argument("--diff", action="store_true", help="Show unified diff, no changes")
    parser.add_argument("--globs", nargs="*", default=None,
                        help="Override include globs (default: README/CLAUDE/prompts/tools)")
    args = parser.parse_args()

    globs = args.globs or DEFAULT_INCLUDE_GLOBS
    files = gather_files(PROJECT_ROOT, globs)
    if not files:
        print("No files matched.", file=sys.stderr)
        return 0

    total_files_with_hits = 0
    total_substitutions = 0

    for f in files:
        original = f.read_text(encoding="utf-8")
        new, hits = sanitize_text(original)
        if not hits:
            continue
        total_files_with_hits += 1
        n = sum(c for _, c in hits)
        total_substitutions += n

        rel = f.relative_to(PROJECT_ROOT).as_posix()
        print(f"\n{rel}: {n} substitutions")
        for desc, count in hits:
            print(f"  [{count:3d}] {desc}")

        if args.diff:
            diff = difflib.unified_diff(
                original.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile=rel, tofile=rel + ".sanitized",
                n=2,
            )
            sys.stdout.writelines(diff)
        elif args.apply:
            backup_file(f)
            f.write_text(new, encoding="utf-8")
            print(f"  → rewritten (backup in .oss-backup/{rel})")

    print()
    print(f"Files with hits: {total_files_with_hits}")
    print(f"Total substitutions: {total_substitutions}")

    if args.check and total_files_with_hits > 0:
        print("CHECK MODE: violations remain — run with --apply to fix.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
