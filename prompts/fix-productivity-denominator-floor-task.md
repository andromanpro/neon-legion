# Task: fix productivity multiplier — global-merged denominator (#107) + sanity-floor numerator (#106 core)

Name: codex-developer
Profile: Codex CLI 0.128+ (gpt-5.5, xhigh reasoning, --sandbox workspace-write)
Goal: Make the productivity multiplier definitionally sound — denominator on one global merged timeline (Gitea #107) and a sanity-floor so a session's manual-equivalent baseline can't be below the AI-active time it consumed (core of Gitea #106) — with diagnostics, tests, and threaded snapshot fields.
Constraints: stdlib-only in tracker/ and backend/; atomic writes; public snapshot must not leak raw session_ids (counts/aggregates only); no Co-Authored-By; do not change intent of existing tests (only extend for new keys).
Watches: Gitea issues #106 #107; files tracker/summary.py, backend/server.py, tests/, schemas/
Produces: tracker/summary.py, backend/server.py, tests/<new+updated>, schemas/<if a productivity schema exists>

## Operational backstory

workspace-write sandbox in `F:/WorkAI/neon-legion` (ASCII path). Python stdlib only, git, ChatGPT-auth. On Windows your shell commands may be wrapped in PowerShell and blocked by policy — do NOT loop trying to run pytest; **the architect runs the test suite on host**. You statically write code + tests and verify by reading. Network is blocked (no pip install). MCP stderr noise is harmless.

## Working directory

`F:/WorkAI/neon-legion` (your `--cd`).

## Project context (read first)

- `AGENTS.md` and `CLAUDE.md` — conventions (stdlib-only tracker/backend, append-only events, atomic writes, no Co-Authored-By).
- `CLAUDE.local.md` — metric definition: `productivity_multiplier = complexity_hours_without_ai / wall_clock_hours_with_ai`; unit of task = one Claude Code session.
- `tracker/summary.py` — read `active_time_hours`, `merged_interval_hours`, `summarize_productivity`, `effective_task_hours`, `events_for_task_metrics`, `read_tasks`.
- `backend/server.py` — read `productivity_payload`, the snapshot `productivity` assembly, and `_productivity_periods` (7d/30d/60d/all/today blocks).
- `tests/` — find existing productivity/summary tests; note the framework (pytest).

## Background (already investigated + 3-way reviewed; do not re-litigate)

`active_time_hours` sums active time per `session_id` then adds across sessions — no global-timeline merge — so concurrent Claude sessions double-count wall-clock (denominator inflated; #107, measured ×1.30 on a 7d window). `merged_interval_hours` (used for calendar_hours) already does the correct interval-merge — mirror that pattern. Separately, marathon sessions get one weak `ai_baseline_hours`, sometimes below their own AI-active time (impossible: a task can't have taken less manual time than the AI time it consumed) — #106; the deterministic guardrail is a per-session floor.

## Deliverables (exact changes)

### 1. #107 — global-merged active time (denominator)

In `tracker/summary.py`:
- Add `active_time_hours_merged(events, gap_minutes=2)`: pool **all** event timestamps across sessions into one sorted list; sum consecutive gaps `<= gap_minutes`. (Same gap rule as `active_time_hours`, but one global timeline. This mirrors how `merged_interval_hours` de-overlaps.)
- Keep existing `active_time_hours` (per-session sum) unchanged — it becomes the **diagnostic**.
- In `summarize_productivity`, set `active_hours_with_ai = active_time_hours_merged(covered_events, gap_minutes)` and add return key `active_hours_per_session_sum = active_time_hours(covered_events, gap_minutes)`.

### 2. #106 core — per-session sanity-floor (numerator)

In `summarize_productivity`, while iterating covered sessions:
- Compute each covered session's own per-session active hours `a_s` (per-session gap rule, single session timeline).
- With `b_s = effective_task_hours(entry)`: use `effective_b_s = max(b_s, a_s)` in the `hours_without_ai` sum.
- Aggregate and return: `baseline_floor_clamped` (int — count of covered sessions where `b_s < a_s`) and `hours_floor_added` (float — Σ(`effective_b_s - b_s`)). **No raw session_ids** in the return (public-snapshot privacy).

### 3. Thread new fields to snapshot

In `backend/server.py` `productivity_payload` and the snapshot/`_productivity_periods` assembly, surface `active_hours_per_session_sum`, `baseline_floor_clamped`, `hours_floor_added` in the top-level `productivity` block and in every `productivity_periods` entry (today/7d/30d/60d/all). Update any productivity JSON schema in `schemas/` and any fixtures/tests that assert the productivity shape.

## Acceptance criteria

- [ ] `active_hours_with_ai` (multiplier denominator) = global merged timeline; per-session-sum retained as `active_hours_per_session_sum` diagnostic.
- [ ] `hours_without_ai` uses per-session floor `max(baseline, session_active)`; `baseline_floor_clamped` + `hours_floor_added` returned and threaded to snapshot + all period blocks.
- [ ] No covered session can yield `effective baseline < its own active hours` after the change.
- [ ] New unit tests: (a) two sessions overlapping 1 real hour → merged active = 1h, per-session-sum diagnostic = 2h; (b) covered session baseline 1h / active 4h → effective baseline 4h, clamp counted, `hours_floor_added` = 3h; (c) non-overlap control unchanged.
- [ ] Existing summary/productivity/server tests updated for new keys; no intent change.
- [ ] stdlib-only; atomic writes preserved; no raw session_ids in snapshot; no Co-Authored-By.

## Test plan

Code statically written and self-verified by Codex (syntax, logic, read the diffs). **Architect runs `py -3.14 -m pytest tests/ -q` on host** + a before/after multiplier repro. Codex does NOT execute pytest in sandbox. In the final report list exactly which test files/cases you added or changed.

## Out of scope (leave in #106 — do NOT do here)

- Rewriting `tracker/estimate-task.py` / `tracker/oracle-prompt.txt` (estimator scaling/saturation).
- Re-running the oracle on historical sessions / rewriting `tracker/tasks.json`.
- Explicit task-id or per-day chunking of marathon sessions.
- `gap_minutes` retuning.
- Any deploy (`deploy-snapshot.sh`) — architect does it after review.

## Final report

Conform to `--output-schema`. Required: `files_created`, `summary`, `tested` (false — architect tests on host), `test_results` (state "not executed in sandbox; tests written"), `open_questions`, `deviations_from_spec`.
