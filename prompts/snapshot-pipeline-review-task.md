# Task: Code review — Phase 3.5 snapshot pipeline (backend writer + WP fetch)

Name: codex-reviewer
Profile: Codex CLI 0.128+ (gpt-5.5, xhigh reasoning, --sandbox read-only)
Goal: Аудит кода Phase 3.5 — snapshot pipeline между backend (port 8089) и WP-страницей. Architect (Claude) написал реализацию за один заход без code review. Найти баги, edge cases, security issues, breakage потенциал.
Constraints: read-only, не править файлы, structured markdown report ≤ 800 слов
Watches:
  - `backend/server.py` (~700 LOC, добавлены build_wp_snapshot, snapshot_loop, CLI args)
  - `dashboard/page-multi-agent.php` (~1100 LOC, добавлены loadSnapshot, applySnapshot, rebuildTimelineFromWeights)

## Operational backstory

User в Windows-окружении, backend живёт на 127.0.0.1:8089 (только localhost), WordPress — на NAS (192.168.1.130:8080) через SMB-mount H:/. Snapshot.json пишется backend'ом каждые 15 мин атомарно (tmp + os.replace) в NAS uploads, WP-страница fetch'ит на load. User эксплицитно сказал «ничего наружу не торчит».

Architect только что завершил эту фазу одним заходом без независимого review. Smoke-test прошёл (snapshot.json записан, page рендерится, fetch работает), но **detail bugs** не проверены.

## Working directory

`F:/WorkAI/multi-agent/` (--cd).

## Зачем Codex'у

Я (Claude/architect) могу пропустить:
- Race conditions в snapshot writer (atomic write vs concurrent reads — у нас атомарность через rename, но Windows FS quirks?)
- Memory/perf issues при больших snapshot'ах (62-day timeline, 8 sessions, models[])
- DOM-ID коллизии в applySnapshot — мои querySelector'ы могут смотреть на wrong element
- Edge cases: snapshot has fields с null/undefined/0 vs missing — обрабатывает ли мой код всё корректно
- PHP/JS escaping: данные из snapshot текут в HTML; risk XSS если description содержит `<script>` или подобное
- I18n breakage: status badge может терять текст при language switch
- Backward compat: existing data-base-* атрибуты могут переопределяться неполноценно (например, multi-input markers + base-hours и пр.)

## Что review'ить (структура отчёта ниже)

### A. Backend (backend/server.py)

1. `build_wp_snapshot()` — все None-safe? `rounded()` теперь принимает None, но что насчёт `int(None)`?
2. `_today_payload()` — что если `events_24h` пустой (свежий установка, нет событий)?
3. `_models_with_pct()` filter `_SYNTHETIC_MODELS` — список достаточный? Не пропускает ли реальные модели с пустым/whitespace name?
4. `_sanitize_desc()` regex/markers — corner cases (text 'estimation' внутри валидного описания, без 'failed')?
5. `write_snapshot()` — atomic write на Windows: `os.replace()` корректно атомарен на NTFS? Что если directory не существует (`mkdir parents=True` есть, но permissions?)?
6. `snapshot_loop()` — `stop_event.wait(interval)` корректно прерывается на Ctrl+C? Race с writing если Ctrl+C во время `write_snapshot`?
7. CLI args: `--snapshot-once` без `--snapshot-path` падает с exit(2) — OK; что если path указывает в директорию, не файл? (mkdir parents может создать file path как dir)
8. Default port 8080 — collision с WP NAS:8080? (на разных хостах, но на той же машине запуск на 8080 при cron?)

### B. WP page (page-multi-agent.php)

1. `wp_upload_dir()` returns array — `trailingslashit($upload_dir['baseurl'])` — что если baseurl пустой/null (multisite)?
2. `loadSnapshot()` — `fetch(SNAPSHOT_URL, { cache: 'no-store' })` — CORS? page и snapshot на одном origin, не должно быть проблем, но проверить
3. `applySnapshot()` — что если snapshot имеет лишние поля (forward-compat)?
4. `rebuildTimelineFromWeights()` — длина массива 62 vs PHP-baked 62: при разнице (snapshot 30 weights) — DOM rebuild через `innerHTML = ''` и пересоздание; теряется ли что-то?
5. **XSS!** `topSession` приходит из snapshot и идёт в `topSession.textContent = t.top_session` — textContent безопасен, но если description содержит unicode RTL/zero-width, может ломать UI?
6. `setSnapshotStatus()` — labels в `el.textContent` — корректно при language switch? Я добавил `ds_loading/ds_live/ds_stale/ds_demo` keys в обе dicts — проверить что после `i18n:applied` пересчитывается badge.
7. `applyPeriod()` re-call после snapshot — multi-display и ratio-display recalc: что если snapshot productivity.multiplier > 0 — будет ли derived формула противоречить snapshot value?
8. `data-multi-input="with-ai"` selector targets `.p-multi .big-number-detail .v:nth-of-type(1)` (косвенно через data attr) — но в `applySnapshot` я делаю `document.querySelectorAll('[data-multi-input="with-ai"], .p-active [data-base-hours]')` — может ли `nth-of-type` selector найти 2 элемента и сломать?

### C. Integration

1. Backend записал snapshot, потом WP page открылась через 5 минут: badge показывает «ЛАЙВ» (≤ 30 мин), но если backend упал и не пишет 1 час — badge должен переключиться в «СНИМОК». Проверить age-вычисление.
2. Если backend пишет в момент когда WP читает: atomic? `os.replace` на NTFS должен быть atomic, но fetch может read tmp вместо final?
3. Localization: today.top_session приходит на русском (например «Этап 5 ИИкона») — escape OK? Прямо в `textContent` — должно быть OK.

### D. Privacy / leak в snapshot

1. `session_id_short` — это первые 8 символов UUID. Privacy концерн? (CLAUDE.md проекта: «обезличить session_id, working_dir»)
2. `desc` приходит из tasks.json `brief_description` — может содержать имена файлов / paths из conversation? Проверить.
3. `sessions[]` в snapshot — не публикуется на сайт (страница загружает но не отображает sessions массив сразу), но лежит в JSON открыто на NAS. Acceptable для local-only?

### E. Out-of-scope (не рассматривать)

- Performance scaling beyond 1k sessions/day
- Multi-server replication
- snapshot.json compression (file ~4kb сейчас)
- Phase 4 (publishing на androman.pro production)

## Output format

```markdown
# Phase 3.5 review report

## A. Backend issues

| # | Severity | Where | Issue | Suggested fix |
|---|---|---|---|---|
| 1 | HIGH | server.py:LXXX | ... | ... |
| 2 | MED  | ... | ... | ... |
...

## B. WP page issues

| # | Severity | Where | Issue | Suggested fix |
|---|---|---|---|---|

## C. Integration issues

...

## D. Privacy issues

...

## Summary

- Total issues: N (HIGH=X, MED=Y, LOW=Z)
- Top 3 fixes I'd apply first:
  1. ...
  2. ...
  3. ...
- Overall: ship-ready / needs fixes / blocked
```

≤ 800 слов structured. Architect применит P0/P1 fixes по списку.
