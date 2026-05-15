# Task: Phase 1.5 follow-up #62 — claim race CAS via claim-comment

Name: codex-developer
Profile: Codex CLI 0.128+ (gpt-5.5, high reasoning, --sandbox workspace-write)
Goal: Close the HIGH B1 finding from the DeepSeek audit — two `bus_worker.py` instances with the same `neon:target/<host>` label can currently both claim the same issue because Gitea has no CAS on label PATCH. Add a poor-man's CAS using monotonic comment IDs: after the label swap, post a `neon-claim:v1` comment, re-fetch comments, and verify ours is the most recent claim. If not, release.
Constraints: stdlib only, idempotent on retry, no real network in tests, race-correct under simulated concurrent claim.
Watches: Gitea issue [#62](http://localhost:3000/androman/neon-legion/issues/62), `tools/bus_worker.py` (`process_issue` claim flow), `tools/bus_gitea.py` (`list_comments`, `comment`, `update_issue`), `docs/phase-1.5-git-bus.md` (Trust model section — single-instance constraint to be relaxed by this PR).
Produces: 1 modified file (`tools/bus_worker.py` — claim flow), 1 modified file (`tests/test_bus_worker.py` — race-correctness tests), 1 modified file (`docs/phase-1.5-git-bus.md` — Trust model #3 update to reflect CAS landing).

## Operational backstory

Phase 1.5 v0.5 milestone closed. The audit pass landed A1/C1/E2/A3 (PR #61). One HIGH finding remains open: claim race. Currently, two workers both `PATCH labels=[..., claimed]` near-simultaneously, both Gitea responses show `claimed`, both proceed to run the handler.

The proposed mitigation uses **monotonic comment IDs as a tie-breaker**:

1. Worker A PATCHes `pending → claimed`.
2. Worker A POSTs `<!-- neon-claim:v1 host=A exec=X1 claimed_at=T1 lease_seconds=N -->`. Gitea returns the new comment's `id` (a monotonically-increasing integer).
3. Worker A re-fetches comments (`bus_gitea.list_comments(N)`), finds the highest-`id` comment matching the `neon-claim:v1` regex.
4. If the highest-ID claim-comment's `exec` field == Worker A's exec_id → A won, proceed to `in-progress`.
5. Else → another worker's comment landed after A's → A lost the race. Release: do **not** revert the label (B2 zombie-completion territory — let the reaper handle the orphan `claimed` state) and log "claim lost to <other_exec>".

The window between step 2 (POST claim) and step 3 (re-fetch + verify) is the only race window. If two workers POST simultaneously, the one whose comment got the higher ID is the canonical winner. Gitea comment IDs are sequential on the issue, so this gives a clean tie-breaker even under near-simultaneous POSTs.

Tests run on host. Mock `bus_gitea.*` to simulate two workers racing on the same issue.

## Working directory

`<project-dir>` (already your `--cd`).

## Project context

Read in order:
1. `AGENTS.md`
2. `tools/bus_worker.py` — focus on `process_issue` (lines 86-149) and the existing `_claim_comment` helper
3. `tools/bus_gitea.py` — `list_comments`, `comment`, `update_issue` signatures
4. `tests/test_bus_worker.py` — existing test fixtures and mocking patterns
5. `docs/phase-1.5-git-bus.md` — Trust model section (point #3 mentions the current single-instance constraint)

## Logic — new claim flow

Replace the current claim block (lines 86-115 of bus_worker.py — `process_issue` from envelope parse through claim-comment POST) with this:

```python
def process_issue(issue: dict, host: str) -> None:
    number = issue["number"]
    envelope = bus_envelope.parse(issue.get("body") or "")
    if envelope is None:
        log(f"#{number} has no valid neon-task envelope; skipping")
        return

    labels = _label_names(issue)
    if PENDING not in labels:
        log(f"#{number} is no longer pending; skipping")
        return

    exec_id = _new_exec_id(host)
    lease_seconds = int(envelope["lease_seconds"])

    # Step 1: optimistic label swap
    try:
        claimed = _set_state(number, labels, CLAIMED)
    except BusGiteaError as exc:
        log(f"#{number} claim PATCH failed: {exc}; skipping", level="error")
        return
    claimed_labels = _label_names(claimed)
    if CLAIMED not in claimed_labels:
        log(f"#{number} claim PATCH did not stick; skipping")
        return

    # Step 2: post claim-comment (carries our exec_id)
    try:
        my_claim_comment = bus_gitea.comment(number, _claim_comment(host, exec_id, lease_seconds))
    except BusGiteaError as exc:
        log(f"#{number} claim comment POST failed: {exc}; skipping (lease will expire)", level="error")
        return

    # Step 3: CAS — verify our claim is the canonical winner
    if not _verify_claim_won(number, exec_id, my_claim_comment.get("id")):
        log(f"#{number} lost claim race to a concurrent worker; releasing")
        # do not revert label; reaper will expire the orphan claimed state
        return

    # Step 4 onwards: same as before (in-progress, handler, finalise)
    current_labels = claimed_labels
    ...
```

Add a helper:

```python
def _verify_claim_won(issue_number: int, my_exec_id: str, my_comment_id: int | None) -> bool:
    """Re-fetch comments and confirm OUR claim-comment has the highest ID
    among all neon-claim:v1 comments on this issue. Monotonic comment IDs
    serve as the tie-breaker under simultaneous claims."""
    try:
        comments = bus_gitea.list_comments(issue_number)
    except BusGiteaError:
        return False  # conservative: if we can't verify, assume we lost
    latest_id = -1
    latest_exec = None
    for c in comments:
        body = c.get("body") or ""
        match = CLAIM_RE.search(body)  # add this regex at module level
        if not match:
            continue
        cid = c.get("id") or 0
        if cid > latest_id:
            latest_id = cid
            latest_exec = match.group("exec")
    return latest_exec == my_exec_id
```

Where `CLAIM_RE`:
```python
CLAIM_RE = re.compile(r"<!--\s*neon-claim:v1\s+host=\S+\s+exec=(?P<exec>\S+)\s+claimed_at=\S+\s+lease_seconds=\d+\s*-->")
```

(Same regex shape as `bus_reaper.py` — keep the two in sync. Optionally factor out to a shared module if you want — but a 1-line duplicate is fine for MVP.)

## Tests — race correctness

Extend `tests/test_bus_worker.py` with ≥4 new tests:

1. `test_claim_win_when_no_concurrent_claim` — single claim-comment in `list_comments` response with my exec_id, highest ID. Verify worker proceeds to in-progress.

2. `test_claim_lost_when_other_worker_comments_after` — `list_comments` returns two `neon-claim:v1` comments: mine with id=100, other with id=101 (newer). Verify worker logs "lost claim race", does NOT post in-progress label, does NOT call handler.

3. `test_claim_won_when_other_worker_comments_before` — two comments: other with id=99, mine with id=100. Verify worker proceeds.

4. `test_claim_verify_handles_list_comments_failure` — `list_comments` raises `BusGiteaError`. Verify worker treats as "lost" (conservative), does NOT run handler.

5. `test_claim_lost_does_not_revert_label` — confirm the worker does NOT call `update_issue` to revert from `claimed` back to `pending`. The orphan claimed state is the reaper's job (per B2 zombie-completion semantics, lease expiry handles it).

All existing 17 tests must still pass. The existing `test_process_issue_happy_path` will need a tweak: it currently doesn't simulate `list_comments` returning the worker's claim comment with the highest ID — extend the mock to include the claim comment with the worker's exec_id.

## docs/phase-1.5-git-bus.md update

In the Trust model section (`## Trust model`), replace point #3:

```markdown
3. **CAS via claim-comment monotonic ID** (was: "One worker per
   `neon:target/<host>` label"). After the label PATCH, the worker posts
   a `neon-claim:v1` comment, re-fetches comments, and verifies the
   highest-ID claim comment is its own `exec_id`. Gitea assigns
   monotonic IDs to issue comments, so even simultaneous claim-POSTs
   resolve to exactly one canonical winner. Lost claims do not revert
   the label; the reaper's lease-expiry path picks up orphan `claimed`
   state.
```

## Acceptance criteria

- `py -3.14 -m unittest tests.test_bus_worker -v` — all ≥21 tests pass (17 existing + 4 new claim-race tests).
- `py -3.14 -m py_compile tools/bus_worker.py tests/test_bus_worker.py` exits 0.
- Existing happy-path test still works after the claim-comment mock extension.
- Full repo suite stays green.

## Out of scope

- Webhook accelerator (still polling, 30s default).
- Cross-issue ordering or fairness (FIFO not promised).
- Reverting the label on lost claim (reaper handles orphan state — see B2 #63).
- Multi-target dispatch (one worker can still only claim one issue at a time).

## Style / project conventions

- Match shape of existing `bus_worker.py`.
- `from __future__ import annotations`.
- No `Co-Authored-By:`.
- Logging: `print(f"[bus-worker] ...", file=sys.stderr)` — same as existing.

## Self-check before "done"

- Tests pass on host.
- `py_compile` clean.
- The new flow logs distinct messages for "lost claim race" vs "list_comments failed" — operators can grep them apart.
- Doc updated in Trust model section #3.
- No real network calls in tests.

## Final report

Conform to schema. Mention the new comment IDs assumption explicitly: **Gitea assigns monotonic integer IDs to issue comments**. If you verify this against the Gitea source / API docs and find it's NOT guaranteed monotonic, flag in `open_questions` and we will redesign.
