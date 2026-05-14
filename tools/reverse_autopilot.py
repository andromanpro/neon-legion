#!/usr/bin/env python
"""Mine Claude Code session transcripts for repeated user-side patterns.

For each pattern that recurs across many sessions, emit a Markdown proposal
into `proposals/<slug>.md` suggesting either:

- a `tools/<x>.py` script (for action patterns: "run X for Y")
- a `prompts/<x>.example.md` template (for prompt scaffolds)

**Never** creates files outside `proposals/`. The human reviews each
proposal and adopts (or ignores) by hand.

Source: `~/.claude/projects/<project>/<session-uuid>.jsonl`. Each line is
a JSON event; user messages have `type: "user"` with `message.content`
either a string or a list of `{type: "text", text: ...}` items. tool_result
events are skipped.

Stdlib only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "proposals"
DEFAULT_MIN_OCCURRENCES = 3
DEFAULT_MIN_SESSIONS = 2
DEFAULT_PREFIX_WORDS = 4
DEFAULT_MAX_PROPOSALS = 30

WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9_]+")

ACTION_VERBS_RU = frozenset({
    "сделай", "напиши", "запусти", "запиши", "проверь", "почини",
    "обнови", "удали", "добавь", "перенеси", "поправь", "развей",
    "сгенерируй", "посчитай", "собери", "опубликуй", "выкати",
    "закоммить", "пушни", "смерджи", "продеплой",
})
ACTION_VERBS_EN = frozenset({
    "run", "write", "build", "fix", "update", "remove", "add",
    "move", "patch", "deploy", "ship", "merge", "commit", "push",
    "generate", "check", "verify", "compile", "test", "publish",
    "audit", "compute", "render", "validate",
})
QUESTION_OPENERS_RU = frozenset({"как", "что", "где", "почему", "зачем", "когда", "сколько"})
QUESTION_OPENERS_EN = frozenset({"how", "what", "where", "why", "when", "should", "can", "could", "is", "do", "does"})


@dataclass
class Pattern:
    key: str
    occurrences: int = 0
    sessions: set[str] = field(default_factory=set)
    examples: list[str] = field(default_factory=list)
    first_seen: str | None = None
    last_seen: str | None = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--projects-dir",
        default=str(DEFAULT_PROJECTS_DIR),
        help="Root directory containing per-project session JSONL files.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Where to write proposal markdown files.",
    )
    parser.add_argument(
        "--min-occurrences",
        type=int,
        default=DEFAULT_MIN_OCCURRENCES,
        help="Minimum total occurrences of a pattern to emit a proposal.",
    )
    parser.add_argument(
        "--min-sessions",
        type=int,
        default=DEFAULT_MIN_SESSIONS,
        help="Minimum distinct sessions a pattern must appear in.",
    )
    parser.add_argument(
        "--prefix-words",
        type=int,
        default=DEFAULT_PREFIX_WORDS,
        help="N-word prefix of each user message used as the pattern key.",
    )
    parser.add_argument(
        "--max-proposals",
        type=int,
        default=DEFAULT_MAX_PROPOSALS,
        help="Cap on proposal files written per run.",
    )
    args = parser.parse_args(argv)

    projects_dir = Path(args.projects_dir)
    if not projects_dir.is_dir():
        print(f"[reverse-autopilot] projects dir not found: {projects_dir}", file=sys.stderr)
        return 2

    patterns = mine_patterns(
        projects_dir,
        prefix_words=args.prefix_words,
    )
    qualified = [
        p for p in patterns.values()
        if p.occurrences >= args.min_occurrences
        and len(p.sessions) >= args.min_sessions
    ]
    qualified.sort(key=lambda p: (p.occurrences, len(p.sessions)), reverse=True)
    qualified = qualified[: args.max_proposals]

    output_dir = Path(args.output_dir)
    written = []
    for pattern in qualified:
        target = output_dir / f"repeat-{_slug(pattern.key)}.md"
        atomic_write(target, render_proposal(pattern))
        written.append(target)

    print(f"[reverse-autopilot] scanned_dir={projects_dir}")
    print(
        f"[reverse-autopilot] candidates={len(patterns)} "
        f"qualified={len(qualified)} written={len(written)}"
    )
    if written:
        print("[reverse-autopilot] proposals:")
        for p in written:
            print(f"  - {p}")
    return 0


def mine_patterns(projects_dir: Path, *, prefix_words: int = DEFAULT_PREFIX_WORDS) -> dict[str, Pattern]:
    """Scan all JSONL session files and return prefix → Pattern."""
    patterns: dict[str, Pattern] = defaultdict(lambda: Pattern(key=""))
    for jsonl_path in projects_dir.rglob("*.jsonl"):
        session_id = jsonl_path.stem
        for ts, text in _iter_user_messages(jsonl_path):
            prefix = _prefix(text, n=prefix_words)
            if not prefix:
                continue
            pattern = patterns[prefix]
            if not pattern.key:
                pattern.key = prefix
            pattern.occurrences += 1
            pattern.sessions.add(session_id)
            if len(pattern.examples) < 3:
                pattern.examples.append(text[:160])
            if ts:
                if pattern.first_seen is None or ts < pattern.first_seen:
                    pattern.first_seen = ts
                if pattern.last_seen is None or ts > pattern.last_seen:
                    pattern.last_seen = ts
    return dict(patterns)


def render_proposal(pattern: Pattern) -> str:
    """Render a single proposal markdown for one pattern."""
    kind, hint = _classify(pattern.key)
    lines: list[str] = []
    lines.append(f"# Repeat-pattern proposal — `{pattern.key}`")
    lines.append("")
    lines.append(f"_Detected by `tools/reverse_autopilot.py`. "
                 f"Reviewed by you, not auto-applied._")
    lines.append("")
    lines.append(f"- **Occurrences:** {pattern.occurrences}")
    lines.append(f"- **Distinct sessions:** {len(pattern.sessions)}")
    if pattern.first_seen and pattern.last_seen:
        lines.append(f"- **Time window:** {pattern.first_seen[:19]} … {pattern.last_seen[:19]}")
    lines.append(f"- **Suggested form:** {kind}")
    lines.append("")
    lines.append("## Hint")
    lines.append("")
    lines.append(hint)
    lines.append("")
    lines.append("## Example messages")
    lines.append("")
    for example in pattern.examples:
        lines.append(f"> {example}")
        lines.append("")
    lines.append("## How to adopt")
    lines.append("")
    if kind == "script":
        lines.append(
            "If this really is an action you keep retyping, add a one-shot script:\n\n"
            "```\n"
            f"tools/{_slug(pattern.key)}.py\n"
            "```\n\n"
            "Then invoke with `py -3.14 tools/<name>.py [args]`. "
            "Delete this proposal once adopted (or ignored)."
        )
    elif kind == "prompt-template":
        lines.append(
            "If this is a prompt scaffold, drop a template:\n\n"
            "```\n"
            f"prompts/{_slug(pattern.key)}.example.md\n"
            "```\n\n"
            "Keep the example tiny; one project-specific concrete instance. "
            "Delete this proposal once adopted (or ignored)."
        )
    else:
        lines.append(
            "This pattern is short and generic — likely a conversational filler. "
            "Either ignore this proposal or refine "
            "`--prefix-words` to capture more signal."
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


# --- internals ----------------------------------------------------------------


def _iter_user_messages(path: Path):
    """Yield (timestamp, text) for each non-empty user message in a session JSONL."""
    try:
        f = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with f:
        for raw in f:
            line = raw.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "user":
                continue
            msg = event.get("message") or {}
            content = msg.get("content")
            ts = event.get("timestamp") or event.get("ts") or ""
            for text in _extract_user_text(content):
                yield ts, text


def _extract_user_text(content) -> list[str]:
    """Pull out plain user text strings, skipping tool_result + system noise."""
    out: list[str] = []
    if isinstance(content, str):
        text = content.strip()
        if text and "tool_use_id" not in text[:50] and not _is_system_noise(text):
            out.append(text)
        return out
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") in ("tool_result", "tool_use"):
                continue
            text_val = item.get("text") or ""
            text_val = text_val.strip()
            if text_val and not _is_system_noise(text_val):
                out.append(text_val)
    return out


# Known system-injected preamble lines that surface as `type: "user"` events
# in Claude Code transcripts but are not human-typed input. Filtered out so
# the proposals reflect what the human actually keeps re-typing.
_SYSTEM_NOISE_PREFIXES = (
    "<system-reminder>",
    "<command-name>",
    "this session is being continued",
    "continue from where you",
    "caveat:",
    "<local-command-",
    "<task-notification",
    "base directory for this",
    "<command-message>",
    "the user has asked",
)


def _is_system_noise(text: str) -> bool:
    head = text[:80].lower().lstrip()
    return any(head.startswith(p) for p in _SYSTEM_NOISE_PREFIXES)


def _prefix(text: str, *, n: int) -> str:
    tokens = [t.lower() for t in WORD_RE.findall(text)]
    if len(tokens) < n:
        return ""
    return " ".join(tokens[:n])


def _classify(prefix: str) -> tuple[str, str]:
    """Return (kind, hint) for a prefix.

    `kind` ∈ {"script", "prompt-template", "noise"}. Hint is a short human-readable
    explanation for the proposal.
    """
    tokens = prefix.split()
    first = tokens[0] if tokens else ""
    if first in ACTION_VERBS_RU or first in ACTION_VERBS_EN:
        return ("script", "First word is an action verb — looks like something you keep manually executing.")
    if first in QUESTION_OPENERS_RU or first in QUESTION_OPENERS_EN:
        return ("prompt-template", "Question-style scaffold — capture the shape as a template if the answer is usually similar.")
    if len(tokens) < 3:
        return ("noise", "Very short prefix — likely conversational filler. Consider increasing `--prefix-words`.")
    return ("prompt-template", "Recurring opener — likely a scaffold you re-type for similar tasks.")


def _slug(text: str) -> str:
    slug = re.sub(r"[^A-Za-zА-Яа-яЁё0-9]+", "-", text.strip()).strip("-")
    if not slug:
        slug = "pattern"
    # Slug must be filesystem-safe ASCII-friendly. Hash Cyrillic-only slugs so
    # the filename is portable across platforms.
    if not re.match(r"^[A-Za-z0-9._-]+$", slug):
        digest = hashlib.blake2b(slug.encode("utf-8"), digest_size=4).hexdigest()
        slug = f"pattern-{digest}"
    return slug.lower()[:60]


if __name__ == "__main__":
    raise SystemExit(main())
