# Task: productivity numerator — high-end sanity guard + plausibility band (#106-A)

Name: codex-developer
Profile: Codex CLI 0.128+ (gpt-5.5, xhigh reasoning, --sandbox workspace-write)
Goal: Mirror the PR #108 per-session sanity-**floor** with a symmetric high-**end** guard so an aborted/stub session (≈2 events, ≈30 s active) cannot inject a large hallucinated `ai_baseline_hours` into the public productivity numerator. Deterministic, consumption-time, no oracle/tasks.json changes. Same rigor and shape as #108 (counts/sums threaded to snapshot + every period block, tests, stdlib-only).
Constraints: stdlib-only in tracker/ and backend/; atomic writes; public snapshot must not leak raw session_ids (counts/aggregates only); no Co-Authored-By; do not change the intent of existing tests (the existing floor tests are a regression pin — they MUST pass unchanged).
Watches: Gitea issue #106 (body + 2 comments); files tracker/summary.py, backend/server.py, tests/, schemas/
Produces: tracker/summary.py, backend/server.py, tests/<new+updated>, schemas/<if a productivity schema exists>

## Operational backstory

workspace-write sandbox in `F:/WorkAI/neon-legion` (ASCII path). Python stdlib only, git, ChatGPT-auth. On Windows your shell commands may be wrapped in PowerShell and blocked by policy — do NOT loop trying to run pytest; **the architect runs the test suite on host**. You statically write code + tests and verify by reading. Network is blocked (no pip install). MCP stderr noise is harmless.

## Working directory

`F:/WorkAI/neon-legion` (your `--cd`).

## Project context (read first)

