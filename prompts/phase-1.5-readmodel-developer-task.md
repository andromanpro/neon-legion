# Task: Phase 1.5 #6 — SQLite read-model at backend startup

Name: codex-developer
Profile: Codex CLI 0.128+ (gpt-5.5, high reasoning, --sandbox workspace-write)
Goal: Add `backend/readmodel.py` that hydrates an in-memory SQLite cache from the canonical JSONL event store at backend startup. Wire it into `backend/server.py` so `build_summary` and `build_productivity` can use the cache. JSONL stays canonical — SQLite is a query accelerator only, rebuilt on every restart, never persisted.
Constraints: stdlib only (`sqlite3` is stdlib), backward-compatible `/api/*` shapes, `--no-readmodel` flag flips the whole pipeline back to JSONL-only.
Watches: Gitea issue [#53](http://localhost:3000/androman/neon-legion/issues/53), `backend/server.py` (`build_summary`, `build_productivity`, current JSONL readers in `tracker/summary.py`), `tracker/*-events.jsonl` (canonical shape).
Produces: 1 new file (`backend/readmodel.py` ~150 LOC), 1 new test file (`tests/test_readmodel.py` ~120 LOC), 1 new benchmark script (`tools/benchmark_readmodel.py` ~50 LOC), modifications to `backend/server.py` (wire-up + health + `--no-readmodel`).

## Operational backstory

You are running with `workspace-write` sandbox. Phase 1.5 #1–#5 merged — bus is operational end to end. #53 is independent of the bus layer; it speeds up the dashboard query path. **JSONL remains the source of truth**: every code path that reads from the read-model must have a JSONL fallback for `--no-readmodel`.

Tests run on host, not in sandbox. Use `unittest` only; the benchmark is a separate one-off script the architect runs against the real event store.

The hot paths to accelerate:
- `summary.read_events(start, end)` — sweep all four provider JSONLs, filter by date
- `summary.summarize_by_provider(events)` — group by provider, sum tokens/cost
- `summary.summarize_productivity(events, tasks, days=N)` — active hours, multipliers, etc.

The read-model accelerates **only the read-events step**. The downstream summarisers can stay in Python — they're cheap once the rows are in memory.

## Working directory

`F:/WorkAI/multi-agent`.

## Project context

Read in this order:
1. `AGENTS.md`
2. `docs/phase-1.5-git-bus.md` — read-model section near the bottom
3. `backend/server.py` — find every call to `summary.read_events(...)`. That's the swap point.
4. `tracker/summary.py` — current JSONL readers. Look at the event keys (`ts`, `session_id`, `model`, `provider`, `input_tokens`, etc.) — the SQLite schema must capture them.
5. `tracker/claude-events.jsonl`, `tracker/codex-events.jsonl`, `tracker/openclaw-events.jsonl`, `tracker/opencode-events.jsonl` — first ~3 lines of each to confirm key shape.

## Schema

```sql
CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,
    ts TEXT NOT NULL,                  -- ISO 8601 string; lex order = chrono order
    session_id TEXT,
    message_uuid TEXT,                 -- claude only; NULL elsewhere
    model TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    cost_estimate_usd REAL DEFAULT 0,
    duration_ms INTEGER DEFAULT 0,
    working_dir TEXT,
    tool_uses INTEGER DEFAULT 0,
    stop_reason TEXT,
    raw_json TEXT NOT NULL             -- the full original line, for any field the schema missed
);
CREATE INDEX idx_events_ts ON events(ts);
CREATE INDEX idx_events_session ON events(session_id);
CREATE INDEX idx_events_provider ON events(provider);

CREATE TABLE tasks (
    session_id TEXT PRIMARY KEY,
    brief_description TEXT,
    ai_baseline_hours REAL,
    human_corrected_hours REAL,
    estimation_confidence TEXT,
    needs_manual_review INTEGER,        -- 0 / 1
    profanity_count INTEGER,
    mood_score REAL,
    estimated_at TEXT,
    transcript_path TEXT,
    raw_json TEXT NOT NULL
);
```

Bus event table is **out of scope** for this PR — the bus-events JSONL doesn't yet exist as a stream (the worker writes events to Gitea, not to a local JSONL). Add a `bus_tasks` placeholder comment in the docstring noting it lands later.

## Public API

```python
def build(events_dir: Path, *, providers: list[str] = None) -> sqlite3.Connection:
    """Build an in-memory SQLite cache from JSONL events + tasks.json.

    Returns a `sqlite3.Connection` to a `:memory:` database with the events
    and tasks tables populated and indexed. Caller closes when done.

    Corrupt JSON lines are logged to stderr and skipped — never raise.
    Missing optional fields become NULL.
    """

def build_with_meta(events_dir: Path, *, providers: list[str] = None) -> tuple[sqlite3.Connection, dict]:
    """Same as build(), plus a meta dict {events: N, tasks: M, built_at: iso8601}
    for /api/health exposure."""

def read_events(conn: sqlite3.Connection, start: date, end: date, providers: list[str] = None) -> list[dict]:
    """Mirror of summary.read_events() against the SQLite cache. Returns
    list of event dicts matching the original JSONL shape exactly (decode
    raw_json so downstream summarisers see no behavioural change)."""
```

The "decode raw_json" trick: the SQLite row is a fast index, but the returned dict comes from `json.loads(row.raw_json)`. This guarantees byte-identical event dicts vs. the JSONL path — no chance of summarisers seeing different keys.

## Wire-up in `backend/server.py`

1. Add `--no-readmodel` arg to the existing parser. Default: cache **enabled**.
2. On startup (before the HTTP server runs): if cache enabled, build it once and stash on a module global `_READMODEL: sqlite3.Connection | None`.
3. Add a `_read_events_dispatch(start, end, providers=None) -> list[dict]` helper that uses the cache if available, falls back to `summary.read_events` otherwise.
4. Replace **every** `summary.read_events(...)` call in `backend/server.py` with `_read_events_dispatch(...)`.
5. Extend the `/api/health` response with `"readmodel": {"events": N, "tasks": M, "built_at": "..."} | null` (null when `--no-readmodel`).

**Do not touch `tracker/summary.py`.** The summarisers downstream of `read_events` must keep working unchanged — they take a list of event dicts as input. The dispatch helper guarantees that contract.

## Benchmark script

`tools/benchmark_readmodel.py`:

- Build once with cache, time `read_events` over a 30-day window N=10 times. Report median.
- Build once without cache (call `summary.read_events` directly), time same window N=10 times. Report median.
- Print both medians and the ratio.
- The architect runs it on host against the real event store and adds the numbers to the commit message.

CLI: `py -3.14 tools/benchmark_readmodel.py [--days 30] [--runs 10]`.

## Deliverables

### 1. `backend/readmodel.py`

Public API as above. Internal helpers as needed. Stdlib only (`sqlite3` IS stdlib).

### 2. `tests/test_readmodel.py`

Unit tests (≥10) using `unittest` + `tempfile.TemporaryDirectory()` for a clean events dir. Coverage:

1. `test_build_empty_dir_returns_connection` — empty dir → connection with 0 events, 0 tasks.
2. `test_build_populates_events_from_one_provider` — one JSONL with 3 events → 3 rows in `events`, correct `provider` column.
3. `test_build_populates_events_from_all_providers` — all four JSONLs → rows from each, `provider` column distinguishes.
4. `test_build_handles_corrupt_line` — JSONL with one bad line + 2 good → 2 rows, warning to stderr.
5. `test_build_handles_missing_optional_fields` — minimal event (just `ts`, `provider`) → row created, optional cols NULL/0.
6. `test_build_populates_tasks_from_json` — `tasks.json` with 2 entries → 2 rows in `tasks`.
7. `test_build_with_meta_returns_counts` — returns `(conn, {events: N, tasks: M, built_at: ...})`.
8. `test_read_events_filters_by_date` — only events within `[start, end]` returned.
9. `test_read_events_filters_by_provider` — `providers=["claude"]` returns only claude events.
10. `test_read_events_decodes_raw_json` — returned dicts match the original JSONL line `json.loads()` byte-for-byte.
11. `test_indexes_exist` — `PRAGMA index_list('events')` includes `idx_events_ts`, `idx_events_session`, `idx_events_provider`.

Tests must run in <3 s total. No real Gitea call, no real backend startup.

### 3. `tools/benchmark_readmodel.py`

Standalone bench (not run as part of unittest). Stdlib only.

### 4. `backend/server.py` modifications

As described above. Keep diffs minimal: parse one arg, build once, dispatch helper, swap call sites, add health field.

## Acceptance criteria

- `py -3.14 -c "import sys; sys.path.insert(0, '.'); from backend.readmodel import build, build_with_meta, read_events; print('ok')"` prints `ok`.
- `py -3.14 -m unittest tests.test_readmodel -v` — all ≥10 tests pass.
- `py -3.14 -m py_compile backend/readmodel.py tests/test_readmodel.py tools/benchmark_readmodel.py backend/server.py` exits 0.
- `py -3.14 backend/server.py --snapshot-once --snapshot-path /tmp/snap.json` succeeds, produces a snapshot identical in shape to the pre-PR snapshot (sanity: same top-level keys).
- `py -3.14 backend/server.py --no-readmodel --snapshot-once --snapshot-path /tmp/snap.json` also succeeds (fallback works).
- Full suite stays green.
- Stdlib only.

## Out of scope

- Persisting SQLite to disk (intentional — rebuild eliminates migration risk).
- Live updates from hooks (rebuild on backend restart is enough; the snapshot writer already runs the snapshot fresh).
- Bus event table (lands when the bus event JSONL stream exists).

## Style / project conventions

- Match shape of `tools/bus_worker.py` (docstring → constants → public → private → `__main__` smoke).
- `from __future__ import annotations`.
- No `Co-Authored-By:`.
- Logging: `print(f"[readmodel] ...", file=sys.stderr)`.

## Self-check before "done"

- All tests pass.
- `_read_events_dispatch` is the **only** swap point in `backend/server.py` — no other `summary.read_events` calls left in the file.
- `--no-readmodel` actually disables the build (verify by running once with each flag and confirming `/api/health` returns `null` in the disabled case).
- The snapshot top-level keys (`totals`, `by_model`, `productivity`, etc.) are unchanged. Run the architect's existing `tools/privacy-scan-snapshot.py` against a generated snapshot to confirm shape hasn't regressed.

## Final report

Conform to schema. Include `tested: true` only if all tests + py_compile + import smoke + snapshot-once smoke (both flag modes) succeeded.
