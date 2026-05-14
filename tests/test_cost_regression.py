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
    baseline_rate: float,
    output_per_day: int = 10,
) -> list[dict]:
    """Synthesize events with `short_rate` for the recent 7d and `baseline_rate`
    for the prior 23d (days 7-29 ago). Baseline period is constructed directly
    — no compensation math — so tests assert the rates the detector should see
    after the fix that excludes the short window from the baseline.
    """
    events = [
        event(day, provider=provider, model=model, output=output_per_day, rate=short_rate)
        for day in range(0, 7)
    ]
    events.extend(
        event(day, provider=provider, model=model, output=output_per_day, rate=baseline_rate)
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
        self.assertEqual(1, payload["summary"]["pairs_with_min_output_tokens"])

    def test_regression_detected_when_threshold_exceeded(self) -> None:
        events = make_series(short_rate=0.0015, baseline_rate=0.001)

        payload = detect_regressions(events, now=NOW, threshold=1.2)

        self.assertEqual(1, payload["summary"]["regressions_count"])
        regression = payload["regressions"][0]
        self.assertEqual("openai", regression["provider"])
        self.assertEqual("gpt-test", regression["model"])
        self.assertAlmostEqual(0.0015, regression["cost_per_otok_7d"])
        self.assertAlmostEqual(0.001, regression["cost_per_otok_30d"])
        self.assertAlmostEqual(1.5, regression["ratio"])

    def test_min_calls_filter_excludes_low_volume(self) -> None:
        events = make_series(short_rate=0.01, baseline_rate=0.001, output_per_day=1)

        payload = detect_regressions(events, now=NOW, threshold=1.2, min_calls=10)

        self.assertEqual([], payload["regressions"])
        self.assertEqual(0, payload["summary"]["pairs_with_min_output_tokens"])

    def test_window_narrowing_finds_contiguous_elevated_days(self) -> None:
        # Short window has 4 elevated days (offset 0-3 = days 2026-05-10..13)
        # followed by 3 normal days; baseline period is uniform.
        events = []
        for day in range(0, 4):
            events.append(event(day, rate=0.002))  # elevated
        for day in range(4, 7):
            events.append(event(day, rate=0.001))  # normal
        for day in range(7, 30):
            events.append(event(day, rate=0.001))  # baseline

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
                baseline_rate=0.001,
            )
        )
        events.extend(
            make_series(
                provider="anthropic",
                model="claude-high",
                short_rate=0.002,
                baseline_rate=0.001,
            )
        )

        payload = detect_regressions(events, now=NOW, threshold=1.2)

        self.assertEqual(["claude-high", "gpt-mid"], [item["model"] for item in payload["regressions"]])
        self.assertGreater(payload["regressions"][0]["ratio"], payload["regressions"][1]["ratio"])

    # DeepSeek MED #1 on PR #83: longest contiguous elevated run wins; zigzag
    # pattern no longer collapses to a 1-day window.
    def test_zigzag_window_picks_longest_run_not_last(self) -> None:
        # 30d baseline. 7d window has zigzag: days 0,2 normal; days 4,5,6 elevated (3-day run).
        # Without longest-run logic, _narrow_window returned just day 0 (the last elevated singleton).
        # Now it should return the 3-day run (days 4..6 offset, i.e. earliest 3 days).
        events = [event(day, rate=0.001) for day in range(30)]
        # Replace days 4, 5, 6 (offsets — earlier in time) with elevated rates.
        # In day_offset terms: 0 = today, 6 = 6 days ago. Days 4-6 = 4-6 days ago = earliest part of 7d.
        elevated_events = [event(day, rate=0.005) for day in (4, 5, 6)]
        # Remove the baseline-rate entries for those days, replace with elevated.
        events = [e for e in events if e["ts"] not in {ev["ts"] for ev in elevated_events}]
        events.extend(elevated_events)

        payload = detect_regressions(events, now=NOW, threshold=1.2)

        # A regression should be flagged.
        self.assertGreaterEqual(payload["summary"]["regressions_count"], 1)
        reg = payload["regressions"][0]
        window_start = reg["window_start"]
        window_end = reg["window_end"]
        # The narrowing should reflect the 3-day run (days -6, -5, -4 from today),
        # not collapse to a 1-day singleton.
        start_date = datetime.fromisoformat(window_start).date()
        end_date = datetime.fromisoformat(window_end).date()
        span = (end_date - start_date).days + 1
        self.assertGreaterEqual(span, 3, f"expected ≥3-day run, got {span}-day window {window_start}..{window_end}")

    # DeepSeek LOW #3 on PR #83: boundary guards (zero cost/output).
    def test_zero_long_cost_skipped(self) -> None:
        # All events have zero cost; should never flag a regression.
        events = [event(day, rate=0.0) for day in range(30)]
        payload = detect_regressions(events, now=NOW, threshold=1.2)
        self.assertEqual(0, payload["summary"]["regressions_count"])

    def test_zero_short_output_skipped(self) -> None:
        # Only old events (day_offset > 6); 7d window has 0 output_tokens.
        events = [event(day, rate=0.001) for day in range(7, 30)]
        payload = detect_regressions(events, now=NOW, threshold=1.2)
        self.assertEqual(0, payload["summary"]["regressions_count"])

    def test_zero_long_output_skipped(self) -> None:
        # All events have output_tokens=0 but non-zero cost (cache-only scenario).
        events = [
            {"ts": (NOW - timedelta(days=d)).isoformat(),
             "provider": "openai",
             "model": "cache-only",
             "output_tokens": 0,
             "cost_estimate_usd": 0.001}
            for d in range(30)
        ]
        payload = detect_regressions(events, now=NOW, threshold=1.2)
        self.assertEqual(0, payload["summary"]["regressions_count"])


