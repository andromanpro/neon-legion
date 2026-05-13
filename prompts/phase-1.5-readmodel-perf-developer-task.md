# Task: Phase 1.5 follow-up #60 — readmodel hot-path optimization

Name: codex-developer
Profile: Codex CLI 0.128+ (gpt-5.5, high reasoning, --sandbox workspace-write)
Goal: Make `backend/readmodel.read_events()` faster than the JSONL fallback by assembling event dicts from column values directly instead of `json.loads(raw_json)` per row. Hit the original AC of ≤30% wall-time vs the JSONL baseline on a 30-day window.
Constraints: stdlib only, byte-identical numeric output (snapshot.json must match `--no-readmodel` to 4 decimal places on all numeric fields).
Watches: Gitea issue [#60](http://localhost:3000/androman/neon-legion/issues/60), `backend/readmodel.py` (existing schema + `read_events`), `tools/benchmark_readmodel.py` (benchmark harness), `backend/server.py` (`_read_events_dispatch` switch point).
Produces: 1 modified file (`backend/readmodel.py` — new `read_events_fast` function), 1 modified file (`backend/server.py` — `_read_events_dispatch` calls fast path by default), 1 modified file (`tests/test_readmodel.py` — ≥2 new tests for `read_events_fast`), 1 modified file (`tools/benchmark_readmodel.py` — include `read_events_fast` in the harness).

## Operational backstory

You are running with `workspace-write` sandbox in the neon-legion project. Phase 1.5 milestone v0.5 closed with #53 readmodel delivering correctness but failing the perf AC (median 1.949s vs JSONL 1.705s = 0.88x, i.e. SLOWER). Root cause: schema stores `raw_json TEXT NOT NULL` and `read_events` does `json.loads(raw_json)` per row to guarantee byte-identical dicts vs JSONL. The JSON decode dominates wall time and SQLite indexes don't help on a full-window no-filter read.

The fix: a second read path that builds dicts from typed column values. All current callers in `backend/server.py` only consume documented schema fields (`ts`, `provider`, `session_id`, `model`, token counts, `cost_estimate_usd`, `working_dir`, `tool_uses`, `stop_reason`, `duration_ms`). The `raw_json` path stays for any future caller that might need un-schemaed fields.

Tests run on host via `py -3.14 -m unittest`. Benchmark runs on host too — Codex can run it inside sandbox but the wall-time numbers depend on host I/O so the AC verification is architect's job.

## Working directory

`F:/WorkAI/multi-agent` (already your `--cd`).

## Project context

Read in this order:
1. `AGENTS.md`
2. `backend/readmodel.py` — current `read_events`, schema, column types, where `raw_json` is decoded
3. `backend/server.py` — find `_read_events_dispatch`, see what callers expect from event dicts
4. `tracker/summary.py` — `read_events`, the JSONL-baseline shape (canonical dict structure)
5. `tools/benchmark_readmodel.py` — current benchmark, what it measures

## Required schema fields for fast path

The `events` table has 17 columns. The dict shape callers in `backend/server.py` expect (verify by grep of `event["..."]` and `event.get("...")` patterns):

| Column | Dict key | Type |
|---|---|---|
| `provider` | `provider` | str |
| `ts` | `ts` | str (ISO 8601) |
| `session_id` | `session_id` | str or None |
| `message_uuid` | `message_uuid` | str or None |
| `model` | `model` | str or None |
| `input_tokens` | `input_tokens` | int |
| `output_tokens` | `output_tokens` | int |
| `cache_read_tokens` | `cache_read_tokens` | int |
| `cache_creation_tokens` | `cache_creation_tokens` | int |
| `total_tokens` | `total_tokens` | int |
| `cost_estimate_usd` | `cost_estimate_usd` | float |
| `duration_ms` | `duration_ms` | int |
| `working_dir` | `working_dir` | str or None |
| `tool_uses` | `tool_uses` | int |
| `stop_reason` | `stop_reason` | str or None |

`raw_json` is NOT included in the fast-path dict.

Edge cases:
- `provider` is the only field that's never None; everything else can be NULL → None in the dict.
- Token counts have `DEFAULT 0` so they should always be `int`, not `None`. Same for `tool_uses` and `duration_ms`.
- `cost_estimate_usd` has `DEFAULT 0` (REAL) — `float`.

## API

```python
def read_events_fast(
    conn: sqlite3.Connection,
    start: date | None = None,
    end: date | None = None,
    providers: list[str] | None = None,
) -> list[dict]:
    """Fast path: assemble event dicts from column values directly.

    Returns dicts with the documented schema fields (no raw_json). For callers
    that need full event payload, use read_events() which decodes raw_json.
    """
```

Implementation hint: a single `SELECT col1, col2, ... FROM events WHERE ts BETWEEN ... AND provider IN (...)` then iterate the cursor with `dict(zip(field_names, row))` — or build the dict explicitly per row, whichever benchmarks faster. Try both, pick the winner.

## Deliverables

### 1. `backend/readmodel.py`

- Add `read_events_fast(conn, start, end, providers)` as documented above.
- Keep existing `read_events` unchanged (it's the safety net for any future caller that needs un-schemaed fields).
- Optionally: extract a shared `_where_clause(start, end, providers)` helper so both functions share the WHERE building.

### 2. `backend/server.py`

- `_read_events_dispatch` switches to `read_events_fast` by default.
- The `--no-readmodel` flag still flips back to the JSONL path (untouched).
- A new optional `--use-slow-readmodel` flag (or similar — pick a clear name) flips the dispatcher to use `read_events` (raw_json decode). This is the debug escape hatch — most operators will never touch it.

### 3. `tests/test_readmodel.py`

Add ≥3 new tests:

1. `test_read_events_fast_returns_same_shape` — both `read_events` and `read_events_fast` against the same fixture return dicts with the same keys and identical values for every documented field.
2. `test_read_events_fast_omits_raw_json` — assert `"raw_json"` not in any returned dict.
3. `test_read_events_fast_handles_nulls` — fixture with NULL `session_id` / `model` / `working_dir` → dicts have `None` for those keys (not missing, not crashed).
4. `test_read_events_fast_filters` — date range + provider filter applied correctly.

All 11 existing tests must still pass unchanged.

### 4. `tools/benchmark_readmodel.py`

Extend the harness to measure three modes:
- `jsonl` (existing)
- `readmodel slow` (raw_json decode — existing)
- `readmodel fast` (new)

Output format:
```
readmodel slow median: X.XXs (5 runs)
readmodel fast median: Y.YYs (5 runs)
jsonl     median:      Z.ZZs (5 runs)
speedup vs jsonl:      <fast/jsonl ratio>
speedup vs slow:       <fast/slow ratio>
```

## Acceptance criteria

- `py -3.14 -m unittest tests.test_readmodel -v` — all ≥14 tests pass (11 existing + ≥3 new).
- `py -3.14 backend/server.py --snapshot-once --snapshot-path /tmp/snap-fast.json` produces a snapshot whose `productivity_periods.*.multiplier`, `*.hours_saved`, `*.active_hours`, and `*.estimated_hours` values are **identical** (4 decimal places) to a snapshot built with `--use-slow-readmodel`.
- `py -3.14 tools/benchmark_readmodel.py --days 30 --runs 5` reports `readmodel fast / jsonl ≤ 0.30` (i.e. fast path takes ≤30% of JSONL wall time).
- Stdlib only.

If the ≤30% AC is not achieved, document the achieved speedup in the final report — partial improvement (e.g. 0.55x) is still useful, just be honest about the gap and propose what else would need to change.

## Out of scope

- Persisting SQLite to disk.
- Materialised views / pre-aggregated tables.
- Live updates from hooks.
- Changing the existing `read_events` function (the safety-net path).

## Style / project conventions

- Match shape of existing `backend/readmodel.py`.
- `from __future__ import annotations`.
- No `Co-Authored-By:`.
- All numeric columns return their typed values (int, float), not str.

## Self-check before "done"

- New function exists and is imported in `backend/server.py`.
- Tests pass.
- Snapshot numbers identical between fast and slow paths (use `--snapshot-once` twice and `diff` the JSONs).
- Benchmark reports the speedup ratio.

## Final report

Conform to schema. **Required**: report the benchmark numbers verbatim (slow median, fast median, jsonl median, speedup vs jsonl, speedup vs slow). If the ≤30% AC is not achieved, mention it in `deviations_from_spec` with the actual ratio.
