# Task: Phase 2 — Aggregator backend (HTTP API над tracker/*)

Name: codex-developer
Profile: Codex CLI 0.128+ (gpt-5.5, xhigh reasoning, --sandbox workspace-write)
Goal: Создать stdlib-only HTTP-сервер на `http.server`, читающий tracker/*.jsonl + tasks.json и отдающий JSON endpoint'ы для Phase 3 dashboard. Reuse logic из существующего summary.py.
Constraints: stdlib only (`http.server`, `json`, `urllib`, `argparse`, `datetime`); single-user local; no auth; no database; CORS allow-all для локального dev; reuse code из tracker/summary.py через import (не дублировать).
Watches: issue #N + existing `tracker/summary.py`, `tracker/estimate-task.py`, `tracker/claude-events.jsonl`, `tracker/tasks.json`
Produces: `backend/server.py` (новый) + `backend/README.md` (новый)

## Operational backstory

You are running with `workspace-write` sandbox в `F:/WorkAI/multi-agent`. Phase 1.0/1.3/1.0.1/1.0.2/1.0.3/1.4 уже в main. Этот phase **не модифицирует** existing tracker/ файлы, только **читает** через import.

**Sandbox limitation** (Phase 1.0.2 lesson): Codex не запускает реальный сервер для тестирования. Static check (py_compile + import test) — Codex; runtime check (`curl localhost:8080/api/health`) — architect на host.

**Stdlib only choice**: использование `http.server` вместо FastAPI/Flask — сознательный выбор. FastAPI требует `pip install` который sandbox блокирует, но также — single-user local dashboard не нуждается в production-grade ASGI server. `http.server` достаточен.

## Working directory

`F:/WorkAI/multi-agent` (already your `--cd`).

## Project context

Read `CLAUDE.md`, `README.md`, `tracker/README.md`. Особенно — `tracker/summary.py` (logic переиспользуется через import), `tracker/estimate-task.py` (для константы pricing если нужна).

## Goal

Backend для будущего Phase 3 dashboard. **HTTP-сервер на stdlib**, читает данные из `tracker/*.jsonl` + `tasks.json`, отдаёт JSON endpoint'ы. Запускается как `py -3.14 backend/server.py --port 8080`. Никакой aut, для single-user local use.

## Deliverables (новые файлы)

### 1. `backend/server.py`

Standalone HTTP-сервер. Структура:

```python
#!/usr/bin/env python
import argparse, json, sys, http.server, urllib.parse
from pathlib import Path
from datetime import datetime, date, timedelta

# Reuse summary.py logic через import
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "tracker"))
import summary  # uses summary.read_events, summarize_by_model, summarize_productivity, etc.

def parse_args():
    parser = argparse.ArgumentParser(description="Multi-agent tracker backend API")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1")
    return parser.parse_args()

class APIHandler(http.server.BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        # CORS preflight
        ...
    def do_GET(self):
        # Route by path
        ...
    def log_message(self, *args, **kwargs):
        # Suppress default per-request logs (или kept short)
        ...

def main():
    args = parse_args()
    server = http.server.ThreadingHTTPServer((args.host, args.port), APIHandler)
    print(f"Backend running on http://{args.host}:{args.port}")
    print("Endpoints: /api/health, /api/summary, /api/productivity, /api/sentiment, /api/budget, /api/timeseries, /api/sessions")
    server.serve_forever()

if __name__ == "__main__":
    main()
```

### 2. Endpoints (все GET, JSON response)

```
GET /api/health
  → {"status": "ok", "timestamp": "ISO", "events_total": int, "tasks_total": int}

GET /api/summary?days=N
  → {
      "period": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD", "days": N},
      "totals": {
        "calls": int,
        "input_tokens": int,
        "output_tokens": int,
        "cache_read_tokens": int,
        "cost_usd": float,
        "savings_usd": float,        // api_cost - subscription
        "subscription_usd": float    // prorated $200/mo
      },
      "by_model": [
        {"model": "claude-opus-4-7", "calls": N, "cost_usd": float, ...}
      ]
    }

GET /api/productivity?days=N&gap_minutes=2
  → {
      "active_hours": float,
      "calendar_span_hours": float,
      "hours_without_ai_estimate": float,  // sum from tasks.json
      "hours_saved": float,
      "multiplier": float,
      "sessions_covered": int,
      "sessions_total": int
    }

GET /api/sentiment?days=N
  → {
      "profanity_total": int,
      "frustration_avg": float,
      "appreciation_avg": float,
      "stress_trend": "improving" | "worsening" | "stable",
      "top_day": {"date": "YYYY-MM-DD", "profanity": int},
      "mood_arcs_top": [{"arc": "frustrated→calm", "count": N}],
      "sessions_covered": int
    }
  → null если no sentiment data в period

GET /api/budget
  → {
      "window_5h": {
        "since": "ISO timestamp 5h ago from now",
        "tokens_used": int,
        "estimated_limit_max5x": 88000,
        "estimated_limit_max20x": 220000,
        "percent_max5x": float,
        "percent_max20x": float
      },
      "window_24h": {
        "tokens_used": int,
        "calls": int,
        "cost_usd": float
      }
    }

GET /api/timeseries?metric=cost&days=N
  → {
      "metric": "cost",
      "period": {...},
      "data": [
        {"date": "YYYY-MM-DD", "value": float}
      ]
    }
  metric values: cost, calls, input_tokens, output_tokens, profanity, frustration

GET /api/sessions?limit=20
  → {
      "sessions": [
        {
          "session_id": "...",
          "first_ts": "ISO",
          "last_ts": "ISO",
          "calls": int,
          "cost_usd": float,
          "model_primary": "claude-opus-4-7",
          "task": {  // null if no entry in tasks.json
            "brief_description": "...",
            "ai_baseline_hours": float,
            "profanity_count": int,
            "mood_arc": "..."
          }
        }
      ]
    }
```

### 3. CORS headers (все responses)

```
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: GET, OPTIONS
Access-Control-Allow-Headers: Content-Type
```

Single-user local — allow-all OK.

### 4. Error handling

- 400 — invalid query params (e.g. `days=abc`)
- 404 — unknown endpoint
- 500 — internal error (вернуть JSON с error message, не traceback)
- Empty data → 200 с пустым результатом (не 404)

### 5. `backend/README.md`

```markdown
# Backend API

Standalone HTTP server для Phase 3 dashboard.

## Run

```
py -3.14 backend/server.py --port 8080
```

## Endpoints

[список endpoints с примерами response — copy from above]

## Architecture

- stdlib `http.server.ThreadingHTTPServer`
- Reuses logic from `tracker/summary.py` через import
- Reads `tracker/claude-events.jsonl` + `tracker/tasks.json` on each request (no cache)
- CORS allow-all для локального development
- No auth (single-user, localhost only)

## Future (Phase 3)

Phase 3 frontend будет poll'ить эти endpoint'ы для cyberpunk dashboard.
WebSocket / SSE — out of scope для MVP.
```

## Constraints

- Stdlib only (`http.server`, `json`, `urllib`, `argparse`, `datetime`, `pathlib`, `sys`)
- Reuse `tracker/summary.py` через import (не дублировать `read_events`, `summarize_by_model`, etc.)
- Server reads JSONL on each request — no caching (data ~30MB, read <1s acceptable)
- Threading server — multiple concurrent requests OK
- CORS headers на всех responses
- UTF-8 encoding
- No persistent state (stateless backend)

## Acceptance criteria

- [ ] `backend/server.py` exists, py_compile passes
- [ ] Все 7 endpoints реализованы (`/api/health`, `/api/summary`, `/api/productivity`, `/api/sentiment`, `/api/budget`, `/api/timeseries`, `/api/sessions`)
- [ ] CORS headers на всех responses (включая OPTIONS preflight)
- [ ] Reuse `tracker/summary.py` через import (не дублирование)
- [ ] Error handling: 400 на bad params, 404 на unknown route, 500 на internal error
- [ ] `backend/README.md` exists с usage instructions
- [ ] Stdlib only

## Test plan

**Codex responsibilities** (sandbox, no network):
- [ ] py_compile `backend/server.py`
- [ ] Import test: `py -3.14 -c "import sys; sys.path.insert(0, 'backend'); import server"`
- [ ] Manual request building через urlparse / json.dumps fixtures без runtime server
- [ ] Code review: все endpoints возвращают valid JSON по schema выше

**Architect responsibilities** (host, after merge):
- [ ] Запуск `py -3.14 backend/server.py --port 8080` на host
- [ ] `curl http://127.0.0.1:8080/api/health` → 200 OK JSON
- [ ] `curl http://127.0.0.1:8080/api/summary?days=60` → корректные числа
- [ ] CORS preflight: `curl -X OPTIONS http://...` → 204 с правильными headers
- [ ] Stress test: `for i in {1..50}; do curl ...; done` без падения

**НЕ запускать сервер во время Codex run** — server.serve_forever() блокирует и Codex повиснет.

## Out of scope

- Auth (single-user, no needed)
- HTTPS (localhost dev)
- WebSocket / SSE для real-time push (Phase 3 будет polling, достаточно)
- Database / persistence (JSONL — single source of truth)
- Caching (premature optimization)
- Phase 3 frontend (отдельный phase)

## Final report

Conform to `--output-schema`. Required: `files_created` (2 new), `summary`, `tested`, `test_results`, `open_questions`, `deviations_from_spec`.
