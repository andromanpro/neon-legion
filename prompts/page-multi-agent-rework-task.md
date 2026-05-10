# Task: Rework page-multi-agent.php — content + layout + new features

Name: codex-developer
Profile: Codex CLI 0.128+ (gpt-5.5, xhigh reasoning, --sandbox workspace-write)
Goal: Большая переделка демо-страницы по user feedback. (1) Hero rewrite в киберпанк-стиле без англицизмов; (2) перевод английских жаргонизмов в RU dict; (3) layout debug — `pheader` / `budget-meta` склеиваются у пользователя; (4) новый виджет «сегодня»; (5) period selector (7д/30д/60д/90д); (6) «фрустрация» → «недовольство».
Constraints: existing data-usd / TRANSLATIONS architecture сохранить, не ломать i18n switching, not introduce dependencies, mock data inline, period selector не меняет mock cipfs (только UI demo), все existing keys которые корректны — оставить
Watches: `dashboard/page-multi-agent.php` (текущая версия после первого review apply, 51 keys в каждом dict)
Produces: same file, modified

## Operational backstory

Sandbox `workspace-write` в `F:/WorkAI/multi-agent`. После твоих изменений architect синхронизирует в `H:/wordpress-androman/.../page-multi-agent.php` на NAS WordPress.

User feedback (verbatim):
- **«Self-hosted productivity-трекер — это ли не англицизм? да и вообще сложно и не по киберпанковски»** — hero rewrite в киберпанк-стиле
- **«5-ЧАСОВОЙ БЮДЖЕТ · ПЕРЕРАСХОД04 · ЛАЙВ»** — pheader склеивается у user'а (видимо flex layout не отрабатывает), и `04 · ЛАЙВ` должен быть справа отдельным блоком pid, не сливаться с titleом
- **«токенов использовано 1 201 422 лимит Max 5× 88 000 процент 1365%»** — budget-meta grid тоже склеивается, должно быть три колонки с пробелами
- **«кэш-токены вне лимита 161 964 125 % от лимита Max 20× 546% статус ПЕРЕРАСХОД»** — то же
- **«Также надо добавить статистику за сегодня»**
- **«Еще дать возможность выбрать период»**
- **«фрустрация — я такое слово не использую, может ярость? или недовольство?»** — заменить на **«недовольство»**

## Working directory

`F:/WorkAI/multi-agent`. Файл — `dashboard/page-multi-agent.php`.

## Goal

Сделать страницу читабельной для русскоязычного user'а, в киберпанк-стиле, с дополнительной аналитикой за сегодня + period selector.

## Deliverables (один файл, modified)

### 1. Hero rewrite

**RU** (в `TRANSLATIONS.ru.hero_sub`) — переписать без англицизмов, в киберпанк-стиле, проще. Идея для образца (не копировать дословно — пиши лучше):

```
ОПЕРАЦИОННЫЙ ЖУРНАЛ. Считает три потери, которые экономит ИИ:
<code>деньги</code> (что бы ты заплатил по API), <code>часы</code> (без помощи модели),
<code>стресс</code> (как ты ругался). Полный поток данных идёт локально на
порту 8089, здесь — снимок для быстрой оценки масштаба.
```

Стиль: короткие предложения, дерзкий тон, без слов «productivity-трекер», «self-hosted», «sentiment». Должно звучать как HUD-сводка из киберпанк-видеоигры, не как README продукта.

**EN** аналогично — без слова «productivity tracker», более концентрированно. Например:
```
OPERATIONS LOG. Three losses the AI saves you from:
<code>money</code> (what API would have cost), <code>hours</code> (without the model),
<code>stress</code> (how much you swore). Live stream runs locally on
port 8089; this page is a snapshot at scale.
```

### 2. Replace англицизмы в TRANSLATIONS.ru

Существующие RU values содержат английские слова без необходимости. Перевести:

