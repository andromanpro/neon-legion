# Task: Code review — Phase 1.3 estimator fix + today panel rework

Name: codex-reviewer
Profile: Codex CLI 0.128+ (gpt-5.5, xhigh reasoning, --sandbox read-only)
Goal: Independent review кучи fix'ов, накопленных architect'ом за последние 3 часа после Phase 3.5. Кода много, всё прошло только smoke-tests, нет независимого ревью.
Constraints: read-only, не править файлы, structured markdown report ≤ 700 слов
Watches:
  - `backend/server.py` (~1000 LOC, добавились _today_payload rework, _profanity_since, _productivity_block clamp)
  - `tracker/estimate-task.py` (oracle переключен с claude на codex CLI, profanity-merge logic)
  - `tracker/summary.py` (`summarize_productivity` like-with-like math)
  - `tracker/run-recent-estimates.py` (новый)
  - `tracker/backfill-profanity.py` (новый)
  - `dashboard/page-multi-agent.php` (multi-panel pro-rate fallback)

## Operational backstory

После Phase 3.5 пользователь сказал «данные неактуальные, я матерился сегодня — а там не показано». Architect диагностировал: estimator (Phase 1.3) падал на subprocess.run("claude") с WinError 2 на Windows + auth не пропагируется в child process. Переключил oracle на `codex exec` (ChatGPT-auth headless работает в subprocess). Параллельно нашёл ещё 5+ багов productivity math'а и today-windowing'а. Каждый чинил итеративно глядя на дашборд. **Без независимого ревью.**

User says «дай кодексу на проверку — мне пока не нравится». Найди реальные баги, edge cases, дизайн-проблемы.

## Working directory

`F:/WorkAI/multi-agent/` (--cd при запуске).

## Что изменилось — обзор

### 1. Estimator: claude → codex CLI

`tracker/estimate-task.py`:
- `_resolve_claude_cli` → `_resolve_codex_cli` (через `shutil.which`)
- `run_oracle()` теперь зовёт `codex exec --sandbox read-only --skip-git-repo-check --output-last-message <tmp>` с **prompt в stdin** (multi-line argv через codex.CMD shim ломается на Windows)
- `failure_entry()` принимает `profanity` параметр; profanity сохраняется ДО oracle-вызова
- `update_task_entry()` теперь делает merge (preserve previous fields), не replace

### 2. Like-with-like productivity math

`tracker/summary.py:summarize_productivity`:
- Раньше: `active_hours_with_ai = active_time_hours(events, ...)` поверх ВСЕХ событий, `hours_without_ai` суммировался только по сессиям с baseline_hours → апples-to-oranges
- Теперь: «covered» сессии = с реальным `effective_task_hours()`, active_hours считается только по их событиям

### 3. Today = с полуночи (не 24h sliding)

`backend/server.py:build_wp_snapshot`:
```python
since_today = datetime.combine(current.date(), datetime.min.time(), tzinfo=current.tzinfo)
events_24h = read_recent_events(since_today, current)
```

### 4. Today.profanity — re-scan transcripts since midnight

`backend/server.py:_profanity_since(transcript_path, since_dt)`:
- Открывает JSONL transcript, фильтрует `type=="user"` + `timestamp >= since_dt`, считает swear-pattern matches
- `_today_payload` for each `sessions_recent` берёт `tasks[sid].transcript_path` и зовёт `_profanity_since`
- Fallback к per-session count если `since_dt is None` или transcript не найден

### 5. Multi-panel pro-rate fallback

`dashboard/page-multi-agent.php`:
- Удалены `data-today-hours` с элементов `data-multi-input="with-ai"`, `data-snap-without-ai`, `data-multi-input="saved"`
- Comment: «Multi panel uses pro-rate (no data-today-hours override): для коротких периодов как today per-session AI baseline >> few minutes of fresh activity → артефактные ratios ×170»
- JS `applyTodayToTodayAttrs` hourMap теперь только `snap-active-hours` + `snap-calendar-hours`

### 6. Productivity clamp

`backend/server.py:_productivity_block`:
- Раньше: coverage threshold 50%
- Теперь: только sanity check `multiplier_raw >= 1` AND `saved_raw >= 0` (since like-with-like math гарантирует consistency на любом N)

### 7. Backend как scheduled task (отключено)

`backend/run-snapshot-writer.cmd` + Register-ScheduledTask `MultiAgentSnapshotWriter` at logon. **User отключил**: `Disable-ScheduledTask`. Manual run: `--snapshot-once`.

