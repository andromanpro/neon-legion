# Task: P2 wow #3 — Cost Regression Detector

Name: codex-developer
Profile: Codex CLI 0.128+ (gpt-5.5, high reasoning, --sandbox workspace-write)
Goal: `tools/cost_regression.py` — detect vendor cost-per-output-token regressions by comparing 7d vs 30d rolling windows per (provider, model). Emit `regressions.json` consumable by a future dashboard ticker widget.
Constraints: stdlib only, idempotent (regen every run), config-driven threshold, integrates into existing `deploy-snapshot.sh` pipeline.
Watches: Gitea issue [#39](http://localhost:3000/androman/neon-legion/issues/39), `tools/config.py` (config.toml accessor pattern), `backend/readmodel.py` (data access — can reuse `aggregate_by_model` if it helps, otherwise read JSONL directly), `tracker/*-events.jsonl` (canonical shape), `wp-dev/tools/deploy-snapshot.sh` (wire-up site).
Produces: 1 new file (`tools/cost_regression.py` ~120 LOC), 1 new file (`tests/test_cost_regression.py` ~150 LOC), 1 modified file (`config.example.toml` — add `[cost_regression]` section), 1 modified file (`wp-dev/tools/deploy-snapshot.sh` — emit regressions.json alongside snapshot).

## Operational backstory

P2 wow #3 from the milestone v0.4 wow layer. Recommended first wow per roadmap. Author of the original idea: DeepSeek. Self-contained, removable file in `tools/`, no shared backend changes required.

The detector reads event data and computes per-(provider, model) rolling cost per output token, then flags whenever the 7-day rolling rate exceeds the 30-day rolling rate by a configurable threshold (default 1.2x = 20% increase week-over-week).

Tests run on host. The script itself must be importable and CLI-runnable.

## Working directory

`F:/WorkAI/multi-agent` (already your `--cd`).

## Project context

Read in order:
1. `tools/config.py` — `cfg.get("section.key", default, type)` pattern.
2. `backend/readmodel.py` — `aggregate_by_model(conn, start, end)` is available if you want to use the readmodel; or read JSONL directly via `tracker/summary.read_events`.
3. `tracker/*-events.jsonl` — sample a few lines via grep / head to see canonical event fields (provider, ts, model, output_tokens, cost_estimate_usd).
4. `wp-dev/tools/deploy-snapshot.sh` — existing pipeline that runs Claude backfill + openclaw-watch + snapshot regen + privacy-scan + scp.

## Detection logic

Per (provider, model) pair across all events in the last 30 days:

1. Group events into daily buckets by ISO date (UTC or local — match snapshot.json convention).
2. For each (provider, model):
   - `cost_per_otok_7d` = SUM(cost_estimate_usd, last 7d) / SUM(output_tokens, last 7d)
   - `cost_per_otok_30d` = SUM(cost_estimate_usd, last 30d) / SUM(output_tokens, last 30d)
   - Skip pairs with `output_tokens_7d < min_calls` (configurable, default 10) or `cost_estimate_usd_30d == 0`. Avoids noise on rare models.
3. If `cost_per_otok_7d / cost_per_otok_30d > threshold` (default 1.2):
   - Flag as regression.
   - Compute the **narrowest sub-window** where the bump appears: scan daily buckets within the last 7d, find the contiguous run where daily cost/otok exceeds the 30d baseline by threshold. Report `window_start` (first elevated day) and `window_end` (most recent elevated day).
4. Output: list of regression entries sorted by descending ratio.

## `regressions.json` schema

```json
{
  "schema_version": 1,
  "generated_at": "2026-05-13T22:30:00+03:00",
  "config": {
    "threshold": 1.2,
    "min_calls": 10,
    "window_short_days": 7,
    "window_long_days": 30
  },
  "regressions": [
    {
      "provider": "anthropic",
      "model": "claude-sonnet-4",
      "cost_per_otok_7d": 0.0000128,
      "cost_per_otok_30d": 0.0000095,
      "ratio": 1.347,
      "window_start": "2026-05-09",
      "window_end": "2026-05-12",
      "delta_pct": 34.7
    }
  ],
  "summary": {
    "total_pairs_scanned": 12,
    "pairs_with_min_calls": 8,
    "regressions_count": 1
  }
}
```

When there are no regressions, `regressions` is `[]` and `summary.regressions_count` is `0`. The dashboard widget reads the file unconditionally and shows a ticker only when count > 0.

## API

```python
def detect_regressions(
    events: list[dict],
    *,
    now: datetime | None = None,
    threshold: float = 1.2,
    min_calls: int = 10,
    short_days: int = 7,
    long_days: int = 30,
) -> dict:
    """Return the regressions.json payload dict."""

def write_regressions(payload: dict, output_path: Path) -> None:
    """Atomic write of regressions.json to disk."""

def main() -> int:
    """CLI: read events via readmodel or JSONL, run detect_regressions,
    write regressions.json to configured path."""
```

CLI:
```
py -3.14 tools/cost_regression.py [--output PATH] [--threshold 1.2] [--min-calls 10]
```

Default output path from `config.toml [cost_regression] output_path` (or `tracker/regressions.json` if unset).

## Config integration

Add to `config.example.toml`:

```toml
[cost_regression]
threshold = 1.2          # 7d/30d cost-per-output-token ratio
min_calls = 10           # skip pairs with fewer output tokens in 7d window
short_days = 7
long_days = 30
output_path = "F:/WorkAI/multi-agent/tracker/regressions.json"
```

Read via `cfg.get("cost_regression.threshold", 1.2, float)` etc.

## deploy-snapshot.sh integration

Add a line after the snapshot regen + before privacy-scan:

```bash
log "regen cost-regression detector"
$PY "$MULTI_AGENT_DIR/tools/cost_regression.py" --output "$LOCAL_REGRESSIONS" 2>&1 | tail -2 || log "WARN: cost-regression non-zero exit (continuing)"
```

Where `LOCAL_REGRESSIONS="H:/wordpress-androman/wp-data/wp-content/uploads/multi-agent/regressions.json"`. Add a corresponding `scp` to push it alongside snapshot.json.

## Tests

≥6 tests in `tests/test_cost_regression.py`:

1. `test_no_events_returns_empty_regressions` — empty event list → `regressions: []`.
2. `test_no_regression_when_ratio_below_threshold` — synthetic 7d cost matches 30d → empty list.
3. `test_regression_detected_when_threshold_exceeded` — synthetic 7d cost 1.5x of 30d → flagged with ratio ~1.5.
4. `test_min_calls_filter_excludes_low_volume` — pair with <10 output tokens in 7d → not flagged even if ratio is huge.
5. `test_window_narrowing_finds_contiguous_elevated_days` — fixture with 7d data where days 1-3 are normal and days 4-7 are elevated → `window_start = day 4`, `window_end = day 7`.
6. `test_regressions_sorted_by_ratio_desc` — multiple regressions → highest ratio first.

## Acceptance criteria

- `py -3.14 -c "import sys; sys.path.insert(0, '.'); from tools.cost_regression import detect_regressions, write_regressions; print('ok')"` prints `ok`.
- `py -3.14 -m unittest tests.test_cost_regression -v` — all ≥6 tests pass.
- `py -3.14 -m py_compile tools/cost_regression.py tests/test_cost_regression.py` exits 0.
- `py -3.14 tools/cost_regression.py --help` shows usage.
- `py -3.14 tools/cost_regression.py --output /tmp/regressions-smoke.json` runs against real `tracker/*-events.jsonl`, produces valid JSON matching the schema (architect verifies).
- Full repo suite stays green.
- Stdlib only.

## Out of scope

- Dashboard widget JS/PHP for the ticker line (separate task in `wp-dev/` theme repo).
- Slack / email notification on regression.
- Vendor rate-table lookup to verify the regression is actually a vendor change (could be just usage pattern shift).
- Historical regression archive (just current state).

## Style / project conventions

- Match shape of `backend/readmodel.py` modules.
- `from __future__ import annotations`.
- No `Co-Authored-By:`.
- Atomic write: `tmp + os.replace`.
- Logging: `print(f"[cost-regression] ...", file=sys.stderr)`.

## Self-check before "done"

- Tests pass on host.
- `--help` shows config flags.
- Smoke run against real tracker data produces a valid JSON (might be empty `regressions: []` if no real regression — that's fine).
- `config.example.toml` documents the new section.
- `deploy-snapshot.sh` integration line added.

## Final report

Conform to schema. Report the result of the smoke run (regressions count + summary block).
