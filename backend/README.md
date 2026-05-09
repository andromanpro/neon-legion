# Backend API

Standalone HTTP server для Phase 3 dashboard.

## Run

```bash
py -3.14 backend/server.py --port 8080
```

Optional host override:

```bash
py -3.14 backend/server.py --host 127.0.0.1 --port 8080
```

## Endpoints

All endpoints return JSON and include CORS headers:

```text
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: GET, OPTIONS
Access-Control-Allow-Headers: Content-Type
```

### `GET /api/health`

```json
{
  "status": "ok",
  "timestamp": "2026-05-09T17:00:00+03:00",
  "events_total": 123,
  "tasks_total": 10
}
```

### `GET /api/summary?days=N`

```json
{
  "period": {"start": "2026-05-01", "end": "2026-05-09", "days": 9},
  "totals": {
    "calls": 123,
    "input_tokens": 1000,
    "output_tokens": 2000,
    "cache_read_tokens": 3000,
    "cost_usd": 42.5,
    "subscription_usd": 60.0,
    "savings_usd": -17.5
  },
  "by_model": [
    {
      "model": "claude-opus-4-7",
      "calls": 123,
      "input_tokens": 1000,
      "output_tokens": 2000,
      "cache_read_tokens": 3000,
      "cost_usd": 42.5,
      "cache_hit_percent": 75,
      "unknown_pricing_events": 0
    }
  ]
}
```

### `GET /api/productivity?days=N&gap_minutes=2`

```json
{
  "active_hours": 3.5,
  "calendar_span_hours": 7.0,
  "hours_without_ai_estimate": 20.0,
  "hours_saved": 16.5,
  "multiplier": 5.7143,
  "sessions_covered": 8,
  "sessions_total": 10
}
```

### `GET /api/sentiment?days=N`

Returns `null` when the selected period has no sentiment data.

```json
{
  "profanity_total": 12,
  "frustration_avg": 0.35,
  "appreciation_avg": 0.6,
  "stress_trend": "stable",
  "top_day": {"date": "2026-05-09", "profanity": 5},
  "mood_arcs_top": [{"arc": "frustrated→calm", "count": 3}],
  "sessions_covered": 8
}
```

### `GET /api/budget`

```json
{
  "window_5h": {
    "since": "2026-05-09T12:00:00+03:00",
    "tokens_used": 25000,
    "estimated_limit_max5x": 88000,
    "estimated_limit_max20x": 220000,
    "percent_max5x": 28.4091,
    "percent_max20x": 11.3636
  },
  "window_24h": {
    "tokens_used": 90000,
    "calls": 40,
    "cost_usd": 30.0
  }
}
```

### `GET /api/timeseries?metric=cost&days=N`

Supported metrics: `cost`, `calls`, `input_tokens`, `output_tokens`, `profanity`, `frustration`.

```json
{
  "metric": "cost",
  "period": {"start": "2026-05-01", "end": "2026-05-09", "days": 9},
  "data": [
    {"date": "2026-05-01", "value": 1.25},
    {"date": "2026-05-02", "value": 0.0}
  ]
}
```

### `GET /api/sessions?limit=20`

```json
{
  "sessions": [
    {
      "session_id": "485d6020-6057-42ae-b657-b89dd1dda900",
      "first_ts": "2026-05-09T13:49:02+03:00",
      "last_ts": "2026-05-09T13:53:51+03:00",
      "calls": 2,
      "cost_usd": 1.0249,
      "model_primary": "claude-opus-4-7",
      "task": {
        "brief_description": "implemented tracking hook",
        "ai_baseline_hours": 4.5,
        "profanity_count": 0,
        "mood_arc": "focused→calm"
      }
    }
  ]
}
```

`task` is `null` when `tracker/tasks.json` has no matching entry.

## Architecture

- stdlib `http.server.ThreadingHTTPServer`
- Reuses logic from `tracker/summary.py` через import
- Reads `tracker/claude-events.jsonl` + `tracker/tasks.json` on each request (no cache)
- CORS allow-all для локального development
- No auth (single-user, localhost only)

## Future (Phase 3)

Phase 3 frontend будет poll'ить эти endpoint'ы для cyberpunk dashboard.
WebSocket / SSE — out of scope для MVP.
