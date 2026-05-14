from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.disagreement_router import (
    apply_decisions,
    main,
    render_proposed_manifest,
    score_flow,
)


HIGH_RISK_LEDGER = [
    {
        "role": "developer", "agent": "codex",
        "runs": 10, "successes": 4, "success_rate": 0.4,
        "non_success_counts": {"timeout": 1, "schema_error": 5},
    },
    {
        "role": "architect", "agent": "claude",
        "runs": 12, "successes": 12, "success_rate": 1.0,
        "non_success_counts": {},
    },
    {
        "role": "reviewer", "agent": "deepseek",
        "runs": 8, "successes": 7, "success_rate": 0.875,
        "non_success_counts": {},
    },
]

LOW_RISK_LEDGER = [
    {
        "role": "developer", "agent": "codex",
        "runs": 20, "successes": 20, "success_rate": 1.0,
        "non_success_counts": {},
    },
    {
        "role": "architect", "agent": "claude",
        "runs": 20, "successes": 20, "success_rate": 1.0,
        "non_success_counts": {},
    },
]

SPARSE_LEDGER = [
    {
        "role": "developer", "agent": "codex",
        "runs": 2, "successes": 2, "success_rate": 1.0,
        "non_success_counts": {},
    },
]

TIMEOUT_LEDGER = [
    {
        "role": "developer", "agent": "codex",
        "runs": 10, "successes": 8, "success_rate": 0.8,
        "non_success_counts": {"timeout": 3},
    },
]


class ScoreFlowTests(unittest.TestCase):
    def test_high_risk_pair_flagged_HIGH(self) -> None:
        decisions = score_flow(
            flow=["architect", "developer", "reviewer"],
            role_assignments={"architect": "claude", "developer": "codex", "reviewer": "deepseek"},
            reputation_ledger=HIGH_RISK_LEDGER,
        )
        risks = {d["role"]: d["risk"] for d in decisions}
        self.assertEqual("HIGH", risks["developer"])
        self.assertEqual("LOW", risks["architect"])
        # reviewer stays "keep" regardless of its own risk.
        self.assertEqual("keep", next(d for d in decisions if d["role"] == "reviewer")["action"])

    def test_high_risk_step_gets_inject_reviewer_action(self) -> None:
        # No reviewer in flow → injection expected on the risky step.
        decisions = score_flow(
            flow=["architect", "developer"],
            role_assignments={"architect": "claude", "developer": "codex"},
            reputation_ledger=HIGH_RISK_LEDGER,
        )
        dev = next(d for d in decisions if d["role"] == "developer")
        self.assertEqual("inject_reviewer", dev["action"])

    def test_low_risk_with_trailing_reviewer_proposes_trim(self) -> None:
        decisions = score_flow(
            flow=["developer", "reviewer"],
            role_assignments={"developer": "codex", "reviewer": "deepseek"},
            reputation_ledger=LOW_RISK_LEDGER,
        )
        dev = next(d for d in decisions if d["role"] == "developer")
        self.assertEqual("trim_reviewer", dev["action"])

    def test_sparse_ledger_marks_MED(self) -> None:
        decisions = score_flow(
            flow=["developer"],
            role_assignments={"developer": "codex"},
            reputation_ledger=SPARSE_LEDGER,
            min_runs_for_confidence=5,
        )
        self.assertEqual("MED", decisions[0]["risk"])

    def test_missing_reputation_pair_is_MED(self) -> None:
        decisions = score_flow(
            flow=["developer"],
            role_assignments={"developer": "openclaw"},
            reputation_ledger=HIGH_RISK_LEDGER,  # no entry for (developer, openclaw)
        )
        self.assertEqual("MED", decisions[0]["risk"])
        self.assertIn("no reputation data", decisions[0]["reason"])

    def test_timeout_heavy_marks_MED(self) -> None:
        # 3 timeouts / 10 runs = 30% ≥ 20% threshold → MED.
        decisions = score_flow(
            flow=["developer"],
            role_assignments={"developer": "codex"},
            reputation_ledger=TIMEOUT_LEDGER,
        )
        self.assertEqual("MED", decisions[0]["risk"])
        self.assertIn("timeouts", decisions[0]["reason"])


