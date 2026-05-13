#!/usr/bin/env python
"""Sentinel-wrapped task envelopes for the Phase 1.5 Git bus."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime


OPEN_RE = re.compile(r"<!--\s*neon-task:v1\s+sha256=([0-9a-fA-F]{64})\s*-->")
CLOSE = "<!-- /neon-task:v1 -->"
HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")
SCHEMA_VERSION = 1
STRING_FIELDS = ("task_id", "kind", "target_host", "payload_ref", "idempotency_key", "created_at")


def _canonical_bytes(task: dict) -> bytes:
    return json.dumps(task, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha256(task: dict) -> str:
    return hashlib.sha256(_canonical_bytes(task)).hexdigest()


def _validate(task: dict, *, require_schema: bool) -> bool:
    if not isinstance(task, dict) or (require_schema and task.get("schema_version") != SCHEMA_VERSION):
        return False
    for field in STRING_FIELDS:
        if not isinstance(task.get(field), str) or not task[field].strip():
            return False
    lease_seconds = task.get("lease_seconds")
    if (
        not isinstance(task.get("payload_sha256"), str)
        or not HEX_RE.fullmatch(task["payload_sha256"])
        or isinstance(lease_seconds, bool)
        or not isinstance(lease_seconds, int)
        or lease_seconds <= 0
    ):
        return False
    try:
        datetime.fromisoformat(task["created_at"].replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _envelopes(text: str):
    for match in OPEN_RE.finditer(text):
        close_at = text.find(CLOSE, match.end())
        if close_at == -1:
            continue
        yield match.group(1), text[match.end():close_at].strip()


def _load_verified(expected: str, body: str) -> dict | None:
    try:
        task = json.loads(body)
    except json.JSONDecodeError:
        return None
    if isinstance(task, dict) and _sha256(task).lower() == expected.lower():
        return task
    return None


def serialize(task: dict) -> str:
    """Wrap task dict in sentinel-delimited envelope with sha256 header."""
    envelope = dict(task)
    envelope.setdefault("schema_version", SCHEMA_VERSION)
    if envelope["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version: {envelope['schema_version']!r}")
    if not _validate(envelope, require_schema=True):
        raise ValueError("invalid bus envelope task")

    digest = _sha256(envelope)
    body = json.dumps(envelope, sort_keys=True, indent=2, ensure_ascii=False)
    return f"<!-- neon-task:v1 sha256={digest} -->\n{body}\n{CLOSE}"


def verify_sha(text: str) -> bool:
    """Confirm sentinel sha256 matches actual body sha256."""
    for expected, body in _envelopes(text):
        return _load_verified(expected, body) is not None
    return False


def parse(text: str) -> dict | None:
    """Extract first valid envelope from text. Returns task dict or None."""
    for expected, body in _envelopes(text):
        task = _load_verified(expected, body)
        if task is None:
            continue
        if task.get("schema_version") != SCHEMA_VERSION:
            print(f"unsupported neon-task schema_version: {task.get('schema_version')!r}", file=sys.stderr)
            return None
        if _validate(task, require_schema=True):
            return task
    return None


if __name__ == "__main__":
    sample = dict(
        task_id="ulid:01HQZSMOKE00000000000000", kind="codex_exec", target_host="win-claude-01",
        payload_ref="smb://nas/neon-bus/payloads/01HQZ.json", payload_sha256="a" * 64,
        lease_seconds=600, idempotency_key="smoke", created_at="2026-05-13T12:30:00Z",
    )
    encoded = serialize(sample)
    assert verify_sha(encoded)
    assert parse(encoded) == {**sample, "schema_version": SCHEMA_VERSION}
    print("ok")
