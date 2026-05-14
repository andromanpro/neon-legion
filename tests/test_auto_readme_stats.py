from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.auto_readme_stats import (
    END_MARKER,
    START_MARKER,
    atomic_write,
    format_stats_block,
    main,
    replace_block,
)


SAMPLE_SNAPSHOT = {
    "totals": {
        "calls": 75725,
        "cost_usd": 72191.19,
        "savings_usd": 71791.19,
        "subscription_usd": 400.0,
        "days": 30,
        "period_start": "2026-04-15",
        "period_end": "2026-05-14",
    },
    "productivity": {
        "multiplier": 7.171,
        "hours_saved": 1236.0,
        "active_hours": 200.3,
    },
    "sentiment": {
        "top_day": {"date": "2026-04-22", "profanity": 10},
    },
}


class FormatStatsBlockTests(unittest.TestCase):
    def test_renders_headline_numbers(self) -> None:
        block = format_stats_block(SAMPLE_SNAPSHOT)

        self.assertIn("Past 30 days", block)
        self.assertIn("75,725 AI calls", block)
        self.assertIn("$71,791 saved", block)
        self.assertIn("×7.17 productivity multiplier", block)
        self.assertIn("1,236 human-hours", block)

    def test_renders_top_stress_day(self) -> None:
        block = format_stats_block(SAMPLE_SNAPSHOT)

        self.assertIn("2026-04-22", block)
        self.assertIn("10 frustrated", block)

    def test_top_day_omitted_when_missing(self) -> None:
        snapshot = dict(SAMPLE_SNAPSHOT)
        snapshot["sentiment"] = {"top_day": {"date": "", "profanity": 0}}

        block = format_stats_block(snapshot)

        self.assertNotIn("Most stressed day", block)

    def test_format_is_pure_function(self) -> None:
        first = format_stats_block(SAMPLE_SNAPSHOT)
        second = format_stats_block(SAMPLE_SNAPSHOT)

        self.assertEqual(first, second)


