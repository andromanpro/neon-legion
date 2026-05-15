# Task: P2 wow #4 — Model Slippage Detector

Name: codex-developer
Profile: Codex CLI 0.128+ (gpt-5.5, high reasoning, --sandbox workspace-write)
Goal: `tools/model_slippage.py` — sibling to #39 Cost Regression but on **task shape**, not raw cost/token. Fingerprint each event by (provider, model, prompt_size_bucket), track median + p95 cost per fingerprint over 7d vs 30d windows, emit `slippage.json` with degraded fingerprints. Stdlib only.
Constraints: stdlib only, same atomic-write/CLI pattern as `cost_regression.py`, sparse-fingerprint tolerant (skip if <min_events in 7d), no schema changes to tracker JSONL.
Watches: Gitea issue [#40](http://localhost:3000/androman/neon-legion/issues/40), `tools/cost_regression.py` (architecture sibling), `tracker/summary.py` (event reader), `tracker/*-events.jsonl` (data source — `input_tokens` + `cost_estimate_usd` + `model` + `provider`).
Produces: 1 new file (`tools/model_slippage.py` ~150 LOC), 1 new file (`tests/test_model_slippage.py` ~140 LOC), modified `config.example.toml` (`[model_slippage]` section), `wp-dev/tools/deploy-snapshot.sh` (architect-wired manually as before).

## Operational backstory

Sibling to #39 (just shipped, PRs #83+#84). Same rolling-window architecture, different axis:
- **#39 Cost Regression**: per (provider, model) — cost per output token, 7d vs 30d.
- **#40 Model Slippage**: per (provider, model, prompt_size_bucket) — median cost per call.

Why bucket by prompt size: a "small refactor" (input ≤ 1k tokens) has fundamentally different economics than a "large feature spec" (input ≥ 100k). Aggregating them together hides drift in either tier. Buckets stratify the data so a 30% degradation in the small-task tier (most common) doesn't get washed out by stable large-task baseline.

**Retry count**: spec mentions retry tracking, but no session-retry data column exists in tracker events. **Out of MVP scope** — note as deferred. The cost-side detection is the immediately shippable wedge.

Tests run on host.

## Working directory

`<project-dir>` (already your `--cd`).

## Project context

Read in order:
1. `tools/cost_regression.py` — `detect_regressions`, atomic write, CLI shape. Sibling to mirror.
2. `tracker/summary.py` — `read_events(start, end)`, `as_int`/`as_float`/`event_provider`.
3. `tracker/*-events.jsonl` — sample lines to confirm `input_tokens`, `output_tokens`, `model`, `provider`, `cost_estimate_usd`, `ts` fields present.

## Fingerprint definition

```python
def _fingerprint(event: dict) -> tuple[str, str, str]:
    provider = summary.event_provider(event)
    model = event.get("model") or "unknown"
    input_tokens = summary.as_int(event.get("input_tokens"))
    bucket = _prompt_size_bucket(input_tokens)
    return (provider, model, bucket)

def _prompt_size_bucket(tokens: int) -> str:
    # Stratified: most Claude tasks land in 1k-10k; codex_exec lands in 10k-100k.
    if tokens < 1_000:   return "xs"        # <1k
    if tokens < 10_000:  return "s"         # 1k-10k
    if tokens < 100_000: return "m"         # 10k-100k
    return "l"                              # ≥100k
```

Skip pairs with `events_7d < min_events` (default 5) — fingerprints with too few samples produce noisy medians.

## Detection logic

For each fingerprint in window:
1. Group events by fingerprint.
2. Compute `median_cost_7d` = median(cost_estimate_usd over events in last 7d for this fingerprint).
3. Compute `median_cost_30d` = median(cost_estimate_usd over events in last 30d).
4. Compute `p95_cost_7d` for context (helps spot tail degradation).
5. If `median_cost_7d / median_cost_30d > threshold` (default 1.3 — slightly looser than #39's 1.2 because per-event cost is noisier than aggregate cost-per-output-token) → flag as slippage.
6. Skip if `events_7d < min_events` or `median_cost_30d == 0`.

`slippage.json` schema:

```json
{
  "schema_version": 1,
  "generated_at": "...",
  "config": {"threshold": 1.3, "min_events": 5, "short_days": 7, "long_days": 30},
  "slippages": [
    {
      "provider": "anthropic",
      "model": "claude-sonnet-4",
      "prompt_size_bucket": "m",
      "events_7d": 23,
      "events_30d": 87,
      "median_cost_7d": 0.0421,
      "median_cost_30d": 0.0298,
      "p95_cost_7d": 0.1234,
      "ratio": 1.413,
      "delta_pct": 41.3
    }
  ],
  "summary": {
    "total_fingerprints_scanned": 42,
    "fingerprints_with_min_events": 18,
    "slippages_count": 1,
    "retry_tracking": "deferred — no session-retry column in tracker events"
  }
}
```

Sorted by descending ratio.

## API

```python
def detect_slippages(
    events: list[dict],
    *,
    now: datetime | None = None,
    threshold: float = 1.3,
    min_events: int = 5,
    short_days: int = 7,
    long_days: int = 30,
) -> dict:
    """Return slippage.json payload."""

def write_slippages(payload: dict, output_path: Path) -> None:
    """Atomic tmp + os.replace."""

def main() -> int:
    """CLI."""
```

CLI:
```
py -3.14 tools/model_slippage.py [--output PATH] [--threshold 1.3] [--min-events 5]
```

Defaults from `config.toml [model_slippage]`.

## Tests

≥6 unit tests in `tests/test_model_slippage.py`:

1. `test_no_events_returns_empty_slippages` — empty input → `slippages: []`.
2. `test_no_slippage_when_costs_stable` — synthetic events with identical cost across 30d → no flag.
3. `test_slippage_detected_when_7d_median_jumps` — events where 7d median is 1.5x of 30d → flagged.
4. `test_min_events_filter_excludes_low_volume` — fingerprint with 3 events in 7d → not flagged.
5. `test_bucket_stratification_isolates_small_from_large` — fixture with small (xs) AND large (l) events of same model; only one bucket spikes; only that bucket flagged.
6. `test_p95_reported_for_each_slippage` — flagged entry has `p95_cost_7d` field.

Tests must run in <1 s — use synthetic event lists, not real `tracker.read_events`.

## Acceptance criteria

- `py -3.14 -c "from tools.model_slippage import detect_slippages, write_slippages; print('ok')"` prints `ok`.
- `py -3.14 -m unittest tests.test_model_slippage -v` — all ≥6 tests pass.
- `py -3.14 -m py_compile tools/model_slippage.py tests/test_model_slippage.py` exits 0.
- `py -3.14 tools/model_slippage.py --help` shows config flags.
- Real-data smoke against `tracker/*-events.jsonl`: produces valid JSON with non-empty `fingerprints_with_min_events` count.
- Stdlib only.
- Full repo suite stays green.

## Out of scope

- Retry count detection (no session-retry data column — would need a separate enhancement to tracker schema).
- Manifest signature in fingerprint (Codex calls don't go through manifests — would only cover orchestrate.py runs, sparse).
- Dashboard widget JS/PHP.

## Style / project conventions

- Match shape of `tools/cost_regression.py`.
- `from __future__ import annotations`.
- No `Co-Authored-By:`.
- Atomic write: tmp + os.replace.
- Logging: `print(f"[model-slippage] ...", file=sys.stderr)`.
- `.gitignore` already excludes `tracker/*.json` after #42 — add `tracker/slippage.json` if not already covered, or document.

## Self-check before "done"

- Tests pass on host.
- `--help` shows config flags.
- Real-data smoke produces valid JSON.
- Sparse fingerprints (events_7d < min_events) excluded from slippages.
- p95 computed from short window only (operator wants "what's bad now", not "what was always bad").

## Final report

Conform to schema. Report real-data smoke result: total_fingerprints + fingerprints_with_min_events + slippages_count + top 1-2 slippage entries.