- `AGENTS.md` / `CLAUDE.md` — conventions (stdlib-only tracker/backend, append-only events, atomic writes, no Co-Authored-By).
- `prompts/fix-productivity-denominator-floor-task.md` — the PR #108 spec this mirrors. **#106-A is its exact high-end counterpart.** Read it.
- `tracker/summary.py` — read `effective_task_hours` (:471), `summarize_productivity` (:572, contains the #108 floor loop at ~608-624), `_active_time_hours_for_timestamps` (:536), `read_tasks`, `events_for_task_metrics`.
- `backend/server.py` — read `productivity_payload` (:340), `_productivity_block` (:930), `_today_payload` (:1131, contains a **second, duplicated** floor loop at ~1190-1206), `_productivity_periods` (:1017).
- `tests/test_productivity_sanity_cap.py` and `tests/test_productivity_multiplier.py` — existing floor/cap tests; the framework is **pytest**; mirror their style.

## Background (already investigated + calibrated by the architect; do not re-litigate)

Issue #106 comment 2: a complementary **over**-estimation failure mode the #108 floor does NOT catch. The oracle (`estimate-task.py` → `normalize_oracle_payload`) has **no upper cap**; `oracle-prompt.txt` is one-shot; `human_corrected_hours` is set 0× across 264 entries. Aborted/stub sessions (a `--bare` call, or a session opened+closed) get 2 events and ~30 s of active time but the oracle still hallucinates large baselines.

Calibration over all 254 covered sessions (architect-run, do not recompute):

- The garbage is a **clean cluster**: every inflated entry has **1–3 events** and **≤0.018 h (≤65 s)** active time, baseline 1.5–36 h, **9–18 h/event**.
- Legit big sessions: **655–3164 events**, **≤0.15 h/event**, `eeefea05` 183 h/1419 ev, `9f15a05f` 135 h/3164 ev. ~200× event-count separation, ~60× h/event gap. **Zero false-positive risk** for a conservative trivial-session rule.
- baseline_per_event percentiles: p50 0.25, p75 0.50, p90 5.0, p99 16, p100 18 — a cliff between p75 and p90 (the cliff is the garbage cluster).
- Aggregate effect of the guard below: all-time numerator 2168 → 1616 h (−25%), 46 sessions clamped, **all via the trivial rule** (the band fires on 0 current sessions — pure defense-in-depth).

## Deliverables (exact changes)

### 1. Shared invariant helper in `tracker/summary.py`

The #108 floor logic is currently **duplicated** in `summarize_productivity` and `_today_payload`. Extract a single source of truth so the floor (#108) and the new ceiling (#106-A) cannot diverge:

```
def effective_session_hours(baseline_hours, session_active_hours, event_count):
    """Returns (effective_hours, kind) where kind in
    {"normal","floor","ceiling_trivial","ceiling_band"}.
    Floor (#108) and ceiling (#106-A) are mutually exclusive per session."""
```

Module-level named constants with a one-line comment citing the calibration above:
- `TRIVIAL_EVENT_MAX = 5`            # smallest legit session = 655 events
- `TRIVIAL_ACTIVE_MAX_HOURS = 0.05`  # 3 min; observed garbage ≤ 65 s
- `TRIVIAL_MIN_BASELINE_HOURS = 1.0` # don't flag a stub estimated ≤1 h (noise)
- `PER_EVENT_CEILING_HOURS = 1.0`    # legit max 0.15 h/ev, garbage min 9 h/ev
- `BAND_MIN_HOURS = 6.0`             # floor for the band; a real small dense session stays safe

Logic, in this exact order:
1. `eff = max(baseline_hours, session_active_hours)` — **#108 floor** (unchanged behavior). If `baseline_hours < session_active_hours` → `kind="floor"`.
2. **Trivial-session guard (#106-A primary)** — if `event_count <= TRIVIAL_EVENT_MAX` and `session_active_hours <= TRIVIAL_ACTIVE_MAX_HOURS` and `baseline_hours > TRIVIAL_MIN_BASELINE_HOURS`: an aborted/stub session is worth only its tiny real footprint → `eff = session_active_hours`, `kind="ceiling_trivial"`.
3. **Plausibility band (#106-A secondary)** — else if `eff > max(BAND_MIN_HOURS, PER_EVENT_CEILING_HOURS * event_count)`: clamp `eff` to that ceiling, `kind="ceiling_band"`.
4. else `kind="normal"`.

(Floor and ceiling are mutually exclusive: a trivial session has `baseline > active` so step 1 sets `kind="floor"` but then step 2 overrides — the helper must report the FINAL kind, and the floor must NOT be counted when a ceiling subsequently fires on the same session. Count exactly one outcome per session.)

### 2. Use the helper in both consumers

- `summarize_productivity` (`tracker/summary.py`): replace the inline `effective_hours = max(...)` floor block with a call to `effective_session_hours(...)`, passing `event_count = len(session_timestamps.get(session_id, []))`. Keep `baseline_floor_clamped` / `hours_floor_added` (counted only when `kind=="floor"`). Add `baseline_ceiling_clamped` (int — count where `kind` starts with `"ceiling"`) and `hours_ceiling_removed` (float — Σ(`baseline_hours - eff`) over those). Also add `baseline_per_event_p95` (float — 95th percentile of `baseline/event_count` over covered sessions with `event_count>0`; AC requires baseline_per_event surfaced).
- `_today_payload` (`backend/server.py`): replace its duplicated floor loop with the same helper call (`event_count = len(events_by_session.get(sid, []))`). Thread the same new keys.

### 3. Thread new fields to snapshot

In `backend/server.py` — wherever `baseline_floor_clamped` / `hours_floor_added` appear (`productivity_payload`, `_productivity_block`, `_today_payload`, `_productivity_periods`), add `baseline_ceiling_clamped`, `hours_ceiling_removed`, `baseline_per_event_p95` alongside, in the top-level `productivity` block and **every** `productivity_periods` entry (today/7d/30d/60d/all). Update any productivity schema in `schemas/` and any fixtures/tests asserting the productivity shape. **No raw session_ids** in any return (public-snapshot privacy).

## Acceptance criteria

- [ ] Single `effective_session_hours` helper is the only place floor+ceiling math lives; both `summarize_productivity` and `_today_payload` call it (duplication removed).
- [ ] Existing floor tests in `tests/test_productivity_sanity_cap.py` / `test_productivity_multiplier.py` pass **unchanged** (floor behavior is byte-identical — regression pin).
- [ ] A trivial session (≤5 events, ≤0.05 h active, baseline >1 h) contributes only `session_active_hours`; `baseline_ceiling_clamped` counts it, `hours_ceiling_removed` accumulates the removed hours.
- [ ] Floor and ceiling are mutually exclusive per session; a session is counted in exactly one of `baseline_floor_clamped` / `baseline_ceiling_clamped`.
- [ ] `baseline_ceiling_clamped`, `hours_ceiling_removed`, `baseline_per_event_p95` threaded to the snapshot `productivity` block and every period block (today/7d/30d/60d/all).
- [ ] New unit tests: (a) trivial 2-event/30 s/36 h session → effective ≈ active, clamp counted, ≈36 h removed; (b) plausibility-band synthetic (e.g. 50 events, baseline 500 h, active 1 h → clamped to 50 h via band, `kind="ceiling_band"`); (c) legit big session (1000 events, baseline 80 h, active 5 h) → unchanged, `kind="normal"`; (d) floor still fires on baseline<active and is NOT also ceiling-counted.
- [ ] stdlib-only; atomic writes preserved; no raw session_ids in snapshot; no Co-Authored-By.

## Test plan

Code statically written and self-verified by Codex (syntax, logic, read the diffs). **Architect runs `py -3.14 -m pytest tests/ -q` on host** (baseline before this task: 342 passed, 1 skipped) + a before/after multiplier repro + `tools/oss-sanitize.py --check`. Codex does NOT execute pytest in the sandbox. In the final report list exactly which test files/cases you added or changed.

## Out of scope (leave in #106 — do NOT do here)

- `tracker/estimate-task.py` / `tracker/oracle-prompt.txt` rework (estimator scaling — that is #106-B, fuzzy/LLM, separate).
- Re-running the oracle on history / rewriting `tracker/tasks.json` (#106-D).
- Explicit task-id or per-day chunking of marathon sessions (#106-C).
- `gap_minutes` retuning.
- Any deploy (`deploy-snapshot.sh` / theme) — architect does it after review + DeepSeek ratify + human approve.

## Final report

Conform to `--output-schema`. Required: `files_created` (use for files modified — path/purpose/loc), `summary`, `tested` (false — architect tests on host), `test_results` ("not executed in sandbox; tests written"), `open_questions`, `deviations_from_spec`.
