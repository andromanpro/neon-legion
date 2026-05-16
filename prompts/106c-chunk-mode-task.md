# Task: #106-C C1 — calendar-day chunk-mode for productivity (behind a flag, default off)

Name: codex-developer
Profile: Codex CLI 0.128+ (gpt-5.5, xhigh reasoning, --sandbox workspace-write)
Goal: Stop treating "1 Claude session = 1 task" for the productivity numerator. Add a calendar-day chunk-mode to the aggregation path, gated by `PRODUCTIVITY_UNIT` (default `"session"` → byte-identical to today). Chunk key = `f"{session_id}:{YYYY-MM-DD}"`. The #106-A guard applies per chunk. When a chunk has no chunk-keyed baseline, fall back to the session-level entry as a single chunk (so flag-on with an un-backfilled tasks.json == flag-off). Also deliver a deterministic re-bucketing **structure** + an architect-run per-day re-estimation script skeleton. NO live-hook estimator change, NO LLM calls in sandbox, NO deploy.
Constraints: stdlib-only in tracker/ & backend/; append-only (`correction_of` philosophy — never destructively rewrite session-keyed entries); atomic writes; public snapshot = counts/sums only, no raw session_ids; flag default `"session"` MUST reproduce current outputs exactly (the existing productivity tests are the regression pin and must pass UNCHANGED); no Co-Authored-By.
Watches: Gitea #106 (+ #106-C design docs/106c-task-chunking-design.md); files tracker/summary.py, backend/server.py, tools/, tests/
Produces: tracker/summary.py, backend/server.py, tools/rechunk_estimate.py (script skeleton), tests/test_chunk_mode.py

## Operational backstory

workspace-write sandbox in `F:/WorkAI/neon-legion` (ASCII). Python stdlib only, git, ChatGPT-auth. PowerShell may be policy-blocked — do NOT loop; **architect runs pytest + the backfill (LLM) + before/after on host**. You write deterministic code + tests and self-verify by reading. The per-day re-estimation makes many oracle calls — that is an architect/host step, NOT sandbox. MCP stderr noise is harmless.

## Read first (exact regions)

