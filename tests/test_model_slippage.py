from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.model_slippage import detect_slippages


NOW = datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc)


def event(
    day_offset: int,
    *,
    provider: str = "openai",
    model: str = "gpt-test",
    input_tokens: int = 1_500,
    cost: float = 0.01,
) -> dict:
    ts = NOW - timedelta(days=day_offset)
    return {
        "ts": ts.isoformat(),
        "provider": provider,
        "model": model,
        "input_tokens": input_tokens,
        "cost_estimate_usd": cost,
    }


def make_series(
    *,
    provider: str = "openai",
    model: str = "gpt-test",
    input_tokens: int = 1_500,
    short_cost: float,
    long_cost: float,
) -> list[dict]:
    events = [
        event(day, provider=provider, model=model, input_tokens=input_tokens, cost=short_cost)
        for day in range(0, 7)
    ]
    events.extend(
        event(day, provider=provider, model=model, input_tokens=input_tokens, cost=long_cost)
        for day in range(7, 30)
    )
    return events


class ModelSlippageTests(unittest.TestCase):
    def test_no_events_returns_empty_slippages(self) -> None:
        payload = detect_slippages([], now=NOW)

        self.assertEqual([], payload["slippages"])
        self.assertEqual(0, payload["summary"]["slippages_count"])
        self.assertEqual(0, payload["summary"]["total_fingerprints_scanned"])

    def test_no_slippage_when_costs_stable(self) -> None:
        events = [event(day, cost=0.01) for day in range(30)]

        payload = detect_slippages(events, now=NOW, threshold=1.3)

        self.assertEqual([], payload["slippages"])
        self.assertEqual(1, payload["summary"]["fingerprints_with_min_events"])

    def test_slippage_detected_when_7d_median_jumps(self) -> None:
        events = make_series(short_cost=0.015, long_cost=0.01)

        payload = detect_slippages(events, now=NOW, threshold=1.3)

        self.assertEqual(1, payload["summary"]["slippages_count"])
        slippage = payload["slippages"][0]
        self.assertEqual("openai", slippage["provider"])
        self.assertEqual("gpt-test", slippage["model"])
        self.assertEqual("s", slippage["prompt_size_bucket"])
        self.assertAlmostEqual(0.015, slippage["median_cost_7d"])
        self.assertAlmostEqual(0.01, slippage["median_cost_30d"])
        self.assertAlmostEqual(1.5, slippage["ratio"])
        self.assertAlmostEqual(50.0, slippage["delta_pct"])

    def test_min_events_filter_excludes_low_volume(self) -> None:
        events = [
            event(day, cost=0.1)
            for day in range(0, 3)
        ]
        events.extend(event(day, cost=0.01) for day in range(7, 30))

        payload = detect_slippages(events, now=NOW, threshold=1.3, min_events=5)

        self.assertEqual([], payload["slippages"])
        self.assertEqual(0, payload["summary"]["fingerprints_with_min_events"])

    def test_bucket_stratification_isolates_small_from_large(self) -> None:
        events = []
        events.extend(make_series(model="same-model", input_tokens=500, short_cost=0.015, long_cost=0.01))
        events.extend(make_series(model="same-model", input_tokens=150_000, short_cost=0.1, long_cost=0.1))

        payload = detect_slippages(events, now=NOW, threshold=1.3)

        self.assertEqual(1, payload["summary"]["slippages_count"])
        slippage = payload["slippages"][0]
        self.assertEqual("same-model", slippage["model"])
        self.assertEqual("xs", slippage["prompt_size_bucket"])
        self.assertEqual(2, payload["summary"]["fingerprints_with_min_events"])

    def test_p95_reported_for_each_slippage(self) -> None:
        events = make_series(short_cost=0.015, long_cost=0.01)
        events.append(event(0, cost=0.05))

        payload = detect_slippages(events, now=NOW, threshold=1.3)

        slippage = payload["slippages"][0]
        self.assertIn("p95_cost_7d", slippage)
        self.assertAlmostEqual(0.05, slippage["p95_cost_7d"])

    def test_slippages_sorted_by_ratio_desc(self) -> None:
        events = []
        events.extend(make_series(model="gpt-mid", short_cost=0.015, long_cost=0.01))
        events.extend(make_series(model="gpt-high", short_cost=0.02, long_cost=0.01))

        payload = detect_slippages(events, now=NOW, threshold=1.3)

        self.assertEqual(["gpt-high", "gpt-mid"], [item["model"] for item in payload["slippages"]])


