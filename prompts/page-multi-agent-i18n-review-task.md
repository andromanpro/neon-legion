# Task: i18n + UX review для page-multi-agent.php

Name: codex-reviewer
Profile: Codex CLI 0.128+ (gpt-5.5, xhigh reasoning, --sandbox read-only)
Goal: Найти все недопереведённые тексты + неясные UX элементы в WordPress page template multi-agent dashboard'а. **Не редактировать файл** — отдать structured markdown report с конкретными fixes (line numbers + suggested replacements). Architect применит fixes.
Constraints: read-only sandbox, no file modifications, focus on text completeness и UX clarity, не на code style
Watches: `<project_root>/dashboard/page-multi-agent.php` (копия из WordPress theme в workspace для sandbox access)

## Operational backstory

You are the **reviewer** role в multi-agent flow (Claude=architect created the page, you're auditing). Sandbox `read-only` — ничего не пишешь. Output — markdown report в `--output-last-message`.

User feedback: «многое не переведено, часть элементов непонятна». Найди именно эти проблемы.

## Working directory

`<project_root>` (--cd). Файл для review: **`dashboard/page-multi-agent.php`** (597 строк, копия из WordPress theme).

ВАЖНО: this is **WordPress page template** (не single-file standalone HTML `dashboard/index.html`!). Файл содержит PHP блок (mock data + CBR rate fetch), inline `<style>`, HTML markup с `data-ma-i18n` / `data-ma-i18n-html` / `data-ma-i18n-text` / `data-usd` атрибутами, и финальный `<script>` с inline TRANSLATIONS dict (RU + EN) + JS для применения переводов и currency conversion.

Не путать с `dashboard/index.html` — это другой файл (standalone live dashboard для backend, не для WP).

## Goal

Page `page-multi-agent.php` отображает demo dashboard на WordPress сайте. Двуязычный (RU default, EN через `?lang=en`). Должны переключаться **все** тексты. Должны быть **понятны** все элементы и подписи.

User feedback `«многое не переведено»` — нужен audit:
1. Все текстовые фрагменты (тексты, labels, units) имеют `data-ma-i18n="key"` атрибут
2. Каждый key есть и в `TRANSLATIONS.ru` и в `TRANSLATIONS.en`
3. Нет текста смешанного типа (например, английских units внутри русского label)

User feedback `«часть элементов непонятна»` — UX audit:
1. Технические термины без объяснения (например `BURST MODE`, `Max 5x`, `Max 20x`, `≤2min gaps` — что это, почему важно?)
2. Аббревиатуры без расшифровки (`vs max 20x`, `cache tokens (отдельно)` — какой context?)
3. Числа без units / контекста
4. Headers секций которые непонятны (`SENTIMENT · STRESS METER` — нужно ли объяснение?)

## Task

Прочесть `page-multi-agent.php` целиком. Сделать audit двух категорий issues. Выдать structured markdown report.

## Output format

```markdown
# i18n + UX review report

## A. Missing/incomplete translations

| # | Line | Current text (RU) | EN translation status | Fix |
|---|---|---|---|---|
| 1 | 234 | «BURST MODE» | hardcoded EN, не в dict | Перенести в TRANSLATIONS как `mode_burst: 'BURST MODE'` |
| 2 | 198 | «токенов использовано» | OK через data-ma-i18n='tokens_used' | — |
| ... | | | | |

## B. UX clarity issues

| # | Line | Element | Problem | Suggested fix |
|---|---|---|---|---|
| 1 | 280 | `cache tokens (отдельно)` | непонятно от чего отделено и почему | Добавить tooltip или подзаголовок: «не считаются в rate-limit budget» |
| 2 | 245 | `Max 5x · Max 20x` markers | пользователь не знает что это tier подписки | Добавить tooltip на cap-marker: «лимит подписки Max 5×» |
| ... | | | | |

## C. Specific TRANSLATIONS dict additions

```javascript
// Add to TRANSLATIONS.ru:
new_key_1: '...',
new_key_2: '...',

// Add to TRANSLATIONS.en:
new_key_1: '...',
new_key_2: '...',
```

## D. HTML attribute fixes

```html
<!-- Line 234, before -->
<span class="v hot">BURST</span>

<!-- After -->
<span class="v hot" data-ma-i18n="mode_burst">BURST</span>
```

## Summary

- Missing translations: N items
- UX clarity issues: M items
- New TRANSLATIONS keys to add: K
- HTML attributes to add: H
```

## Constraints

- **NOT modify** the file (sandbox read-only — даже если попытаешься, упадёт)
- Keep report under 1500 words (focused, не overcomplicate)
- Suggest fixes that match existing style (data-ma-i18n pattern, лаконичные RU/EN тексты)
- НЕ предлагать редизайн — только текстовые / clarity fixes
- Игнорировать: style/CSS issues, performance, security

## Out of scope

- Refactoring code structure
- Adding new ops-panel widgets
- Changing layout / colors
- Backend changes
- Modifications to `assets/i18n.json` (это main site dict, мы не трогаем)

## Final report

Markdown в stdout / `--output-last-message`. Я (architect) распарсю и применю fixes через Edit tool.
