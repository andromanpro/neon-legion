# Task: Apply i18n + UX fixes to page-multi-agent.php

Name: codex-developer
Profile: Codex CLI 0.128+ (gpt-5.5, xhigh reasoning, --sandbox workspace-write)
Goal: Применить fixes из review к `dashboard/page-multi-agent.php` (workspace копия). 20 i18n + 8 UX + 10 new TRANSLATIONS keys + ~6 HTML attribute patterns. Sandbox только workspace — обновляешь копию, architect синхронизирует обратно в WP theme на NAS.
Constraints: minimum diff (точечно), не ломать существующую функциональность (data-usd / TRANSLATIONS dict / fetch override / event handlers), сохранить existing keys которые корректны
Watches: `dashboard/page-multi-agent.php` (597 строк)
Produces: same file, modified

## Operational backstory

Sandbox `workspace-write` в `<project_root>`. Файл `dashboard/page-multi-agent.php` — копия из WordPress theme. После твоих изменений architect скопирует файл обратно в `<wp_theme>/page-multi-agent.php`.

## Working directory

`<project_root>`.

## Goal

Применить **все** fixes из review report ниже. **Не делать редизайн**, только текстовые / i18n изменения.

## Review findings (apply these)

### A. Translations updates в TRANSLATIONS.ru

| Key | New RU value |
|---|---|
| `status` | `ТРЕКЕР · ДЕМО-РЕЖИМ · ТЕСТОВЫЕ ДАННЫЕ` |
| `meta` | `62 ДНЯ · 79,7 ТЫС. СОБЫТИЙ · 3 ОСИ ЭКОНОМИИ` |
| `hero_sub` | `Self-hosted productivity-трекер для Claude Code, Codex CLI и openclaw. Считает три оси экономии: <code>$-saved</code> (vs API rates), <code>time-saved</code> (активные часы против оценки без ИИ) и <code>sentiment</code> (фрустрация / благодарность / мат по сессиям). Числа на странице — тестовые; полный live-дашборд работает локально на бэкенд-порту 8089.` |
| `p_savings` | `ЭКОНОМИЯ · СРАВНЕНИЕ С API` |
| `multi_sub` | `активные часы против оценки без ИИ` |
| `active_sub` | `паузы между сообщениями ≤ 2 мин считаются активным временем` |
| `p_budget` | `5-ЧАСОВОЙ БЮДЖЕТ · ПЕРЕРАСХОД` |
| `limit_max5x` | `лимит Max 5×` |
| `cache_tokens` | `кэш-токены вне лимита` |
| `vs_max20` | `% от лимита Max 20×` |
| `p_sentiment` | `ТОНАЛЬНОСТЬ · УРОВЕНЬ СТРЕССА` |

### Translations updates в TRANSLATIONS.en

| Key | New EN value |
|---|---|
| `meta` | `62 DAYS · 79.7K EVENTS · 3 SAVINGS AXES` (как было — оставить) |
| `hero_sub` | оставить как есть, **но** проверить что технические термины ОК (можно `pay-as-you-go API rates` вместо просто `API rates`) |
| `p_savings` | `SAVINGS · API COMPARISON` |
| `active_sub` | `gaps ≤ 2 min count as active time` |
| `p_budget` | `5-HOUR BUDGET · OVER LIMIT` |
| `limit_max5x` | `Max 5× cap` |
| `cache_tokens` | `cache tokens outside cap` |
| `vs_max20` | `% of Max 20× cap` |

### B. New TRANSLATIONS keys (add to BOTH dicts)

```javascript
// RU
status_live: 'ЛАЙВ',
unit_hours_short: ' ч',
mode_burst: 'ПЕРЕРАСХОД',
phase_short: 'Ф',
calls_unit: 'событий',
cap_max5x_title: 'лимит подписки Max 5×',
cap_max20x_title: 'лимит подписки Max 20×',
mood_stable: 'стабильно',
mood_calm: 'спокойно',
mood_frustrated_calm: 'фрустрация → спокойно',

// EN
status_live: 'LIVE',
unit_hours_short: ' h',
mode_burst: 'OVER LIMIT',
phase_short: 'P',
calls_unit: 'calls',
cap_max5x_title: 'subscription cap: Max 5×',
cap_max20x_title: 'subscription cap: Max 20×',
mood_stable: 'stable',
mood_calm: 'calm',
mood_frustrated_calm: 'frustrated → calm',
```

### C. HTML attribute changes

1. **Line ~324** — wrap LIVE в i18n:
```html
<span class="pid">04 · <span data-ma-i18n="status_live">LIVE</span></span>
```

2. **Line ~340** — wrap BURST:
```html
<span class="v hot" data-ma-i18n="mode_burst">BURST</span>
```

