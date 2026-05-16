# #106-C — drop "1 session = 1 task": design proposal (review before code)

Status: **DESIGN — no code written.** Decision gate for the user before any implementation.

## Why this is the last real lever

`#106` decomposed into: #106-A (high-end stub guard — shipped, deterministic), #107 (denominator — shipped), #106-B (size-aware oracle — shipped, honest "modest" result), #106-D (historical re-run — closed moot), **#106-C (this)**.

The original 3-way analysis put **~80% of the numerator distortion on the data model**, not the estimator: `tracker/tasks.json` is keyed by `session_id`, so a Claude Code session that spans days and contains dozens of distinct tasks gets exactly **one** `ai_baseline_hours`. #106-B confirmed empirically that prompt wording cannot fix this — a 5118-event, 5-day session compressed to a single oracle number is lossy *and* high-variance (±22h run-to-run) no matter how good the prompt. The only structural fix is to stop treating a session as the unit of work.

## The three options (issue's "implementer picks/combines", with tradeoffs)

### C1 — Calendar-day chunking (recommended first cut)
Split each session's events by calendar day; the unit of estimation becomes `(session_id, YYYY-MM-DD)`. One marathon session → N day-chunks → N baselines, summed.

- **Backfillable**: derivable purely from existing event timestamps already in the JSONL. No new data capture needed; history can be re-chunked.
- **No hook / UX change**: SessionStart/Stop hooks unchanged; no user discipline required.
- **Blast radius: moderate** (see below).
- **Limitation**: a day is a proxy for "a task", not a true task boundary — a day with 3 unrelated tasks still gets one estimate (but a *much* better-scoped one than a 5-day session).

### C2 — Explicit task boundaries (task-id / Gitea-issue tag)
A hook or the user marks task boundaries; one session → explicit task records.

- **Highest fidelity** — true task units.
- **Cannot backfill** — no historical task tags exist; only improves *future* data (same limitation that made #106-B low-impact and #106-D moot). The historical public number stays driven by the deterministic guards.
- **Blast radius: high** — hook changes, a tagging UX, user discipline, and a new identity scheme.
- Verdict: **defer.** It repeats the "future-only" weakness; not worth the blast radius now.

### C3 — Hybrid
C1 by default (backfillable), C2 override when an explicit task tag is present.

- Best end-state, but only worth it once C1 is proven. **Defer the C2 half.**

**Recommendation: implement C1 only. Defer C2/C3.**

## C1 blast radius (the honest list)

| Area | Change | Risk |
|---|---|---|
| `tracker/tasks.json` schema | key `session_id` → `task_id` = `f"{session_id}:{date}"`; old keys must still read (migration/back-compat) | **High** — every reader touches this |
| `tracker/estimate-task.py` | estimate per day-chunk, not per session → **N× more `codex exec` calls** (cost ~0 by subscription, but wall-time + the ±22h variance now multiplies across chunks) | Medium |
| `tracker/summary.py` | `read_tasks`, `effective_task_hours`, `summarize_productivity` aggregate per-task; **the #106-A guard currently operates per session — must move to per-chunk** (open question below) | **High** — the load-bearing public-number path |
| hooks (`SessionStart`/`Stop`) | trigger per-day pending estimation, not per-session; the "sliding 24h" dispatch logic | Medium |
| `tracker/backfill*.py` | re-bucket existing events into day-chunks; one-shot historical migration | Medium |
| `backend/server.py` | `_today_payload`, `_productivity_block`, `_productivity_periods` — task counts, "sessions_covered" semantics change to "task-chunks" | Medium |
| Public all-time ×N | **Will move — likely UP** (marathons currently 1 weak baseline → sum of per-day ≥ that). Needs before/after + human gate + deploy, exactly like #106-A | **High visibility** |
| Tests | new chunking unit tests; existing productivity/guard tests re-baselined for the new unit | Medium |

## Interaction with already-shipped fixes

- **#107** (merged-timeline denominator): session-independent → unaffected. ✓
- **#106-A guard**: currently per-session (`effective_session_hours(baseline, session_active, event_count)`). Under chunking the unit is a day-chunk. **Open question:** does the trivial-stub guard apply per chunk (a near-empty *day* inside a real marathon) or still per session? Mis-design here re-introduces either the stub leak or false-clamps real marathon days.
- **#106-B size-aware oracle**: `compute_session_metrics` becomes `compute_chunk_metrics` (events/active/span scoped to the day). Conceptually clean; the SESSION SIZE block already generalises.

## Migration & rollback

- **Append-only / `correction_of`** constraint (per #106): chunk-keyed records added; session-keyed records not destructively rewritten — a migration writes new task-chunk entries; readers prefer chunk-keys, fall back to session-key.
- **Feature-flag the aggregation path** in `summarize_productivity` (session-mode vs chunk-mode) so the snapshot can be regenerated both ways and the before/after computed deterministically before the public number moves.
- Rollback = flip the flag back to session-mode; chunk records are additive and inert when the flag is off.

## Open questions for the user (decide at review)

1. **C1 only, or also build the C2 explicit-tag half now?** (Recommendation: C1 only.)
2. **Chunk key**: `session_id:date` (simple, calendar-day) vs a finer task-id scheme. (Recommendation: `session_id:date`.)
3. **#106-A guard granularity under chunking**: per chunk or per session? (Needs an explicit rule — propose: per chunk, since a stub *day* contributes nothing and the calibration separation still holds at chunk level; verify with the same kind of calibration repro as #106-A.)
4. **Public-number movement**: #106-C will move the all-time ×N (likely up). Same governance as #106-A (before/after table in PR, human gate, deploy)? (Recommendation: yes, identical governance.)
5. **Scope of historical re-chunk**: only sessions with live transcripts can be re-*estimated* per day; sessions with pruned transcripts can only be re-*bucketed* structurally but not re-estimated — their single old baseline would have to be split heuristically or left as a single chunk. (Recommendation: live-transcript sessions get true per-day estimates; pruned sessions keep one chunk = current behavior, still guarded by #106-A.)

## Proposed plan if approved

Same multi-agent pipeline as #106-A/B: this design → Codex implements C1 behind the aggregation flag → architect review of actual diff → host tests + a deterministic before/after (session-mode vs chunk-mode on the same data, no oracle re-run needed for the structural part) → DeepSeek ratify (money/metric math + migration safety) → human gate with the before/after → merge → deploy. The oracle re-estimation of live-transcript day-chunks is the only LLM-variance-exposed part and should be multi-sample-median'd (lesson from #106-B).
