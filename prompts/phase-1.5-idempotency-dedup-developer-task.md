# Task: Phase 1.5 follow-up #67 — idempotency-key dedup cache

Name: codex-developer
Profile: Codex CLI 0.128+ (gpt-5.5, high reasoning, --sandbox workspace-write)
Goal: Honour the envelope's `idempotency_key`. If the same key has already been processed (cached result available from a prior task), skip running the handler and return the cached result instead. Closes DeepSeek E3.
Constraints: stdlib only, cache scope = single worker process lifetime (no on-disk persistence), no concurrency primitives beyond a plain dict + worker's existing single-threaded process_issue loop.
Watches: Gitea issue [#67](http://localhost:3000/androman/neon-legion/issues/67), `tools/bus_worker.py` (`process_issue`, post-merge of #66 — bus-events.jsonl emit calls already in place), the `tracker/bus-events.jsonl` shape from #66.
Produces: 1 modified file (`tools/bus_worker.py` — cache map + lookup + hit-path), 1 modified file (`tests/test_bus_worker.py` — hit/miss/non-collision tests).

## Operational backstory

Per the design doc: *"Every task carries an `idempotency_key`. The worker SHOULD record the (idempotency_key → result) mapping in its local read-model cache so a re-issue from the same originator (e.g. dropped network ack) returns the prior result instead of re-running."*

DeepSeek E3 flagged this is unimplemented. Now that #66 has emitted `bus_tasks` events with task_id, we can:

1. Maintain an in-memory `dict[str, dict]` mapping idempotency_key → result envelope.
2. Optionally hydrate it at worker start by replaying `tracker/bus-events.jsonl` and reading the `neon-result:v1` comments on those issues. (Optional — see "Hydration scope" below.)
3. Before dispatching a fresh task to its handler, look up the key — if found, post the cached result envelope + transition to done immediately. No handler run.

The cache lifetime is the worker process. A worker restart drops the cache and the next re-issue of the same key would re-run — that's acceptable for MVP. DeepSeek E3 explicitly said "out of scope: persisting the index beyond a single backend process."

## Working directory

`F:/WorkAI/multi-agent` (already your `--cd`).

## Project context

Read in order:
1. `tools/bus_worker.py` — focus on the `process_issue` flow after PR #78 merged. Find the `bus_envelope.parse` step at line ~118 and the handler dispatch at line ~166.
2. `bus_envelope.py` — the envelope schema (idempotency_key is field #7).
3. `tools/bus_worker._post_result` and `_set_state` — used to short-circuit the cached-hit path with a "done" transition.

## Logic — where to insert

After successful envelope parse (line ~119) and BEFORE the optimistic label PATCH (line ~133), check the cache:

```python
idempotency_key = envelope.get("idempotency_key")
cached = IDEMPOTENCY_CACHE.get(idempotency_key) if isinstance(idempotency_key, str) and idempotency_key else None
if cached is not None:
    # Replay path: same key seen before → reuse the prior result envelope.
    log(f"#{number} idempotency-cache HIT for key={idempotency_key}; replaying prior result")
    # Still PATCH labels to claimed → in-progress → done so the issue
    # state machine reflects "handled" without a fresh handler run.
    exec_id = _new_exec_id(host)
    try:
        claimed = _set_state(number, labels, CLAIMED)
        claimed_labels = _label_names(claimed)
        bus_gitea.comment(number, _claim_comment(host, exec_id, int(envelope.get("lease_seconds", 0))))
        _emit_bus_event(envelope, exec_id, host, number, "claimed")
        in_progress_labels = _label_names(_set_state(number, claimed_labels, IN_PROGRESS))
        _emit_bus_event(envelope, exec_id, host, number, "in-progress")
        _post_result(number, exec_id, {**cached, "replay_of_idempotency_key": idempotency_key})
        _set_state(number, in_progress_labels, DONE, close=True)
        _emit_bus_event(envelope, exec_id, host, number, "done")
    except BusGiteaError as exc:
        log(f"#{number} idempotency replay failed mid-transition: {exc}", level="error")
    return
```

(Skip the CAS verify on replay — it's a single-worker fast-path. The trade-off: a concurrent worker could ALSO see the cache and replay; in MVP single-worker-per-target deployment, this isn't an issue. The new claim CAS in `_verify_claim_won` is only needed for fresh handler runs where actual work happens.)

Then update the cache on the success path (after the handler returns, before final `_set_state(DONE)`):

```python
if isinstance(idempotency_key, str) and idempotency_key and terminal_state == DONE:
    IDEMPOTENCY_CACHE[idempotency_key] = dict(result)  # copy to prevent later mutation
```

## Module-level state

Add at the top of `bus_worker.py`:

```python
# DeepSeek E3 — idempotency dedup. Process-lifetime cache; worker restart
# drops it. A re-issued task within the same process returns the cached
# result without re-running the handler.
IDEMPOTENCY_CACHE: dict[str, dict] = {}
```

Expose a `clear_idempotency_cache()` for tests.

## Deliverables

### 1. `tools/bus_worker.py`

- `IDEMPOTENCY_CACHE: dict[str, dict]` module-level (with comment).
- `clear_idempotency_cache()` test helper.
- Cache lookup before claim CAS in `process_issue`; replay path on hit.
- Cache write after successful handler run (terminal_state == DONE only — failed runs are NOT cached, so the user can retry by re-issuing).

### 2. `tests/test_bus_worker.py`

Add ≥3 tests. Setup adds `bus_worker.clear_idempotency_cache()` to setUp's addCleanup to isolate tests.

1. `test_idempotency_cache_hit_replays_without_running_handler` — first issue with key="K1" runs handler, populates cache. Second issue with key="K1" but different task_id: handler NOT called, but issue still transitions to done and posts the cached result + a `replay_of_idempotency_key` marker.
2. `test_idempotency_cache_miss_runs_handler` — first time seeing key="K2", handler runs normally.
3. `test_idempotency_cache_does_not_collide_across_keys` — different keys → different cache slots; running task K1 doesn't return K2's cached result.

Optional 4th: `test_failed_handler_does_not_populate_cache` — handler raises → cache NOT updated for that key → next time the key is seen, handler runs again.

## Acceptance criteria

- `py -3.14 -m unittest tests.test_bus_worker -v` — all ≥36 tests pass (33 existing + 3 new).
- `py -3.14 -m py_compile tools/bus_worker.py tests/test_bus_worker.py` exits 0.
- Full repo suite stays green.
- Stdlib only.
- Cache is module-level dict — no threading.Lock, no persistence.

## Out of scope

- Persisting cache to disk (E3 explicitly skipped this).
- Hydrating cache from `tracker/bus-events.jsonl` at worker start (would need to also read prior `neon-result:v1` comments from Gitea — adds complexity, out of MVP).
- Cross-worker cache sharing.
- Cache size eviction (LRU/TTL).

## Style / project conventions

- Match shape of existing `bus_worker.py`.
- `from __future__ import annotations`.
- No `Co-Authored-By:`.
- Logging: existing `log(...)` helper.

## Self-check before "done"

- Tests pass on host.
- `py_compile` clean.
- Cache hit-path does NOT call the handler (verified via `Mock.assert_not_called()`).
- Cache hit-path DOES post a result envelope + transition issue to done.
- Failed handler runs do NOT populate cache.
- `clear_idempotency_cache()` called in test setUp/cleanup.

## Final report

Conform to schema. Note explicitly whether the 4th optional test was added.
