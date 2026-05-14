from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.consensus_matrix import (
    JACCARD_THRESHOLD,
    Finding,
    build_matrix,
    extract_findings,
    main,
    render_report,
)


ARCH_TEXT = """# Architect

## Findings

### Path traversal on tasks.json

The reader does not validate the resolved path is under tracker/.

### Atomic writes missing on snapshot writer

snapshot.json is written via plain open(w) — risk of partial reads.

- Subscription pro-rate uses simple month math (could surprise on Feb)
"""

DEV_TEXT = """# Developer

Implemented per the architect spec.

### Path traversal on tasks.json

Fixed via Path.resolve().is_relative_to(TRACKER_ROOT) check.

### Schema versioning added

Persisted records now carry schema_version=1.
"""

REV_TEXT = """# Reviewer

## Residual issues

- Atomic writes still inconsistent across some snapshot helpers
- Subscription pro-rate edge case on leap year not covered by tests
"""


class ExtractFindingsTests(unittest.TestCase):
    def test_h3_headings_extracted(self) -> None:
        findings = extract_findings("Architect", ARCH_TEXT)
        titles = [f.title for f in findings]
        self.assertIn("Path traversal on tasks.json", titles)
        self.assertIn("Atomic writes missing on snapshot writer", titles)

    def test_top_level_bullets_extracted(self) -> None:
        findings = extract_findings("Reviewer", REV_TEXT)
        titles = [f.title for f in findings]
        self.assertIn("Atomic writes still inconsistent across some snapshot helpers", titles)
        self.assertIn("Subscription pro-rate edge case on leap year not covered by tests", titles)

    def test_code_blocks_ignored(self) -> None:
        text = "## Real finding\n\n```\n## fake heading inside code\n- fake bullet\n```\n"
        findings = extract_findings("Architect", text)
        titles = [f.title for f in findings]
        self.assertEqual(["Real finding"], titles)

    def test_finding_id_is_stable_across_runs(self) -> None:
        a = extract_findings("Architect", "### Atomic writes missing on snapshot writer\n")
        b = extract_findings("Developer", "### Atomic writes missing on snapshot writer\n")
        self.assertEqual(a[0].fid, b[0].fid)

    def test_dedup_within_role(self) -> None:
        text = "### Same\n\nbody\n\n- Same\n\n### Same\n"
        findings = extract_findings("Architect", text)
        # 'Same' appears 3 times raw — should dedupe to 1
        self.assertEqual(1, len(findings))

    def test_empty_text_returns_nothing(self) -> None:
        self.assertEqual([], extract_findings("Architect", ""))


class BuildMatrixTests(unittest.TestCase):
    def _findings(self) -> dict[str, list[Finding]]:
        return {
            "Architect": extract_findings("Architect", ARCH_TEXT),
            "Developer": extract_findings("Developer", DEV_TEXT),
            "Reviewer": extract_findings("Reviewer", REV_TEXT),
        }

    def test_identical_text_shows_as_raised_in_both_roles(self) -> None:
        findings = self._findings()
        matrix = build_matrix(findings)
        path_row = next(r for r in matrix if "Path traversal" in r["title"])
        self.assertEqual("raised", path_row["cells"]["Architect"])
        self.assertEqual("raised", path_row["cells"]["Developer"])
        self.assertEqual("silent", path_row["cells"]["Reviewer"])

    def test_similar_text_corroborates(self) -> None:
        # Architect says "Atomic writes missing on snapshot writer".
        # Reviewer says "Atomic writes still inconsistent across some snapshot helpers".
        # High token overlap → corroborated.
        findings = self._findings()
        matrix = build_matrix(findings)
        atomic_row = next(r for r in matrix if "Atomic writes" in r["title"])
        self.assertEqual("raised", atomic_row["cells"]["Architect"])
        self.assertEqual("corroborated", atomic_row["cells"]["Reviewer"])

    def test_solo_finding_marked_silent_in_others(self) -> None:
        findings = self._findings()
        matrix = build_matrix(findings)
        schema_row = next(r for r in matrix if "Schema versioning" in r["title"])
        self.assertEqual("silent", schema_row["cells"]["Architect"])
        self.assertEqual("raised", schema_row["cells"]["Developer"])
        self.assertEqual("silent", schema_row["cells"]["Reviewer"])

    def test_matrix_dedupes_similar_findings(self) -> None:
        # Architect raises "Atomic writes missing on snapshot writer".
        # Reviewer raises "Atomic writes still inconsistent across some snapshot helpers".
        # They should appear as ONE canonical row, not two.
        findings = self._findings()
        matrix = build_matrix(findings)
        atomic_rows = [r for r in matrix if "Atomic" in r["title"]]
        self.assertEqual(1, len(atomic_rows))


