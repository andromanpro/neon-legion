from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.capability_cards import (
    atomic_write,
    group_by_agent,
    main,
    render_card,
    slugify,
)


SAMPLE_LEDGER = [
    {
        "role": "architect",
        "agent": "claude",
        "runs": 10,
        "successes": 9,
        "success_rate": 0.9,
        "mean_duration_ms": 2500,
        "mean_cost_usd": 0.04,
        "task_fingerprint": "neon-legion-architect",
        "last_run_at": "2026-05-13T14:30:00+03:00",
        "non_success_counts": {"timeout": 1},
    },
    {
        "role": "reviewer",
        "agent": "claude",
        "runs": 4,
        "successes": 4,
        "success_rate": 1.0,
        "mean_duration_ms": 1800,
        "mean_cost_usd": 0.02,
        "task_fingerprint": "neon-legion-reviewer",
        "last_run_at": "2026-05-13T11:00:00+03:00",
        "non_success_counts": {},
    },
    {
        "role": "developer",
        "agent": "codex",
        "runs": 7,
        "successes": 5,
        "success_rate": 0.714,
        "mean_duration_ms": 45000,
        "mean_cost_usd": 0.18,
        "task_fingerprint": "neon-legion-developer",
        "last_run_at": "2026-05-13T15:00:00+03:00",
        "non_success_counts": {"timeout": 2, "schema_error": 1},
    },
]


class GroupByAgentTests(unittest.TestCase):
    def test_buckets_entries_by_agent(self) -> None:
        grouped = group_by_agent(SAMPLE_LEDGER)
        self.assertEqual({"claude", "codex"}, set(grouped.keys()))
        self.assertEqual(2, len(grouped["claude"]))
        self.assertEqual(1, len(grouped["codex"]))

    def test_unknown_agent_bucket_for_missing_field(self) -> None:
        entries = [{"role": "architect", "runs": 1, "successes": 1}]
        grouped = group_by_agent(entries)
        self.assertIn("unknown", grouped)


class RenderCardTests(unittest.TestCase):
    def test_card_has_per_role_table_rows(self) -> None:
        card = render_card(
            agent="claude",
            entries=[e for e in SAMPLE_LEDGER if e["agent"] == "claude"],
            generated_at="2026-05-14T10:00:00+03:00",
            global_summary={},
        )
        self.assertIn("# Capability card — `claude`", card)
        self.assertIn("`architect`", card)
        self.assertIn("`reviewer`", card)
        self.assertIn("| `architect` | 10 |", card)
        self.assertIn("| `reviewer` | 4 |", card)

    def test_card_aggregates_successes_across_roles(self) -> None:
        # claude: 9+4 successes / 10+4 runs = 13/14 = ~93%
        card = render_card(
            agent="claude",
            entries=[e for e in SAMPLE_LEDGER if e["agent"] == "claude"],
            generated_at="2026-05-14T10:00:00+03:00",
            global_summary={},
        )
        self.assertIn("13/14", card)
        self.assertIn("93%", card)

    def test_card_top_failures_when_present(self) -> None:
        card = render_card(
            agent="codex",
            entries=[e for e in SAMPLE_LEDGER if e["agent"] == "codex"],
            generated_at="2026-05-14T10:00:00+03:00",
            global_summary={},
        )
        self.assertIn("`timeout` × 2", card)
        self.assertIn("`schema_error` × 1", card)

    def test_card_says_none_when_no_failures(self) -> None:
        clean_entry = {
            "role": "architect",
            "agent": "claude",
            "runs": 5,
            "successes": 5,
            "success_rate": 1.0,
            "mean_duration_ms": 1000,
            "mean_cost_usd": 0.01,
            "non_success_counts": {},
        }
        card = render_card(
            agent="claude",
            entries=[clean_entry],
            generated_at="now",
            global_summary={},
        )
        self.assertIn("_None recorded._", card)

    def test_card_handles_missing_cost(self) -> None:
        entry = dict(SAMPLE_LEDGER[0])
        entry["mean_cost_usd"] = None
        card = render_card(
            agent="claude",
            entries=[entry],
            generated_at="now",
            global_summary={},
        )
        # Em-dash for missing cost cell:
        self.assertIn("| — |", card)

    def test_sparse_summary_adds_warning(self) -> None:
        card = render_card(
            agent="claude",
            entries=[e for e in SAMPLE_LEDGER if e["agent"] == "claude"],
            generated_at="now",
            global_summary={"sparse": True, "ledger_entries": 3, "total_runs_scanned": 1},
        )
        self.assertIn("sparse", card.lower())
        self.assertIn("Treat these numbers as directional", card)

    def test_no_sparse_note_when_summary_dense(self) -> None:
        card = render_card(
            agent="claude",
            entries=[e for e in SAMPLE_LEDGER if e["agent"] == "claude"],
            generated_at="now",
            global_summary={"sparse": False},
        )
        self.assertNotIn("Treat these numbers as directional", card)

    def test_latency_formatted_seconds_for_large_values(self) -> None:
        card = render_card(
            agent="codex",
            entries=[e for e in SAMPLE_LEDGER if e["agent"] == "codex"],
            generated_at="now",
            global_summary={},
        )
        # 45000 ms → "45.0 s"
        self.assertIn("45.0 s", card)