| Key | Current RU | New RU |
|---|---|---|
| `meta` | `62 ДНЯ · 79,7 ТЫС. СОБЫТИЙ · 3 ОСИ ЭКОНОМИИ` | OK (оставить) |
| `multi_sub` | `активные часы против оценки без ИИ` | `чистое время с ИИ против ручной оценки` |
| `active_sub` | `паузы между сообщениями ≤ 2 мин считаются активным временем` | `считаются паузы между сообщениями короче 2 минут` |
| Mood arcs `frustrated → calm` | hardcoded в данных | заменить на «недовольство → спокойствие» |

Также в `$sessions` array (PHP блок) — `mood: 'frustrated→calm'` поменять на новый key `'frustrated_calm'` или похожий, с новым отображением «недовольство → спокойствие».

### 3. «Фрустрация» → «недовольство» — везде

- `TRANSLATIONS.ru.frustration: 'фрустрация'` → `'недовольство'`
- В hero_sub упоминание sentiment: убрать «фрустрация» как термин
- В sentiment panel: `<span class="sent-label" data-ma-i18n="frustration">фрустрация</span>` → default text «недовольство»

EN остаётся `frustration`.

### 4. Layout debug — pheader / budget-meta склеиваются

User видит:
- `5-ЧАСОВОЙ БЮДЖЕТ · ПЕРЕРАСХОД04 · ЛАЙВ` — нет space-between'а между ptitle (слева) и pid (справа)
- Budget meta — три span'а в одну строку без gap'а

CSS уже имеет `display: flex; justify-content: space-between` для `.pheader` и `display: grid; grid-template-columns: 1fr auto auto; gap: 12px;` для `.budget-meta`. **Возможные причины склеивания:**
- Inline child spans в pid (после моего i18n wrapping) могли сломать flex
- На narrow viewport (≤1100px) responsive media-queries могут override layout
- WP theme global CSS может конфликтовать

**Действие**:
- Добавить `gap: 12px` в `.pheader` явно (на случай override)
- Добавить `flex-shrink: 0` на `.pheader .pid` чтобы не схлопывался
- В `.budget-meta` добавить `align-items: center` и `min-width: 0` для каждой ячейки, чтобы grid колонки чётко разделялись
- Для responsive — `.budget-meta { grid-template-columns: repeat(3, 1fr); }` чтобы три равных колонки независимо от viewport
- Проверить что нет `text-transform` или `white-space: nowrap` которые могут визуально сломать layout

### 5. Новая ops-panel «СЕГОДНЯ»

