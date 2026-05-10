# multi-agent

Multi-agent SDLC: Claude Code (architect) + Codex CLI (developer) + openclaw (Telegram-bridge для mobile-режима).

## Roadmap

| Phase | Описание | Status |
|---|---|---|
| **1.0** | Claude Code tracking hook + summary ($ tracking) | ✅ done (2026-05-09) |
| **1.0.1** | Retroactive backfill — обход всех `~/.claude/projects/*/*.jsonl` за прошлые месяцы, дедупликация по `(session_id, message_uuid)` | pending |
| 1.1 | Codex CLI tracking layer | ✅ done (2026-05-10) |
| 1.2 | openclaw call tracking | pending |
| 1.3 | Task complexity estimation (часы «как без ИИ») — для time-saved метрики | in progress |
| 1.4 | Sentiment / emotion tracking — frustration counter, маты, благодарности per session | pending |
| 2 | Aggregator backend ($ saved + hours saved + productivity multiplier + emotion index) | ✅ done |
| 3 | Live cyberpunk dashboard (графики time/$ saved, big-number widget «×N» множитель, **5h-budget remaining widget real-time**) | ✅ done |
| **3.5** | WP-page ↔ live backend через JSON-snapshot pipeline (без портов наружу) | ✅ done (2026-05-09) |
| 4 | Public stats на androman.pro (wow-метрика «N дней сэкономлено за месяц») | pending |
| 5 | Conversation graph viz (human↔AI как граф) | pending |
| 6 | AR overlay (Xreal/Quest/Vision Pro) — синий неон-HUD статуса агентов в углу зрения, голосовые команды через гарнитуру: «start phase X», «status», «merge PR» — управление multi-agent системой не отрываясь от повседневных дел | future / sci-fi |
| 7 | Productization track — competitor research, market validation, packaging (open-source release / SaaS / hybrid), pricing model, onboarding, distribution. Цель: продаваемый продукт для AI productivity audience | research |

## Активные задачи

См. issues в Gitea: http://nas.local:3000/androman/multi-agent/issues

## Структура каталогов

```
multi-agent/
├── hooks/              # Claude Code hook'и (регятся в ~/.claude/settings.json)
├── tracker/            # JSONL события + агрегаторы (data в gitignore)
├── prompts/            # Role-промпты (architect/developer/reviewer)
├── schemas/            # JSON-схемы для codex exec --output-schema
├── tools/              # Локальные bridge/wrapper-утилиты
└── docs/               # Архитектура, диаграммы
```

## Связанные репо

