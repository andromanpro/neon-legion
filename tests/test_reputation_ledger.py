from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.reputation_ledger import build_ledger, emit_roles_suggested


NOW = datetime(2026, 5, 13, 22, 30, tzinfo=timezone.utc)


ROLES = """[role.architect]
provider = "human"
model = "sample-local"
invocation = "human-relay"

[role.developer]
provider = "human"
model = "sample-local"
invocation = "human-relay"

[role.reviewer]
provider = "human"
model = "sample-local"
invocation = "human-relay"
"""


class ReputationLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp_parent = ROOT / ".codex-test-tmp"
        tmp_parent.mkdir(exist_ok=True)
        safe_name = "".join(ch if ch.isalnum() else "-" for ch in self.id())
        self.root = tmp_parent / f"{os.getpid()}-{safe_name}"
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir()
        self.runs_dir = self.root / "orchestrate-runs"
        self.runs_dir.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def write_run(
        self,
        name: str,
        *,
        started_at: datetime | None = None,
        roles: str = ROLES,
        steps: list[dict] | None = None,
    ) -> Path:
        run_dir = self.runs_dir / name
        run_dir.mkdir(parents=True)
        started_at = started_at or NOW
        (run_dir / "roles.used.toml").write_text(roles, encoding="utf-8")
        (run_dir / "manifest.used.toml").write_text('[task]\ntitle = "Ledger fixture"\n', encoding="utf-8")
        if steps is None:
            steps = [
                self.step(0, "architect", started_at, duration=2),
                self.step(1, "developer", started_at + timedelta(seconds=1), duration=3),
                self.step(2, "reviewer", started_at + timedelta(seconds=2), duration=4),
            ]
        for step in steps:
            output_path = run_dir / f"{step['index'] + 1:02d}-{step['role']}.md"
            output_path.write_text(f"# {step['role']} deliverable\n\nok\n", encoding="utf-8")
            step["output_path"] = str(output_path)
        state = {
            "schema_version": 1,
            "run_id": name,
            "status": "completed",
            "created_at": started_at.isoformat(),
            "updated_at": started_at.isoformat(),
            "steps": steps,
        }
        (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
        return run_dir

    def step(self, index: int, role: str, started_at: datetime, *, ok: bool = True, status: str = "completed", duration: int = 1) -> dict:
        return {
            "index": index,
            "role": role,
            "status": status,
            "started_at": started_at.timestamp(),
            "result": {
                "ok": ok,
                "exit_code": 0 if ok else 1,
                "duration_ms": duration,
                "output_path": "",
                "error": None if ok else "boom",
            },
        }

    def test_empty_runs_dir_returns_sparse_payload(self) -> None:
        payload = build_ledger(self.runs_dir, now=NOW)

        self.assertEqual([], payload["ledger"])
        self.assertTrue(payload["summary"]["sparse"])
        self.assertEqual(0, payload["summary"]["total_runs_scanned"])

    def test_single_run_basic_ledger(self) -> None:
        self.write_run("run1")

        payload = build_ledger(self.runs_dir, now=NOW)

        self.assertEqual(3, payload["summary"]["ledger_entries"])
        by_role = {entry["role"]: entry for entry in payload["ledger"]}
        self.assertEqual({"architect", "developer", "reviewer"}, set(by_role))
        for entry in by_role.values():
            self.assertEqual(1, entry["runs"])
            self.assertEqual(1, entry["successes"])
            self.assertEqual(1.0, entry["success_rate"])
            self.assertEqual("human", entry["agent"])

    def test_lookback_filter_excludes_old_runs(self) -> None:
        old = NOW - timedelta(days=60)
        self.write_run("old", started_at=old, steps=[self.step(0, "developer", old)])
        self.write_run("fresh", started_at=NOW, steps=[self.step(0, "developer", NOW)])

        payload = build_ledger(self.runs_dir, lookback_days=30, now=NOW)

        self.assertEqual(1, payload["summary"]["total_runs_scanned"])
        self.assertEqual(1, payload["ledger"][0]["runs"])

    def test_failed_step_lowers_success_rate(self) -> None:
        steps = [
            self.step(0, "developer", NOW, ok=True, status="completed"),
            self.step(1, "developer", NOW + timedelta(seconds=1), ok=False, status="failed"),
        ]
        self.write_run("failed", steps=steps)

        payload = build_ledger(self.runs_dir, now=NOW)

        entry = payload["ledger"][0]
        self.assertEqual(2, entry["runs"])
        self.assertEqual(1, entry["successes"])
        self.assertLess(entry["success_rate"], 1.0)

    def test_roles_suggested_emits_keep_when_no_better_option(self) -> None:
        run_dir = self.write_run("run1", steps=[self.step(0, "developer", NOW)])
        payload = build_ledger(self.runs_dir, now=NOW)
        output = self.root / "roles.suggested.toml"

        emit_roles_suggested(payload, run_dir / "roles.used.toml", output)

        text = output.read_text(encoding="utf-8")
        self.assertIn("Never auto-applied", text)
        self.assertIn("[role.developer]", text)
        self.assertIn("# Suggestion: keep current (human)", text)
        self.assertIn('model = "sample-local"', text)

    def test_no_data_roles_suggested_is_minimal(self) -> None:
        payload = build_ledger(self.runs_dir, now=NOW)
        output = self.root / "roles.suggested.toml"

        emit_roles_suggested(payload, None, output)

        text = output.read_text(encoding="utf-8")
        self.assertIn("No historical runs found", text)
        self.assertNotIn("[role.", text)

    # DeepSeek HIGH #1 on PR #85: Wilson lower bound prevents 1-run 100%
    # from outranking 10-run 90%. Sample size is intrinsic to the score.
    def test_wilson_scoring_prefers_stable_over_flash_perfect(self) -> None:
        from tools.reputation_ledger import _best_by_role, _wilson_lower_bound

        # 1-run 100% vs 10-run 90% — old logic preferred flash, Wilson prefers stable.
        flash = {"role": "architect", "agent": "flash", "runs": 1, "successes": 1,
                 "success_rate": 1.0, "mean_duration_ms": 50}
        stable = {"role": "architect", "agent": "stable", "runs": 10, "successes": 9,
                  "success_rate": 0.9, "mean_duration_ms": 50}

        best = _best_by_role([flash, stable])

        self.assertEqual(best["architect"]["agent"], "stable",
                         "Wilson score must rank stable 10-run agent above flash 1-run")
        # Quick sanity: Wilson(1, 1) ≈ 0.21, Wilson(9, 10) ≈ 0.59
        self.assertLess(_wilson_lower_bound(1, 1), _wilson_lower_bound(9, 10))

    # DeepSeek MED #3 on PR #85: cancelled/expired steps count in runs denominator.
    def test_cancelled_steps_count_as_runs_not_successes(self) -> None:
        steps = [
            {"index": 0, "role": "architect", "status": "completed",
             "result": {"ok": True, "duration_ms": 10}, "started_at": (NOW.timestamp() - 100)},
            {"index": 1, "role": "architect", "status": "cancelled",
             "result": None, "started_at": (NOW.timestamp() - 90)},
            {"index": 2, "role": "architect", "status": "expired",
             "result": None, "started_at": (NOW.timestamp() - 80)},
        ]
        self.write_run("run-mixed", steps=steps)

        payload = build_ledger(self.runs_dir, now=NOW)
        arch = next(e for e in payload["ledger"] if e["role"] == "architect")
        self.assertEqual(arch["runs"], 3, "all 3 statuses count as attempted runs")
        self.assertEqual(arch["successes"], 1, "only the completed+ok step counts as success")
        self.assertAlmostEqual(arch["success_rate"], 1 / 3, places=4)
        # non_success_counts surfaces the breakdown
        self.assertEqual(arch["non_success_counts"].get("cancelled"), 1)
        self.assertEqual(arch["non_success_counts"].get("expired"), 1)

    # DeepSeek MED #2 on PR #85: openai-API invocation no longer collapses to codex agent.
    def test_agent_openai_does_not_collapse_to_codex(self) -> None:
        from tools.reputation_ledger import _agent

        openai_role = {"invocation": "openai-direct", "model": "gpt-5"}
        codex_role = {"invocation": "codex-exec", "model": "gpt-5"}

        self.assertEqual(_agent(openai_role), "openai")
        self.assertEqual(_agent(codex_role), "codex")


if __name__ == "__main__":
    unittest.main()
