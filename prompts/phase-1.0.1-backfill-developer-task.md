# Task: Phase 1.0.1 — Retroactive backfill из ~/.claude/projects/

You are the **developer** role. The architect (Claude) wrote this plan. Implement it and report via `--output-schema`.

Working directory: `<project_root>` (already your `--cd`).

Project context: read `CLAUDE.md` (project conventions, Phase 1.0 done с Stop hook, Phase 1.3 done с SessionStart hook). Phase 1.0.1 — retroactive backfill всех прошлых Claude Code сессий из `~/.claude/projects/<encoded-cwd>/<session_id>.jsonl`, чтобы у трекера сразу была история за все месяцы работы пользователя.

## Goal

Создать `tracker/backfill.py` — CLI tool который сканирует все transcript'ы Claude Code на машине и бэкфиллит `tracker/claude-events.jsonl` с полной историей (с дедупликацией). После запуска `tracker/summary.py --days 30` (или больше) должен показать реальную картину работы пользователя за всё время.

## Deliverable

**Один файл**: `tracker/backfill.py`

CLI:
```
py -3.14 tracker/backfill.py [--from-date YYYY-MM-DD] [--to-date YYYY-MM-DD] [--scan-dir <path>] [--dry-run] [--verbose]
```

Defaults:
- `--scan-dir`: `~/.claude/projects` (если не передан)
- `--from-date`: без ограничения (всё что есть)
- `--dry-run`: без флага — реальная запись; с флагом — только отчёт без модификации файлов

## Алгоритм

1. Read existing `tracker/claude-events.jsonl` → set `seen = {(session_id, message_uuid)}`. Если файла нет — `seen = set()`.
2. Find all transcript files: `glob` по `<scan-dir>/*/*.jsonl` (две глубины).
3. Для каждого transcript file:
   a. Извлечь `session_id` из имени файла (basename без `.jsonl`)
   b. Восстановить `working_dir` из имени parent dir (см. ниже про decoding)
   c. Streaming-iterate транскрипт построчно:
      - Skip битые JSON lines (try/except, log в stderr если `--verbose`)
      - Filter `event.type == "assistant"` AND `event.message.usage` (тот же критерий что Phase 1.0 hook)
      - Извлечь `message_uuid` (`event.uuid`), `model`, `usage` (input/output/cache_read/cache_creation), `stop_reason`, `ts` (`event.timestamp` — ISO 8601)
      - Apply `--from-date` / `--to-date` filter если указаны
      - Skip if `(session_id, message_uuid) in seen`
      - Иначе append candidate event к буферу
4. Sort всех новых events по `ts`
5. Append к existing JSONL атомарно (через tempfile + `os.replace`):
   - Read existing content (если есть)
   - Append new lines в конец
   - Write через temp + replace
6. Update `tracker/.last-uuids.json`: для каждого session_id с новыми events — записать последний (по ts) message_uuid. Не перезаписывать если уже есть и был более поздний uuid.
7. Print отчёт в stdout (markdown):
```
## Backfill report

- **Scanned**: 142 transcript files in `<scan-dir>`
- **Total assistant events with usage**: 8,341
- **Already in tracker**: 8,289 (skipped as duplicates)
- **New events appended**: 52
- **Date span of new events**: 2026-03-15 .. 2026-05-09
- **Total cost added (estimated)**: $14.73
- **Sessions touched**: 47

If `--dry-run`: reports same numbers, but **no files modified**.
```

## Pricing table

Re-use из `hooks/claude-track-calls.py` (тот же словарь, та же формула). НЕ дублировать словарь — **import** через relative path:

```python
import sys
HOOK_DIR = Path(__file__).resolve().parents[1] / "hooks"
sys.path.insert(0, str(HOOK_DIR))
# Теперь можно использовать функции из claude-track-calls
```

Или, если import проблематичен (`-` в имени модуля) — `import importlib.util`:

```python
import importlib.util
hook_path = Path(__file__).resolve().parents[1] / "hooks" / "claude-track-calls.py"
spec = importlib.util.spec_from_file_location("claude_track_calls", hook_path)
hook_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook_mod)

# Use: hook_mod.PRICING, hook_mod.estimate_cost(...)
```

