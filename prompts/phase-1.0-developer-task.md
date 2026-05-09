# Task: Phase 1.0 — Claude Code tracking hook + summary

You are the **developer** role in a multi-agent workflow. The architect (Claude) wrote this plan. Implement it and report via `--output-schema`. The reviewer (Claude) will check your work against acceptance criteria.

Working directory: `F:/WorkAI/multi-agent` (already your `--cd`).

Project context: see `CLAUDE.md` and `README.md` in working dir for project conventions and roadmap.

## Goal

Build basic tracking layer for Claude Code calls — foundation for cost-savings dashboard.

## Deliverables

1. **`hooks/claude-track-calls.py`** — Claude Code Stop hook. Reads JSON from stdin, parses last assistant message from transcript JSONL, writes one JSONL line.
2. **`tracker/summary.py`** — CLI tool. Reads JSONL events, outputs markdown table.
3. **`tracker/README.md`** — explains how to register the hook in `~/.claude/settings.json` (full snippet).

## Hook input (JSON on stdin from Claude Code Stop hook)

```json
{
  "session_id": "abc-123",
  "transcript_path": "C:/Users/Roono/.claude/projects/.../transcript.jsonl",
  "cwd": "F:/WorkAI",
  "stop_hook_active": false
}
```

## Transcript format (JSONL)

Each line is one event. Assistant events look like:

```json
{
  "type": "assistant",
  "uuid": "msg-uuid-here",
  "timestamp": "2026-05-09T12:34:56.789Z",
  "message": {
    "model": "claude-opus-4-7",
    "stop_reason": "end_turn",
    "usage": {
      "input_tokens": 1234,
      "output_tokens": 567,
      "cache_creation_input_tokens": 0,
      "cache_read_input_tokens": 800
    },
    "content": [
      {"type": "text", "text": "..."},
      {"type": "tool_use", "name": "Read", "input": {...}}
    ]
  }
}
```

You need the **last** assistant event in the file (iterate to end). `tool_uses` count = number of `tool_use` blocks in `content`.

Some assistant events may lack `usage` or `model` (system messages, etc.) — skip those, find the latest one with `usage`.

## Output JSONL schema (one line per turn, append to file)

```json
{
  "ts": "2026-05-09T12:34:56.789+10:00",
  "session_id": "abc-123",
  "message_uuid": "msg-uuid-here",
  "model": "claude-opus-4-7",
  "input_tokens": 1234,
  "output_tokens": 567,
  "cache_creation_tokens": 0,
  "cache_read_tokens": 800,
  "cost_estimate_usd": 0.0456,
  "duration_ms": 0,
  "working_dir": "F:/WorkAI",
  "tool_uses": 3,
  "stop_reason": "end_turn"
}
```

`ts` — current local time when hook fires (not transcript timestamp), ISO 8601 with timezone offset.

`duration_ms` — set to 0 for now; we'll wire actual duration in Phase 1.1.

## Pricing table (hardcode in the hook with comment "as of 2026-05-09")

```python
PRICING = {
    "claude-opus-4-7":      {"in": 15.00, "out": 75.00, "cache_read": 1.50, "cache_write": 18.75},
    "claude-opus-4-7[1m]":  {"in": 15.00, "out": 75.00, "cache_read": 1.50, "cache_write": 18.75},
    "claude-sonnet-4-6":    {"in":  3.00, "out": 15.00, "cache_read": 0.30, "cache_write":  3.75},
    "claude-haiku-4-5":     {"in":  1.00, "out":  5.00, "cache_read": 0.10, "cache_write":  1.25},
    "claude-haiku-4-5-20251001": {"in":  1.00, "out":  5.00, "cache_read": 0.10, "cache_write":  1.25},
}
# Per 1M tokens. If model not in dict — fall back to opus pricing AND log warning to stderr.
```

`cost_estimate_usd = (input * in + output * out + cache_read * cache_read + cache_creation * cache_write) / 1_000_000`, round to 4 decimals.

## Constraints