### 8. Backfill scripts

- `tracker/backfill-profanity.py` — local-only profanity counting для всех сессий (no oracle)
- `tracker/run-recent-estimates.py` — лимитированный (5 сессий) full estimation через codex

## Review focus

### A. Math correctness

1. `summarize_productivity` like-with-like: верно ли что `active_hours_with_ai` теперь точно соответствует множеству `covered_session_ids`? Что если события одной сессии "выходят" за пределы её `session_ranges` time-frame (не должно, но проверь)?
2. `_profanity_since`: парсинг ISO timestamp с "Z" suffix через `datetime.fromisoformat(ts.replace("Z", "+00:00"))` — какие edge cases (UTC offset с дробями, naïve vs aware comparison с `since_dt`)?
3. `_productivity_block`: `if multiplier_raw < 1` — корректно clamp-ит когда estimates пустые (multiplier=0)?
4. `_today_payload` `active_hours_for_estimate` — что если `estimated_session_ids` непустой но события этих сессий ВЫШЛИ за `events_24h` window (сессия с last_ts вчера, но baseline есть)? `covered_events` будет пустой → `active_hours_for_estimate=0`, может привести к div-by-zero в WP?

### B. Edge cases / failure modes

1. Empty transcript / corrupted JSONL: `_profanity_since` падает gracefully?
2. `_load_profanity_patterns`: cache global mutable — thread-safe? (server.py runs threading.ThreadingHTTPServer)
3. `run_oracle` через codex stdin: что если prompt > 1MB (огромный transcript)?
4. `update_task_entry` merge: что если previous имеет поле X со значением "value", new entry имеет X со значением None — затирает или сохраняет previous?
5. `run-recent-estimates.py` использует `subprocess.run([sys.executable, ...])` — а `sys.executable` это `pythonw.exe` или `python.exe` если запускать через `py.exe` launcher? subprocess в loop спавнит ещё python — потенциально каждый старт ~200ms overhead.

### C. Security / privacy

1. `_profanity_since` читает arbitrary `transcript_path` из `tasks.json[*].transcript_path` — что если кто-то подменит tasks.json и положит туда `C:/Windows/system32/config/SAM`? Path validation отсутствует.
2. Public mode (`--public`) применяет ли scrubbing к `top_session` корректно после моих изменений? (скан кода — `_today_payload` имеет ветку `if public_mode: top_session = _scrub_for_public(...)`)
3. WP-страница рендерит `top_session` через `textContent` — XSS защищён, но что про ZWJ / RTL chars в _sanitize_desc?

### D. Дизайн / UX

1. Multi-panel pro-rate fallback при period=today показывает all-period multi (например ×13.9). User сегодня сказал «явно меньше сделал» → expected меньшее значение. Pro-rate stable but не решает проблему.
2. `today.active_hours_for_estimate` exposed в snapshot но WP больше не использует (after Multi-panel pro-rate fallback). Удалять или оставить?
3. Mock multiplier 7.31 (PHP $productivity['multiplier']) теперь отличается от real ×13.9. Какой источник истины?
4. `run-recent-estimates.py` — limit=5 по transcript mtime (most recent first). Но «recent» по mtime файла ≠ «recent» по last_event_ts в tasks.json. Возможен mismatch.

### E. Деплоймент / эксплуатация

1. Cron disabled per user request. Но SessionStart hook (`hooks/claude-session-start.py`) тоже зовёт `estimate-task.py` async-фоном — он работает с новым codex-based oracle?
2. tasks.json после полугодовой эксплуатации может вырасти до 100k записей. `summarize_productivity` linear-scan tasks для каждого snapshot. Перформанс?
3. `_profanity_since` в snapshot regen — открывает 4-8 transcript-файлов. Размер ~1-5MB каждый? Время snapshot regen?

## Output format

```markdown
# Phase 1.3 review report

## A. Math issues
| # | Severity | Where | Issue | Fix |

## B. Edge cases
| # | Severity | Where | Issue | Fix |

## C. Security
| # | Severity | Where | Issue | Fix |

## D. Design / UX
| # | Severity | Where | Issue | Suggestion |

## E. Deployment
| # | Severity | Where | Issue | Fix |

## Summary
- Issues: HIGH=X, MED=Y, LOW=Z
- Top 3 fixes for architect
- Overall: ship-ready / needs fixes / blocked
```

≤ 700 слов structured.