class SlugifyTests(unittest.TestCase):
    def test_lowercases_and_strips_unsafe_chars(self) -> None:
        self.assertEqual("claude-code", slugify("Claude Code"))
        self.assertEqual("gpt-5.5-pro", slugify("GPT-5.5-pro"))
        self.assertEqual("deepseek_v4", slugify("DeepSeek_v4"))

    def test_unknown_fallback(self) -> None:
        self.assertEqual("unknown", slugify(""))
        self.assertEqual("unknown", slugify("--"))


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.input_path = self.tmp / "reputation.json"
        self.output_dir = self.tmp / "out"
        self.input_path.write_text(
            json.dumps({
                "schema_version": 1,
                "generated_at": "2026-05-14T10:00:00+03:00",
                "summary": {"sparse": False, "ledger_entries": 3, "total_runs_scanned": 21},
                "ledger": SAMPLE_LEDGER,
            }),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cli_writes_one_card_per_agent(self) -> None:
        rc = main([
            "--input", str(self.input_path),
            "--output-dir", str(self.output_dir),
        ])
        self.assertEqual(0, rc)
        files = sorted(p.name for p in self.output_dir.glob("*.md"))
        self.assertEqual(["claude.md", "codex.md"], files)

    def test_cli_exits_2_on_missing_input(self) -> None:
        rc = main([
            "--input", str(self.tmp / "missing.json"),
            "--output-dir", str(self.output_dir),
        ])
        self.assertEqual(2, rc)

    def test_cli_handles_empty_ledger_gracefully(self) -> None:
        self.input_path.write_text(
            json.dumps({"ledger": [], "summary": {}}), encoding="utf-8"
        )
        rc = main([
            "--input", str(self.input_path),
            "--output-dir", str(self.output_dir),
        ])
        self.assertEqual(0, rc)
        # No files written:
        files = list(self.output_dir.glob("*.md")) if self.output_dir.exists() else []
        self.assertEqual([], files)

    def test_cli_never_writes_roles_toml(self) -> None:
        # Belt-and-suspenders: ensure no roles.toml byproduct anywhere in output dir.
        main([
            "--input", str(self.input_path),
            "--output-dir", str(self.output_dir),
        ])
        toml_files = list(self.tmp.rglob("roles*.toml"))
        self.assertEqual([], toml_files)


class AtomicWriteTests(unittest.TestCase):
    def test_writes_then_cleans_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "card.md"
            atomic_write(target, "hello\n")
            self.assertEqual("hello\n", target.read_text(encoding="utf-8"))
            leftover = list(Path(tmp).glob(".*.tmp.*"))
            self.assertEqual([], leftover)


if __name__ == "__main__":
    unittest.main()