class RenderReportTests(unittest.TestCase):
    def test_renders_table_when_findings_exist(self) -> None:
        findings_by_role = {
            "Architect": extract_findings("Architect", ARCH_TEXT),
            "Developer": extract_findings("Developer", DEV_TEXT),
            "Reviewer": extract_findings("Reviewer", REV_TEXT),
        }
        matrix = build_matrix(findings_by_role)
        report = render_report(
            run_dir=Path("/tmp/example"),
            matrix=matrix,
            findings_by_role=findings_by_role,
            missing=[],
            state_meta={},
        )
        self.assertIn("Consensus Matrix", report)
        self.assertIn("| # | fid | Finding |", report)
        self.assertIn("Architect", report)
        self.assertIn("Developer", report)
        self.assertIn("Reviewer", report)
        self.assertIn("Legend:", report)

    def test_renders_graceful_note_when_no_findings(self) -> None:
        report = render_report(
            run_dir=Path("/tmp/empty"),
            matrix=[],
            findings_by_role={"Architect": [], "Developer": [], "Reviewer": []},
            missing=[],
            state_meta={},
        )
        self.assertIn("No findings detected", report)
        self.assertNotIn("| # |", report)

    def test_renders_missing_file_note(self) -> None:
        report = render_report(
            run_dir=Path("/tmp/partial"),
            matrix=[],
            findings_by_role={"Architect": [], "Developer": [], "Reviewer": []},
            missing=["03-reviewer.md"],
            state_meta={},
        )
        self.assertIn("missing role file(s): 03-reviewer.md", report)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "01-architect.md").write_text(ARCH_TEXT, encoding="utf-8")
        (self.tmp / "02-developer.md").write_text(DEV_TEXT, encoding="utf-8")
        (self.tmp / "03-reviewer.md").write_text(REV_TEXT, encoding="utf-8")

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cli_writes_consensus_md(self) -> None:
        rc = main([str(self.tmp)])
        self.assertEqual(0, rc)
        out = self.tmp / "consensus.md"
        self.assertTrue(out.exists())
        text = out.read_text(encoding="utf-8")
        self.assertIn("Consensus Matrix", text)
        self.assertIn("Path traversal", text)

    def test_cli_renders_sample_run_without_errors(self) -> None:
        # Acceptance: "Renders for docs/sample-run/ without errors."
        # Sample-run has prose paragraphs only — no findings — but tool must
        # still exit 0 and produce a valid markdown file with the graceful note.
        sample_run = ROOT / "docs" / "sample-run"
        with tempfile.TemporaryDirectory() as out_dir:
            out_path = Path(out_dir) / "consensus.md"
            rc = main([str(sample_run), "--output", str(out_path)])
            self.assertEqual(0, rc)
            self.assertTrue(out_path.exists())

    def test_cli_exits_2_on_missing_dir(self) -> None:
        rc = main(["/nonexistent/path"])
        self.assertEqual(2, rc)

    def test_cli_does_not_rewrite_role_files(self) -> None:
        before_arch = (self.tmp / "01-architect.md").read_text(encoding="utf-8")
        main([str(self.tmp)])
        after_arch = (self.tmp / "01-architect.md").read_text(encoding="utf-8")
        self.assertEqual(before_arch, after_arch)


class JaccardThresholdTests(unittest.TestCase):
    def test_threshold_is_in_sensible_range(self) -> None:
        # Guard against accidental refactor that breaks corroboration tuning.
        # Sweet spot is ~0.35-0.5: lower → false matches on common nouns
        # ("snapshot", "config"); higher → splits clearly-related pairs.
        self.assertGreaterEqual(JACCARD_THRESHOLD, 0.35)
        self.assertLessEqual(JACCARD_THRESHOLD, 0.6)


if __name__ == "__main__":
    unittest.main()
