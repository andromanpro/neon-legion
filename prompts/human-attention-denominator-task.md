# Task: human-attention denominator for the productivity multiplier

Role: developer. Implement Phase A in full; scope Phase B (don't half-build it).
Work on a new branch `feat/human-attention-denominator`. **Do NOT touch main,
do NOT run any deploy script, do NOT scp anything.** Run tests on host.

## Why (consensus of two prior reviews)

Current: `multiplier = hours_without_ai / active_hours_with_ai`, where
`active_hours_with_ai = active_time_hours_merged(covered_events, gap=2)` — the
merged wall-clock during which ANY AI session emitted events. That measures
**AI busy time**, not **human attention**. The user runs 3-5 agents in parallel
while doing other manual work; autonomous agent runtime inflates the denominator
→ multiplier reads low on parallel weeks. Fix: denominator = the user's own
attention time, derived from human-message timestamps. Autonomous stretches
between the user's prompts then cost ~0, crediting parallelism honestly.

## Phase A — human-attention denominator (implement fully)

### A1. Extract human-message timestamps from a transcript
Add to `tracker/summary.py` (or a small helper module it imports) a function that
reads a Claude transcript `.jsonl` and returns a sorted `list[datetime]` of
**user-message** timestamps. Schema: each line is JSON; user turns have
`type == "user"` (mirror how `estimate-task.py:read_transcript` /
`transcript_role` detect roles). Timestamp field is the per-line `timestamp`
(ISO-8601). Reuse existing ts parsing (`parse_event_ts` or equivalent). Ignore
lines without a parseable user timestamp.

### A2. human_attention_hours()
Add `HUMAN_ATTENTION_GAP_MINUTES = 5` (module constant). Add a function that,
given the covered sessions (the same `covered_session_ids` set used today),
pools user-message timestamps **across all covered sessions into one timeline**
and returns merged active hours via the existing
`_active_time_hours_for_timestamps(timestamps, HUMAN_ATTENTION_GAP_MINUTES)`.

Transcript path per session: `tasks.json[session_id]["transcript_path"]`
(already stored). Resolve it; if the file is missing on disk, **fall back** to
that session's current AI-event active time (so a rotated-transcript session is
not dropped — conservative: it keeps the old, higher cost). Count and report how
many sessions fell back.

### A3. Wire it as the headline denominator
- In `summarize_productivity` (both `unit == "session"` and the chunk branch),
  add `human_attention_hours_with_ai` to the returned dict alongside the existing
  `active_hours_with_ai`. **Rename nothing**; keep `active_hours_with_ai` as-is.
- In `backend/server.py` productivity payload (around line 356-378), compute the
  headline `multiplier` and `hours_saved` from **`human_attention_hours_with_ai`**
  when present and > 0, else fall back to today's `active_hours_with_ai`.
  Expose BOTH in the payload/snapshot:
  - `multiplier` / `hours_saved` → human-attention based (headline)
  - `ai_active_wall_clock_hours` → the current merged AI-active value (secondary,
    keep visible for diagnostics)
  - `human_attention_hours` → the new denominator value
- Preserve the existing `multiplier < 1 → 0` guard and period/chunk (#106-C)
  logic. The chunk-mode per-(session,day) aggregation must use human-attention
  timestamps too (pool user-msgs per chunk).

### A4. Tests
- Unit tests for the new functions in `tests/`: parallel-overlap case (two
  sessions whose AI events overlap but whose user-prompts are sparse → human
  attention << AI active), missing-transcript fallback, empty input.
- A **paired eval** script/output: compute OLD multiplier (AI-active denom) and
  NEW multiplier (human-attention denom) on the real `tracker/tasks.json` +
  events, print both for periods all/30d/7d/today. This is the number we'll
  show the user before deciding to deploy. Do NOT deploy.

## Phase B — provider-agnostic numerator (scope only, don't half-build)

`events_for_task_metrics` (summary.py:295) filters to anthropic-only, so parallel
**Codex / opencode / openclaw** worker sessions contribute neither baseline
(numerator) nor time (denominator) — their saved hours are invisible. Naively
unfiltering would add their events to the denominator without baselines → makes
the multiplier WORSE. The correct fix needs baselines for non-Claude task
sessions (run the estimator on `codex-events.jsonl` / opencode transcripts).

Deliver a short written plan (in your final message, not code): what it takes to
estimate baselines for non-Claude sessions, how their human-launch attention
(the prompt the user fired) enters the denominator, and the risk of double-
counting when a Claude orchestrator already launched them. Recommend whether to
do it now or as a follow-up.

## Constraints
- Stdlib only in `tracker/`, `backend/`. No new deps.
- Don't touch profanity/sentiment, baselines, or the oracle.
- Append-only events; tasks.json is derived (OK to read, don't rewrite here).
- No `Co-Authored-By:` trailers.
- Branch only. No deploy. Tests on host.

## Final message
Summarize: files changed, the paired-eval OLD vs NEW multiplier (all/30d/7d),
fallback count, and the Phase B recommendation.
