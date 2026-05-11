# Task: Phase 1.3 — Task complexity estimation через SessionStart hook

You are the **developer** role in a multi-agent workflow. The architect (Claude) wrote this plan. Implement it and report via `--output-schema`.

Working directory: `<project_root>` (already your `--cd`).

Project context: read `CLAUDE.md` (especially section "Trigger для baseline-оценки сложности") and `README.md` for roadmap. Phase 1.0 already shipped: `hooks/claude-track-calls.py` writes JSONL events on Stop, `tracker/summary.py` outputs markdown. Now we add complexity estimation для time-saved metric.

## Goal

Implement collection of human-hours-without-AI estimates for each Claude Code session, asynchronously via SessionStart hook, with manual override CLI.

## Architecture

User does NOT formally close sessions — он просто открывает новые. So SessionEnd hook ненадёжен. Trigger через **SessionStart**: при запуске новой сессии hook async-фоном:
1. Находит «висящие» session_id (которые есть в `tracker/claude-events.jsonl` за последние 24 часа, но отсутствуют в `tracker/tasks.json`).
2. Для каждой висящей — запускает оценочный subprocess `claude -p --bare` (флаг `--bare` отключает hooks → НЕ зацикливается обратно в трекер).
3. Записывает результат в `tracker/tasks.json`.

Hook сам **не блокирует** startup — `subprocess.Popen` с `start_new_session=True` (или `creationflags=subprocess.CREATE_NEW_PROCESS_GROUP` на Windows) для detached background. Hook exit 0 в течение <500ms.

## Deliverables

1. **`hooks/claude-session-start.py`** — SessionStart hook (async dispatcher)
2. **`tracker/estimate-task.py`** — sub-script, который запускается hook'ом для одной session: парсит transcript, вызывает `claude -p --bare`, парсит ответ, обновляет tasks.json
3. **`tracker/note-task.py`** — CLI для ручного override / list / pending
4. **`tracker/oracle-prompt.txt`** — текст промпта для `claude -p` (вынесен из кода чтобы редактировать без правки py)
5. **Обновление `tracker/summary.py`** — блок time-saved metric (если в periode есть оценки)
6. **Обновление `tracker/README.md`** — регистрация SessionStart hook + объяснение workflow + `note-task.py` usage

## Hook input (SessionStart, JSON on stdin)

```json
{
  "session_id": "<новый session_id>",
  "transcript_path": "<user_home>/.claude/projects/<encoded-cwd>/<session_id>.jsonl",
  "cwd": "<новый cwd>",
  "source": "startup" | "resume" | "clear"
}
```

## tasks.json schema

```json
{
  "<session_id>": {
    "ai_baseline_hours": 4.5,
    "human_corrected_hours": null,
    "brief_description": "implementing tracking hook for Phase 1.0",
    "estimated_at": "2026-05-09T13:00:00+10:00",
    "estimation_confidence": "high",
    "needs_manual_review": false,
    "transcript_path": "<user_home>/.claude/projects/.../abc.jsonl"
  }
}
```

`human_corrected_hours` имеет приоритет над `ai_baseline_hours` если задан. Display = `human_corrected_hours or ai_baseline_hours`.

## hooks/claude-session-start.py логика

```
1. Read JSON from stdin → new_session_id
2. Find tracker dir: PROJECT_ROOT = Path(__file__).resolve().parents[1]; TRACKER = PROJECT_ROOT/'tracker'
3. If tracker/claude-events.jsonl missing → exit 0 (Phase 1.0 ещё не запускался)
4. Read JSONL, collect set of session_ids за последние 24h excluding new_session_id
5. Read tasks.json (default {}); collect pending = recent_sids - tasks.keys()
6. If not pending → exit 0
7. For each pending sid:
   a. Find transcript: glob <user_home>/.claude/projects/*/{sid}.jsonl
   b. If not found → log warning to tracker/.estimation-logs/<sid>.log, write tasks.json entry с needs_manual_review=true, ai_baseline_hours=null, brief_description="transcript not found"
   c. Else: subprocess.Popen([py -3.14, 'tracker/estimate-task.py', sid, transcript_path], detached, redirect stderr/stdout to .estimation-logs/<sid>.log)
8. exit 0
```

Hook должен **не падать** на любом исключении — silent exit 0.

## tracker/estimate-task.py логика

```
Args: session_id, transcript_path

1. Read transcript JSONL
2. Build truncated context:
   - First 3 user messages (text content)
   - Last 5 assistant messages (text content, без tool_use spam)
   - Total ≤ ~15k characters; иначе truncate ещё агрессивнее
3. Read oracle-prompt.txt (text)
4. Run subprocess: claude -p --bare --output-format json
   stdin = oracle-prompt + "\n\n=== TRANSCRIPT (truncated) ===\n" + truncated_context
   timeout = 60 seconds
5. Parse claude output JSON. Extract result:
   - brief_description (string)
   - ai_baseline_hours (float)
   - estimation_confidence (high/medium/low)
   - needs_manual_review (bool)
6. Atomic update tasks.json: read → merge → tempfile → os.replace
   Add entry for session_id with the parsed fields + estimated_at + transcript_path
7. If subprocess timed out / failed → write entry с needs_manual_review=true, error logged
```

