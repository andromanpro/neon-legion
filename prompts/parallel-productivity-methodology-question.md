# Methodology question: productivity multiplier under parallel/concurrent AI work

You are a measurement-methodology reviewer. This is a **methodology question**,
not an implementation task. Read the code, then answer the questions at the end.
Files are in this repo: `tracker/summary.py`, `backend/server.py`.

## Context

A personal AI-usage tracker computes a "productivity multiplier":

```
multiplier      = hours_without_ai / active_hours_with_ai
hours_saved     = hours_without_ai − active_hours_with_ai
```

- `hours_without_ai` = Σ over covered sessions of an LLM-estimated "how long
  this task would take me by hand" baseline (per session, with floor/ceiling
  clamps via `effective_session_hours`). One Claude/Codex session ≈ one task.
- `active_hours_with_ai` = `active_time_hours_merged(covered_events, gap_minutes=2)`
  (summary.py:622). It pools **all event timestamps from all covered sessions
  into ONE sorted timeline**, then sums the gaps between consecutive events that
  are ≤ 2 minutes apart. Gaps > 2 min are dropped (treated as "away").

So the denominator is "merged wall-clock time during which ANY session was
active, with ≤2-min bridging".

There is also `active_time_hours` (summary.py:572) — the **per-session** variant
that sums each session's active time independently (overlaps double-counted).
The multiplier uses the **merged** one; the per-session sum is kept only as a
diagnostic (`active_hours_per_session_sum`).

## The user's real working pattern (the reason for this question)

The user frequently runs **several AI sessions in parallel** (fires 3-5 agents
at once) **and simultaneously works on a different task by hand**. So at any
given minute, 3-5 autonomous agent sessions may be emitting events while the
human is busy elsewhere.

The user reports the multiplier feels **too low** and suspects either:
(a) parallel sessions are mismeasured, or
(b) "time between calls" (the gap bridging) is computed wrong.

## What the merge does to parallel sessions (verify my reasoning)

When sessions overlap, their interleaved events make the merged timeline DENSE:
consecutive events (from *different* sessions) are almost always < 2 min apart,
so nearly all of it counts as "active". Net effect: the denominator ≈ the
union of all engaged wall-clock, NOT the sum of per-task durations. So merging
*shrinks* the denominator vs summing → *raises* the multiplier. That seems
favorable, yet the user still sees a low number on parallel-heavy weeks
(observed: 7-day ×3.3 while all-time ×8.0–10.9).

## Questions (answer each concisely, with reasoning)

1. **Is `Σ baselines / merged_active_time` the right model** for a productivity
   multiplier when work is parallelized? If a user fires 3 agents in 1 wall-clock
   hour and each task would take 5 h by hand, the "true" saving is 15 h in 1 h
   (×15). Does the current formula capture that, or does something compress it?

2. **Where does parallel + concurrent-human-work bias the denominator?**
   Specifically: when an agent runs autonomously for, say, 10 min with events
   every ~30 s while the human is away on another task — is that 10 min counted
   as "active AI time" (inflating the denominator and lowering the multiplier),
   even though the human spent ~0 of their own attention on it? Is the merged
   timeline measuring *the AI's busy time* when it should measure *the human's
   spent time*?

3. **Is `gap_minutes=2` the right bridge** for autonomous parallel runs? Argue
   for a value (or an adaptive rule). Note the trade-off: too small → fragments
   long autonomous runs into uncounted gaps (raises multiplier); too large →
   bridges genuine away-time (lowers multiplier).

4. **What denominator would you recommend** so the multiplier honestly reflects
   "value produced per unit of the *human's* engaged time", crediting
   parallelism? Options to weigh: (a) keep merged active time; (b) use the
   human's own keystroke/prompt cadence as the attention signal and treat
   autonomous agent-only stretches as near-zero human cost; (c) a hybrid.
   Recommend one and state what data the tracker would need.

5. **Sanity check the #106/#107 history:** prior fixes merged the timeline to
   stop double-counting concurrent wall-clock (#107) and added baseline
   floor/ceiling clamps + per-calendar-day chunking (#106). Given the user's
   parallel pattern, is the *remaining* dominant error in the **numerator**
   (baselines saturating / one-session-one-task breaking) or the **denominator**
   (merged AI-busy time ≠ human-attention time)? Which single change moves the
   number most toward honesty?

Keep it tight and concrete. Prefer "do X because Y" over hedging. No preface.
