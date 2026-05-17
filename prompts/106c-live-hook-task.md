# Task: #106-C live-hook — calendar-day chunk estimation (hybrid: frozen past days, live today)

Name: codex-developer
Profile: Codex CLI 0.128+ (gpt-5.5, xhigh reasoning, --sandbox workspace-write)
Goal: Make the live estimation hook (`tracker/estimate-task.py::estimate_session`, called on Stop/SessionStart) write per-calendar-day chunk entries so NEW sessions are counted per-day like the #106-C C1 backfill — otherwise every new whole-session entry slowly re-introduces the marathon-undercount C1 just fixed. Hybrid: a session's PAST days are estimated once and frozen (immutable, append-only spirit, skip-if-already-estimated); the session's LATEST day is "live" — re-estimated every hook fire to reflect its actual current state, freezing automatically once a newer day appears. The existing session-level entry (profanity/sentiment + a whole-session fallback baseline) is preserved unchanged.
Constraints: stdlib-only; reuse `summary.chunk_date` as the SINGLE source of the date-key format (producer/consumer MUST agree); oracle output schema + `normalize_oracle_payload` + `run_oracle` + `failure_entry` UNCHANGED; do NOT touch `summary.py` / `backend/server.py` / the consumer / the `PRODUCTIVITY_UNIT` flag (all already shipped & deployed); sentiment/profanity stay session-keyed (out of scope for chunking); per-chunk failure must not abort the hook or other chunks; no Co-Authored-By; no deploy.
Watches: Gitea #106 (+ docs/106c-task-chunking-design.md, prompts/106c-chunk-mode-task.md, tools/rechunk_estimate.py for the established chunk-entry shape); files tracker/estimate-task.py, tests/
Produces: tracker/estimate-task.py, tests/test_live_chunk_estimation.py

## Operational backstory

workspace-write sandbox in `F:/WorkAI/neon-legion` (ASCII). Python stdlib only, git, ChatGPT-auth. PowerShell may be policy-blocked — do NOT loop; **the architect runs pytest on host**. You write code + tests and self-verify by reading. No real Codex/oracle calls in sandbox (unit tests mock `run_oracle`). MCP stderr noise harmless.

## Read first (exact)

- `tracker/estimate-task.py` — current `estimate_session` (the ONLY function to change), `update_task_entry` (works for ANY key incl. `sid:date`; lock+atomic+merge), `compute_session_metrics`, `build_estimation_prompt`, `run_oracle`, `read_transcript`, `read_tasks`, `parse_transcript_ts`, `transcript_role`, `_tool_call_count_for_event`, `failure_entry`, `main`.
- `tracker/summary.py` — `chunk_date(ts)` (returns `ts.date().isoformat()`) — IMPORT and reuse this exact function for the date key; do not re-implement (drift = mismatched keys = silent breakage).
- `tools/rechunk_estimate.py` — mirror its chunk-entry annotations (`source_session_id`, `chunk_date`, `chunk_event_count`, `estimation_mode`) and its per-day metric/prompt construction (`compute_chunk_metrics`-style) for consistency.
- `prompts/106c-chunk-mode-task.md` — the consumer’s fallback rules (all-chunks-present → split; partial/none → session-level fallback). This is WHY the session-level entry must keep a baseline.

## Background (decided — do not re-litigate)

#106-C C1 (consumer chunk-mode + top-15 backfill) is shipped & deployed (`PRODUCTIVITY_UNIT=chunk` in deploy-snapshot.sh). The live hook still writes ONE whole-session baseline → new sessions decay the fix. The oracle is an inherently noisy one-shot LLM (#106-B) — accepted; the live hook re-estimating the latest day across fires gives natural resampling, so live chunks use a SINGLE oracle sample (NOT median-of-3; that was for the one-shot historical backfill).

## Deliverable — rework `estimate_session(session_id, transcript_path)` only

Keep the existing opening EXACTLY: profanity-first `update_task_entry(session_id, {transcript_path, profanity_count})`, then whole-session `compute_session_metrics` + `build_estimation_prompt` + `run_oracle` + `update_task_entry(session_id, entry)` (this session-level entry stays as the consumer’s documented fallback + carries profanity/sentiment — unchanged shape).

