from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.slop_score import (
    DEFAULT_WEIGHTS,
    aggregate,
    main,
    score_run,
    score_text,
)


class ScoreTextTests(unittest.TestCase):
    def test_empty_text_scores_zero(self) -> None:
        result = score_text("")
        self.assertEqual(0.0, result["score"])
        self.assertEqual(0, result["word_count"])

    def test_imperative_action_text_scores_low(self) -> None:
        # Lots of action verbs, no hedge, no generic phrases, no trigram repeats.
        text = (
            "Run the migration. Verify the snapshot. Build the dashboard. "
            "Commit the diff. Push to main. Merge after review. "
            "Restart the worker. Validate the output."
        )
        result = score_text(text)
        self.assertLess(result["score"], 25.0, f"got {result}")

    def test_generic_filler_text_scores_high(self) -> None:
        # Stock phrases + hedge language + no action verbs.
        text = (
            "In summary, it is important to note that you might consider whether "
            "the approach could possibly work. In general, it appears to be "
            "potentially viable. That being said, let me know if you have "
            "questions. I hope this helps. In conclusion, please note that "
            "broadly speaking, you should perhaps consider the trade-offs."
        )
        result = score_text(text)
        self.assertGreater(result["score"], 40.0, f"got {result}")

    def test_repeated_trigrams_increase_score(self) -> None:
        unique = "Apple banana cherry. Dog elephant fox. Goat horse iguana."
        repeat = "The quick brown fox. The quick brown fox. The quick brown fox. The quick brown fox."
        s_unique = score_text(unique)
        s_repeat = score_text(repeat)
        self.assertGreater(
            s_repeat["components"]["trigram_repetition"],
            s_unique["components"]["trigram_repetition"],
        )

    def test_hedge_only_caps_at_one(self) -> None:
        text = "might could should perhaps possibly potentially consider"
        result = score_text(text)
        # All hedges, no imperatives → ratio component = 1.0
        self.assertAlmostEqual(1.0, result["components"]["hedge_imperative_ratio"], places=3)

    def test_imperative_only_zeros_hedge_signal(self) -> None:
        text = "Run build commit push merge deploy test validate scan"
        result = score_text(text)
        self.assertEqual(0.0, result["components"]["hedge_imperative_ratio"])

    def test_overrides_let_caller_swap_blocklists(self) -> None:
        text = "this phrase should hit the override blocklist"
        # Without override: no hits expected.
        baseline = score_text(text)
        # With override: phrase listed → density rises.
        override = score_text(text, config={"generic_phrases": ("override blocklist",)})
        self.assertGreaterEqual(
            override["components"]["generic_phrase_density"],
            baseline["components"]["generic_phrase_density"],
        )
        self.assertGreater(
            override["components"]["generic_phrase_density"], 0.0
        )

    def test_custom_weights_change_total(self) -> None:
        text = (
            "In summary, it might be worth considering. "
            "Run the build. Verify outputs."
        )
        baseline = score_text(text)
        # Heavily upweight generic phrases:
        boosted = score_text(text, config={"weights": {"trigram": 0.1, "generic": 0.8, "hedge": 0.1}})
        self.assertNotEqual(baseline["score"], boosted["score"])

    def test_components_present_in_result(self) -> None:
        result = score_text("Some text.")
        self.assertIn("trigram_repetition", result["components"])
        self.assertIn("generic_phrase_density", result["components"])
        self.assertIn("hedge_imperative_ratio", result["components"])


class AggregateTests(unittest.TestCase):
    def test_aggregate_buckets_by_session_agent_role(self) -> None:
        scored = [
            {"run_id": "r1", "agent": "claude", "role": "architect", "score": 20.0, "created_at": "2026-05-12T09:00:00+03:00"},
            {"run_id": "r1", "agent": "codex", "role": "developer", "score": 40.0, "created_at": "2026-05-12T09:00:00+03:00"},
            {"run_id": "r2", "agent": "claude", "role": "architect", "score": 30.0, "created_at": "2026-05-13T09:00:00+03:00"},
        ]
        summary = aggregate(scored)
        self.assertEqual(2, len(summary["sessions"]))
        self.assertEqual(3, summary["messages_scored"])

        claude_arch = next(e for e in summary["by_agent_role"]
                           if e["agent"] == "claude" and e["role"] == "architect")
        self.assertEqual(25.0, claude_arch["mean_score"])
        self.assertEqual(2, claude_arch["samples"])

    def test_sessions_sorted_by_created_at(self) -> None:
        scored = [
            {"run_id": "rB", "agent": "claude", "role": "architect", "score": 10.0, "created_at": "2026-05-14T09:00:00+03:00"},
            {"run_id": "rA", "agent": "claude", "role": "architect", "score": 20.0, "created_at": "2026-05-12T09:00:00+03:00"},
        ]
        summary = aggregate(scored)
        self.assertEqual(["rA", "rB"], [s["run_id"] for s in summary["sessions"]])

    def test_empty_input_returns_clean_zero_payload(self) -> None:
        summary = aggregate([])
        self.assertEqual(0, summary["messages_scored"])
        self.assertEqual([], summary["sessions"])
        self.assertEqual([], summary["by_agent"])


class ScoreRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.run_dir = self.tmp / "20260501T1200-abc123"
        self.run_dir.mkdir()
        (self.run_dir / "01-architect.md").write_text(
            "Run the build. Verify outputs. Commit and merge.",
            encoding="utf-8",
        )
        (self.run_dir / "02-developer.md").write_text(
            "In summary, it might be worth considering that perhaps the build "
            "could possibly succeed. That being said, let me know if you have "
            "questions. I hope this helps.",
            encoding="utf-8",
        )
        (self.run_dir / "state.json").write_text(
            json.dumps({
                "schema_version": 1,
                "run_id": "20260501T1200-abc123",
                "created_at": "2026-05-01T12:00:00+03:00",
                "roles_path": str(self.run_dir / "roles.used.toml"),
                "steps": [
                    {"index": 0, "role": "architect", "response_path": str(self.run_dir / "01-architect.md")},
                    {"index": 1, "role": "developer", "response_path": str(self.run_dir / "02-developer.md")},
                ],
            }),
            encoding="utf-8",
        )
        (self.run_dir / "roles.used.toml").write_text(
            '[role.architect]\nprovider = "claude"\n\n[role.developer]\nprovider = "codex"\n',
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_score_run_emits_one_entry_per_step(self) -> None:
        scored = score_run(self.run_dir)
        self.assertEqual(2, len(scored))
        roles = {s["role"] for s in scored}
        self.assertEqual({"architect", "developer"}, roles)

    def test_score_run_resolves_agent_from_roles_toml(self) -> None:
        scored = score_run(self.run_dir)
        by_role = {s["role"]: s["agent"] for s in scored}
        self.assertEqual("claude", by_role["architect"])
        self.assertEqual("codex", by_role["developer"])

    def test_developer_filler_scores_higher_than_architect(self) -> None:
        scored = score_run(self.run_dir)
        by_role = {s["role"]: s["score"] for s in scored}
        self.assertGreater(by_role["developer"], by_role["architect"])

    def test_score_run_missing_state_returns_empty(self) -> None:
        empty_dir = self.tmp / "no-state"
        empty_dir.mkdir()
        self.assertEqual([], score_run(empty_dir))


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.runs_dir = self.tmp / "orchestrate-runs"
        self.runs_dir.mkdir()
        run_dir = self.runs_dir / "20260512T1200-test"
        run_dir.mkdir()
        (run_dir / "01-architect.md").write_text("Run the build. Verify outputs.", encoding="utf-8")
        (run_dir / "state.json").write_text(
            json.dumps({
                "run_id": "20260512T1200-test",
                "created_at": "2026-05-12T12:00:00+03:00",
                "steps": [{"role": "architect", "response_path": str(run_dir / "01-architect.md")}],
            }),
            encoding="utf-8",
        )
        self.output = self.tmp / "slop.json"

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cli_writes_slop_json(self) -> None:
        rc = main(["--runs-dir", str(self.runs_dir), "--output", str(self.output)])
        self.assertEqual(0, rc)
        payload = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(1, payload["summary"]["messages_scored"])
        self.assertIn("config", payload)
        self.assertIn("by_agent_role", payload["summary"])

    def test_cli_exits_2_on_missing_runs_dir(self) -> None:
        rc = main(["--runs-dir", str(self.tmp / "nope"), "--output", str(self.output)])
        self.assertEqual(2, rc)


class WeightsTests(unittest.TestCase):
    def test_default_weights_sum_to_one(self) -> None:
        total = sum(DEFAULT_WEIGHTS.values())
        self.assertAlmostEqual(1.0, total, places=4)


if __name__ == "__main__":
    unittest.main()
