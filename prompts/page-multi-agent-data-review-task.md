# Task: Data accuracy review — page-multi-agent.php mock numbers

Name: codex-reviewer
Profile: Codex CLI 0.128+ (gpt-5.5, xhigh reasoning, --sandbox read-only)
Goal: Audit всех mock numerical values в `dashboard/page-multi-agent.php` на consistency + математическую корректность + соответствие реальным research findings из MEMORY про Anthropic Max limits 2026.
Constraints: read-only sandbox, output structured markdown report, не править файл, focus на числа, не на текст
Watches: `dashboard/page-multi-agent.php` (workspace копия из WP theme)

## Operational backstory

Reviewer-only mode. Read-only sandbox. Мы только что отправили dashboard на NAS WordPress, user попросил сверить точность mock numbers с реальными research findings.

## Working directory

`F:/WorkAI/multi-agent` (--cd).

Файл для аудита: `dashboard/page-multi-agent.php`.

## Reference numbers (from prior research)

**Real backfill** (2026-05-09 на user'овом Claude Code, 62 дня):
- Total events: ~79,716
- Real cost (по pricing с Opus 4.7 fallback for old Opus 4.6): $60,299 за 62 дня
- Subscription Max 20× = $200/mo → ~$413 за 62 дня (62/30 × $200)
- Active hours (≤2min gaps): ~216.6h
- Calendar span: ~1462.7h
- Productivity multiplier (когда tasks.json заполнится): ~×7.3 (ожидаемое)
- Sentiment (user): ~47 profanity counts, frustration_avg ~0.34, appreciation ~0.62

**Anthropic Max limits 2026** (из research):
- Pre-May 2026 doubling: Pro ~44k, Max 5× ~225 messages ≈ 220k tokens, Max 20× ~900 messages ≈ 880k
- Post-May 2026 doubling: Pro ~88k, Max 5× ~440k-880k tokens, Max 20× ~1.7M-3.5M tokens
- Cache_read tokens **не считаются** в rate-limit budget (officially https://platform.claude.com/docs/en/api/rate-limits)
- Cache_creation tokens **считаются**

User фактически не превышает лимит подписки → real Max 20× cap ≥ 1.5M (ему 1.2M комфортно).

**Models breakdown (real):**
- Opus 4.7: ~53,854 calls / ~$54,394 (~90% от total cost)
- Opus 4.6: ~9,418 calls / ~$5,044 (~8.4%)
- Sonnet 4.6: ~16,275 calls / ~$862 (~1.4%)

## Что проверить

### A. Consistency mock data (числа сами по себе)

1. `$totals['cost_usd']` (60,299.41) vs sum of `$models[*]['cost']` — sum should match
2. `$totals['savings_usd']` (59,886.08) = `cost_usd` − `subscription_usd` — формула правильная?
3. `$totals['subscription_usd']` (413.33) = `200 × 62 / 30` ≈ 413.33 — да или нет?
4. `$models[*]['pct']` (90.2 + 8.4 + 1.4 = 100%) — sums to 100?
5. `$productivity['multiplier']` (7.3) vs `hours_saved / active_hours` (1367.2 / 216.6 ≈ 6.31) — несоответствие?
6. `$productivity['hours_saved']` (1367.2) — derivation? Должно быть `(active × multiplier) − active = active × (multiplier − 1)` = 216.6 × 6.3 ≈ 1364, или другая формула?
7. `$budget` percent_5x and percent_20x recalculated formulas — match?

### B. Соответствие real research

1. `$budget['limit_5h']` (440,000) vs research lower bound 440k post-May 2026 — совпадает?
2. `$budget['limit_20x']` (1,700,000) vs research lower bound 1.7M — совпадает?
3. `$totals['cost_usd']` ($60,299) vs real backfill ($60,299) — реальные данные, не подкручены?
4. `$productivity['active_hours']` (216.6h) vs real measured 216.6h — точные?
5. `$timeline_weights` array sum vs `$totals['calls']` (79,737) — sum близко?
6. `$sessions[X]` mood values: после rework `'frustrated→calm'` → key `'frustrated_calm'`. Соответствует ли использование в HTML?

### C. Edge cases / суспициозные значения

1. `$today` — `cost: 0.51` ваш реальный? Или synthetic? (можно сказать "synthetic placeholder, реалистично")
2. `$today['profanity']` (0) — соответствует "сегодняшней" сессии или нет?
3. `$sentiment['top_day']` (2026-04-22, 12 profanity) — synthetic peaked day, разумно?
4. `$models[*]['pct']` derivation от `cost_usd` или `calls`? Если от cost — 54394/60299 ≈ 90.2% — match. Verify
5. `$totals['days']` (62) vs date_diff(period_start, period_end) = 62 — match?

### D. Privacy / leak checks

1. Никаких privately user'овых session_id (UUIDs)?
2. Никаких реальных working_dir paths (F:/WorkAI/...)?
3. Никаких email / login / token references?

## Output format

```markdown
# Data accuracy review report

## A. Consistency

| # | Check | Expected | Actual | Status | Note |
|---|---|---|---|---|---|
| 1 | sum(models.cost) | totals.cost_usd | $60,299.41 | $60,299.41 | OK |
| 2 | savings formula | cost - subscription | ... | ... | OK / FAIL |
...

## B. Real research alignment

| # | Field | Mock | Research | Status |
...

## C. Edge cases

| # | Field | Value | Plausibility |
...

## D. Privacy

| # | Check | Status |

## Summary

- Math errors: N
- Research mismatches: M
- Privacy issues: K
- Recommended fixes: brief list
```

Под 800 слов, structured. Architect применит fixes по списку.
