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
    _model_short,
    aggregate,
    main,
    score_run,
    score_text,
    score_transcripts,
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

    def test_multi_word_hedges_now_count(self) -> None:
        # DeepSeek MED: «consider that», «it seems», «in general», etc. were
        # in DEFAULT_HEDGE_WORDS but never matched because `_hedge_imperative`
        # tokenized text into single words then did set membership. Fixed
        # by splitting hedge entries into single-word vs phrase passes.
        text = "Consider that you might want to run the build."
        # 1 multi-word hedge ("consider that") + 1 single hedge ("might")
        # + 1 imperative ("run", "want" not in list — only "run")
        # = h≥2, i=1 → ratio = min(2/1, 1.0) = 1.0
        result = score_text(text)
        self.assertGreater(result["components"]["hedge_imperative_ratio"], 0.5)

    def test_phrase_only_hedges_no_imperatives_caps_at_one(self) -> None:
        # Pure multi-word hedges, no imperatives → cap at 1.0
        text = "It seems that in general the approach tends to work."
        result = score_text(text)
        self.assertAlmostEqual(1.0, result["components"]["hedge_imperative_ratio"])


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

    def test_score_run_handles_single_quoted_roles_toml(self) -> None:
        # DeepSeek MED: the old in-file regex only matched provider = "double".
        # Single-quoted values were silently agent="unknown".
        # Centralized in tools.config.read_role_providers (uses tomllib).
        (self.run_dir / "roles.used.toml").write_text(
            "[role.architect]\nprovider = 'claude'\n\n"
            "[role.developer]\nprovider = 'codex'\n",
            encoding="utf-8",
        )
        scored = score_run(self.run_dir)
        by_role = {s["role"]: s["agent"] for s in scored}
        self.assertEqual("claude", by_role["architect"])
        self.assertEqual("codex", by_role["developer"])


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


class ModelShortTests(unittest.TestCase):
    def test_labels_match_dashboard_style(self) -> None:
        self.assertEqual(_model_short("claude-opus-4-8"), "opus 4.8")
        self.assertEqual(_model_short("claude-fable-5"), "fable 5")
        self.assertEqual(_model_short("claude-sonnet-5"), "sonnet 5")
        self.assertEqual(_model_short("gpt-5.6-sol"), "gpt-5.6-sol")
        self.assertEqual(_model_short(None), "unknown")


class ScoreTranscriptsTests(unittest.TestCase):
    def _write_transcript(self, root: Path, project: str, name: str, lines: list[dict]) -> None:
        d = root / project
        d.mkdir(parents=True, exist_ok=True)
        with (d / name).open("w", encoding="utf-8") as fh:
            for obj in lines:
                fh.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def _assistant(self, session: str, model: str, text: str, ts: str, uuid: str) -> dict:
        return {
            "type": "assistant",
            "session_id": session,
            "uuid": uuid,
            "timestamp": ts,
            "message": {"role": "assistant", "model": model, "content": [{"type": "text", "text": text}]},
        }

    def test_scores_one_item_per_session_model_and_tags_agent(self) -> None:
        from datetime import datetime, timezone

        now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_transcript(root, "projA", "s1.jsonl", [
                self._assistant("s1", "claude-opus-4-8", "Here is a concrete fix. Applied it.", "2026-07-15T10:00:00Z", "u1"),
                self._assistant("s1", "claude-opus-4-8", "Ran the tests, all green.", "2026-07-15T10:05:00Z", "u2"),
                self._assistant("s1", "claude-fable-5", "Quick answer.", "2026-07-15T10:06:00Z", "u3"),
                # user line + synthetic must be ignored
                {"type": "user", "session_id": "s1", "message": {"role": "user", "content": "do it"}},
                self._assistant("s1", "<synthetic>", "ignore", "2026-07-15T10:07:00Z", "u4"),
            ])
            scored = score_transcripts(root, lookback_days=30, now=now)

        agents = {s["agent"] for s in scored}
        self.assertEqual(agents, {"opus 4.8", "fable 5"})
        # opus 4.8 has two messages pooled into ONE (session, model) item
        opus = [s for s in scored if s["agent"] == "opus 4.8"]
        self.assertEqual(len(opus), 1)
        self.assertTrue(all(s["role"] == "assistant" for s in scored))
        self.assertTrue(all(0 <= s["score"] <= 100 for s in scored))

    def test_dedupes_by_uuid_and_respects_window(self) -> None:
        from datetime import datetime, timezone

        now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # same uuid twice (overlapping transcripts) + one out-of-window
            self._write_transcript(root, "projA", "a.jsonl", [
                self._assistant("s1", "claude-opus-4-8", "one two three four", "2026-07-15T10:00:00Z", "dup"),
            ])
            self._write_transcript(root, "projB", "b.jsonl", [
                self._assistant("s1", "claude-opus-4-8", "one two three four", "2026-07-15T10:00:00Z", "dup"),
                self._assistant("s2", "claude-opus-4-8", "old work", "2026-01-01T10:00:00Z", "old"),
            ])
            scored = score_transcripts(root, lookback_days=30, now=now)

        # dup collapsed, s2 out of window → only session s1 survives
        self.assertEqual({s["run_id"] for s in scored}, {"s1"})


if __name__ == "__main__":
    unittest.main()