THEN add per-day chunking:
1. Read the transcript once; bucket events by `summary.chunk_date(parse_transcript_ts(ev))` (skip events whose ts won’t parse). Reuse the existing transcript-reading helpers; do not double-read wastefully if avoidable.
2. `latest_date = max(day_keys)` — the in-flight "today" day, relative to the session’s own events (tz-consistent with the consumer; do NOT use wall-clock).
3. For each `date` with events:
   - Build per-day metrics (mirror `tools/rechunk_estimate.py::compute_chunk_metrics`: event/user/assistant/tool counts, span_hours, active_hours via the same gap rule) and the size-aware prompt (`build_estimation_prompt`).
   - **Past day** (`date < latest_date`): if `f"{session_id}:{date}"` already exists in `read_tasks()` AND has a numeric `ai_baseline_hours` → SKIP (frozen, immutable, idempotent, saves oracle calls). Else estimate once (single `run_oracle`) and `update_task_entry(f"{session_id}:{date}", chunk_entry)`.
   - **Latest day** (`date == latest_date`): ALWAYS (re)estimate (single `run_oracle`) and `update_task_entry(f"{session_id}:{date}", chunk_entry)` — overwrite to reflect current state. (When a newer day later appears this date becomes a past day and is then frozen by the rule above.)
   - `chunk_entry` = the normalized oracle payload + `transcript_path`, `source_session_id=session_id`, `chunk_date=date`, `chunk_event_count`, `estimation_mode="calendar-day-chunk-live"`. Do NOT put `profanity_count`/sentiment on chunk entries (session-level only).
4. Per-chunk robustness: wrap each day’s estimate in try/except; on failure, log to stderr (`chunk-estimate-failed\t{session_id}\t{date}\t{exc}`) and CONTINUE to the next day. A failed past day must remain retry-eligible (only skip a past day when it already has a numeric baseline — a failed/missing one retries on the next fire). Never raise out of the chunk loop.
5. `main()`’s existing whole-session failure path and `remove_inflight_lock` stay as the outer backstop — unchanged.

## Acceptance criteria

- [ ] Session-level entry behavior byte-identical to before (profanity-first write; whole-session baseline; `failure_entry` path) — existing estimate-task tests pass UNCHANGED.
- [ ] Multi-day session → one `session_id` entry + one `session_id:YYYY-MM-DD` entry per day; date key == `summary.chunk_date` (imported, not re-implemented).
- [ ] Past-day chunk with a numeric baseline is NOT re-estimated on a subsequent run (assert `run_oracle` not called for it / entry unchanged); latest-day chunk IS re-estimated/overwritten when events change.
- [ ] Single-day session → exactly one chunk (the latest day) + the session entry; no regression.
- [ ] One chunk’s oracle failure does not abort the hook or other chunks; failed past day retries next run; session-level path still completes.
- [ ] stdlib-only; oracle schema/normalize/run_oracle/failure_entry untouched; summary.py/server.py/consumer/flag untouched; no Co-Authored-By.
- [ ] New `tests/test_live_chunk_estimation.py` (mock `run_oracle`): multi-day split, past-day-frozen, latest-day-rewrite, single-day, per-chunk-failure-isolation, profanity-stays-session-level. Existing tests/ green.

## Test plan

Codex statically self-verifies. **Architect on host:** `pytest tests/ -q` (expect prior 356 + new; 0 prior tests changed — regression pin) + read the diff. No sandbox oracle calls. Then light DeepSeek pass (pipeline-correctness/idempotency) + human gate + merge. No deploy (consumer/flag already live; hook uses repo’s estimate-task.py after merge+pull).

## Out of scope

- Touching summary.py / server.py / the consumer / `PRODUCTIVITY_UNIT` / #106-A guard / #107.
- median-of-3 for live chunks (single sample by design); re-running history (rechunk_estimate.py already did top-15); the remaining 46 marathons; C2/C3 explicit task-id; sentiment/profanity re-keying; any deploy.

## Final report

Conform to `--output-schema`: `files_created` (path/purpose/loc), `summary`, `tested`=false, `test_results` ("not executed in sandbox; tests written"), `open_questions`, `deviations_from_spec`.