class BaselineWindowExcludesShortTests(unittest.TestCase):
    """Regression cover for DeepSeek MED: baseline window MUST NOT include
    the recent short window (otherwise the spike pulls up its own baseline)."""

    def test_baseline_excludes_short_window_in_payload(self) -> None:
        events = make_series(short_cost=0.05, long_cost=0.01)
        payload = detect_slippages(events, now=NOW, threshold=1.3)
        slippage = payload["slippages"][0]
        # New field — explicit baseline count (23 days = long_days - short_days)
        self.assertIn("events_baseline", slippage)
        self.assertEqual(23, slippage["events_baseline"])
        # median_cost_baseline reflects clean baseline (no contamination)
        self.assertIn("median_cost_baseline", slippage)
        self.assertAlmostEqual(0.01, slippage["median_cost_baseline"])

    def test_overlap_signal_sharpens_in_14d_vs_30d_config(self) -> None:
        # 14 events at 0.05 in the recent 14d, 16 events at 0.01 in the prior 16d.
        # OLD overlap-included code: 30d median is pulled up by the 14 high
        # values → median ≈ 0.03 → ratio 0.05/0.03 ≈ 1.67 (dampened).
        # NEW baseline-only code: baseline median = 0.01 (only the 16 prior
        # days) → ratio 0.05/0.01 = 5.0 (full signal).
        events = []
        for d in range(14):
            events.append(event(d, cost=0.05))
        for d in range(14, 30):
            events.append(event(d, cost=0.01))
        payload = detect_slippages(
            events, now=NOW, threshold=1.3, short_days=14, long_days=30
        )
        slippage = payload["slippages"][0]
        # Ratio should be ~5.0 (full signal), not ~1.67 (dampened).
        self.assertGreaterEqual(slippage["ratio"], 4.0)

    def test_no_slippage_when_baseline_empty(self) -> None:
        # Only the recent 7d has data — no events in the prior 23d window.
        # Should skip (median_baseline <= 0), not divide by zero / produce NaN.
        events = [event(day, cost=0.05) for day in range(7)]
        payload = detect_slippages(events, now=NOW, threshold=1.3)
        self.assertEqual([], payload["slippages"])


class BaselineLandscapeTests(unittest.TestCase):
    """The `baselines` array is the cost/prompt-size landscape shown when no
    drift is flagged — one row per qualifying (model, bucket) fingerprint."""

    def test_baselines_emitted_even_with_zero_slippage(self) -> None:
        events = [event(day, cost=0.01) for day in range(30)]  # stable → no drift
        payload = detect_slippages(events, now=NOW, threshold=1.3)

        self.assertEqual([], payload["slippages"])
        self.assertEqual(1, len(payload["baselines"]))
        self.assertEqual(1, payload["summary"]["baselines_count"])
        row = payload["baselines"][0]
        self.assertEqual("openai", row["provider"])
        self.assertEqual("gpt-test", row["model"])
        self.assertEqual("s", row["prompt_size_bucket"])
        self.assertAlmostEqual(0.01, row["median_cost"])
        self.assertIn("p95_cost", row)
        self.assertIn("events", row)

    def test_baselines_one_row_per_bucket_sorted_by_cost_desc(self) -> None:
        events = []
        # xs bucket (cheap), l bucket (pricier) for the same model
        events += [event(day, model="m", input_tokens=100, cost=0.002) for day in range(30)]
        events += [event(day, model="m", input_tokens=90_000, cost=0.05) for day in range(30)]
        payload = detect_slippages(events, now=NOW, threshold=1.3)

        buckets = [b["prompt_size_bucket"] for b in payload["baselines"]]
        # two distinct prompt-size buckets for the same model
        self.assertEqual(2, len(buckets))
        self.assertEqual(2, len(set(buckets)))
        # sorted by median_cost desc → the pricier bucket comes first
        costs = [b["median_cost"] for b in payload["baselines"]]
        self.assertEqual(costs, sorted(costs, reverse=True))
        self.assertGreater(costs[0], costs[1])

    def test_low_volume_fingerprint_excluded_from_baselines(self) -> None:
        events = [event(day, cost=0.01) for day in range(3)]  # < min_events
        payload = detect_slippages(events, now=NOW, threshold=1.3, min_events=5)
        self.assertEqual([], payload["baselines"])


if __name__ == "__main__":
    unittest.main()
