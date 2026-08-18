"""Reader for Codex CLI/Desktop rollout transcripts (~/.codex/sessions/**/*.jsonl).

Until 2026-08-18 the productivity metric measured Claude Code only: both the
baseline estimate and the human-attention denominator were read from
~/.claude/projects transcripts. Codex sessions contributed tokens and dollars
but zero hours, so 19,129 desktop-Codex calls over 30 days (99.7% of all Codex
usage, driven by the human directly) rendered as no work at all.

Rollout format (differs from Claude Code transcripts):

    {"timestamp": "...Z", "type": "session_meta",
     "payload": {"session_id": "...", "originator": "Codex Desktop",
                 "source": "vscode", "thread_source": "user", ...}}
    {"timestamp": "...Z", "type": "response_item",
     "payload": {"type": "message", "role": "user"|"assistant"|"developer",
                 "content": [{"type": "input_text"|"output_text", "text": "..."}]}}

Two traps this module exists to handle:

1. `role: "user"` is NOT the same as "the human typed this". The app injects
   plugin catalogues, environment blocks and whole AGENTS.md dumps under the
   user role. Measured over all 953 local rollouts: 2,421 genuine messages vs
   ~1,026 injected ones. Counting the injected blocks would inflate attention
   AND poison the estimator, whose prompt budget takes the FIRST three user
   messages — message #1 is an 11.8 KB plugin list in every desktop session.

2. Agent-dispatched sessions must not be estimated. `codex exec` runs
   (764 of the rollouts) are launched by Claude from inside a Claude session
   whose own baseline already covers that work; giving them a separate
   baseline would double-count. Only human-driven sessions qualify — see
   `is_human_driven`.

Stdlib only: imported by both tracker/summary.py and tracker/estimate-task.py.
Keep it that way — the estimator is spawned per session start.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

# Blocks the Codex app injects under role="user". Matched on the leading tag,
# so genuine prompts that merely contain such a word are unaffected.
INJECTED_TAGS = frozenset({
    "recommended_plugins",
    "environment_context",
    "app-context",
    "codex_internal_context",
    "turn_aborted",
    "user_instructions",
    "INSTRUCTIONS",
})

# The app also injects the project AGENTS.md verbatim as a user message.
INJECTED_TEXT_PREFIXES = ("# AGENTS.md instructions for",)

# "# Files mentioned by the user:" is deliberately NOT filtered: the app writes
# it, but only because the human attached a file — it marks a real moment of
# human action and should count toward attention.


def _leading_tag(text: str) -> str | None:
    if not text.startswith("<"):
        return None
    end = 1
    while end < len(text) and (text[end].isalnum() or text[end] in "_-"):
        end += 1
    if end == 1 or end >= len(text) or text[end] not in " >\n\t":
        return None
    return text[1:end]


def is_injected(text: str) -> bool:
    """True for app-generated content masquerading as a user message."""
    stripped = text.lstrip()
    tag = _leading_tag(stripped)
    if tag is not None and tag in INJECTED_TAGS:
        return True
    return stripped.startswith(INJECTED_TEXT_PREFIXES)


def payload_text(payload: dict) -> str:
    parts = []
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for chunk in content:
            if not isinstance(chunk, dict):
                continue
            if chunk.get("type") in ("input_text", "output_text", "text"):
                value = chunk.get("text")
                if isinstance(value, str):
                    parts.append(value)
    return "\n".join(parts)


def message_role(event: dict) -> str | None:
    """'user'/'assistant' for a rollout message line, else None.

    `developer` (app scaffolding) and every non-message record type are
    deliberately excluded.
    """
    if not isinstance(event, dict) or event.get("type") != "response_item":
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "message":
        return None
    role = payload.get("role")
    return str(role) if role in ("user", "assistant") else None


def is_human_prompt(event: dict) -> bool:
    """True only for a genuine human-typed (or human-attached) rollout message."""
    if message_role(event) != "user":
        return False
    payload = event.get("payload")
    text = payload_text(payload).strip() if isinstance(payload, dict) else ""
    return bool(text) and not is_injected(text)


def parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def is_rollout(path) -> bool:
    """Sniff whether a .jsonl is a Codex rollout (vs a Claude transcript).

    Content-based on purpose: the path is not a reliable signal once a
    transcript_path has been copied into tasks.json.
    """
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as source:
            for line in source:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    return False
                if not isinstance(event, dict):
                    return False
                return event.get("type") in ("session_meta", "response_item", "turn_context")
    except OSError:
        return False
    return False


def read_meta(path) -> dict:
    """The session_meta payload (session_id, originator, source, ...) or {}."""
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as source:
            for line in source:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and event.get("type") == "session_meta":
                    payload = event.get("payload")
                    return payload if isinstance(payload, dict) else {}
    except OSError:
        return {}
    return {}


def is_human_driven(meta: dict) -> bool:
    """True when the HUMAN drove this session, not an agent dispatch.

    Mirrors summary.codex_origin's desktop/tui classification: `codex exec`
    (originator codex_exec / source exec) and subagent spawns are excluded so
    their hours are not counted on top of the Claude session that launched them.
    """
    if not isinstance(meta, dict):
        return False
    originator = str(meta.get("originator") or "").lower()
    source = meta.get("source")
    source_text = str(source or "").lower()
    if originator == "codex_exec" or source_text == "exec":
        return False
    if not isinstance(source, str):  # subagent spawn records a dict here
        return False
    if str(meta.get("thread_source") or "").lower() == "subagent":
        return False
    return source_text in ("vscode", "desktop", "cli")


def read_messages(path) -> tuple[list[str], list[str]]:
    """(user_messages, assistant_messages) with injected blocks removed."""
    user_messages: list[str] = []
    assistant_messages: list[str] = []
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as source:
            for line in source:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                role = message_role(event)
                if role is None:
                    continue
                text = payload_text(event.get("payload") or {}).strip()
                if not text:
                    continue
                if role == "user":
                    if is_injected(text):
                        continue
                    user_messages.append(text)
                else:
                    assistant_messages.append(text)
    except OSError:
        return [], []
    return user_messages, assistant_messages


def read_human_timestamps(path) -> list[datetime]:
    """Sorted timestamps of genuine human messages in a rollout."""
    timestamps: list[datetime] = []
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as source:
            for line in source:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not is_human_prompt(event):
                    continue
                ts = parse_ts(event.get("timestamp"))
                if ts is not None:
                    timestamps.append(ts)
    except OSError:
        return []
    timestamps.sort()
    return timestamps