class ReplaceBlockTests(unittest.TestCase):
    README = (
        "# Project\n\n"
        "Intro.\n\n"
        "## Live stats\n\n"
        f"{START_MARKER}\n\n"
        "OLD CONTENT — should be replaced.\n\n"
        f"{END_MARKER}\n\n"
        "## Next section\n\n"
        "Trailing content unchanged.\n"
    )

    def test_replaces_block_content(self) -> None:
        updated, changed, reason = replace_block(self.README, "NEW")

        self.assertTrue(changed)
        self.assertIn("NEW", updated)
        self.assertNotIn("OLD CONTENT", updated)
        self.assertEqual(reason, "block updated")

    def test_preserves_content_outside_block(self) -> None:
        updated, _, _ = replace_block(self.README, "NEW")

        self.assertIn("# Project", updated)
        self.assertIn("## Next section", updated)
        self.assertIn("Trailing content unchanged.", updated)
        # Markers themselves preserved verbatim:
        self.assertIn(START_MARKER, updated)
        self.assertIn(END_MARKER, updated)

    def test_idempotent_second_run_no_change(self) -> None:
        once, _, _ = replace_block(self.README, "NEW")
        twice, changed, reason = replace_block(once, "NEW")

        self.assertFalse(changed)
        self.assertEqual(twice, once)
        self.assertEqual(reason, "block already current")

    def test_missing_markers_reports_reason(self) -> None:
        no_markers = "# Project\n\nNo markers here.\n"

        updated, changed, reason = replace_block(no_markers, "NEW")

        self.assertFalse(changed)
        self.assertEqual(updated, no_markers)
        self.assertEqual(reason, "markers missing")

    def test_marker_inside_code_fence_ignored(self) -> None:
        # DeepSeek MED: naive `.index()` could grab a START_STATS marker
        # that appears inside a code block (tutorial showing the marker
        # syntax) instead of the real one. Result: tool eats arbitrary
        # README content between fence-embedded marker and real END.
        readme = (
            "# Project\n\n"
            "Here's how the live block looks:\n\n"
            "```\n"
            f"{START_MARKER}\n"
            "documentation example block\n"
            f"{END_MARKER}\n"
            "```\n\n"
            "## Actual live block\n\n"
            f"{START_MARKER}\n\n"
            "OLD\n\n"
            f"{END_MARKER}\n\n"
            "Trailing content.\n"
        )
        updated, changed, reason = replace_block(readme, "NEW")
        self.assertTrue(changed)
        self.assertEqual(reason, "block updated")
        # The fence-embedded "documentation example block" must remain untouched:
        self.assertIn("documentation example block", updated)
        # OLD between real markers gets replaced:
        self.assertNotIn("\nOLD\n", updated)
        self.assertIn("NEW", updated)
        # Section header and trailing content preserved:
        self.assertIn("## Actual live block", updated)
        self.assertIn("Trailing content.", updated)

    def test_tilde_fence_also_protects(self) -> None:
        # Symmetric with ``` — ~~~ fences should also hide markers.
        readme = (
            "# Project\n\n"
            "~~~\n"
            f"{START_MARKER}\nfake\n{END_MARKER}\n"
            "~~~\n\n"
            f"{START_MARKER}\n\nOLD\n\n{END_MARKER}\n"
        )
        updated, changed, _ = replace_block(readme, "NEW")
        self.assertTrue(changed)
        self.assertIn("fake", updated)  # fence-embedded survives
        self.assertNotIn("\nOLD\n", updated)
        self.assertIn("NEW", updated)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.tmp = Path(tempfile.mkdtemp())
        self.readme = self.tmp / "README.md"
        self.snapshot = self.tmp / "snapshot.json"

        self.readme.write_text(
            f"# Sample\n\n{START_MARKER}\n\nOLD\n\n{END_MARKER}\n",
            encoding="utf-8",
        )
        self.snapshot.write_text(json.dumps(SAMPLE_SNAPSHOT), encoding="utf-8")

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_apply_updates_readme(self) -> None:
        rc = main(["--readme", str(self.readme), "--snapshot", str(self.snapshot)])
        self.assertEqual(rc, 0)

        text = self.readme.read_text(encoding="utf-8")
        self.assertIn("75,725 AI calls", text)
        self.assertNotIn("\nOLD\n", text)
        # Markers preserved verbatim:
        self.assertIn(START_MARKER, text)
        self.assertIn(END_MARKER, text)

    def test_check_returns_1_when_stale(self) -> None:
        rc = main([
            "--check",
            "--readme", str(self.readme),
            "--snapshot", str(self.snapshot),
        ])
        self.assertEqual(rc, 1)
        # Check mode must not write:
        self.assertIn("\nOLD\n", self.readme.read_text(encoding="utf-8"))

    def test_check_returns_0_when_current(self) -> None:
        # First apply, then check should return 0
        main(["--readme", str(self.readme), "--snapshot", str(self.snapshot)])
        rc = main([
            "--check",
            "--readme", str(self.readme),
            "--snapshot", str(self.snapshot),
        ])
        self.assertEqual(rc, 0)

    def test_missing_markers_exits_2(self) -> None:
        self.readme.write_text("# No markers here\n", encoding="utf-8")
        rc = main(["--readme", str(self.readme), "--snapshot", str(self.snapshot)])
        self.assertEqual(rc, 2)

    def test_missing_snapshot_exits_2(self) -> None:
        rc = main([
            "--readme", str(self.readme),
            "--snapshot", str(self.tmp / "nonexistent.json"),
        ])
        self.assertEqual(rc, 2)


class AtomicWriteTests(unittest.TestCase):
    def test_writes_content_and_cleans_tmp(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.md"
            atomic_write(target, "hello\n")

            self.assertEqual(target.read_text(encoding="utf-8"), "hello\n")
            # No tmp left behind:
            leftover = list(Path(tmp).glob(".*.tmp.*"))
            self.assertEqual(leftover, [])


if __name__ == "__main__":
    unittest.main()