## tracker/oracle-prompt.txt

```
You are estimating the human-time equivalent of a software development session done with AI assistance.

Read the truncated Claude Code transcript that follows the marker. Output strict JSON only, no prose, no preamble, no code fences.

Output schema:
{
  "brief_description": "<1 line — what was accomplished>",
  "ai_baseline_hours": <float>,
  "estimation_confidence": "<high|medium|low>",
  "needs_manual_review": <bool>
}

Hours guidance (without AI assistance):
- Trivial (typo, single-line fix): 0.25–1
- Simple (one file feature, small refactor): 1–3
- Medium (multi-file design, integration): 3–12
- Complex (architecture, debugging long bugs, multi-system): 12–40
- Research / investigation: count investigation hours, not code lines

Set needs_manual_review=true ONLY if transcript is too truncated, off-topic, or unintelligible. Otherwise set false.

Set estimation_confidence based on transcript clarity (low if many tools were used but final outcome unclear).
```

## tracker/note-task.py CLI

```
py -3.14 tracker/note-task.py --session-id X --hours N [--description "..."]
py -3.14 tracker/note-task.py --list                    # все entries
py -3.14 tracker/note-task.py --pending                 # session_ids в JSONL без entry в tasks.json
py -3.14 tracker/note-task.py --reestimate <session_id> # удалить entry, чтобы hook оценил заново
```

`--hours N` устанавливает `human_corrected_hours = N`. Не трогает `ai_baseline_hours`.

`--list` outputs markdown table:

```
| Session ID (short) | Description | AI baseline (h) | Human corrected (h) | Effective (h) |
```

## summary.py обновление

В существующий output добавить (если в period есть **хотя бы одна** session с tasks.json entry):

```
## Productivity (Phase 1.3)

**Hours with AI** (wall clock): 12.4
**Hours without AI** (estimated): 67.0
**Hours saved**: 54.6 ✅
**Productivity multiplier**: ×5.4

Sessions covered: 8 of 10 (2 pending complexity estimation)
```

`Hours with AI` = sum по session_id в period: `(last_ts − first_ts).total_seconds() / 3600`.
`Hours without AI` = sum `human_corrected_hours or ai_baseline_hours` по session_id в period (только если в tasks.json).
`Sessions covered` = count session_ids с entry в tasks.json / total session_ids в period.

If no tasks.json entries → не показывать блок Productivity.

## Constraints

- SessionStart hook **<500ms** runtime (subprocess Popen detached, не wait)
- estimate-task.py timeout 60s на одну оценку
- UTF-8 везде
- Atomic write tasks.json (tempfile + os.replace)
- Idempotent: повторный запуск hook'а на тех же pending — не дублирует, не зависает (если уже estimated — skip)
- Логи estimation в `tracker/.estimation-logs/<session_id>.log` (gitignore-ed)
- Только stdlib (json, os, sys, datetime, pathlib, argparse, subprocess, glob)

## Update .gitignore

Add: `tracker/tasks.json`, `tracker/.estimation-logs/`

## Acceptance criteria

- [ ] `hooks/claude-session-start.py` exists, async-dispatch logic
- [ ] `tracker/estimate-task.py` exists, calls `claude -p --bare`
- [ ] `tracker/note-task.py` CLI with `--session-id`, `--hours`, `--list`, `--pending`, `--reestimate`
- [ ] `tracker/oracle-prompt.txt` exists with the prompt above
- [ ] `tracker/README.md` updated with SessionStart registration snippet + workflow
- [ ] `tracker/summary.py` shows Productivity block when tasks exist
- [ ] `.gitignore` updated
- [ ] Hook handles missing JSONL / empty pending / missing transcript gracefully (silent exit 0)
- [ ] note-task.py works on missing tasks.json (creates empty)
- [ ] No external dependencies, Python 3.14+

## Test it before reporting

1. **Dry-run hook with no events**: temporarily move `tracker/claude-events.jsonl` aside (if exists), echo SessionStart input → hook exits 0 silently.
2. **Hook with events but all sessions estimated**: create fake tasks.json covering all session_ids → hook exits 0 silently (no subprocess spawn).
3. **note-task.py --pending**: add fake event with new session_id → command lists it.
4. **note-task.py --hours**: write override → tasks.json has `human_corrected_hours` set.
5. **summary.py с tasks**: add 2-3 fake task entries → summary shows Productivity block.
6. **summary.py без tasks**: empty tasks.json → no Productivity block.

DO NOT actually run `claude -p --bare` in tests — just verify the subprocess invocation is structured correctly. Real estimation will happen in production.

## Out of scope

- Cron daily fallback (Phase 1.3+)
- Turn-block detection (Phase 1.3+)
- Codex/openclaw equivalents (Phase 1.1, 1.2)
- Web UI / API for tasks.json (Phase 2+)

## Final report

Conform to `--output-schema`: `files_created`, `summary`, `tested`, `test_results`, `open_questions`, `deviations_from_spec`.
