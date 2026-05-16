# Task: size-aware oracle — estimate scales with session size (#106-B)

Name: codex-developer
Profile: Codex CLI 0.128+ (gpt-5.5, xhigh reasoning, --sandbox workspace-write)
Goal: Fix the root cause behind #106 numerator distortion: the oracle (`tracker/estimate-task.py` + `tracker/oracle-prompt.txt`) physically cannot see session size — it only receives 3 first user + 5 last assistant messages clipped to 15K chars, so a 2-event stub and a 3164-event multi-day marathon look identical to it. Inject deterministic session-size metrics into the prompt and rewrite the prompt so the estimate scales monotonically with size (stub -> ~0, marathon -> sum-of-tasks, no 40h saturation). Build an eval harness so the architect can validate old-vs-new on a sampled size-spectrum BEFORE merge.
Constraints: stdlib-only in tracker/ and tools/; the oracle output JSON schema and `normalize_oracle_payload` contract are UNCHANGED (downstream must still parse identically); existing failure paths intact; do not modify existing tests (only add new); no Co-Authored-By; no deploy.
Watches: Gitea #106 (body + 2 comments + the #106-A comment on PR #109); files tracker/estimate-task.py, tracker/oracle-prompt.txt, tools/, tests/
Produces: tracker/estimate-task.py, tracker/oracle-prompt.txt, tools/eval-oracle.py, tests/test_session_metrics.py

## Operational backstory

workspace-write sandbox in `F:/WorkAI/neon-legion` (ASCII path). Python stdlib only, git, ChatGPT-auth. On Windows your shell may be PowerShell-wrapped/policy-blocked — do NOT loop on blocked commands; **the architect runs the eval harness + pytest on host** (the eval makes many `codex exec` oracle calls — that is an architect/human validation gate, not a sandbox step). You statically write code + the prompt + tests and self-verify by reading. MCP stderr noise is harmless.

## Project context (read first)

- `AGENTS.md` / `CLAUDE.md` — conventions (stdlib-only tracker/, append-only, no Co-Authored-By).
- `tracker/estimate-task.py` — READ ALL. Key: `read_transcript` (:242), `build_truncated_context_from_messages` (:331), `estimate_session` (:512), `run_oracle` (:450, `codex exec --sandbox read-only`), `normalize_oracle_payload` (:377 — its input/output contract is FROZEN), `failure_entry` (:495).
- `tracker/oracle-prompt.txt` — the current prompt (schema + "Hours guidance" + sentiment).
- `tracker/summary.py` — `_active_time_hours_for_timestamps` (:536) and `parse_event_ts`: the gap-based active-time rule (`gap_minutes=2`). Reuse the SAME rule so the oracle sees the same `active_hours` the #106-A guard uses (estimator and guard must be consistent).
- Transcript JSONL schema (verified): each line is an event dict; `type` in {user, assistant, system, queue-operation, attachment, last-prompt, custom-title}; `timestamp` ISO 8601 (e.g. `2026-05-09T16:16:44.652Z`); assistant `message.content` is a list of blocks with `type` in {text, thinking, tool_use}; tool results carry `toolUseID` / `toolUseResult`.

## Deliverables (exact changes)

### 1. `tracker/estimate-task.py` — session-size metrics + injection

Add `compute_session_metrics(transcript_path) -> dict` (stdlib only). One pass over the JSONL:
- `event_count` — total non-blank parseable lines.
- `user_message_count`, `assistant_message_count` — by `type` (reuse `transcript_role`).
- `tool_call_count` — count assistant `message.content` blocks with `type == "tool_use"` (fallback: events bearing `toolUseID`).
- `span_hours` — (last `timestamp` − first `timestamp`) in hours (parse ISO; tolerate `Z`).
- `active_hours` — gap-based sum over all event timestamps with the SAME 2-minute gap rule as `tracker/summary.py:_active_time_hours_for_timestamps` (port the rule locally; stdlib; do not import backend).
- All fields degrade safely to 0 on parse errors (never raise — the oracle path must stay robust; mirror existing best-effort style).

In `estimate_session`, compute metrics and prepend a ground-truth block to the prompt, BEFORE the truncated transcript:

```
=== SESSION SIZE (ground truth — the transcript below is TRUNCATED to a few messages; trust THESE numbers for scale, not the transcript length) ===
events=<N>  user_msgs=<U>  assistant_msgs=<A>  tool_calls=<T>  span_hours=<S>  active_hours=<Ac>
```

`run_oracle`, `normalize_oracle_payload`, the output schema, `failure_entry`, lock/atomic-write, profanity handling — all UNCHANGED.

### 2. `tracker/oracle-prompt.txt` — rewrite for size-aware scaling

