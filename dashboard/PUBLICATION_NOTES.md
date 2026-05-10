# Multi-Agent Tracker — заметки к публикации / publication notes

Статус: подготовлено к ревью, не опубликовано.
Дата: 2026-05-10.

## Русская версия

### Обзор рынка

В нише "трекеров использования AI-инструментов для разработки" уже есть серьезные инструменты:

- `ccusage` отслеживает использование Claude Code и также содержит companion-анализаторы для Codex/OpenCode:
  https://github.com/ryoppippi/ccusage
- `claude-usage` — локальный дашборд для Claude Code с оценкой стоимости по API-тарифам и оговорками про подписки:
  https://github.com/phuryn/claude-usage
- `CodexBar` — macOS menu-bar монитор квот по нескольким coding-провайдерам:
  https://github.com/steipete/CodexBar
- `opencode-quota` добавляет панели квот/токенов и slash-команды прямо в OpenCode:
  https://github.com/slkiser/opencode-quota
- OpenRouter документирует встроенный учет использования: токены, стоимость, cache и reasoning:
  https://openrouter.ai/docs/guides/administration/usage-accounting

Вывод: проект не стоит позиционировать как "первый трекер токенов". Сильнее выглядит угол публичного кейса / личного ops-дашборда: телеметрия multi-agent workflow, ROI подписок, базовые оценки продуктивности и стресс-сигналы в одном snapshot, готовом для WordPress.

### Отличия

- Учитывает смешанный человеческий workflow: Claude Code, Codex, OpenClaw, OpenCode.
- Разделяет "кто думал" и "кто запустил другого агента".
- Показывает ROI подписок как API-эквивалент минус пропорциональная стоимость подписок.
- Считает productivity/sentiment только по orchestrator-сессиям Claude, чтобы не задваивать делегированную работу.
- WordPress-страница готова к публикации как почти статический snapshot, а не только как локальный TUI.

### Проверка приватности

Перед публичной публикацией:

1. Сгенерировать snapshot в публичном режиме:

   ```cmd
   py -3.14 backend\server.py --snapshot-once --public --snapshot-path "H:\wordpress-androman\wp-data\wp-content\uploads\multi-agent\snapshot.json"
   ```

2. Просканировать snapshot на локальные пути, имена пользователей, идентификаторы и приватные названия рабочих папок:

   ```cmd
   py -3.14 tools\privacy-scan-snapshot.py
   ```

3. Вручную проверить:

   - `today.top_session`
   - `sessions[].desc`
   - `providers[].origins[]`
   - HTML, отрендеренный WordPress

4. Не публиковать промпты, ответы, пути к transcript-файлам, raw session ids, Telegram ids, API keys, email-адреса или локальные пути файловой системы.

### Текущие блокеры публикации

- Непосредственно перед публикацией нужно заново сгенерировать public snapshot с `--public`.
- Описания сессий являются краткими task summaries; после public scrub они не должны содержать персональные данные, но все равно требуют ручного просмотра.
- Методология теперь объяснена на странице, но в статье/посте вокруг дашборда нужно явно сказать: "сэкономленные часы" — это оценка, а не лабораторно точное доказательство продуктивности.

## English Version

Status: prepared for review, not published.
Date: 2026-05-10.

### Market Scan

There are already serious tools in the "AI coding usage tracker" space:

- `ccusage` tracks Claude Code usage and also has Codex/OpenCode companion analyzers:
  https://github.com/ryoppippi/ccusage
- `claude-usage` is a local Claude Code dashboard with API-price estimates and subscription caveats:
  https://github.com/phuryn/claude-usage
- `CodexBar` is a macOS menu-bar quota monitor across many coding providers:
  https://github.com/steipete/CodexBar
- `opencode-quota` adds quota/tokens panels and slash commands directly inside OpenCode:
  https://github.com/slkiser/opencode-quota
- OpenRouter documents built-in usage accounting with token counts, cost, cache, and reasoning details:
  https://openrouter.ai/docs/guides/administration/usage-accounting

Conclusion: this should not be positioned as "the first token tracker". The stronger angle is a public case study / personal ops dashboard: multi-agent workflow telemetry, subscription ROI, productivity baselines, and stress signals in one WordPress-ready snapshot.

### Differentiators

- Tracks a mixed human workflow: Claude Code, Codex, OpenClaw, OpenCode.
- Separates "who thought" from "who launched another agent".
- Shows subscription ROI as API equivalent minus prorated subscriptions.
- Keeps productivity/sentiment on Claude orchestrator sessions to avoid double-counting delegated work.
- WordPress page is publishable as a static-ish snapshot, not only a local TUI.

### Privacy Checklist

Before public publishing:

1. Generate snapshot with public mode:

   ```cmd
   py -3.14 backend\server.py --snapshot-once --public --snapshot-path "H:\wordpress-androman\wp-data\wp-content\uploads\multi-agent\snapshot.json"
   ```

2. Scan snapshot for local paths, usernames, IDs, and private workspace names:

   ```cmd
   py -3.14 tools\privacy-scan-snapshot.py
   ```

3. Manually review:

   - `today.top_session`
   - `sessions[].desc`
   - `providers[].origins[]`
   - WordPress rendered HTML

4. Do not publish prompts, responses, transcript paths, raw session ids, Telegram ids, API keys, emails, or local filesystem paths.

### Current Publication Blockers

- Public snapshot should be regenerated with `--public` immediately before publishing.
- Session descriptions are task summaries; they are not personal data after public scrub, but still deserve a human read-through.
- The page explains methodology now, but the article/post around it should say clearly that "saved hours" is an estimate, not laboratory-grade productivity proof.
