# Task: Phase 1.0.2 — Hot-fix backfill safety + pricing accuracy + summary correctness

You are the **developer** role. The architect (Claude) wrote this hot-fix plan. Implement it and report via `--output-schema`.

Working directory: `<project_root>` (already your `--cd`).

Project context: read `CLAUDE.md`, `README.md`, и `tracker/README.md`. Phase 1.0 (Stop hook) и Phase 1.3 (SessionStart hook) и Phase 1.0.1 (backfill) уже в main. Real backfill только что выполнен — 79,716 events в `tracker/claude-events.jsonl` (30MB). Несколько критичных багов нужно срочно поправить.

## Goal

Fix 5 связанных issues в один hot-fix:
1. **SessionStart hook throttle** — без этого следующий `claude` start попытается оценить 247 sessions одновременно через `claude -p --bare` → rate-limit kill
2. **`<synthetic>` events skip** — internal Claude Code markers, не реальные API calls (169 шт в текущем JSONL)
3. **Extended pricing** — старые модели (Opus 4.6, Sonnet 4.5, Haiku 4.4) сейчас fallback'ятся на Opus 4.7 pricing → overestimate. Использовать **prefix-matching** для всех `claude-opus-4-*`, `claude-sonnet-4-*`, `claude-haiku-4-*`. Если truly unknown — `cost_estimate_usd: null`, не fallback
4. **Summary savings logic перевёрнут** — показывает «Доплата» когда должно быть «Savings» (для Max-подписки api_cost > subscription = SAVINGS, не overpayment)
5. **Summary hours_with_ai неверный** — суммирует per-session wall-clock, а sessions overlap. Использовать total unique span

## Deliverables (изменения в существующих файлах)

### 1. `hooks/claude-track-calls.py` (Stop hook)

**Изменения:**
- В `read_latest_assistant`: skip events где `message.model == "<synthetic>"` (фильтр перед всем остальным)
- Заменить hardcoded PRICING dict на функцию `pricing_for_model(model: str) -> dict | None` с prefix-matching:
  ```python
  OPUS_PRICING = {"in": 15.00, "out": 75.00, "cache_read": 1.50, "cache_write": 18.75}
  SONNET_PRICING = {"in": 3.00, "out": 15.00, "cache_read": 0.30, "cache_write": 3.75}
  HAIKU_PRICING = {"in": 1.00, "out": 5.00, "cache_read": 0.10, "cache_write": 1.25}

  def pricing_for_model(model: str) -> dict | None:
      if not model:
          return None
      if model.startswith("claude-opus-4"):
          return OPUS_PRICING
      if model.startswith("claude-sonnet-4"):
          return SONNET_PRICING
      if model.startswith("claude-haiku-4"):
          return HAIKU_PRICING
      return None  # truly unknown
  ```
- В `estimate_cost`: если `pricing_for_model` возвращает None — return None (не 0, не fallback). Не печатать warning (раньше было — теперь убираем).
- В `build_event`: если `cost_estimate_usd` is None — оставить как None в JSONL (`json.dumps` корректно сериализует null)
- Сохранить export'ы `acquire_lock`, `release_lock`, `estimate_cost`, `pricing_for_model` для backfill.py reuse

### 2. `tracker/backfill.py`

**Изменения:**
- Skip `<synthetic>` events в `usage_dict` (после `event.get("type") != "assistant"` check, добавить `if message.get("model") == "<synthetic>": return None`)
- Использовать новую `HOOK.pricing_for_model` через тот же importlib path (если оно None — `cost_estimate_usd: null`)
- Никаких других изменений в логике

### 3. `hooks/claude-session-start.py` (SessionStart hook throttle)

**Изменения:**
- Добавить константу `MAX_DISPATCH_PER_FIRE = 5`
- В `main`: после `pending = sorted(...)` добавить `pending = pending[:MAX_DISPATCH_PER_FIRE]`
- Также skip session_ids с **только** `<synthetic>` events (если в JSONL для session нет ни одного non-synthetic event — нечего оценивать). Реализация: при сборе recent_session_ids фильтровать.

### 4. `tracker/summary.py`

