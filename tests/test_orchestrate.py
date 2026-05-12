import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import orchestrate as o


def roles_toml(names):
    return "\n".join(
        f'[role.{name}]\nmodel = "{name}-model"\ninvocation = "codex-exec"\n'
        for name in names
    )


class OrchestrateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        os.chmod(self.root, 0o777)
        self.extra_roots = []
        self.old = o.PROJECT_ROOT, o.RUNS_DIR
        o.PROJECT_ROOT = self.root
        o.RUNS_DIR = self.root / "orchestrate-runs"

    def tearDown(self):
        o.PROJECT_ROOT, o.RUNS_DIR = self.old
        shutil.rmtree(self.root)
        for path in self.extra_roots:
            shutil.rmtree(path, ignore_errors=True)

    def write_roles(self, name="roles.example.toml", body=None):
        path = self.root / name
        path.write_text(body or roles_toml(["architect", "developer", "reviewer"]), encoding="utf-8")
        return path

    def write_manifest(self, flow=None, dependencies=None, context_files=None):
        flow = flow or ["architect", "developer"]
        body = [
            "[task]",
            'title = "Test task"',
            'description = "Test description"',
            "flow = " + json.dumps(flow),
        ]
        if context_files:
            body.append("context_files = " + json.dumps(context_files))
        if dependencies:
            body.append("")
            body.append("[task.dependencies]")
            for role, deps in dependencies.items():
                body.append(f"{role} = " + json.dumps(deps))
        path = self.root / "manifest.toml"
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
        return path

    def fake_success(self, seen=None):
        def _fake(role_config, _prompt, output_path):
            if seen is not None:
                seen.append(role_config["name"])
            o.atomic_write_text(output_path, f"{role_config['name']} output\n")
            return {"ok": True, "exit_code": 0, "duration_ms": 1, "output_path": str(output_path), "error": None}
        return _fake

    def test_load_roles_prefers_real_over_example(self):
        self.write_roles("roles.example.toml", '[role.developer]\nmodel = "example"\ninvocation = "codex-exec"\n')
        self.write_roles("roles.toml", '[role.developer]\nmodel = "real"\ninvocation = "codex-exec"\n')
        roles, path = o.load_roles()
        self.assertEqual(path.name, "roles.toml")
        self.assertEqual(roles["developer"]["model"], "real")

    def test_load_roles_falls_back_to_example(self):
        self.write_roles("roles.example.toml", '[role.developer]\nmodel = "example"\ninvocation = "codex-exec"\n')
        roles, path = o.load_roles()
        self.assertEqual(path.name, "roles.example.toml")
        self.assertEqual(roles["developer"]["model"], "example")

    def test_load_roles_invalid_toml_exits_2(self):
        self.write_roles("roles.toml", "[role.developer\n")
        manifest = self.write_manifest(["developer"])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = o.main(["run", str(manifest)])
        self.assertEqual(code, 2)
        self.assertIn("error:", err.getvalue())

    def test_validate_context_path_inside_root_OK(self):
        path = self.root / "docs" / "note.md"
        path.parent.mkdir()
        path.write_text("context", encoding="utf-8")
        self.assertEqual(o.resolve_context_path("docs/note.md"), Path(os.path.realpath(path)))

    def test_validate_context_path_escape_via_dotdot_rejected(self):
        with self.assertRaises(ValueError):
            o.resolve_context_path("../../../etc/passwd")

    def test_validate_context_path_junction_bypass_rejected(self):
        outside = Path(tempfile.mkdtemp())
        self.extra_roots.append(outside)
        (outside / "secret.txt").write_text("secret", encoding="utf-8")
        link = self.root / "linked"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        with self.assertRaises(ValueError):
            o.resolve_context_path("linked/secret.txt")

    def test_init_state_has_schema_version(self):
        state = o.init_state("run1", self.root / "manifest.toml", self.root / "roles.toml", ["developer"])
        self.assertEqual(state["schema_version"], 1)

    def test_init_state_has_run_id_status_steps(self):
        state = o.init_state("run1", self.root / "manifest.toml", self.root / "roles.toml", ["developer"])
        self.assertEqual(state["run_id"], "run1")
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["steps"][0]["role"], "developer")

    def test_list_roles_without_manifest_OK(self):
        self.write_roles()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = o.main(["run", "--list-roles"])
        self.assertEqual(code, 0)
        self.assertIn("roles_source=", stdout.getvalue())

    def test_dry_run_writes_nothing_but_prints_plan(self):
        self.write_roles()
        manifest = self.write_manifest(["architect", "developer"])
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = o.main(["run", str(manifest), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("planned_flow=architect -> developer", stdout.getvalue())
        self.assertFalse((o.RUNS_DIR / "state.json").exists())
        self.assertFalse(o.RUNS_DIR.exists())

    def test_failure_message_includes_run_id(self):
        self.write_roles(body=roles_toml(["developer"]))
        manifest = self.write_manifest(["developer"])
        failed = {"ok": False, "exit_code": 9, "duration_ms": 1, "output_path": "", "error": "boom"}
        stdout = io.StringIO()
        with mock.patch.object(o, "run_id", return_value="run-fail"):
            with mock.patch.object(o, "invoke", return_value=failed):
                with contextlib.redirect_stdout(stdout):
                    code = o.main(["run", str(manifest)])
        self.assertEqual(code, 1)
        self.assertIn("run_id=run-fail", stdout.getvalue())

    def test_resume_skips_completed_steps(self):
        self.write_roles()
        run_dir = o.RUNS_DIR / "resume-run"
        run_dir.mkdir(parents=True)
        (run_dir / "roles.used.toml").write_text(roles_toml(["architect", "developer"]), encoding="utf-8")
        self.write_manifest(["architect", "developer"]).replace(run_dir / "manifest.used.toml")
        first = run_dir / "01-architect.md"
        first.write_text("architect output", encoding="utf-8")
        state = o.init_state("resume-run", run_dir / "manifest.used.toml", run_dir / "roles.used.toml", ["architect", "developer"])
        state["next_index"] = 1
        state["steps"][0].update({"status": "completed", "output_path": str(first)})
        o.atomic_write_json(run_dir / "state.json", state)
        seen = []
        with mock.patch.object(o, "invoke", side_effect=self.fake_success(seen)):
            self.assertEqual(o.main(["resume", "resume-run"]), 0)
        self.assertEqual(seen, ["developer"])

    def test_human_relay_exits_78(self):
        self.write_roles(body='[role.approver]\ninvocation = "human-relay"\n')
        manifest = self.write_manifest(["approver"])

        def fake_human(_cfg, _prompt, output_path):
            return {"ok": True, "exit_code": 0, "duration_ms": 1, "output_path": str(output_path.with_name("01-approver-PROMPT.md")), "error": None, "waiting_for_human": True, "response_path": str(output_path)}

        with mock.patch.object(o, "run_id", return_value="human-run"):
            with mock.patch.object(o, "invoke", side_effect=fake_human):
                code = o.main(["run", str(manifest)])
        self.assertEqual(code, o.EX_CONFIG)
        state = json.loads((o.RUNS_DIR / "human-run" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "waiting_for_human")

    def test_dependency_resolution_topological(self):
        self.write_roles(body=roles_toml(["a", "b", "c"]))
        manifest = self.write_manifest(["c", "b", "a"], {"c": ["b"], "b": ["a"]})
        seen = []
        with mock.patch.object(o, "run_id", return_value="dag-run"):
            with mock.patch.object(o, "invoke", side_effect=self.fake_success(seen)):
                self.assertEqual(o.main(["run", str(manifest)]), 0)
        self.assertEqual(seen, ["a", "b", "c"])

    def test_atomic_state_writes(self):
        run_dir = o.RUNS_DIR / "atomic-run"
        run_dir.mkdir(parents=True)
        state = o.init_state("atomic-run", self.root / "manifest.toml", self.root / "roles.toml", ["developer"])
        with mock.patch.object(o.os, "replace", wraps=os.replace) as replace:
            o.update_state(run_dir, state, "running")
        src, dst = replace.call_args.args
        self.assertTrue(Path(src).name.startswith(".state.json.tmp-"))
        self.assertEqual(Path(dst).name, "state.json")
        self.assertEqual(json.loads((run_dir / "state.json").read_text(encoding="utf-8"))["run_id"], "atomic-run")
        self.assertEqual(list(run_dir.glob(".state.json.tmp-*")), [])


if __name__ == "__main__":
    unittest.main()
