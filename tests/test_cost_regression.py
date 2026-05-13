from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.cost_regression import detect_regressions


NOW = datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc)


def event(day_offset: int, *, provider: str = "openai", model: str = "gpt-test", output: int = 10, rate: float = 0.001) -> dict:
    ts = NOW - timedelta(days=day_offset)
    return {
        "ts": ts.isoformat(),
        "provider": provider,
        "model": model,
        "output_tokens": output,
        "cost_estimate_usd": output * rate,
    }


def make_series(
    *,
    provider: str = "openai",
    model: str = "gpt-test",
    short_rate: float,
    long_target_rate: float,
    output_per_day: int = 10,
) -> list[dict]:
    events = [
        event(day, provider=provider, model=model, output=output_per_day, rate=short_rate)
        for day in range(0, 7)
    ]
    short_cost = 7 * output_per_day * short_rate
    total_output = 30 * output_per_day
    old_output = 23 * output_per_day
    old_rate = ((total_output * long_target_rate) - short_cost) / old_output
    events.extend(
        event(day, provider=provider, model=model, output=output_per_day, rate=old_rate)
        for day in range(7, 30)
    )
    return events


class CostRegressionTests(unittest.TestCase):
    def test_no_events_returns_empty_regressions(self) -> None:
        payload = detect_regressions([], now=NOW)

        self.assertEqual([], payload["regressions"])
        self.assertEqual(0, payload["summary"]["regressions_count"])
        self.assertEqual(0, payload["summary"]["total_pairs_scanned"])

    def test_no_regression_when_ratio_below_threshold(self) -> None:
        events = [event(day, rate=0.001) for day in range(30)]

        payload = detect_regressions(events, now=NOW, threshold=1.2)

        self.assertEqual([], payload["regressions"])
        self.assertEqual(1, payload["summary"]["pairs_with_min_calls"])

    def test_regression_detected_when_threshold_exceeded(self) -> None:
        events = make_series(short_rate=0.0015, long_target_rate=0.001)

        payload = detect_regressions(events, now=NOW, threshold=1.2)

        self.assertEqual(1, payload["summary"]["regressions_count"])
        regression = payload["regressions"][0]
        self.assertEqual("openai", regression["provider"])
        self.assertEqual("gpt-test", regression["model"])
        self.assertAlmostEqual(0.0015, regression["cost_per_otok_7d"])
        self.assertAlmostEqual(0.001, regression["cost_per_otok_30d"])
        self.assertAlmostEqual(1.5, regression["ratio"])

    def test_min_calls_filter_excludes_low_volume(self) -> None:
        events = make_series(short_rate=0.01, long_target_rate=0.001, output_per_day=1)

        payload = detect_regressions(events, now=NOW, threshold=1.2, min_calls=10)

        self.assertEqual([], payload["regressions"])
        self.assertEqual(0, payload["summary"]["pairs_with_min_calls"])

    def test_window_narrowing_finds_contiguous_elevated_days(self) -> None:
        events = []
        for day in range(0, 4):
            events.append(event(day, rate=0.002))
        for day in range(4, 7):
            events.append(event(day, rate=0.001))
        short_cost = (4 * 10 * 0.002) + (3 * 10 * 0.001)
        total_output = 30 * 10
        old_rate = ((total_output * 0.0012) - short_cost) / (23 * 10)
        for day in range(7, 30):
            events.append(event(day, rate=old_rate))

        payload = detect_regressions(events, now=NOW, threshold=1.2)

        regression = payload["regressions"][0]
        self.assertEqual("2026-05-10", regression["window_start"])
        self.assertEqual("2026-05-13", regression["window_end"])

    def test_regressions_sorted_by_ratio_desc(self) -> None:
        events = []
        events.extend(
            make_series(
                provider="openai",
                model="gpt-mid",
                short_rate=0.0015,
                long_target_rate=0.001,
            )
        )
        events.extend(
            make_series(
                provider="anthropic",
                model="claude-high",
                short_rate=0.002,
                long_target_rate=0.001,
            )
        )

        payload = detect_regressions(events, now=NOW, threshold=1.2)

        self.assertEqual(["claude-high", "gpt-mid"], [item["model"] for item in payload["regressions"]])
        self.assertGreater(payload["regressions"][0]["ratio"], payload["regressions"][1]["ratio"])


if __name__ == "__main__":
    unittest.main()
