# Dashboard

Single-file cyberpunk live dashboard для Phase 1.0+ метрик.

## Run

1. Запустить backend: `py -3.14 backend/server.py --port 8089`
2. Открыть `http://127.0.0.1:8089/` в браузере

## Features

- Hero: $-saved, productivity multiplier, active time
- 5h-budget remaining (real-time, every 5s)
- Stress meter (Phase 1.4 sentiment)
- Activity timeline (60 days, daily aggregates)
- Models breakdown
- Recent sessions (last 10)

## Tech

- Vanilla HTML + CSS + JS (single file, no build)
- Polling `fetch()` — fast (5s) for budget/health, medium (30s) for stats, slow (60s) for sessions/timeseries
- Cyberpunk theme: cyan `#06b6d4` + violet/magenta accents + JetBrains Mono + grid pattern + glitch on hero numbers

## Phase 4 (отдельно)

Public-share snapshot на <your-blog>.example — обезличенные числа, без `working_dir` / `session_id`.
