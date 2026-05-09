# multi-agent — оркестрация Claude + Codex + openclaw

Pet-проект про peer-to-peer и Telegram-bridge оркестрацию между:

- **Claude Code** (Max-подписка) — роль архитектора (план, design, review)
- **Codex CLI** (ChatGPT-подписка) — роль разработчика (implementation, тесты)
- **openclaw** (Docker на NAS, OpenRouter под капотом) — Telegram-вход в mobile-режиме

## Топология

**Desktop (за компом):** peer-to-peer Claude ↔ Codex через `claude mcp serve` ↔ `codex mcp-server`. openclaw в стороне — его модели через OpenRouter не задействуются, две подписки уже оплачены.

**Mobile (Telegram через Клавбота):** Telegram → openclaw (NAS) → маршрутизирует на Claude/Codex MCP-серверы.

## Структура

| Папка | Что |
|---|---|
| `hooks/` | Claude Code hook'и; путь регистрируется абсолютным в `~/.claude/settings.json` |
| `tracker/` | JSONL-события и агрегаторы (для дашборда экономии) |
| `prompts/` | Role-промпты (architect/developer/reviewer) |
| `schemas/` | JSON-схемы для `codex exec --output-schema` |
| `docs/` | Архитектура, диаграммы |

## Roadmap

| Phase | Что |
|---|---|
| 1.0 | Claude Code tracking hook + summary ($ tracking) — done 2026-05-09 |
| 1.0.1 | Retroactive backfill из `~/.claude/projects/*/*.jsonl` (дедуп по session_id+message_uuid) |
| 1.1 | Codex CLI tracking |
| 1.2 | openclaw call tracking |
| 1.3 | Task complexity estimation (часы «как без ИИ») для time-saved метрики |
| 1.4 | Sentiment / emotion tracking per session (профанити, frustration, благодарности) |
| 2 | Aggregator backend ($ saved + hours saved + productivity multiplier + emotion index) |
| 3 | Live cyberpunk dashboard (time/$ saved графики, «×N» множитель big-number) |
| 4 | Public stats на androman.pro (wow-метрика «N дней сэкономлено») |
| 5 | Conversation graph viz (human↔AI как граф) |

## Метрики

Две оси экономии:

**$ saved** = сколько API стоил бы за токены минус pro-rated подписка. Уже считается в Phase 1.0.

**Time saved** = `complexity_hours_without_ai − wall_clock_hours_with_ai` (Phase 1.3 + 2). Где:
- `complexity_hours_without_ai` — оценка сложности задачи в человеко-часах (вариант: Claude/Codex выдаёт baseline в конце сессии, user корректирует через `tracker/note-task.py` или Telegram).
- `wall_clock_hours_with_ai` — `last_ts − first_ts` по `session_id` в JSONL (трекер уже это знает).
- `productivity_multiplier = complexity_hours_without_ai / wall_clock_hours_with_ai` — для wow-виджета.

Единица «задачи» = одна Claude Code session (`session_id` — естественная группировка). Если задача охватывает несколько сессий — group по тегу / Gitea Issue (Phase 1.3 решит).

## Конвенции

- **Issue в Gitea = задача.** Body содержит Goal, Deliverables, Acceptance Criteria, Out-of-scope, Workflow (architect→developer→reviewer→human).
- **Cost tracking** — JSONL в `tracker/`, локально (gitignore — не публикуем сырые данные с путями и метаданными).
- **Без `Co-Authored-By:` в commit messages** — пользователь единственный автор. Инструменты не авторы.
- **Trust boundaries обкатываются постепенно** — сначала human-approve каждой фазы; автоматизация цепочек только когда стабильно.

## Privacy hardening для Phase 4 (публикация на androman.pro)

OpenAI policy позволяет наш Codex headless под ChatGPT-auth (compliant в trusted private infra), но для публикации метрик на публичный блог — **обезличить данные**:

- ❌ Не публиковать сырые transcript'ы / output Codex'а verbatim
- ✅ Только агрегированные метрики (числа, графики, дашборды без raw payload)
- ✅ `working_dir` обезличить или хешировать (сейчас раскрывает FS-структуру + имена проектов; плюс там mojibake кириллицы который тоже надо чинить)
- ✅ `session_id` укоротить до short-hash при публикации, не raw UUID
- См. `reference_openai_codex_policy.md` в memory для деталей policy-research

## Внешние ссылки

- Gitea репо: http://nas.local:3000/androman/multi-agent
- openclaw на NAS: 192.168.1.130:8789 (Docker, alpine/openclaw:latest)
- Codex CLI: 0.128.0 (через npm, `C:/Users/Roono/AppData/Roaming/npm/codex.cmd`)

## Если ты Claude в новой сессии и работаешь над этим проектом

1. Прочитай `README.md` для текущего статуса фаз.
2. Активные задачи — в Gitea issues по лейблу `phase:*`.
3. Следуй конвенциям выше.
4. Не выдумывай фазы / архитектуру — сверяйся с roadmap и issues.
