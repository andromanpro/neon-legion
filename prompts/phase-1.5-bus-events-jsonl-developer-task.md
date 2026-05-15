# Task: Phase 1.5 follow-up #66 — bus-events.jsonl stream

Name: codex-developer
Profile: Codex CLI 0.128+ (gpt-5.5, high reasoning, --sandbox workspace-write)
Goal: Mirror the worker's Gitea state transitions into a local append-only `tracker/bus-events.jsonl` stream so the readmodel SQLite cache can hydrate a `bus_tasks` query view at backend startup. Closes DeepSeek E1.
Constraints: stdlib only, atomic write (tmp + os.replace), append-only (never rewrite past lines), worker is non-blocking on JSONL write failure.
Watches: Gitea issue [#66](http://localhost:3000/androman/neon-legion/issues/66), `tools/bus_worker.py` (process_issue transitions), `backend/readmodel.py` (schema + populate), `tracker/*-events.jsonl` (existing provider streams for shape reference).
Produces: 1 modified file (`tools/bus_worker.py` — emit transitions to JSONL), 1 modified file (`backend/readmodel.py` — `bus_tasks` table + hydration), 1 modified file (`tests/test_bus_worker.py` — JSONL write tests), 1 modified file (`tests/test_readmodel.py` — bus_tasks hydration tests), 1 new file (`tracker/bus-events.jsonl` — empty placeholder for fresh installs; or just gitignored).

## Operational backstory

The Phase 1.5 worker writes state to Gitea (labels + sentinel comments). DeepSeek E1 from the original audit noted the readmodel schema has a `bus_tasks` placeholder column but no source of data. This PR closes that gap.

Tests run on host. JSONL writes use `tmp + os.replace` for atomicity, same as the existing `hooks/claude-track-calls.py` pattern.

The worker should NEVER block on a JSONL write failure. The Gitea state is the source of truth; the JSONL is a local convenience for the read-model. If the disk is full or the directory missing, log + continue.

## Working directory

`<project-dir>` (already your `--cd`).

## Project context

Read in order:
1. `AGENTS.md` — append-only events convention.
2. `tools/bus_worker.py` — find the state-transition sites: claim PATCH (line ~133), in-progress PATCH (line ~160), final PATCH at lines ~200-208.
3. `backend/readmodel.py` — `_create_schema` (line ~207), `_load_event_file` (line ~250), `_load_tasks` (line ~310). Add a `_load_bus_events` paralleling them.
4. `hooks/claude-track-calls.py` lines 185-234 — reference for atomic write + lock pattern.
5. `tracker/claude-events.jsonl` — read one or two lines to see the canonical shape.

## Bus event shape

Each event line is a JSON object:

```json
{
  "schema_version": 1,
  "provider": "bus",
  "ts": "2026-05-13T22:30:00.123+03:00",
  "task_id": "ulid:01HQZ...",
  "session_id": "ulid:01HQZ...",
  "kind": "echo",
  "transition": "claimed",
  "exec_id": "win-claude-01-1700000000-abc123",
  "target_host": "win-claude-01",
  "issue_number": 50,
  "lease_seconds": 600,
  "input_tokens": 0,
  "output_tokens": 0,
  "cost_estimate_usd": 0,
  "duration_ms": 0
}
```

Notes:

- `session_id` mirrors `task_id` so the readmodel can join with `tasks.json` if any.
- `transition` is one of `claimed`, `in-progress`, `done`, `failed`.
- Token / cost fields are zero — bus events don't consume LLM tokens; they're orchestration metadata. The readmodel sums them and they contribute nothing to cost/active-hours, which is correct.
- `ts` is ISO 8601 with timezone.
- Lost-claim race: do NOT emit a transition for losing workers — only the canonical winner writes events.

## Logic — where to emit

In `tools/bus_worker.py process_issue`, emit after a successful state transition:

| Site | When | Transition |
|---|---|---|
| After successful claim CAS (line ~152, just before payload load) | Worker A won claim race | `claimed` |
| After successful `_set_state(IN_PROGRESS)` (line ~160) | Payload verified, handler about to run | `in-progress` |
| After successful `_set_state(DONE)` (line ~203) | Handler completed | `done` |
| In the `BusGiteaError` except at line ~204 | Orphaned but result was posted | (skip — terminal state is unknown locally) |
| After successful `_set_state(FAILED)` | Handler failed | `failed` |

The "lease lost" path (line ~191-199 return) does NOT emit — that worker isn't the canonical owner of this task.

Implementation hint:

```python
def _emit_bus_event(envelope: dict, exec_id: str, host: str, issue_number: int, transition: str) -> None:
    """Append a bus-events.jsonl entry. Non-blocking: log + continue on any error."""
    event = {
        "schema_version": 1,
        "provider": "bus",
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "task_id": envelope.get("task_id"),
        "session_id": envelope.get("task_id"),  # join key for tasks.json
        "kind": envelope.get("kind"),
        "transition": transition,
        "exec_id": exec_id,
        "target_host": host,
        "issue_number": issue_number,
        "lease_seconds": int(envelope.get("lease_seconds", 0)),
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_estimate_usd": 0,
        "duration_ms": 0,
    }
    try:
        path = PROJECT_ROOT / "tracker" / "bus-events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
        # Read existing, append our line, atomic replace.
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        with tmp.open("w", encoding="utf-8", newline="\n") as f:
            f.write(existing)
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as exc:
        log(f"failed to emit bus event for #{issue_number} {transition}: {exc}", level="error")
```

(Same pattern as `hooks/claude-track-calls.py append_event`. Less paranoid lock — bus_worker is single-threaded per host, no concurrent writes from the same process.)

## Readmodel hydration

In `backend/readmodel.py`:

1. **Schema**: add a `bus_tasks` table:

```sql
CREATE TABLE bus_tasks (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    task_id TEXT,
    kind TEXT,
    transition TEXT NOT NULL,
    exec_id TEXT,
    target_host TEXT,
    issue_number INTEGER,
    lease_seconds INTEGER DEFAULT 0,
    raw_json TEXT NOT NULL
);
CREATE INDEX idx_bus_tasks_task ON bus_tasks(task_id);
CREATE INDEX idx_bus_tasks_ts ON bus_tasks(ts);
```

2. **Populate** via a new `_load_bus_events(conn, path)` paralleling `_load_event_file` — read `tracker/bus-events.jsonl`, INSERT each line. Skip corrupt JSON lines (log + continue, same as event_file path). Return count.

3. **Wire into `build_with_meta`** — call `_load_bus_events` alongside `_load_event_file` for each provider. Add the count to the returned meta dict as `bus_tasks: N`.

4. **`/api/health` extension** in `backend/server.py`: where `readmodel.events` is exposed, also expose `bus_tasks: N`.

## Tests

### `tests/test_bus_worker.py`

Add ≥3 tests:

1. `test_emit_bus_event_writes_jsonl_line` — call `_emit_bus_event` directly with a synthetic envelope, assert the file gets one valid JSON line with all expected fields.
2. `test_process_issue_happy_path_emits_three_transitions` — run the existing happy-path scenario, then read `tracker/bus-events.jsonl` and assert three transitions appeared: `claimed`, `in-progress`, `done`.
3. `test_lost_claim_does_not_emit_bus_event` — use the lost-claim race fixture, assert NO line was appended to bus-events.jsonl.

Use a `tempfile.TemporaryDirectory` for `PROJECT_ROOT/tracker` so tests don't pollute the real JSONL.

### `tests/test_readmodel.py`

Add ≥2 tests:

1. `test_bus_events_jsonl_populates_bus_tasks` — write a synthetic 3-event JSONL, build the readmodel, assert `SELECT COUNT(*) FROM bus_tasks` returns 3.
2. `test_bus_events_corrupt_line_skipped` — synthetic JSONL with one valid + one corrupt line, build, count is 1, error logged.

## Acceptance criteria

- `py -3.14 -m unittest tests.test_bus_worker tests.test_readmodel -v` — all new tests pass + existing 30 worker + 18 readmodel tests stay green (≥53 total in these two files).
- `py -3.14 -m py_compile tools/bus_worker.py backend/readmodel.py tests/test_bus_worker.py tests/test_readmodel.py` exits 0.
- `py -3.14 backend/server.py --snapshot-once --snapshot-path /tmp/snap-bus.json` succeeds; the snapshot keeps existing numeric fields byte-identical.
- Full repo suite stays green.
- Stdlib only.

## Out of scope

- Idempotency-key dedup using bus_tasks (that's #67, builds on this).
- Webhook push from Gitea (still polling).
- Backfilling bus events from existing Gitea issue history (future task).

## Style / project conventions

- Match shape of `hooks/claude-track-calls.append_event` (atomic write).
- `from __future__ import annotations`.
- No `Co-Authored-By:`.
- Logging: `print(f"[bus-worker] ...", file=sys.stderr)`.
- Add `tracker/bus-events.jsonl` to `.gitignore` (existing provider JSONLs are gitignored — mirror that pattern).

## Self-check before "done"

- `tools/bus_worker.py` emits at exactly four sites: claim-won, in-progress, done, failed. Lost-claim path does NOT emit.
- JSONL write failures are caught + logged, never propagate.
- `bus_tasks` table populated; corrupt lines skipped.
- `/api/health` shows `bus_tasks: N`.

## Final report

Conform to schema. Report the count of new tests added (worker + readmodel).
