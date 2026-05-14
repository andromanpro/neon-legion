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


if __name__ == "__main__":
    unittest.main()