Keep the EXACT output JSON schema and ALL sentiment guidance (frustration/appreciation/mood/intensity) verbatim — `normalize_oracle_payload` depends on it. Change only the estimation logic. The rewritten prompt must instruct:
- The SESSION SIZE block is ground truth for scale; the transcript is a tiny truncated sample — do NOT infer scale from transcript length.
- The estimate must increase monotonically with size signals (primarily `events` and `active_hours`, with `span_hours` distinguishing a focused burst from a multi-day marathon).
- **Stub rule:** `events <= ~3` and `active_hours` near zero ⇒ aborted/empty session ⇒ `ai_baseline_hours` in 0–0.25 and `estimation_confidence` low. A 2-event session cannot be worth hours.
- **Marathon rule:** thousands of events spanning many hours/days = a session that contains MANY distinct tasks. Estimate the SUM of human-equivalent time across all of them. Do NOT cap at 40h — marathons legitimately reach 100h+ equivalent. The old per-session ceiling was the saturation bug.
- **Plausibility band:** sanity-check the estimate against `active_hours` and `events` — real work is roughly 0.02–0.5 h per event for substantial sessions; a result far outside that for the given size is wrong. (Calibration intuition: legit sessions ≤0.15 h/event; stubs that scored 9–18 h/event were the bug.)
- Keep the bucket guidance but reframe it as PER-TASK, then explicitly: total = sum of tasks for multi-task marathons.
Output must remain strict JSON only, no prose/fences (unchanged).

### 3. `tools/eval-oracle.py` — old-vs-new validation harness (architect runs on host)

Stdlib only. CLI: `--sample-per-bucket N` (default 3), `--out <md>`.
- Read `tracker/tasks.json`; for entries with an existing `ai_baseline_hours` (= the OLD estimate) and an existing `transcript_path`, compute `compute_session_metrics`, bucket by `event_count`: stub `<=3`, small `4–50`, medium `51–500`, large `501–1500`, marathon `>1500`. Sample N per bucket (deterministic: sort by sid, take first N).
- For each sampled session, run the NEW oracle once via the same `run_oracle` path (reuse `estimate_session` internals or call the module) → `new_baseline`. The OLD estimate is the stored `ai_baseline_hours` (do not re-run old; the stored value IS the old oracle output).
- Emit a markdown table sorted by `event_count`: sid(8), events, active_h, span_h, old_base, new_base, old_h/ev, new_h/ev.
- Emit PASS/FAIL heuristics: (a) every stub-bucket `new_base <= 0.5`; (b) every marathon-bucket `new_base >= old_base` AND `new_base >= 1.0` (non-saturation); (c) rough monotonicity: median `new_base` non-decreasing across the 5 buckets ordered stub→marathon. Print a final `EVAL: PASS` / `EVAL: FAIL (reasons...)`.
- Never raise on a single bad session — skip + note it.

### 4. `tests/test_session_metrics.py`

Pytest. Write a tiny synthetic JSONL fixture (tempfile) with known events/timestamps/tool_use blocks; assert `compute_session_metrics` returns exact `event_count`, `user_message_count`, `assistant_message_count`, `tool_call_count`, `span_hours`, and `active_hours` (construct timestamps so the 2-min gap rule yields a known value — mirror the arithmetic in existing `tests/test_productivity_*`). Add a malformed-line case → metrics still return, no raise.

## Acceptance criteria

- [ ] `compute_session_metrics` is stdlib-only, single-pass, never raises; active_hours uses the exact 2-min gap rule from summary.py.
- [ ] Prompt receives the SESSION SIZE ground-truth block before the truncated transcript.
- [ ] `oracle-prompt.txt` output JSON schema + sentiment guidance byte-for-byte preserved; only estimation logic changed; stub/marathon/band rules present.
- [ ] `normalize_oracle_payload` and the failure paths are untouched and still parse the (unchanged) schema.
- [ ] `tools/eval-oracle.py` produces the comparison table + PASS/FAIL heuristics; stdlib-only; robust to per-session failures.
- [ ] `tests/test_session_metrics.py` added; existing tests unmodified; `pytest tests/ -q` green on host (architect runs).
- [ ] stdlib-only; no Co-Authored-By; no deploy; no schema/normalize change.

## Test plan

Codex statically self-verifies (syntax, logic, reads diffs). **Architect on host:** `pytest tests/ -q`, then `py -3.14 tools/eval-oracle.py --out F:/temp/eval-oracle-106b.md` (many `codex exec` oracle calls — the validation gate). Architect iterates `oracle-prompt.txt` wording against eval output, re-runs eval until heuristics pass, then DeepSeek ratify + human gate. Codex does NOT run the eval or pytest in sandbox.

## Out of scope (do NOT do here)

- #106-C task-id / per-day chunking (data model — separate).
- #106-D historical re-estimation / rewriting tasks.json (separate, AFTER B ships).
- Changing the oracle output schema or `normalize_oracle_payload`.
- Touching the #106-A guard (`summary.py` / `server.py` ceiling), or any deploy.

## Final report

Conform to `--output-schema`. Required: `files_created` (use for files modified — path/purpose/loc), `summary`, `tested` (false — architect runs eval+pytest on host), `test_results` ("not executed in sandbox; tests + eval harness written"), `open_questions`, `deviations_from_spec`.
