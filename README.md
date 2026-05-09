# multi-agent

Multi-agent SDLC: Claude Code (architect) + Codex CLI (developer) + openclaw (Telegram-bridge для mobile-режима).

## Roadmap

| Phase | Описание | Status |
|---|---|---|
| **1.0** | Claude Code tracking hook + summary ($ tracking) | ✅ done (2026-05-09) |
| **1.0.1** | Retroactive backfill — обход всех `~/.claude/projects/*/*.jsonl` за прошлые месяцы, дедупликация по `(session_id, message_uuid)` | pending |
| 1.1 | Codex CLI tracking layer | pending |
| 1.2 | openclaw call tracking | pending |
| 1.3 | Task complexity estimation (часы «как без ИИ») — для time-saved метрики | in progress |
| 1.4 | Sentiment / emotion tracking — frustration counter, маты, благодарности per session | pending |
| 2 | Aggregator backend ($ saved + hours saved + productivity multiplier + emotion index) | pending |
| 3 | Live cyberpunk dashboard (графики time/$ saved, big-number widget «×N» множитель) | pending |
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
└── docs/               # Архитектура, диаграммы
```

## Связанные репо

- [agent-tasks](http://nas.local:3000/androman/agent-tasks) — переименован в `multi-agent`, это он же
