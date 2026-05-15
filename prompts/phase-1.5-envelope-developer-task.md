# Task: Phase 1.5 #1 — bus envelope library

Name: codex-developer
Profile: Codex CLI 0.128+ (gpt-5.5, high reasoning, --sandbox workspace-write)
Goal: Self-contained `tools/bus_envelope.py` (stdlib only) that handles the sentinel-wrapped JSON envelope used by the Phase 1.5 Git bus.
Constraints: stdlib only, UTF-8, atomic write, no third-party deps, **forward-compat** schema versioning.
Watches: Gitea issue [#48](http://localhost:3000/androman/neon-legion/issues/48), design doc `docs/phase-1.5-git-bus.md`.
Produces: 2 new files (`tools/bus_envelope.py` ~50 LOC + `tests/test_bus_envelope.py` ~80 LOC).

## Operational backstory

You are running with `workspace-write` sandbox in the neon-legion project. Phase 1.5 starts after P0 (live OpenClaw tracking + mojibake normalize) closed in commit 746f9a1. The bus design is in `docs/phase-1.5-git-bus.md`. This task is the foundation that #2 (Gitea client) and #3 (worker loop) depend on.

**Sandbox limitation** (Phase 1.0.2 lesson): tests run on host via `py -3.14 -m unittest`, not inside Codex sandbox. Codex does py_compile + table-driven assertions inside `if __name__ == "__main__"` for smoke validation.

## Working directory

`<project-dir>` (already your `--cd`).

## Project context

Read `AGENTS.md`, `README.md`, `docs/phase-1.5-git-bus.md`. The bus is **routing layer, not data layer** — envelopes carry pointer fields, payload lives on NAS share. Sentinel-wrapped because the envelope can land inside an issue body or a comment alongside other text.

## Envelope spec

Sentinel-wrapped JSON:

```
<!-- neon-task:v1 sha256=<hex64> -->
{
  "schema_version": 1,
  "task_id": "ulid:01HQZ...",
  "kind": "codex_exec",
  "target_host": "win-claude-01",
  "payload_ref": "smb://nas/neon-bus/payloads/01HQZ.json",
  "payload_sha256": "<hex64>",
  "lease_seconds": 600,
  "idempotency_key": "openclaw-2026-05-13T12:30Z-codex-exec-7",
  "created_at": "2026-05-13T12:30:00Z"
}
<!-- /neon-task:v1 -->
```

The opening sentinel's `sha256=` is computed over the canonical JSON payload (sorted keys, `separators=(",", ":")`, ensure_ascii=False).

## Deliverables

### 1. `tools/bus_envelope.py`

Public API (stdlib only):

```python
def serialize(task: dict) -> str:
    """Wrap task dict in sentinel-delimited envelope with sha256 header."""

def parse(text: str) -> dict | None:
    """Extract first valid envelope from text. Returns task dict or None."""

def verify_sha(text: str) -> bool:
    """Confirm sentinel sha256 matches actual body sha256."""
```

Required envelope fields (raise `ValueError` if missing in `serialize`, return `None` if missing in `parse`):

- `task_id` (string, non-empty)
- `kind` (string, non-empty)
- `target_host` (string, non-empty)
- `payload_ref` (string, non-empty)
- `payload_sha256` (string, 64 hex chars)
- `lease_seconds` (int, > 0)
- `idempotency_key` (string, non-empty)
- `created_at` (ISO 8601 string)

Forward-compat: `schema_version` ≠ 1 in `parse` → return `None` and log to stderr (do not raise).

Canonical JSON for sha256:
```python
json.dumps(task, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
```

Multiple envelopes in same text → return the first.

### 2. `tests/test_bus_envelope.py`

Unit tests using `unittest`. Coverage:

1. `test_roundtrip` — serialize a known task, parse it back, assert equality.
2. `test_serialize_sha256_correctness` — explicit golden sha256 for a fixture.
3. `test_parse_returns_none_on_wrong_sha` — flip one byte in body, parse returns None.
4. `test_parse_returns_none_on_missing_field` — drop `task_id` from body, parse returns None.
5. `test_parse_returns_none_on_malformed_json` — sentinel-wrapped `{not json}`, parse returns None.
6. `test_parse_returns_none_on_v2_schema` — body has `schema_version: 2`, parse returns None + a stderr message captured.
7. `test_parse_picks_first_envelope` — text contains two envelopes, returns the first.
8. `test_parse_returns_none_on_no_sentinel` — plain text without any envelope.
9. `test_verify_sha_true_on_match` and `test_verify_sha_false_on_mismatch`.
10. `test_serialize_raises_on_missing_field` — task dict without `task_id` raises `ValueError`.

Tests must be runnable as `py -3.14 -m unittest tests.test_bus_envelope` from project root.

## Out of scope

- Persistence (file writes, Gitea calls).
- Compression / signing.
- Webhook receiver.
- The `unittest` runner itself — tests run on host.

## Acceptance criteria

- `py -3.14 -c "import sys; sys.path.insert(0, '.'); from tools.bus_envelope import serialize, parse, verify_sha; print('ok')"` prints `ok`.
- `py -3.14 -m unittest tests.test_bus_envelope -v` passes all 10 tests.
- `py -3.14 -m py_compile tools/bus_envelope.py tests/test_bus_envelope.py` exits 0.
- Stdlib only. No new dependencies.

## Style / project conventions

Per `AGENTS.md` "Conventions for any agent":

1. No outbound network — pure parsing module.
2. Stdlib only.
3. Atomic writes (n/a, no file writes).
4. No `Co-Authored-By:` trailers in commits.
5. Privacy by default (this module handles bus metadata, never raw prompts).
6. UTF-8 everywhere (the envelope body is UTF-8).

## Workflow

1. Read `docs/phase-1.5-git-bus.md` (full design doc), `AGENTS.md`.
2. Implement `tools/bus_envelope.py`.
3. Implement `tests/test_bus_envelope.py`.
4. Run `py -3.14 -m py_compile` on both files.
5. Run `py -3.14 -m unittest tests.test_bus_envelope -v` and confirm 10/10 pass.
6. Print a one-paragraph summary describing what landed, file sizes, and any deviations from this spec.

## Self-check before reporting "done"

- All 10 unit tests pass.
- `py_compile` passes.
- No `import` of anything outside stdlib.
- All envelope fields validated, schema_version ≠ 1 path tested.
- sha256 is computed over canonical JSON (sorted keys, no spaces, ensure_ascii=False) — verify with the golden test #2.
- The module is importable from `<project-dir>` root as `tools.bus_envelope`.