- `docs/106c-task-chunking-design.md` — the approved design. C1 only. Locked: chunk key `session_id:YYYY-MM-DD`; guard per-chunk; governance = #106-A-style human gate; history = live-transcript per-day re-est + pruned single-chunk; flag default off.
- `tracker/summary.py` — `parse_event_ts`, `_active_time_hours_for_timestamps` (:536), `effective_session_hours` (#106-A helper), `summarize_productivity` (:572 — the per-session loop with floor/ceiling/p95 diagnostics), `active_time_hours_merged` (#107 denominator), `read_tasks`, `events_for_task_metrics`.
- `backend/server.py` — `_today_payload` (its own duplicated per-session loop), `_productivity_block`, `_today_productivity_block`, `_productivity_periods`, `productivity_payload`.
- `tests/test_productivity_multiplier.py`, `tests/test_productivity_sanity_cap.py`, `tests/test_productivity_ceiling_guard.py` — the regression pins. They must pass unchanged with the flag defaulting to `"session"`.

## Background (decided — do not re-litigate)

#106-B proved prompt wording can't fix the estimator (inherent ±22h LLM variance). The ~80% lever is structural: a multi-day session gets one weak `ai_baseline_hours`. C1 = bucket a session's events by calendar day; numerator aggregates per `(session_id, date)`. Denominator (#107 global merged timeline) is unit-independent and UNCHANGED. Sentiment/profanity stay session-keyed and are OUT OF SCOPE — chunking touches only the productivity baseline path.

## Deliverables (exact)

### 1. `tracker/summary.py` — flag + chunk-mode in `summarize_productivity`

- Module-level: `PRODUCTIVITY_UNIT = os.environ.get("PRODUCTIVITY_UNIT", "session")` (accept only `"session"`/`"chunk"`; anything else → `"session"`). Add `import os` if absent.
- Helper `chunk_date(ts: datetime) -> str`: the calendar date of an already-`parse_event_ts`-parsed timestamp, `.date().isoformat()`. Use the SAME tz semantics as the existing code path — do not introduce tz conversion the rest of summary.py doesn't already do.
- Refactor `summarize_productivity` so the covered-unit loop is unit-parametrised:
  - `"session"` (default): **exactly today's behavior** — group by `session_id`, same floor/ceiling via `effective_session_hours`, same return keys/values. Byte-identical.
  - `"chunk"`: group covered events by `(session_id, chunk_date)`. For each chunk: `baseline = effective_task_hours(tasks.get(f"{sid}:{date}"))`; **if that key is absent → fall back to `effective_task_hours(tasks.get(sid))` and treat the whole session as ONE chunk for that session** (process the session once, not per day, when only a session-level entry exists). Per-chunk active hours via `_active_time_hours_for_timestamps` on that chunk's timestamps; apply `effective_session_hours(baseline, chunk_active, chunk_event_count)` **per chunk** (guard per-chunk). Accumulate the same `baseline_floor_clamped`/`hours_floor_added`/`baseline_ceiling_clamped`/`hours_ceiling_removed`/`baseline_per_event_p95` diagnostics over chunks.
  - Return dict: keep all existing keys (in chunk-mode `sessions_covered`/`sessions_total` = covered/total **chunk** counts) and ADD `"unit": PRODUCTIVITY_UNIT`.
- Invariant: with the fallback, `"chunk"` on a tasks.json that has zero `sid:date` keys MUST produce the same numerator/denominator/diagnostics as `"session"` (each session = one chunk). Add a test asserting this.

### 2. `backend/server.py` — same flag in `_today_payload`

Mirror the identical chunk grouping + per-chunk guard + fallback in `_today_payload`'s duplicated loop. Thread `"unit"` into `productivity_payload`, `_productivity_block`, `_today_productivity_block`, every `_productivity_periods` block. No semantic change when unit=`"session"`.

### 3. `tools/rechunk_estimate.py` — architect-run skeleton (deterministic parts only)

Stdlib. CLI `--dry-run` (default) / `--write`, `--samples N` (default 3). For each covered session that has a **live transcript**: split its transcript events by `chunk_date`; per day-chunk build the (#106-B) size-aware prompt via `tracker/estimate-task.py` importables; **the actual oracle calls + multi-sample median are invoked here but only when `--write` AND only by the architect on host** (in `--dry-run` just print the chunk plan: sid, date, event_count, would-call). Append-only: write `f"{sid}:{date}"` entries via the existing atomic-write/lock path; never delete/modify the session-level entry. Pruned-transcript sessions: skip (they stay single-chunk via the fallback). Print a summary table. This script is NOT run in sandbox.

### 4. `tests/test_chunk_mode.py` (pytest)

- `chunk_date` correctness incl. a session whose events straddle midnight → 2 chunks.
- Flag default `"session"` → `summarize_productivity` identical to pre-change (construct events+tasks, assert full dict equality vs hard-coded expected — the regression pin).
- `"chunk"` with NO `sid:date` keys → identical aggregate to `"session"` (fallback correctness).
- `"chunk"` WITH `sid:date` keys: a 2-day marathon session, baseline per day → numerator = sum of per-day effective hours; guard fires per chunk (a stub day clamps, a real day doesn't) and is NOT double-counted.
- `_today_payload` chunk-mode threads `unit` + per-chunk diagnostics.
- Existing productivity tests still pass unchanged (do not edit them).

## Acceptance criteria

- [ ] `PRODUCTIVITY_UNIT` default `"session"` → all existing productivity/guard tests pass UNCHANGED; outputs byte-identical to pre-change.
- [ ] `"chunk"` with un-backfilled tasks.json == `"session"` (fallback: session = 1 chunk).
- [ ] `"chunk"` with `sid:date` entries aggregates per calendar-day chunk; #106-A guard applied per chunk, mutually-exclusive floor/ceiling per chunk, no double-count.
- [ ] Denominator (`active_time_hours_merged`) and sentiment/profanity paths UNCHANGED.
- [ ] `"unit"` surfaced in snapshot productivity + every period block.
- [ ] `tools/rechunk_estimate.py` is append-only, `--dry-run` default, makes NO oracle calls in dry-run, never rewrites session-keyed entries.
- [ ] stdlib-only; atomic writes; no raw session_ids in snapshot; no Co-Authored-By; no deploy.

## Test plan

Codex statically self-verifies. **Architect on host:** `pytest tests/ -q` (expect prior 351 + new, 0 regressions, flag-off pin green); then `rechunk_estimate.py --dry-run` review; then (host, LLM) `--write --samples 3` on live-transcript sessions; then deterministic before/after = `summarize_productivity` with `PRODUCTIVITY_UNIT=session` vs `=chunk` on the populated tasks.json (pure recompute, no LLM); DeepSeek ratify; human gate; merge; flip flag; deploy.

## Out of scope (do NOT do here)

- Changing the live `estimate-task.py` Stop-hook to write per-day (deferred follow-up; noted in design doc).
- Any oracle/LLM call inside the sandbox; any deploy; any flag flip (architect, post-gate).
- Sentiment/profanity re-keying; #106-A constant retuning; #107 denominator; C2/C3 (explicit task-id).

## Final report

Conform to `--output-schema`: `files_created` (path/purpose/loc), `summary`, `tested`=false, `test_results` ("not executed in sandbox; tests written"), `open_questions`, `deviations_from_spec`.