Новый widget между hero и существующим grid (или в первой строке grid'а — нагрузить).

PHP mock data:
```php
$today = array(
    'calls'         => 87,
    'cost_usd'      => 0.51,
    'active_hours'  => 1.4,
    'profanity'     => 0,
    'top_session'   => 'Phase 3 cyberpunk dashboard',
);
```

UI: ops-panel `p-today` с pheader `СЕГОДНЯ` / `00 · LIVE`. Body — 4 mini-метрики:
- Calls: 87
- Cost: $0.51 / по курсу ₽
- Active: 1.4ч
- Profanity: 0

Можно использовать `metrics-row` pattern из page-dashboard.php (3 ячейки в ряд) или собственный layout. Главное — компактно.

Span: `grid-column: span 12` (полный ряд) или `span 6` (с другим виджетом). На твоё усмотрение.

### 6. Period selector

Над grid'ом или в hero — кнопки выбора периода. Demo only — клик меняет визуальный indicator, mock numbers не меняются (потому что mock data статичная).

```html
<div class="ma-period-bar">
  <span class="ma-period-label" data-ma-i18n="period_label">ПЕРИОД</span>
  <button class="ma-period-btn" data-period="today" data-ma-i18n="period_today">сегодня</button>
  <button class="ma-period-btn active" data-period="60d" data-ma-i18n="period_60d">60 дней</button>
  <button class="ma-period-btn" data-period="7d" data-ma-i18n="period_7d">7 дней</button>
  <button class="ma-period-btn" data-period="30d" data-ma-i18n="period_30d">30 дней</button>
  <button class="ma-period-btn" data-period="all" data-ma-i18n="period_all">всё время</button>
</div>
```

Стиль кнопок — киберпанк (border cyan, hover glow, active filled). Маленькие, monospace.

JS handler — при клике toggle `.active` class. Реальная фильтрация — out of scope (mock data static).

Default active button — `60d` (соответствует текущему mock period).

### 7. Translations updates

Добавить новые keys в **обоих** dicts:

```javascript
// RU
hero_sub: '<новый текст из секции 1>',
multi_sub: 'чистое время с ИИ против ручной оценки',
active_sub: 'считаются паузы между сообщениями короче 2 минут',
frustration: 'недовольство',
mood_frustrated_calm: 'недовольство → спокойствие',

p_today: 'СЕГОДНЯ',
today_calls: 'вызовов',
today_cost: 'стоимость',
today_active: 'активно',
today_profanity: 'недовольство',
today_top_session: 'главная сессия',

period_label: 'ПЕРИОД',
period_today: 'сегодня',
period_7d: '7 дней',
period_30d: '30 дней',
period_60d: '60 дней',
period_all: 'всё время',

// EN
hero_sub: '<новый EN текст>',
multi_sub: 'clean AI time vs manual estimate',
active_sub: 'gaps between messages under 2 minutes count',
mood_frustrated_calm: 'annoyed → calm',  // было frustrated → calm

p_today: 'TODAY',
today_calls: 'calls',
today_cost: 'cost',
today_active: 'active',
today_profanity: 'frustration',
today_top_session: 'top session',

period_label: 'PERIOD',
period_today: 'today',
period_7d: '7 days',
period_30d: '30 days',
period_60d: '60 days',
period_all: 'all time',
```

### 8. PHP $sessions data update

Mood values `'frustrated→calm'` (есть только в Phase 1.0.2 entry) → новый internal key `'frustrated_calm'`. HTML data-ma-i18n preserved через тот же mapping pattern что уже есть.

## Constraints

- НЕ изменять existing `data-usd` атрибуты или currency conversion
- НЕ ломать `i18n:applied` event listener
- НЕ удалять existing translations keys которые корректны
- Mock data в PHP блоке (`$totals`, `$productivity`, etc.) — не менять (только `$sessions[5].mood` на новый key, и добавить `$today`)
- UTF-8 везде, без BOM
- PHP синтаксис валидный

## Acceptance criteria

- [ ] Hero rewrite в RU и EN — короче, без «productivity tracker», киберпанк-тон
- [ ] «Фрустрация» → «недовольство» в RU dict
- [ ] CSS fix для pheader (gap, flex-shrink) и budget-meta (явно equal-width grid)
- [ ] Новая ops-panel `p-today` с 4 mini-метриками
- [ ] Period selector с 5 кнопками (today/7d/30d/60d/all), default 60d active, JS toggle
- [ ] 12 новых translations keys в обоих dicts (today_*, period_*, p_today)
- [ ] PHP синтаксис валидный
- [ ] Все existing data-ma-i18n references resolved

## Test plan

**Codex (sandbox)**:
- [ ] PHP syntax check (если возможно)
- [ ] grep для hardcoded «фрустрация» — не должно остаться в HTML body (только в RU dict как defaults для new keys где applicable, но не в visible static text)
- [ ] Diff между RU и EN dicts (set keys должен быть равен)
- [ ] Period selector — 5 buttons с правильными data-period значениями
- [ ] Today panel — 4 mini-metrics + правильные data-usd (для cost)

**Architect (host, after merge)**:
- Sync в H:/ и curl-verify рендера
- Visual check в браузере: layout fix, period selector toggle, today panel

## Out of scope

- Реальная фильтрация по периоду (только UI toggle)
- Backend integration с live data
- Mobile-specific tweaks (если existing responsive work — оставить)
- Phase 4 publish на androman.pro

## Final report

Conform to `--output-schema`. Required: `files_created` (один modified), `summary`, `tested`, `test_results`, `open_questions`, `deviations_from_spec`.