**Изменения (savings logic):**
- В `print_summary`: переменная `delta = total["cost_estimate_usd"] - prorated` (было наоборот!)
- Если `delta >= 0` → `**Savings vs API rates**: ${delta:.2f} ✅`
- Если `delta < 0` → `**Subscription not fully used**: ${abs(delta):.2f}` (вместо «Доплата»)
- Текст metric — переписать чтобы было ясно: «If paid by API rates would owe $X, subscription costs $Y, saved $Z»

**Изменения (hours_with_ai):**
- В `summarize_productivity`: вместо `sum((last_ts - first_ts) ... for each session)` использовать **total unique span**: `(max_event_ts_overall - min_event_ts_overall).total_seconds() / 3600`
- ИЛИ — точнее — interval merge (если sessions overlap во времени, объединять интервалы; total = sum длин merged intervals). Это технически правильно. Если сложно — использовать total span как baseline (overestimate, но не absurd как было).
- В output: «Hours with AI (wall clock total period)» — пометить что это calendar span, не sum of session durations.

**Изменения (cost_estimate_usd null handling):**
- При суммировании `total["cost_estimate_usd"] += as_float(event.get("cost_estimate_usd"))` — если None, treat as 0 для total, но в per-model breakdown отдельно показывать count событий с unknown pricing если они есть (сноска: «N events have unknown pricing»)

### 5. `tracker/recost.py` (новый файл)

CLI script для re-вычисления `cost_estimate_usd` для существующих JSONL events после обновления pricing:

```
py -3.14 tracker/recost.py [--dry-run]
```

Логика:
1. Read `tracker/claude-events.jsonl` line by line
2. Для каждого event: re-call `pricing_for_model(event.model)` + `estimate_cost(...)` через `importlib` от hook
3. Если cost изменился — записать новый. Skip `<synthetic>` events (они должны быть удалены — но recost их не трогает; отдельный flag для cleanup)
4. Atomic write через tempfile + os.replace
5. Report: total events, cost changed in N, new total cost, old total cost

## Acceptance criteria

- [ ] Stop hook skip'ает `<synthetic>` events (не пишет их в JSONL)
- [ ] Stop hook использует prefix-matching pricing
- [ ] Stop hook сохраняет null cost для truly unknown models (не fallback)
- [ ] backfill.py skip'ает `<synthetic>` (не дублируется логика — через тот же import от hook'а)
- [ ] SessionStart hook MAX_DISPATCH_PER_FIRE=5 применяется
- [ ] SessionStart hook не dispatch'ит для sessions с только synthetic events
- [ ] summary.py savings logic корректный (api_cost > subscription = savings)
- [ ] summary.py использует total span для hours_with_ai (с пометкой что это calendar span)
- [ ] recost.py создан и работает на existing JSONL
- [ ] Все экспорты (`acquire_lock`, `release_lock`, `pricing_for_model`, `estimate_cost`) доступны для import от backfill / recost
- [ ] Stdlib only, Python 3.14+
- [ ] No external dependencies

## Test it before reporting

1. **Stop hook synthetic skip**: создай fake transcript с одним `<synthetic>` assistant event → подай stdin → JSONL не должен получить запись
2. **Stop hook prefix pricing**: fake transcript с `claude-opus-4-3` model → JSONL должен иметь cost рассчитанный по Opus pricing (не null)
3. **Stop hook null cost**: fake transcript с моделью `gpt-4` → JSONL запись с `cost_estimate_usd: null`
4. **SessionStart throttle**: fake JSONL с 20 session_ids → SessionStart hook должен dispatch'нуть только 5
5. **Summary savings**: запустить на текущих данных → должен показать `Savings: $59,xxx`, не «Доплата»
6. **recost.py**: dry-run на текущем JSONL → должен показать сколько изменится. Реальный run → JSONL обновлён.

NOTE: НЕ делай реальный recost на production JSONL в тестах — только dry-run. Реальный run — пользователь запустит вручную.

## Out of scope

- Phase 1.4 sentiment (отдельная фаза)
- Recost на cleanup `<synthetic>` записей (опционально расширение recost — добавь `--cleanup-synthetic` flag)
- Streaming append optimization Stop hook'а (P3, оставляем read+write через `os.replace` пока)

## Final report

Conform to `--output-schema`: `files_created`, `summary`, `tested`, `test_results`, `open_questions`, `deviations_from_spec`.

`files_created` должен содержать ВСЕ изменённые/созданные файлы, не только новые.
