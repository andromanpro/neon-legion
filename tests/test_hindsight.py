import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tools.hindsight as h


ROLES_TOML = """
[role.architect]
model = "claude-opus-4"
invocation = "claude-cli-headless"

[role.developer]
model = "gpt-5.5"
invocation = "codex-exec"
sandbox = "workspace-write"

[role.reviewer]
model = "openrouter/deepseek/deepseek-v4-pro"
invocation = "opencode-run"

[role.approver]
invocation = "human-relay"
"""


class HindsightTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.old = h.PROJECT_ROOT, h.RUNS_DIR, h.EVENTS_FILE
        h.PROJECT_ROOT = self.root
        h.RUNS_DIR = self.root / "orchestrate-runs"
        h.EVENTS_FILE = self.root / "tracker" / "hindsight-events.jsonl"
        h.RUNS_DIR.mkdir(parents=True)
        (self.root / "roles.example.toml").write_text(ROLES_TOML, encoding="utf-8")

    def tearDown(self):
        h.PROJECT_ROOT, h.RUNS_DIR, h.EVENTS_FILE = self.old
        shutil.rmtree(self.root)

    def make_run(self, name="run1", status="completed", role="developer", content="deliverable"):
        run_dir = h.RUNS_DIR / name
        run_dir.mkdir()
        out = run_dir / f"01-{role}.md"
        out.write_text(content, encoding="utf-8")
        (run_dir / "roles.used.toml").write_text(ROLES_TOML, encoding="utf-8")
        state = {
            "run_id": name,
            "status": status,
            "manifest_path": str(self.root / "manifest.toml"),
            "roles_path": str(self.root / "roles.example.toml"),
            "steps": [{"index": 0, "role": role, "status": "completed", "output_path": str(out)}],
        }
        (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
        return run_dir, out

    def fake_invoke(self, _cfg, _prompt, output_path):
        h.atomic_write_text(output_path, "Critique body\n")
        return {"ok": True, "duration_ms": 12, "output_path": str(output_path), "error": None}

    def read_events(self):
        return [json.loads(line) for line in h.EVENTS_FILE.read_text(encoding="utf-8").splitlines()]

    def test_critic_selection_determinism(self):
        self.assertEqual(h.select_critic("codex-exec"), "opencode-run")
        self.assertEqual(h.select_critic("opencode-run"), "codex-exec")
        self.assertEqual(h.select_critic("claude-cli-headless"), "opencode-run")
        self.assertEqual(h.select_critic("human-relay"), "opencode-run")

    def test_critic_override(self):
        self.make_run()
        with mock.patch.object(h.role_invoke, "invoke", side_effect=self.fake_invoke) as invoke:
            self.assertEqual(h.run_hindsight("run1", None, "claude-cli-headless", False), 0)
        self.assertEqual(invoke.call_args.args[0]["invocation"], "claude-cli-headless")
        self.assertEqual(self.read_events()[0]["critic_invocation"], "claude-cli-headless")

    def test_refuses_uncompleted_state(self):
        self.make_run(status="running")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(h.run_hindsight("run1", None, None, False), 1)
        self.assertIn("not 'completed'", err.getvalue())
        self.assertFalse(h.EVENTS_FILE.exists())

    def test_list_and_all_pending(self):
        self.make_run("r1")
        run2, out2 = self.make_run("r2")
        h.atomic_write_text(h.hindsight_path(run2, out2, "developer"), "done\n")
        self.make_run("r3", status="running")
        self.assertEqual(h.pending_runs(), ["r1"])
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(h.main(["--list"]), 0)
        self.assertEqual(stdout.getvalue().strip(), "r1")
        with mock.patch.object(h.role_invoke, "invoke", side_effect=self.fake_invoke):
            self.assertEqual(h.main(["--all-pending"]), 0)
        self.assertTrue((h.RUNS_DIR / "r1" / "developer.hindsight.md").exists())
        self.assertEqual(self.read_events()[0]["run_id"], "r1")

    def test_dry_run_writes_nothing(self):
        self.make_run()
        stdout = io.StringIO()
        with mock.patch.object(h.role_invoke, "invoke") as invoke, contextlib.redirect_stdout(stdout):
            self.assertEqual(h.main(["run1", "--dry-run"]), 0)
        invoke.assert_not_called()
        self.assertIn("codex-exec -> opencode-run", stdout.getvalue())
        self.assertFalse(h.EVENTS_FILE.exists())
        self.assertFalse((h.RUNS_DIR / "run1" / "developer.hindsight.md").exists())

    def test_event_shape(self):
        self.make_run(content="non-empty")
        with mock.patch.object(h.role_invoke, "invoke", side_effect=self.fake_invoke):
            self.assertEqual(h.run_hindsight("run1", None, None, False), 0)
        event = self.read_events()[0]
        self.assertEqual(event["schema_version"], 1)
        self.assertEqual(event["run_id"], "run1")
        self.assertEqual(event["task_name"], "developer")
        self.assertEqual(event["original_invocation"], "codex-exec")
        self.assertEqual(event["critic_invocation"], "opencode-run")
        self.assertEqual(event["critic_model"], "openrouter/deepseek/deepseek-v4-pro")
        self.assertTrue(event["ok"])
        self.assertGreater(event["deliverable_bytes"], 0)
        self.assertGreater(event["hindsight_bytes"], 0)
        self.assertTrue(event["output_path"].endswith("developer.hindsight.md"))

    def test_task_description_falls_back_to_manifest_used_toml(self):
        run_dir, _out = self.make_run(content="non-empty")
        (run_dir / "manifest.used.toml").write_text(
            '[task]\ntitle = "Test task"\ndescription = "Manifest description."\n',
            encoding="utf-8",
        )
        with mock.patch.object(h.role_invoke, "invoke", side_effect=self.fake_invoke) as invoke:
            self.assertEqual(h.run_hindsight("run1", None, None, False), 0)
        self.assertIn("Test task", invoke.call_args.args[1])

    def test_empty_deliverable_skips_without_error(self):
        self.make_run(content="")
        with mock.patch.object(h.role_invoke, "invoke") as invoke:
            self.assertEqual(h.run_hindsight("run1", None, None, False), 0)
        invoke.assert_not_called()
        event = self.read_events()[0]
        self.assertTrue(event["ok"])
        self.assertTrue(event["skipped"])
        self.assertEqual(event["skip_reason"], "trivial_deliverable")
        self.assertEqual(event["deliverable_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
