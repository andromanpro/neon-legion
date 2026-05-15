# Task: Phase 1.5 follow-up #69 — pre-aggregated SUM/GROUP tables for readmodel

Name: codex-developer
Profile: Codex CLI 0.128+ (gpt-5.5, high reasoning, --sandbox workspace-write)
Goal: Hit the original ≤30% wall-time AC from #60 by aggregating at SQL level instead of materialising every event in Python. Add `aggregate_by_model` and `aggregate_totals` SQL aggregations in `backend/readmodel.py`; wire them into `build_summary` as the default fast path.
Constraints: stdlib only, snapshot top-level numbers byte-identical to the current `read_events_fast` path on the same window, existing 161 tests stay green.
Watches: Gitea issue [#69](http://localhost:3000/androman/neon-legion/issues/69), `backend/readmodel.py` (events table schema + existing `read_events_fast`), `backend/server.py build_summary` (line 387), `tracker/summary.py summarize_by_model` (the JSONL baseline behaviour to mirror), `tools/benchmark_readmodel.py`.
Produces: 1 modified file (`backend/readmodel.py` — `aggregate_by_model` + `aggregate_totals`), 1 modified file (`backend/server.py` — `build_summary` uses aggregates by default), 1 modified file (`tools/benchmark_readmodel.py` — adds "aggregate" mode), 1 modified file (`tests/test_readmodel.py` — aggregate parity tests).

## Operational backstory

PR #68 (#60 partial close) hit 0.75x readmodel speedup vs JSONL — better than slow path (0.88x) but not the original AC of ≤30% wall time. Architect (me) called out that the bottleneck is row-by-row dict materialisation in Python from SQLite cursor. Real speedup needs **moving the aggregation into SQL itself** — `SELECT SUM(...), provider, model FROM events GROUP BY provider, model WHERE ts BETWEEN ... AND ...`.

`build_summary` is the hot path:
- Reads all events in window (~100k rows on 30d window).
- Calls `summary.summarize_by_model(events)` which iterates events in Python, accumulates per-model stats into a dict.
- Returns `{period, totals, by_model}`.

The aggregation can run entirely in SQL: one query for `by_model`, one query for `totals`. The returned dict-of-dicts shape stays identical so `model_payload` / `stats_payload` consumers in `build_summary` don't change.

`build_productivity` is more complex (active_hours uses gap-based timeline — hard to express in SQL). **Out of scope for this PR** — keep using `read_events_fast` + `summary.summarize_productivity` for now. Tracking that as a separate optimisation if needed.

Tests run on host. Benchmark validates the AC.

## Working directory

`<project-dir>` (already your `--cd`).

## Project context

Read in order:
1. `backend/readmodel.py` — events table schema (cols: provider, ts, session_id, model, *_tokens, cost_estimate_usd, etc.), existing `read_events_fast`.
2. `tracker/summary.py` lines 401-500 — `summarize_by_model(events)`. Important: it groups by model and accumulates `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `cost_estimate_usd`, `calls` (= row count), `unknown_pricing_events` (events with model not in pricing map), plus `models` and `origins` sub-dicts. The aggregate must match this shape.
3. `tracker/summary.py summarize_by_provider` — same shape per provider.
4. `backend/server.py` `_read_events_dispatch` and `build_summary` — the wire-up site.
5. `tools/benchmark_readmodel.py` — current 3-mode benchmark; add a 4th "aggregate" mode.

## API

In `backend/readmodel.py`:

```python
def aggregate_by_model(
    conn: sqlite3.Connection,
    start: date,
    end: date,
    providers: list[str] | None = None,
) -> tuple[dict[str, dict], dict]:
    """SQL-level GROUP BY model. Returns (by_model, totals) matching
    summary.summarize_by_model's output shape exactly."""

def aggregate_by_provider(
    conn: sqlite3.Connection,
    start: date,
    end: date,
) -> dict[str, dict]:
    """SQL-level GROUP BY provider. Returns dict[provider] -> stats dict."""
```

Implementation hint — single query for `by_model`:

```sql
SELECT
  model,
  json_provider,        -- normalized provider name
  COUNT(*) AS calls,
  SUM(input_tokens) AS input_tokens,
  SUM(output_tokens) AS output_tokens,
  SUM(cache_read_tokens) AS cache_read_tokens,
  SUM(cache_creation_tokens) AS cache_creation_tokens,
  SUM(cost_estimate_usd) AS cost_estimate_usd
FROM events
WHERE ts >= ? AND ts < ?
  -- AND provider IN (...)  -- optional
GROUP BY model, json_provider
```

Iterate cursor in Python to build the dict. Same date-range logic as `read_events_fast` (the `_safe_date_offset` ±1 trick). Handle NULL `model` / `json_provider` with the same fallback rules `_event_provider` uses.

For `unknown_pricing_events`: needs the pricing map lookup. Two options:
1. Compute in Python after aggregation: for each `(model, provider)` pair, check if pricing is known; if not, `unknown_pricing_events = calls`. Acceptable — adds O(distinct_models) Python work, not O(events).
2. Hardcode the model prefix check in SQL via CASE WHEN. Avoid — pricing logic stays in `summary.py`.

Use option 1.

## Wire-up

`backend/server.py build_summary`:

```python
def build_summary(query):
    days = parse_days(query)
    start, end = period_for_days(days)
    if _READMODEL is not None and not _USE_SLOW_READMODEL:
        by_model, total = readmodel.aggregate_by_model(_READMODEL, start, end)
    else:
        events = _read_events_dispatch(start, end)
        by_model, total = summary.summarize_by_model(events)
    # ... rest unchanged
```

Add a `--use-loop-summary` flag (or similar — pick a clear name) for debugging discrepancies between aggregate and loop paths. Default to aggregate.

## Benchmark

Extend `tools/benchmark_readmodel.py` to time `aggregate_by_model` over the same 30d window:

```
readmodel slow median:  X.XXs
readmodel fast median:  Y.YYs
readmodel aggregate:    Z.ZZs   ← new
jsonl     median:       W.WWs
speedup vs jsonl:       <Z/W ratio>
```

Target: `aggregate / jsonl ≤ 0.30`.

## Tests — `tests/test_readmodel.py`

Add ≥3 tests:

1. `test_aggregate_by_model_matches_summarize_by_model` — build readmodel from a fixture; call `aggregate_by_model`; call `summary.summarize_by_model(read_events_fast(...))` against the same data; assert dict-equality for the model dict AND for the totals.
2. `test_aggregate_by_model_handles_unknown_pricing` — fixture includes an event with model="claude-mystery-7" (no pricing); `unknown_pricing_events` count matches across both paths.
3. `test_aggregate_by_model_filters_by_provider` — restrict to provider="claude"; only claude events in result.

## Correctness verification

Architect will run `py -3.14 backend/server.py --snapshot-once --snapshot-path /tmp/snap-agg.json` and compare with `--use-loop-summary` mode. All `productivity_periods.*.multiplier`, `*.hours_saved`, `*.active_hours`, `*.estimated_hours` numeric values must be byte-identical to 4 decimals.

## Acceptance criteria

- `py -3.14 -m unittest tests.test_readmodel -v` — all 20 existing + ≥3 new tests pass.
- `py -3.14 -m py_compile backend/readmodel.py backend/server.py tools/benchmark_readmodel.py tests/test_readmodel.py` exits 0.
- Snapshot byte-identity (architect verifies).
- `py -3.14 tools/benchmark_readmodel.py --days 30 --runs 5` reports `aggregate / jsonl ≤ 0.30`.
- Stdlib only.

If the ≤0.30 target is missed by a small margin (e.g. 0.35), report the achieved ratio in `deviations_from_spec`; partial improvement is still useful.

## Out of scope

- `build_productivity` SQL refactor (active_hours gap-based timeline is hard in SQL).
- Persisting aggregates between backend restarts.
- Materialised views (would need persistent storage).
- Incremental aggregation as new events arrive.

## Style / project conventions

- Match shape of `backend/readmodel.read_events_fast`.
- `from __future__ import annotations`.
- No `Co-Authored-By:`.
- Add `# DeepSeek audit #60 follow-up` comments where relevant.

## Self-check before "done"

- All ≥23 readmodel tests pass.
- `py_compile` clean.
- `aggregate_by_model` returns the SAME shape as `summary.summarize_by_model` (architect will diff).
- Benchmark reports the ratio.
- `_USE_LOOP_SUMMARY` (or similar) flag exposed for debug.

## Final report

Conform to schema. **Required**: report benchmark numbers verbatim (aggregate median, jsonl median, ratio). If ≤30% AC not hit, mention actual ratio in `deviations_from_spec`.