class ApplyDecisionsTests(unittest.TestCase):
    def test_inject_appends_reviewer_after_risky_step(self) -> None:
        decisions = [
            {"index": 0, "role": "architect", "action": "keep"},
            {"index": 1, "role": "developer", "action": "inject_reviewer"},
        ]
        result = apply_decisions(["architect", "developer"], decisions)
        self.assertEqual(["architect", "developer", "reviewer"], result)

    def test_trim_drops_redundant_following_reviewer(self) -> None:
        decisions = [
            {"index": 0, "role": "developer", "action": "trim_reviewer"},
            {"index": 1, "role": "reviewer", "action": "keep"},
        ]
        result = apply_decisions(["developer", "reviewer"], decisions)
        self.assertEqual(["developer"], result)

    def test_no_action_keeps_flow_intact(self) -> None:
        decisions = [
            {"index": 0, "role": "architect", "action": "keep"},
            {"index": 1, "role": "reviewer", "action": "keep"},
        ]
        result = apply_decisions(["architect", "reviewer"], decisions)
        self.assertEqual(["architect", "reviewer"], result)


class ReviewerInjectionCapTests(unittest.TestCase):
    """DeepSeek MED: 3 unknown roles → 3 injected reviewers without a cap.
    Now we cap CONSECUTIVE injections — after one inject the next risky
    step keeps without injecting."""

    def test_all_unknown_flow_does_not_inject_after_every_step(self) -> None:
        decisions = score_flow(
            flow=["a", "b", "c"],
            role_assignments={},  # all unknown → all MED
            reputation_ledger=[],
        )
        actions = [d["action"] for d in decisions]
        # First MED step gets inject; second skips (chain cap); third gets
        # inject again (cap reset after the skip).
        self.assertEqual("inject_reviewer", actions[0])
        self.assertEqual("keep", actions[1])
        # The skip should be reflected in the reason string:
        self.assertIn("chain cap", decisions[1]["reason"])

    def test_explicit_reviewer_resets_chain_cap(self) -> None:
        # Without the cap, an all-MED flow `[a, b, reviewer, c, d]` would
        # inject after every MED step. With the cap + explicit-reviewer
        # reset: step 0 injects, step 1 caps, explicit reviewer resets the
        # chain, then step 3 injects again, step 4 caps.
        decisions = score_flow(
            flow=["a", "b", "reviewer", "c", "d"],
            role_assignments={},
            reputation_ledger=[],
        )
        actions = [d["action"] for d in decisions]
        self.assertEqual("inject_reviewer", actions[0])  # 1st MED → inject
        self.assertEqual("keep", actions[1])             # chain cap
        self.assertEqual("keep", actions[2])             # explicit reviewer, resets cap
        # `c` is after explicit reviewer with `next_role="d"` (not reviewer) →
        # cap was reset → can inject again.
        self.assertEqual("inject_reviewer", actions[3])
        self.assertEqual("keep", actions[4])             # chain cap again

    def test_apply_decisions_respects_cap(self) -> None:
        # Full integration: 3-step flow with all-unknown roles + cap →
        # final flow has 2 reviewers, not 3.
        decisions = score_flow(
            flow=["a", "b", "c"],
            role_assignments={},
            reputation_ledger=[],
        )
        new_flow = apply_decisions(["a", "b", "c"], decisions)
        reviewer_count = new_flow.count("reviewer")
        self.assertLess(reviewer_count, 3, f"got {new_flow}")