- [agent-tasks](http://nas.local:3000/androman/agent-tasks) — переименован в `multi-agent`, это он же

## Phase 1.1 — Codex CLI tracking layer

Codex CLI пока не имеет hook-системы как Claude Code, поэтому трекинг идёт через wrapper:

```bash
py -3.14 F:/WorkAI/multi-agent/tracker/codex-track.py exec --sandbox read-only "Output JSON {\"ok\":1}"
```

Wrapper принудительно добавляет `--json` для `codex exec`, стримит stdout/stderr как native `codex exec --json`, парсит `thread_id` и финальный `usage`, затем пишет событие в:

```text
tracker/codex-events.jsonl
```

Для удобства можно вызывать cmd-обёртку:

```cmd
F:\WorkAI\multi-agent\tracker\codex-track.cmd exec --sandbox read-only "prompt"
```

Или добавить alias в shell-профиль:

```bash
alias codex='py -3.14 F:/WorkAI/multi-agent/tracker/codex-track.py'
```

Важно: `summary.py` и snapshot объединяют token/cost/calls по Claude + Codex, но task/productivity/sentiment метрики остаются на Claude-сессиях из `tasks.json`, чтобы не дублировать работу, которую Codex забрал у Claude orchestration.

## openclaw ↔ Codex bridge для F:\WorkAI

openclaw работает в Docker на NAS и не видит Windows-диск `F:\WorkAI` напрямую. Для безопасного первого слоя настроен read-only bridge через его workspace:

```text
H:/openclaw/workspace/codex-bridge/
├── inbox/      # openclaw кладёт JSON-запросы
├── outbox/     # Codex/Windows пишет JSON-ответы
├── archive/    # обработанные запросы
└── README.md   # формат запросов
```

Watcher на Windows:

```cmd
F:\WorkAI\multi-agent\tools\run-openclaw-codex-bridge.cmd
```

Поддержанные действия: `ping`, `list`, `read`, `rg`, `git_status`, `handoff_to_codex`, `codex_exec`, `codex_status`, `codex_cancel`. Прямые file actions read-only и не читают типовые секреты. `codex_exec` запускает headless Codex CLI на Windows под `F:\WorkAI` в sandbox `read-only`; режим `workspace-write` требует явного `allow_workspace_write: true`, а `danger-full-access` через openclaw-bridge запрещён.

## Phase 3.5 — snapshot pipeline (WP ↔ backend)

Backend на Windows (порт 8089, только localhost) пишет JSON-snapshot раз в 15 мин в SMB-mount NAS-директории. WP-страница `/multi-agent/` тянет файл через стандартный uploads URL — оба сервиса остаются в LAN, ничего наружу не торчит.

### Запуск backend с writer'ом

```bash
# Из F:/WorkAI/multi-agent/
py -3.14 backend/server.py \
  --port 8089 \
  --snapshot-path "H:/wordpress-androman/wp-data/wp-content/uploads/multi-agent/snapshot.json" \
  --snapshot-interval 900 \
  --snapshot-days 62
```

`--snapshot-once` — записать snapshot и выйти (для cron / smoke-теста):

```bash
py -3.14 backend/server.py --snapshot-once \
  --snapshot-path "H:/wordpress-androman/wp-data/wp-content/uploads/multi-agent/snapshot.json"
```

### Privacy hardening для Phase 4

Для публикации того же snapshot на `androman.pro` включай `--public`. В этом режиме `session_id_short` больше не берётся из первых символов UUID: backend пишет стабильный 8-hex hash от `session_id` с локальным salt. Это сохраняет группировку сессий внутри публичного snapshot, но не раскрывает исходный UUID-prefix.

Salt создаётся автоматически в `~/.multi-agent-snapshot-salt`, если файла ещё нет: 32 random bytes, атомарная запись через tmp + replace, права `0600` на POSIX. Этот файл не коммитить и не публиковать: публичный snapshot содержит только salt-hash, но приватность держится на том, что salt остаётся локальным.

Также `--public` scrub'ит текстовые поля `sessions[].desc` и `today.top_session`: абсолютные Windows/Unix пути, email-адреса, похожие на секреты токены и имена клиентов из blocklist-файла.

```bash
py -3.14 backend/server.py --snapshot-once \
  --snapshot-path "H:/wordpress-androman/wp-data/wp-content/uploads/multi-agent/snapshot-public.json" \
  --public \
  --customers-blocklist "F:/WorkAI/private/customer-blocklist.txt"
```

Формат `--customers-blocklist`: одно имя на строку, пустые строки игнорируются, строки с `#` считаются комментариями.

```text
Сфера
3Лоджик
# comment
```

### Топология

```
[Claude Code session] → claude-track-calls.py → tracker/claude-events.jsonl
[Codex CLI exec] ────→ codex-track.py ───────→ tracker/codex-events.jsonl
                                                       │
                                                       ▼
[backend/server.py @ localhost:8089] ── читает события, агрегирует
                                                       │
                                                       ▼
                  каждые 15 мин → snapshot.json (атомарная запись)
                                                       │
                       /wp-content/uploads/multi-agent/snapshot.json
                       (SMB-mount H:/, NAS видит как обычный uploads-файл)
                                                       │
                                                       ▼
[WP page-multi-agent.php @ NAS:8080] ── fetch на load → JS overrides PHP-baked
```

### Что в snapshot есть

`totals` · `providers` · `productivity` · `budget` · `sentiment` · `today` · `models[]` · `sessions[]` · `timeline_weights[62]` · `generated_at`

### WP-page fallback

Если snapshot.json отсутствует или недоступен — страница работает на PHP-baked mock'ах, бейдж в шапке показывает «ДЕМО · моки». При наличии snapshot:
- `LIVE · обновлено HH:MM` — снимок свежее 30 мин
- `СНИМОК HH:MM` — старше

Per-field fallback: scalar fields с реальным значением `> 0` overridable; multiplier=0 (Phase 1.3 estimation не отработала) → остаётся PHP-baked ×7.3 чтобы UX не ломался.
