"""OpenAI/Codex pricing must follow the official rate card AND its history.

Regression (found 2026-09-10): gpt-6-astra (launched 2026-09-03) was missing
from the price map and silently billed at the old Sol fallback — $273.86 in the
ledger against $720.50 at official rates. Meanwhile OpenAI cut GPT-5.6 prices
(Terra/Luna on 2026-07-30, Sol on 2026-08-21) and the ledger kept the old rates.

Source: developers.openai.com/api/docs/pricing, .../models/gpt-6-astra and
.../changelog, fetched 2026-09-10. Price changes apply by the event's UTC date:
history before a cut keeps the rate that was in force at the time.
"""

from __future__ import annotations

import importlib.util
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ct = _load("codex_track_under_test", ROOT / "tracker" / "codex-track.py")

# 1M input of which 800k cached, 10k output — easy to verify by hand.
TOKENS = (1_000_000, 800_000, 10_000, 0)


def cost(model, tokens=TOKENS, at=None):
    return ct.estimate_cost(model, *tokens, at=at)


class RateHistoryTests(unittest.TestCase):
    def test_sol_before_cut_keeps_old_rate(self):
        # 200k x $5 + 800k x $0.50 + 10k x $30 = $1.70
        self.assertAlmostEqual(cost("gpt-5.6-sol", at="2026-08-20T23:00:00+00:00"), 1.70, places=6)

    def test_sol_from_cut_date_uses_new_rate(self):
        # 200k x $4 + 800k x $0.40 + 10k x $20 = $1.32
        self.assertAlmostEqual(cost("gpt-5.6-sol", at="2026-08-21T00:30:00+00:00"), 1.32, places=6)

    def test_cut_boundary_is_utc_date(self):
        # 02:00 Moscow on Aug 21 is still Aug 20 in UTC -> old rate. Pinned so the
        # boundary rule is explicit rather than machine-timezone dependent.
        self.assertAlmostEqual(cost("gpt-5.6-sol", at="2026-08-21T02:00:00+03:00"), 1.70, places=6)

    def test_terra_cut_on_july_30(self):
        self.assertAlmostEqual(cost("gpt-5.6-terra", at="2026-07-29T12:00:00Z"), 0.85, places=6)
        self.assertAlmostEqual(cost("gpt-5.6-terra", at="2026-07-30T12:00:00Z"), 0.68, places=6)

    def test_luna_cut_on_july_30(self):
        self.assertAlmostEqual(cost("gpt-5.6-luna", at="2026-07-29T12:00:00Z"), 0.34, places=6)
        self.assertAlmostEqual(cost("gpt-5.6-luna", at="2026-07-30T12:00:00Z"), 0.068, places=6)

    def test_no_timestamp_means_current_rate(self):
        # The live wrapper prices events as they happen.
        self.assertAlmostEqual(cost("gpt-5.6-sol"), 1.32, places=6)

    def test_datetime_and_date_inputs_are_accepted(self):
        before = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        self.assertAlmostEqual(cost("gpt-5.6-sol", at=before), 1.70, places=6)
        self.assertAlmostEqual(cost("gpt-5.6-sol", at=date(2026, 8, 21)), 1.32, places=6)

    def test_prefix_variant_follows_rate_history(self):
        self.assertAlmostEqual(cost("gpt-5.6-sol-ultra", at="2026-09-01T00:00:00Z"), 1.32, places=6)

    def test_models_without_price_change_are_date_independent(self):
        self.assertAlmostEqual(
            cost("gpt-5.5", at="2026-05-01T00:00:00Z"),
            cost("gpt-5.5", at="2026-09-10T00:00:00Z"),
            places=6,
        )


class AstraTests(unittest.TestCase):
    def test_astra_is_priced_not_defaulted(self):
        self.assertIsNot(ct.pricing_for_model("gpt-6-astra"), ct.DEFAULT_PRICING)

    def test_astra_short_context(self):
        # 10k x $10 + 90k x $1 + 1k x $50 = $0.24
        self.assertAlmostEqual(cost("gpt-6-astra", (100_000, 90_000, 1_000, 0)), 0.24, places=6)

    def test_astra_long_context_full_request_multiplier(self):
        # >272K input: input and cache x2, output x1.5, for the whole request.
        # 20k x $20 + 280k x $2 + 2k x $75 = $1.11
        self.assertAlmostEqual(cost("gpt-6-astra", (300_000, 280_000, 2_000, 0)), 1.11, places=6)

    def test_astra_threshold_is_strictly_greater_than_272k(self):
        self.assertAlmostEqual(cost("gpt-6-astra", (272_000, 0, 0, 0)), 2.72, places=6)
        self.assertAlmostEqual(cost("gpt-6-astra", (272_001, 0, 0, 0)), 5.44002, places=6)


class InvariantTests(unittest.TestCase):
    def test_reasoning_is_inside_output_not_billed_twice(self):
        with_reasoning = cost("gpt-5.6-sol", (1_000_000, 800_000, 10_000, 5_000), at="2026-09-01T00:00:00Z")
        without = cost("gpt-5.6-sol", (1_000_000, 800_000, 10_000, 0), at="2026-09-01T00:00:00Z")
        self.assertAlmostEqual(with_reasoning, without, places=6)

    def test_unknown_model_uses_default_regardless_of_date(self):
        self.assertIs(ct.pricing_for_model("codex-auto-review", at="2026-09-01T00:00:00Z"), ct.DEFAULT_PRICING)

    def test_recost_provider_passes_event_timestamp(self):
        recost = _load("recost_under_test", ROOT / "tracker" / "recost.py")
        provider = recost.CodexProvider()
        event = {
            "model": "gpt-5.6-sol",
            "input_tokens": 1_000_000,
            "cached_input_tokens": 800_000,
            "output_tokens": 10_000,
            "reasoning_tokens": 0,
        }
        self.assertAlmostEqual(provider.cost({**event, "ts": "2026-08-01T10:00:00+03:00"}), 1.70, places=6)
        self.assertAlmostEqual(provider.cost({**event, "ts": "2026-09-01T10:00:00+03:00"}), 1.32, places=6)


if __name__ == "__main__":
    unittest.main()