class RenderProposedManifestTests(unittest.TestCase):
    def test_reasoning_log_included_as_comments(self) -> None:
        text = render_proposed_manifest(
            original={"task": {"title": "demo", "flow": ["architect", "developer"]}},
            new_flow=["architect", "developer", "reviewer"],
            decisions=[
                {"index": 0, "role": "architect", "agent": "claude", "risk": "LOW", "reason": "stable", "action": "keep"},
                {"index": 1, "role": "developer", "agent": "codex", "risk": "HIGH", "reason": "low success_rate", "action": "inject_reviewer"},
            ],
            manifest_path=Path("/tmp/example.toml"),
            config_used={"high_risk_threshold": 0.7, "min_runs_for_confidence": 5},
        )
        self.assertIn("# Reasoning log", text)
        self.assertIn("risk=HIGH", text)
        self.assertIn("action=inject_reviewer", text)
        # flow rendered with reviewer injected:
        self.assertIn('flow = ["architect", "developer", "reviewer"]', text)
        # Title preserved:
        self.assertIn('title = "demo"', text)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.manifest = self.tmp / "manifest.toml"
        self.manifest.write_text(
            'title-placeholder = "x"\n'
            "[task]\n"
            'title = "test"\n'
            'flow = ["architect", "developer"]\n',
            encoding="utf-8",
        )
        self.reputation = self.tmp / "reputation.json"
        self.reputation.write_text(json.dumps({"ledger": HIGH_RISK_LEDGER}), encoding="utf-8")
        self.roles = self.tmp / "roles.toml"
        self.roles.write_text(
            '[role.architect]\nprovider = "claude"\n\n'
            '[role.developer]\nprovider = "codex"\n',
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cli_writes_proposed_toml_with_injected_reviewer(self) -> None:
        rc = main([
            str(self.manifest),
            "--reputation", str(self.reputation),
            "--roles", str(self.roles),
        ])
        self.assertEqual(0, rc)
        proposed = self.manifest.with_suffix(".proposed.toml")
        self.assertTrue(proposed.exists())
        text = proposed.read_text(encoding="utf-8")
        # Developer is high-risk → reviewer should follow:
        self.assertIn('flow = ["architect", "developer", "reviewer"]', text)
        self.assertIn("risk=HIGH", text)

    def test_cli_never_modifies_original_manifest(self) -> None:
        original_text = self.manifest.read_text(encoding="utf-8")
        main([
            str(self.manifest),
            "--reputation", str(self.reputation),
            "--roles", str(self.roles),
        ])
        self.assertEqual(original_text, self.manifest.read_text(encoding="utf-8"))

    def test_cli_exits_2_on_missing_manifest(self) -> None:
        rc = main([
            str(self.tmp / "nope.toml"),
            "--reputation", str(self.reputation),
        ])
        self.assertEqual(2, rc)

    def test_cli_exits_2_on_missing_reputation(self) -> None:
        rc = main([
            str(self.manifest),
            "--reputation", str(self.tmp / "nope.json"),
        ])
        self.assertEqual(2, rc)

    def test_cli_threshold_override(self) -> None:
        # With high_risk_threshold = 0.99, even 87.5% reviewer success becomes
        # HIGH and would mark architect (rate=1.0) still LOW. Architect+codex
        # would be HIGH but architect is mapped to claude (rate=1.0) so it stays LOW.
        # Test that the override is plumbed end-to-end.
        rc = main([
            str(self.manifest),
            "--reputation", str(self.reputation),
            "--roles", str(self.roles),
            "--high-risk-threshold", "0.99",
        ])
        self.assertEqual(0, rc)
        text = self.manifest.with_suffix(".proposed.toml").read_text(encoding="utf-8")
        self.assertIn("high_risk_threshold=0.99", text)


class DefaultModeTests(unittest.TestCase):
    def test_no_auto_mode_documented(self) -> None:
        # The module-level docstring explicitly states proposal-only.
        import tools.disagreement_router as mod
        doc = (mod.__doc__ or "").lower()
        self.assertIn("propose", doc)
        # Either "proposal-only" or "propose-only" wording is acceptable —
        # both mean the same thing.
        self.assertTrue(
            "proposal-only" in doc or "propose-only" in doc,
            "module docstring should state proposal/propose-only mode",
        )


if __name__ == "__main__":
    unittest.main()