class BaselineWindowExcludesShortTests(unittest.TestCase):
    """Baseline window MUST NOT include the recent short window — otherwise the
    spike pulls up its own baseline and dampens the ratio (the bug fixed for
    model_slippage in PR #99 fix #4 was architecturally identical here).
    """

    def test_baseline_rate_excludes_short_window_in_payload(self) -> None:
        # Recent 7d at 5x baseline; if baseline overlaps short, cost_per_otok_30d
        # is contaminated upward and ratio dampens.
        events = make_series(short_rate=0.005, baseline_rate=0.001)
        payload = detect_regressions(events, now=NOW, threshold=1.2)

        regression = payload["regressions"][0]
        # baseline rate equals the constructed baseline_rate exactly — proof
        # that days 0-6 are not aggregated into cost_per_otok_baseline.
        self.assertAlmostEqual(0.001, regression["cost_per_otok_baseline"])
        # Deprecated alias mirrors the same value for backward compat.
        self.assertAlmostEqual(0.001, regression["cost_per_otok_30d"])
        self.assertAlmostEqual(0.005, regression["cost_per_otok_7d"])
        self.assertAlmostEqual(5.0, regression["ratio"])

    def test_overlap_signal_sharpens_in_14d_vs_30d_config(self) -> None:
        # 14 days elevated + 16 days normal in a 14d/30d config. With overlap-
        # included logic, the 30d aggregate is pulled up by the 14 elevated
        # days → ratio dampens. With baseline-only logic, ratio is sharp.
        events = []
        for d in range(14):
            events.append(event(d, rate=0.005))
        for d in range(14, 30):
            events.append(event(d, rate=0.001))

        payload = detect_regressions(
            events, now=NOW, threshold=1.2, short_days=14, long_days=30
        )

        regression = payload["regressions"][0]
        self.assertAlmostEqual(0.005, regression["cost_per_otok_7d"])
        self.assertAlmostEqual(0.001, regression["cost_per_otok_30d"])
        # Sharp 5.0; old overlap-included logic gave ~2.78 here.
        self.assertGreaterEqual(regression["ratio"], 4.0)

    def test_no_regression_when_baseline_empty(self) -> None:
        # Only the recent 7d has data; baseline period has zero events.
        # baseline_cost == 0 → skip (no division-by-zero, no false-positive
        # against a non-existent baseline).
        events = [event(day, rate=0.005) for day in range(7)]
        payload = detect_regressions(events, now=NOW, threshold=1.2)
        self.assertEqual([], payload["regressions"])


if __name__ == "__main__":
    unittest.main()
