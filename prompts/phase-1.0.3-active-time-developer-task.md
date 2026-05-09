# Task: Phase 1.0.3 — Active time metric (gap-based, ≤Nmin)

You are the **developer** role. The architect (Claude) wrote this plan. Implement it and report via `--output-schema`.

Working directory: `F:/WorkAI/multi-agent` (already your `--cd`).

Project context: read `CLAUDE.md`, `README.md`. Phase 1.0.2 hot-fix только что merged. Сейчас `summary.py` показывает «Hours with AI» через merged-intervals = calendar span sessions, что выдаёт 1462.5h за 62 дня (физически невозможно). Phase 1.0.3 — добавить **active-time** метрику.

## Goal

Добавить в `summary.py` правильную active-time метрику по правилу «gap ≤ N минут между consecutive turn'ами в session». Сохранить старую calendar-span метрику как контекст. Productivity multiplier использовать active-time.

**User reference (апрельский пост «Разгон»):** «147 часов активного общения с моделью по правилу <2 минут между сообщениями». Это и есть target.

## Deliverables (изменения в существующем файле)

**Только `tracker/summary.py`** — никаких новых файлов.

### Изменения

1. **Новая функция `active_time_hours(events, gap_minutes=2) -> float`:**
   - Group events by `session_id`
   - Sort timestamps в каждой session
   - Iterate consecutive pairs, calculate gap
   - If `gap ≤ timedelta(minutes=gap_minutes)` — add `gap.total_seconds()` to total
   - Else: skip (пауза, не считается)
   - Return total / 3600

2. **CLI flag `--gap-minutes N` (default 2)** в `parse_args`

3. **Productivity block (`print_productivity` / `summarize_productivity`)** — добавить **обе** метрики:
   ```
   ## Productivity (Phase 1.3)

   **Hours with AI (active, ≤2min gaps)**: 147.3
   **Hours with AI (calendar span)**: 1462.5
   **Hours without AI** (estimated): 0.0
   **Hours saved**: ...
   **Productivity multiplier**: ×N.N (active-based)

   Sessions covered: ...
   ```

4. **Productivity multiplier** теперь = `hours_without_ai / active_time_hours` (не merged calendar). Это даёт реалистичный множитель.

5. **Hours saved** теперь = `hours_without_ai - active_time_hours` (где active — реальная мера, calendar — контекст).

## Constraints

- Only stdlib (json, argparse, datetime, pathlib, sys, collections)
- UTF-8 везде
- Если `gap_minutes` <= 0 → ValueError, exit 2
- Если в session только 1 turn → active_time для этой session = 0 (нечего считать gap'ы между)
- Performance: на 80k events должно отрабатывать <1 секунды

## Acceptance criteria

- [ ] `--gap-minutes N` CLI flag работает (default 2)
- [ ] Output показывает **обе** метрики (active + calendar span) в Productivity блоке
- [ ] productivity_multiplier использует active time
- [ ] hours_saved использует active time
- [ ] На текущей JSONL data active time выйдет в районе 150-400h (не 1462)
- [ ] Single-turn session не падает (active = 0)
- [ ] Stdlib only

## Test it before reporting

1. **Single-turn session**: fake JSONL с 1 event одного session_id → active = 0
2. **Tight cluster**: 5 events с gap ≤ 1 min между ними → active = sum(4 gaps)
3. **Pause session**: events с gap > 2 min между парой → пауза не считается
4. **Mix**: real-data summary on production JSONL → должно показать реалистичные числа
5. **--gap-minutes 5**: с порогом 5 min активное время будет больше чем с 2

## Out of scope

- Phase 1.4 sentiment (отдельная фаза)
- Persisting active-time в JSONL events (это derived метрика, не сохранять в события)
- Multi-day session handling — единый алгоритм для всех

## Final report

Conform to `--output-schema`. Required: `files_created` (один updated file), `summary`, `tested`, `test_results`.
