# Audit: human-attention productivity denominator + all-time window

Role: read-only auditor. Read the actual files in this repo, then report findings.
Do NOT modify anything. Output a concise findings list ranked by severity
(HIGH/MED/LOW), each: file:line — one-line problem — why it's wrong. No preface,
no praise, real bugs only.

## What changed recently (focus here)

A "productivity multiplier" = `hours_without_ai / denominator`. The denominator
was switched from AI-event-busy time to **human attention** (time the human
actually spent), so parallel/autonomous agent runtime no longer inflates it.

Read and audit these:

1. `tracker/summary.py` — the human-attention block:
   - `is_human_prompt(event)` — detects genuine human prompts, excludes
     tool_result "user" lines and `isSidechain` sub-agent turns.
   - `read_human_message_timestamps(path)` — parses user-prompt timestamps.
   - `resolve_transcript_path(value)`.
   - `_human_attention_hours_for_units(units, tasks, ai_session_timestamps, gap)`
     — pools human-prompt timestamps across coverage units; each unit is
     `(session_id, date_key|None)`; date_key restricts to that calendar day
     (chunk mode); None = whole session restricted to the session's in-window
     active days (derived from ai_session_timestamps); missing transcript →
     fallback to that session's AI-event timestamps.
   - `human_attention_hours(...)` — session-mode wrapper.
   - Constants `HUMAN_ATTENTION_GAP_MINUTES = 5`,
     `HUMAN_ATTENTION_FLOOR_MIN_PER_SESSION = 5`.
   - `summarize_productivity(...)` — both the `unit == "session"` branch and the
     chunk branch now return `human_attention_hours_with_ai` +
     `human_attention_fallbacks`. Check the covered-session/chunk sets feeding
     human attention match the numerator's covered set (no mismatch that would
     divide a whole-session denominator by a per-day-chunk numerator, or vice
     versa).

2. `backend/server.py` — `build_productivity(...)` (~line 355-395):
   - Headline `multiplier`/`hours_saved` now derive from
     `human_attention_hours` when > 0, else fall back to `active_hours`
     (AI-active). A per-session floor
     (`sessions_covered * HUMAN_ATTENTION_FLOOR_MIN_PER_SESSION/60`) guards
     divide-by-near-zero.
   - `active_hours` field now holds the human-attention denominator;
     `ai_active_wall_clock_hours` holds the old AI-active value.

## Specific things to check

- **Numerator/denominator unit mismatch** in the chunk branch: does the human
  attention pool exactly the covered `(session, day)` chunks that the baseline
  sum (`hours_without_ai`) covers? A mismatch skews every chunk-mode multiplier
  (this is prod's mode).
- **Floor correctness**: `sessions_covered` in chunk mode counts chunks, not
  sessions — does the floor use the right count so it isn't too large/small?
- **is_human_prompt false positives/negatives**: any prompt shape that leaks a
  tool_result through, or drops a real prompt (e.g. content is a list mixing a
  text block AND a tool_result; or `type` present but message.role absent).
- **Timezone consistency**: `chunk_date` / `parse_event_ts` — human-prompt
  timestamps (from transcripts, UTC "Z") vs AI-event timestamps — are they
  compared in the same tz when filtering by day? A tz skew would misassign
  prompts to the wrong day at midnight boundaries.
- **Fallback double-application**: when a session falls back to AI timestamps
  AND is also date-restricted, is the day-filter applied consistently so the
  fallback isn't silently emptied or over-counted?
- **all-time window** (`F:/WorkAI/wp-dev/tools/deploy-snapshot.sh`, if readable):
  now computes `--snapshot-days` from the earliest event across all provider
  jsonl. Sanity-check: does a 130-day window break the 60d/30d/7d period
  sub-windows or the timeline weights list length? Any off-by-one on the
  earliest-event day?

## Output
Ranked findings only. If a section is clean, say so in one line. End with a
one-sentence verdict: is the human-attention denominator sound enough to keep
driving the public multiplier, or is there a HIGH bug to fix first.