Преимущество — одно место правды для pricing. Если в hook'е обновим цены — backfill автоматически подхватит.

## working_dir восстановление из encoded directory name

Claude Code кодирует cwd в имя директории заменяя:
- `:` → `--`
- `\` → `-` (Windows)
- `/` → `-` (Unix)

Пример: `<project_root>` → `F--WorkAI-multi-agent`

Decoder:
```python
def decode_project_dir(name: str) -> str:
    # Best-effort — exact reverse не возможен (несколько `-` могут быть из разных источников)
    # Простая heuristic: первый `--` → `:`, остальные `-` → `/` (или `\`)
    if "--" in name:
        head, _, tail = name.partition("--")
        # head — drive letter (F), tail — путь с '-' separators
        return f"{head}:/{tail.replace('-', '/')}"
    return name.replace("-", "/")
```

Это эвристика, может ошибаться (если в имени директории были сами `-` символы). Для отчёта это OK — в JSONL запишем что получилось. Если decoding fails — оставить raw encoded name + флаг `working_dir_raw: true`.

## Output JSONL schema

Тот же что Phase 1.0 hook (для consistency):

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
  "working_dir": "<workspace>",
  "tool_uses": 3,
  "stop_reason": "end_turn",
  "backfilled": true
}
```

`backfilled: true` — флаг отличия от live-events. Полезно потом фильтровать.

`ts` берётся из `event.timestamp` транскрипта (исторически правильное время), не текущее время.

`tool_uses` — count `tool_use` блоков в `event.message.content`.

## Constraints

- **Streaming**: не загружать transcript целиком в память. Iterate построчно.
- **Atomic write**: tempfile + `os.replace` для финального merge JSONL
- **Idempotent**: повторный запуск не дублирует (set-based dedup)
- **UTF-8** I/O везде
- **Stdlib only**: json, glob, pathlib, datetime, argparse, sys, os, importlib.util
- **Performance**: 100+ transcript файлов, каждый возможно 50-500MB — должно работать <2 минут
- **Robustness**: битый JSONL line, missing fields, missing usage block — skip с silent continue (или verbose-log если флаг)

## Acceptance criteria

- [ ] `tracker/backfill.py` exists
- [ ] CLI args работают: `--from-date`, `--to-date`, `--scan-dir`, `--dry-run`, `--verbose`
- [ ] Streaming чтение transcript'ов (не загружает целиком)
- [ ] Pricing импортируется из `hooks/claude-track-calls.py` (single source of truth)
- [ ] Дедупликация по `(session_id, message_uuid)` через сравнение с existing JSONL
- [ ] Атомарная запись JSONL через tempfile + os.replace
- [ ] Update `.last-uuids.json` корректно (последний uuid per session)
- [ ] working_dir восстанавливается из encoded имени директории (best-effort)
- [ ] `--dry-run` не модифицирует файлы, но выдаёт ту же статистику
- [ ] Отчёт в markdown формате
- [ ] Стdlib only, Python 3.14+

## Test it before reporting

1. Создай fake transcript directory `F:/temp/fake-projects/F--temp-fake/abc-123.jsonl` с 5 assistant events с usage
2. `py -3.14 tracker/backfill.py --scan-dir F:/temp/fake-projects --dry-run` → выводит отчёт без модификаций
3. Реальный run на этом же fake dir → JSONL получает 5 новых строк
4. Повторный run → 0 новых (deduped)
5. С `--from-date` filter → только подходящие events
6. Cleanup fake-projects после тестов

DO NOT тестировать на реальном `~/.claude/projects/` — реальный backfill будет запускаться пользователем явно.

## Out of scope

- Multi-process параллельная обработка (можно потом)
- Sentiment analysis (Phase 1.4)
- Codex/openclaw transcripts (Phase 1.1, 1.2)
- UI / web report

## Final report

Conform to `--output-schema`: `files_created`, `summary`, `tested`, `test_results`, `open_questions`, `deviations_from_spec`.
