# Task: Phase 1.5 follow-up #65 — test quality (D1 + D2)

Name: codex-developer
Profile: Codex CLI 0.128+ (gpt-5.5, high reasoning, --sandbox workspace-write)
Goal: Close DeepSeek D1 + D2 test-quality findings from the Phase 1.5 audit: replace the over-fitted `test_wait_or_stop_returns_early_on_stop` with a behaviour-only assertion, and add a worker-vs-reaper dynamic-state race test.
Constraints: test-only changes, no production code modifications, all existing tests must still pass.
Watches: Gitea issue [#65](http://localhost:3000/androman/neon-legion/issues/65), DeepSeek Phase 1.5 audit findings D1 and D2 (`tests/test_bus_worker.py` lines around `test_wait_or_stop_returns_early_on_stop`, and the lease-finalise flow in `tools/bus_worker.py`).
Produces: 1 modified file (`tests/test_bus_worker.py`).

## Operational backstory

Test improvements for the Phase 1.5 bus worker. The DeepSeek audit on
the original Phase 1.5 milestone (May 13) flagged two test-quality issues:

- **D1** (over-fitted): `test_wait_or_stop_returns_early_on_stop` asserts
  `sleep.assert_called_once_with(1.0)`. The 1.0 is the internal step size
  of `_wait_or_stop`. If a future refactor changes the step to 0.5s, the
  test fails despite identical observable behaviour ("returns early when
  stop is set").
- **D2** (dynamic-state race untested): no test simulates Gitea state
  changing across successive calls within `process_issue`. E.g. issue is
  `claimed` at the lease-check `get_issue`, then reaper sets `expired`
  before `_set_state(DONE)`. The B2 zombie guard from PR #73 mitigates
  this but has zero test coverage for the actual race window.

This task is test-only — do NOT modify `tools/bus_worker.py`. The
zombie-guard code is already in place; we just need a test that exercises
the dynamic state path.

## Working directory

`<project-dir>` (already your `--cd`).

## Project context

Read in order:
1. `tests/test_bus_worker.py` — current 29 tests, find `test_wait_or_stop_returns_early_on_stop` and the zombie-guard tests (`test_finalise_skipped_when_issue_expired_during_run`, etc.).
2. `tools/bus_worker.py` — `_wait_or_stop` definition (1s step size), `_verify_lease_held` (the get_issue + list_comments verify), and the finalise block (lines ~190-208).

## Deliverable D1 — replace over-fitted wait_or_stop test

Current shape (line ~210):

```python
def test_wait_or_stop_returns_early_on_stop(self):
    with patch.object(bus_worker._STOP, "is_set", side_effect=[False, True]), patch("tools.bus_worker.time.sleep") as sleep:
        started = time.monotonic()
        bus_worker._wait_or_stop(30)
        elapsed = time.monotonic() - started

    self.assertLessEqual(elapsed, 1.0)
    sleep.assert_called_once_with(1.0)
```

The `sleep.assert_called_once_with(1.0)` ties the test to the implementation
detail. The behavioural contract is:

- when `_STOP` is set before the interval elapses, the function returns
  in well under `interval` real time;
- it never blocks longer than necessary (≤ one step size, which is internal
  and may change).

Replace the assertion with:

```python
self.assertLessEqual(elapsed, 1.5)  # generous bound, behaviour-only
# Optional: assert sleep was called AT LEAST once (proves we entered the loop)
self.assertGreaterEqual(sleep.call_count, 1)
```

Drop the exact-arg assertion. Keep the elapsed-time bound generous enough
that step-size refactors (0.5s, 2s, etc.) don't break the test.

## Deliverable D2 — worker-vs-reaper dynamic state race test

Add a new test that simulates: worker A completes its handler, then between
`_verify_lease_held`'s `get_issue` returning `claimed` and `_set_state(DONE)`,
the reaper expires the issue. The worker's final `_set_state(DONE)` PATCH
fails with `BusGiteaError(409, "label conflict")` or similar.

The existing C1 fix wraps the final `_set_state` in a try/except and logs
"orphaned" on failure — the test should verify:

- `_post_result` IS called (result was posted before the state transition).
- `_set_state(..., DONE)` IS attempted but fails.
- A log entry mentions "orphaned".
- The exception is NOT propagated out of `process_issue`.

Implementation hint — use a side_effect list on `bus_gitea.update_issue` so
that the early PATCHes succeed (pending→claimed, claimed→in-progress) but
the final PATCH (in-progress→done) raises:

```python
from tools.bus_gitea import BusGiteaError

def flaky_update(number, *, labels=None, state=None):
    if labels and bus_worker.DONE in labels:
        raise BusGiteaError(409, "label conflict — reaper expired")
    return original_fake_update(number, labels=labels, state=state)
```

Name the test something like `test_finalise_set_state_failure_logs_orphan_and_does_not_raise`.

A second optional D2 test if you have time: `test_lease_check_passes_but_set_state_fails_due_to_dynamic_expiry` — exercises the SAME flaky_update pattern but explicitly checks the lease-check path proceeded (i.e. `_verify_lease_held` returned True).

## Acceptance criteria

- `py -3.14 -m unittest tests.test_bus_worker -v` — all ≥30 tests pass (29 existing + 1 new D2 test, possibly +1 second D2 test = 31).
- `py -3.14 -m py_compile tests/test_bus_worker.py` exits 0.
- `tools/bus_worker.py` is UNCHANGED — verify via `git diff tools/bus_worker.py` returning empty.
- Full repo suite stays green.

## Out of scope

- Production code changes.
- New zombie-guard logic (already in PR #74).
- Pre-aggregated readmodel tables (separate issue #69).

## Style / project conventions

- Match shape of existing `tests/test_bus_worker.py`.
- `from __future__ import annotations` if you import type-only.
- No `Co-Authored-By:`.
- Logging assertions: `patch("tools.bus_worker.log") as log` then `any("orphaned" in call.args[0] for call in log.call_args_list)`.

## Self-check before "done"

- Tests pass on host.
- `py_compile` clean.
- `git diff tools/bus_worker.py` is empty.
- D1 test no longer asserts `sleep.assert_called_once_with(1.0)`.
- D2 test simulates dynamic Gitea state via side_effect list, not a static fixture.

## Final report

Conform to schema. State explicitly whether 1 or 2 D2 tests were added.