- **Performance**: <200ms total runtime
- **Robustness**: silently `exit 0` on any of: empty transcript, missing transcript file, malformed JSONL, no `usage` block in last assistant event, missing input fields. Never crash, never block Claude Code.
- **UTF-8**: explicit `encoding="utf-8"` on all file I/O
- **Atomic write**: write JSONL line to `tracker/.claude-events.jsonl.tmp.<pid>`, then `os.replace()` to merge with existing file. Concurrent runs must not corrupt.
- **Idempotent**: dedup key is `(session_id, message_uuid)`. Track last seen UUID per session in `tracker/.last-uuids.json` (atomic write same way). If hook fires twice for same UUID, second invocation is no-op.
- **Output file**: `F:/WorkAI/multi-agent/tracker/claude-events.jsonl`, autocreate parent dir
- **Path resolution**: hook is run with arbitrary cwd by Claude Code. Use `__file__` to find project root, not relative paths.

## summary.py CLI

```
py -3.14 tracker/summary.py [--days N] [--from YYYY-MM-DD] [--to YYYY-MM-DD]
```

Default `--days 1` (today, local timezone).

Output to stdout — markdown:

```markdown
## Claude Code stats: 2026-05-09 (1 day)

| Model | Calls | In tok | Out tok | Cache hit % | API cost ($) |
|---|---|---|---|---|---|
| claude-opus-4-7 | 12 | 45,201 | 8,900 | 62% | 1.34 |
| **Total** | 12 | 45,201 | 8,900 | 62% | 1.34 |

**Period API cost**: $1.34
**Max prorated** ($200/mo for this period): $6.67
**Savings**: $5.33 ✅
```

If period spans more than 1 day — also show daily aggregates.

If savings negative (you spent more than prorated subscription): show as `**Доплата**: $X.XX` without minus sign, no checkmark.

If JSONL empty / no events in period — print "No events in period" gracefully.

`Cache hit %` = `cache_read_tokens / (cache_read_tokens + input_tokens) * 100`, rounded to integer.

Number formatting: thousands with comma separator (`45,201`).

## tracker/README.md

Must explain:
1. What the hook does (one paragraph)
2. **Full settings.json snippet** to register it (use absolute path `F:/WorkAI/multi-agent/hooks/claude-track-calls.py`)
3. Where data lives (`tracker/claude-events.jsonl`)
4. How to run summary (`py -3.14 tracker/summary.py`)
5. How to verify hook is firing (`tail -f tracker/claude-events.jsonl` after a session)

## Acceptance criteria (will be checked by reviewer verbatim)

- [ ] `hooks/claude-track-calls.py` exists
- [ ] `tracker/summary.py` exists
- [ ] `tracker/README.md` exists with settings.json snippet
- [ ] Hook handles empty transcript without crash (test: pipe `{"session_id":"x","transcript_path":"/nonexistent","cwd":"."}`)
- [ ] Hook handles missing transcript file gracefully (silent exit 0)
- [ ] Hook is idempotent: running twice on same input creates only ONE jsonl line
- [ ] Pricing matches the table above (check exact numbers)
- [ ] No external pip dependencies (stdlib only — `json`, `pathlib`, `argparse`, `datetime`, `os`, `sys`, `tempfile`)
- [ ] Compatible with `py -3.14` (use modern syntax: type hints, match statements ok)

## Test it before reporting

After writing files, actually run:

1. **Empty input test**: `echo '{"session_id":"x","transcript_path":"/nonexistent","cwd":"."}' | py -3.14 hooks/claude-track-calls.py` — should exit 0 silently, nothing in tracker/.
2. **Real transcript test**: create `F:/temp/fake-transcript.jsonl` with one assistant event matching schema above, run `echo '{"session_id":"test1","transcript_path":"F:/temp/fake-transcript.jsonl","cwd":"."}' | py -3.14 hooks/claude-track-calls.py` — verify `tracker/claude-events.jsonl` has one line with correct values.
3. **Idempotency test**: run #2 again with same input — JSONL should still have only one line.
4. **Different message**: append a different assistant event with different uuid to fake-transcript.jsonl, run hook — JSONL should now have two lines.
5. **Summary**: run `py -3.14 tracker/summary.py` — should show your test data correctly.
6. **Empty summary**: delete jsonl, run summary again — should print "No events in period".

Report each test result in `test_results` field.

## Out of scope (do NOT implement)

- Codex CLI tracking (Phase 1.1)
- openclaw tracking (Phase 1.2)
- Backend / API
- Dashboard / UI
- Public site integration
- Performance metrics / latency tracking (just `duration_ms: 0` for now)

## Final report

Conform to `--output-schema`. Required fields: `files_created`, `summary`, `tested`. Strongly recommended: `test_results`, `open_questions`, `deviations_from_spec`.

If you couldn't run tests (e.g. python not available) — set `tested: false` and explain in `summary`. Don't fake it.
