# Audit: полный проход по neon-legion + дашборд (2026-07-31)

Name: codex-auditor
Profile: Codex CLI, bypass sandbox (аудит read-only по формулировке — НИЧЕГО не менять)
Goal: найти реальные баги в метриках, money-math, инжесте и рендере дашборда; выдать findings list.
Constraints: read-only; никаких правок файлов; никаких git-команд кроме read (log/show/diff)
Watches: tracker/, backend/, hooks/, tools/, tests/; тема F:/WorkAI/wp-dev/theme-source/page-neon-legion.php; деплой F:/WorkAI/wp-dev/tools/deploy-snapshot.sh
Produces: findings list в stdout (final message), формат ниже

Role: read-only auditor. Читай реальные файлы, проверяй каждое утверждение по коду
перед включением в отчёт. Do NOT modify anything. No preface, no praise, real bugs only.

## Контекст системы

neon-legion — персональный трекер AI-usage. Пайплайн:
- **Хуки/трекеры** пишут события в `tracker/*-events.jsonl` (append-only):
  `hooks/claude-track-calls.py` (Claude Code Stop-hook), `tracker/codex-track.py`
  (обёртка Codex CLI), бэкфиллы `tracker/backfill-*.py` (codex-sessions, opencode,
  dscall, human-attention).
- **Агрегация**: `tracker/summary.py` (метрики, продуктивность, сентимент),
  `backend/readmodel.py` (склейка провайдеров), `backend/server.py`
  (`--snapshot-once --public` пишет snapshot.json для сайта).
- **Виджеты**: `tools/slop_score.py`, `tools/model_slippage.py`,
  `tools/git_diff_cost*` (дорогие сессии), `tools/auto_readme_stats.py`.
- **Деплой**: `F:/WorkAI/wp-dev/tools/deploy-snapshot.sh` — бэкфиллы → снапшот →
  privacy-scan → scp на прод и NAS.
- **Рендер**: `F:/WorkAI/wp-dev/theme-source/page-neon-legion.php` — клиентский JS
  читает snapshot.json (+ regressions/diff_cost/slop/slippage/reputation.json) и
  рисует дашборд androman.pro/neon-legion/.

Ключевая метрика: productivity multiplier = `hours_without_ai / human_attention`.
Human attention = таймстемпы настоящих промптов человека (не tool-result, не
sidechain), склейка HUMAN_ATTENTION_GAP_MINUTES=30 (только что поменяли с 5 —
проверь согласованность), пол HUMAN_ATTENTION_FLOOR_MIN_PER_SESSION=5 мин/сессия.
Замороженный кэш `tracker/human-attention.json` спасает сессии с отротированными
транскриптами (порядок: живой транскрипт → кэш → AI-fallback).

## Что недавно чинили (регрессии ищи в первую очередь)

1. Прайсинг на реальные рейты июля-2026 (Opus $5/$25, Fable $10/$50, Sonnet $3/$15;
   codex-track: gpt-5.6-sol/terra/luna, 5.5, 5.4) + `recost.py --provider claude|codex`.
2. ds-call DeepSeek: `backfill-dscall.py` → `dscall-events.jsonl` → провайдер
   openrouter, слияние с opencode-путём по (provider, model).
3. Заморозка human-attention (`backfill-human-attention.py`).
4. Гэп склейки внимания 5→30 минут (сегодняшний коммит b2d4592).
5. git_diff_cost — мультирепо через config.toml [git_diff_cost].repos.
6. slop_score --source both (транскрипты ~/.claude + orchestrate-runs).

## Оси аудита (по каждой пройтись явно)

**A. Метрики** (`tracker/summary.py`, `backend/server.py`):
корректность multiplier в session- и chunk-режимах; floor/ceiling клампы;
`distinct_sessions_covered` vs `sessions_covered`; периоды 7d/30d/all в
`productivity_payload`; согласованность big-number и periods; деление на ноль.

**B. Money math** (`hooks/claude-track-calls.py`, `tracker/codex-track.py`,
`tracker/recost.py`, агрегация в readmodel/summary):
единицы (per-1M токенов!), cache_read/cache_write учёт, prefix-matching моделей,
unknown model → что происходит с ценой; двойной счёт стоимости; cost_saved формула;
recost идемпотентность.

**C. Инжест/дедуп** (бэкфиллы): идемпотентность по event_id; возможен ли двойной
счёт DeepSeek между `opencode-events.jsonl` и `dscall-events.jsonl`; session_id
коллизии; ротация транскриптов; append-only не нарушен.

**D. Границы дней/таймзоны**: `chunk_date` использует local tz, события в UTC —
проверь все места где день вычисляется (streaks, periods, chunk-режим, бэкфиллы,
git_diff_cost, slop lookback): нет ли смеси UTC-дней и local-дней.

**E. Рендер темы** (`page-neon-legion.php`): JS читает ключи, которых нет в
snapshot? null/0/NaN → что на экране; переключение периодов; hero берёт 30d;
i18n ru/en рассинхрон; fallback-и на PHP-baked значения vs live snapshot.

**F. Privacy (public snapshot)**: утечки путей/UUID/имён проектов/личных данных
в snapshot.json и в соседние json (slop, diff_cost, slippage, reputation);
что реально проверяет privacy-scan и что мимо него проходит.

**G. Деплой** (`deploy-snapshot.sh`): молчаливые падения шагов; неатомарные записи;
что будет при пустом/битом snapshot; ALL_DAYS вычисление.

## Правила отчёта

- Формат finding: `SEVERITY file:line — проблема одной строкой — почему это баг
  (конкретный сценарий данных)`.
- HIGH = неверные публичные цифры / потеря событий / крэш пайплайна.
  MED = неверный корнер-кейс, деградация без крэша. LOW = запах/хрупкость.
- Перед включением находки — перечитай код и убедись, что сценарий реален
  (никаких «возможно»). Если не уверен — в отдельную секцию UNVERIFIED.
- Не предлагай рефакторинг, только дефекты.
- Финальное сообщение: сначала таблица-сводка (severity → count), затем findings,
  затем UNVERIFIED. Без вступлений.

## Окружение (не тратить время на подбор)

- Все файлы проекта — **UTF-8**. Windows-локаль — cp1251, поэтому
  `pathlib.read_text()` и `open()` БЕЗ явного encoding падают с UnicodeDecodeError.
  Всегда: `read_text(encoding='utf-8', errors='replace')`, `open(..., encoding='utf-8')`.
- Python вызывать как `py -3.14` (не `python3`, не `python`).
- Вывод кириллицы: `PYTHONIOENCODING=utf-8` в env ИЛИ первой строкой
  `sys.stdout.reconfigure(encoding='utf-8')`.
- Однострочники Python гнать через Git Bash, не через PowerShell.
- Тесты можно запускать: `py -3.14 -m pytest tests/ -q` (host, сеть не нужна).
- Данные в `tracker/*-events.jsonl` живые — читать можно, писать нельзя.

## Out of scope

- Правки любых файлов.
- Стиль, naming, рефакторинг.
- `orchestrate-runs/`, `.oss-backup/`, `pytestdir/`, временные tmp*-файлы.