3. **Hours suffix** — каждое число с `ч` суффиксом обернуть unit:
```html
<!-- Was: <span class="v"><?php echo number_format( $productivity['active_hours'], 1 ); ?>ч</span> -->
<!-- Now: -->
<span class="v"><?php echo number_format( $productivity['active_hours'], 1 ); ?><span data-ma-i18n="unit_hours_short">ч</span></span>
```

Apply этот pattern ко **всем** местам где есть hardcoded `ч` суффикс (active hours / calendar hours / без-AI hours / hours saved / avg per day):

- multi panel (lines ~302-305): С ИИ hours, Без ИИ hours, hours saved
- active panel (lines ~313-318): main number, calendar span, avg/day

4. **Line ~390** — phase prefix:
```html
<span class="sp"><span data-ma-i18n="phase_short">P</span><?php echo esc_html( $s['phase'] ); ?></span>
```

5. **Mood labels** в session list — translation. У каждой session есть `mood_ru` / `mood_en` ИЛИ можно использовать `data-ma-i18n`:

Текущий код:
```php
$sessions = array(
    array( 'phase' => '3', 'desc_ru' => '...', 'desc_en' => '...', 'cost' => 0.51, 'mood' => 'stable' ),
    ...
);
```

Mood values: `stable`, `calm`, `frustrated→calm`. Нужно их переводить.

Лучший подход — map mood string → i18n key в HTML:
```html
<span class="mood" data-ma-i18n="mood_<?php echo str_replace( array( '→', ' ' ), array( '_', '_' ), $s['mood'] ); ?>"><?php echo esc_html( $s['mood'] ); ?></span>
```

Этот expression преобразует `'stable'` → `mood_stable`, `'frustrated→calm'` → `mood_frustrated_calm`. Default text — английский (или русский, тогда поменять initial value на `'стабильно'` etc).

6. **Line ~419** — calls unit:
```html
<span class="v">2547 <span data-ma-i18n="calls_unit">calls</span> (2026-05-09)</span>
```

7. **Cap marker titles** (lines ~334-335) — add `data-ma-i18n-title`:
```html
<div class="budget-cap-marker" style="left: 7.32%;" title="Max 5×" data-ma-i18n-title="cap_max5x_title"></div>
<div class="budget-cap-marker" style="left: 18.31%;" title="Max 20×" data-ma-i18n-title="cap_max20x_title"></div>
```

### D. JS update — add data-ma-i18n-title support

В функции `applyMaTranslations` добавить обработку `data-ma-i18n-title` атрибутов:

```javascript
// Title attribute translations
document.querySelectorAll('[data-ma-i18n-title]').forEach(function(el) {
  const key = el.dataset.maI18nTitle;
  if (dict[key]) el.title = dict[key];
});
```

Добавить ПОСЛЕ `[data-ma-i18n-text]` блока, ПЕРЕД currency conversion.

## Constraints

- НЕ удалять existing keys в TRANSLATIONS которые корректны
- НЕ менять структуру PHP блока (mock data array, fetch rate function)
- НЕ менять CSS / `<style>` блок
- НЕ менять `data-usd` атрибуты или currency conversion logic
- НЕ менять `getCurrentLang` / `i18n:applied` event listener
- Минимальный diff — только текстовые и attribute changes
- UTF-8 везде (BOM не добавлять)

## Acceptance criteria

- [ ] 12 RU keys обновлены
- [ ] 8 EN keys обновлены (где указано)
- [ ] 10 новых keys добавлены в **обоих** dicts
- [ ] HTML changes applied (LIVE, BURST, hour units, phase, calls, mood, cap titles)
- [ ] JS получил `data-ma-i18n-title` handler
- [ ] PHP синтаксис валидный (`php -l` clean)
- [ ] Файл UTF-8

## Test plan

**Codex (sandbox)**:
- [ ] PHP syntax check: `php -l dashboard/page-multi-agent.php`
- [ ] Проверка что обе dicts всё ещё имеют equal keys (символическая diff)
- [ ] Проверка что **все** `data-ma-i18n="X"` references есть в TRANSLATIONS
- [ ] Поиск hardcoded `BURST`, `LIVE`, `calls`, `Max 5x` в HTML body — не должно остаться

**Architect (host, after merge)**:
- Sync обратно в `<wp_root>/.../page-multi-agent.php`
- Curl `http://localhost:8080/multi-agent/` + проверить рендер

## Out of scope

- Изменение CSS / styles
- Изменение mock data values (numbers)
- Refactoring code structure
- Adding new ops-panels

## Final report

Conform to `--output-schema`. Required: `files_created` (один modified file), `summary`, `tested`, `test_results`.
