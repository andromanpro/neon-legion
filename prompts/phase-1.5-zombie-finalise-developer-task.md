# Task: Phase 1.5 follow-up #63 — zombie completion guard

Name: codex-developer
Profile: Codex CLI 0.128+ (gpt-5.5, high reasoning, --sandbox workspace-write)
Goal: Close DeepSeek B2 finding — if a worker loses the network mid-handler and the reaper expires its claim, the worker should NOT finalise to `done`/`failed` over the `expired` state when its network comes back. Add a pre-finalise re-fetch + lease-ownership check.
Constraints: stdlib only, no real network in tests, lose-lease path is non-destructive (no state revert, no result posted).
Watches: Gitea issue [#63](http://localhost:3000/androman/neon-legion/issues/63), `tools/bus_worker.py` (`process_issue` finalise block), `tools/bus_gitea.py` (`get_issue`, `list_comments`), `tools/bus_reaper.py` (sets `expired` state on stale leases).
Produces: 1 modified file (`tools/bus_worker.py` — pre-finalise lease check), 1 modified file (`tests/test_bus_worker.py` — race tests for lost-lease finalise).

## Operational backstory

Phase 1.5 #62 (claim CAS, PR #71→#72) merged. The CAS prevents concurrent claims at start. But mid-run, if a worker's network drops and stays down longer than `lease_seconds * 1.5`, the reaper flips the issue from `claimed`/`in-progress` to `expired` (and closes it). When the worker's network recovers and it tries to PATCH `done`, two things can happen:

1. The reaper-set `expired` label gets overwritten by `done`, the issue gets re-opened then re-closed by Gitea's state-machine, the issue now reads as a successful run that actually expired mid-flight. **Zombie completion.**
2. If a fresh worker re-claimed the expired issue, the zombie's result envelope posts on top of the new worker's work. **Double-post.**

DeepSeek B2 from the Phase 1.5 audit (May 13). Severity MED.

## Working directory

`F:/WorkAI/multi-agent` (already your `--cd`).

## Project context

Read in order:
1. `AGENTS.md`
2. `tools/bus_worker.py` — focus on `process_issue` lines 95-180 (claim flow + finalise block), `_verify_claim_won` helper, the existing `_post_result` + `_set_state` pattern at the end of `process_issue`.
3. `tools/bus_gitea.py` — `get_issue`, `list_comments` signatures.
4. `tools/bus_reaper.py` — what the reaper does on expiry (sets `expired` label, posts `neon-expired:v1` comment, closes the issue).

## Logic — pre-finalise lease check

Just before the final `_post_result` + `_set_state` block (current line ~170-180 of `bus_worker.py`), insert:

```python
if not _verify_lease_held(number, exec_id):
    log(
        f"#{number} lease lost during handler run (reaper expired or "
        f"another worker re-claimed); skipping finalise. Result was: "
        f"{result['status']} reason={result.get('reason', '<none>')}",
        level="error",
    )
    return  # do NOT post result, do NOT transition state
```

Where `_verify_lease_held` checks:

```python
def _verify_lease_held(issue_number: int, my_exec_id: str) -> bool:
    """True if we still own the lease — state is still claimed/in-progress
    AND the lowest-id neon-claim:v1 comment carries our exec_id.

    Conservative: on any bus_gitea error, return False (treat as lost).
    """
    try:
        issue = bus_gitea.get_issue(issue_number)
    except BusGiteaError:
        return False
    labels = {l["name"] for l in (issue.get("labels") or []) if isinstance(l, dict)}
    if CLAIMED not in labels and IN_PROGRESS not in labels:
        return False

    try:
        comments = bus_gitea.list_comments(issue_number)
    except BusGiteaError:
        return False
    lowest_id = None
    lowest_exec = None
    for c in comments:
        match = CLAIM_RE.search(c.get("body") or "")
        if not match:
            continue
        cid = c.get("id")
        if cid is None:
            continue
        if lowest_id is None or cid < lowest_id:
            lowest_id = cid
            lowest_exec = match.group("exec")
    return lowest_exec == my_exec_id
```

Note: lease-held check uses the SAME "lowest-id is the canonical claimer" rule as `_verify_claim_won`. The two helpers share intent; factor the common loop into a private `_lowest_claim_exec(comments) -> str | None` if you like, or keep two copies — your call. Each is ~10 lines.

## Deliverables

### 1. `tools/bus_worker.py`

- New `_verify_lease_held(issue_number, my_exec_id)` helper.
- New branch in `process_issue` before the existing `_post_result` + `_set_state` block: if `not _verify_lease_held(...)`, log and return.

### 2. `tests/test_bus_worker.py`

Add ≥4 new tests. Use `unittest.mock.patch` on `bus_gitea.get_issue` and `bus_gitea.list_comments`. Coverage:

1. `test_finalise_skipped_when_issue_expired_during_run` — happy path through handler, then `get_issue` returns issue with `expired` label → no `_post_result` call, no `_set_state` call, log mentions "lease lost".
2. `test_finalise_skipped_when_another_worker_reclaimed` — issue label still `claimed`, but `list_comments` shows a different worker's claim with the lowest ID → finalise skipped, log mentions "lease lost".
3. `test_finalise_proceeds_when_lease_still_held` — issue label `claimed` or `in-progress`, `list_comments` shows our claim as lowest → finalise proceeds, result posted, state transitions to `done`.
4. `test_finalise_skipped_on_get_issue_error` — `get_issue` raises `BusGiteaError(500, ...)` → conservative skip, log mentions "lease lost".

For tests #1–#3, use `bus_gitea.comment` mocked with explicit `id` so `my_comment_id` matches the `list_comments` fixture (same pattern as #62 fix tests).

Existing 24 worker tests must continue to pass — they already use `fake_list_comments` returning the worker's claim comment, so `_verify_lease_held` will see the worker as the lowest-id claimant and the existing finalise paths will succeed. You may need to update `setUp.patchers` to also mock `bus_gitea.get_issue` returning `{"labels": [{"name": CLAIMED}, ...]}` so lease-held verification succeeds by default.

## Acceptance criteria

- `py -3.14 -m unittest tests.test_bus_worker -v` — all ≥28 tests pass (24 existing + 4 new).
- `py -3.14 -m py_compile tools/bus_worker.py tests/test_bus_worker.py` exits 0.
- Full repo suite stays green.
- Stdlib only.

## Out of scope

- Reaping the orphan result-less issue from the worker side (still the reaper's job — lease expiry will run again next cycle).
- Notification of "lost lease" to operator (just log).
- Resurrecting the orphan into pending state.

## Style / project conventions

- Match shape of `_verify_claim_won` (same module, same private-function style).
- `from __future__ import annotations`.
- No `Co-Authored-By:`.
- Logging: `print(f"[bus-worker] ...", file=sys.stderr)`.

## Self-check before "done"

- Tests pass on host.
- `py_compile` clean.
- Lose-lease path does NOT post result, does NOT transition state.
- Test mocks for `get_issue` return labels as a list of dicts (Gitea's actual shape: `[{"name": "neon:state/claimed"}, ...]`), not bare strings.

## Final report

Conform to schema. If you factor the common claim-iteration logic between `_verify_claim_won` and `_verify_lease_held` into a shared helper, mention it under `deviations_from_spec`.
