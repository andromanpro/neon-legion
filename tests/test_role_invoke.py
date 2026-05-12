import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import role_invoke as ri


class RoleInvokeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        os.chmod(self.root, 0o777)

    def tearDown(self):
        shutil.rmtree(self.root)

    def completed(self, returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(["tool"], returncode, stdout, stderr)

    def test_claude_json_unwrap_response_key(self):
        self.assertEqual(ri._extract_claude_response('{"response": "hello"}'), "hello")

    def test_claude_json_unwrap_result_key(self):
        self.assertEqual(ri._extract_claude_response('{"result": "done"}'), "done")

    def test_claude_json_unwrap_content_list(self):
        raw = '{"content": [{"type": "text", "text": "one"}, {"type": "text", "text": " two"}]}'
        self.assertEqual(ri._extract_claude_response(raw), "one two")

    def test_claude_json_unwrap_falls_back_to_raw(self):
        self.assertEqual(ri._extract_claude_response("{not json"), "{not json")

    def test_claude_json_unwrap_unrecognized_shape(self):
        raw = '{"metadata": {"id": 1}}'
        self.assertEqual(ri._extract_claude_response(raw), raw)

    def test_strip_ansi_color_codes(self):
        self.assertEqual(ri._strip_ansi("a\x1b[31mred\x1b[0mz"), "aredz")

    def test_strip_ansi_osc(self):
        raw = "a\x1b]8;;https://example.test\x07link\x1b]8;;\x07z"
        self.assertEqual(ri._strip_ansi(raw), "alinkz")

    def test_strip_ansi_empty(self):
        self.assertEqual(ri._strip_ansi(""), "")

    def test_command_not_found_raises_with_hint(self):
        with mock.patch.object(ri.shutil, "which", return_value=None):
            with self.assertRaises(FileNotFoundError) as caught:
                ri._command("missing-binary")
        self.assertIn("missing-binary", str(caught.exception))
        self.assertIn("Install it", str(caught.exception))

    def test_command_found_returns_path(self):
        with mock.patch.object(ri.shutil, "which", return_value="C:/bin/python.exe"):
            self.assertEqual(ri._command("python"), "C:/bin/python.exe")

    def test_invoke_claude_writes_unwrapped(self):
        out = self.root / "claude.md"
        with mock.patch.object(ri, "_command", return_value="claude"):
            with mock.patch.object(ri.subprocess, "run", return_value=self.completed(stdout='{"response": "body"}')):
                result = ri.invoke({"invocation": "claude-cli-headless"}, "prompt", out)
        self.assertTrue(result["ok"])
        self.assertEqual(out.read_text(encoding="utf-8"), "body")

    def test_invoke_claude_oauth_error_in_message(self):
        out = self.root / "claude.md"
        failed = self.completed(returncode=1, stderr="OAuth token expired")
        with mock.patch.object(ri, "_command", return_value="claude"):
            with mock.patch.object(ri.subprocess, "run", return_value=failed):
                result = ri.invoke({"invocation": "claude-cli-headless"}, "prompt", out)
        self.assertFalse(result["ok"])
        self.assertIn("OAuth", result["error"])
        self.assertIn("re-authenticate", result["error"])

    def test_invoke_claude_timeout(self):
        out = self.root / "claude.md"
        with mock.patch.object(ri, "_command", return_value="claude"):
            with mock.patch.object(ri.subprocess, "run", side_effect=subprocess.TimeoutExpired("claude", 300)):
                result = ri.invoke({"invocation": "claude-cli-headless"}, "prompt", out)
        self.assertEqual(result["exit_code"], 124)
        self.assertIn("timed out", result["error"])

    def test_invoke_opencode_strips_ansi(self):
        out = self.root / "opencode.md"
        with mock.patch.object(ri, "_command", return_value="opencode"):
            with mock.patch.object(ri, "_openrouter_key_from_git", return_value=None):
                with mock.patch.object(ri.subprocess, "run", return_value=self.completed(stdout="\x1b[32mok\x1b[0m")):
                    result = ri.invoke({"invocation": "opencode-run", "model": "deepseek"}, "prompt", out)
        self.assertTrue(result["ok"])
        self.assertEqual(out.read_text(encoding="utf-8"), "ok")

    def test_invoke_opencode_openrouter_key_from_git(self):
        out = self.root / "opencode.md"
        captured = {}

        def fake_run(*_args, **kwargs):
            captured["env"] = kwargs["env"]
            return self.completed(stdout="ok")

        with mock.patch.object(ri, "_command", return_value="opencode"):
            with mock.patch.object(ri, "_openrouter_key_from_git", return_value="sk-test"):
                with mock.patch.object(ri.subprocess, "run", side_effect=fake_run):
                    result = ri.invoke({"invocation": "opencode-run", "model": "deepseek"}, "prompt", out)
        self.assertTrue(result["ok"])
        self.assertEqual(captured["env"]["OPENROUTER_API_KEY"], "sk-test")

    def test_invoke_codex_uses_tmp_output_then_replaces(self):
        out = self.root / "codex.md"

        def fake_run(cmd, **_kwargs):
            tmp = Path(cmd[cmd.index("--output-last-message") + 1])
            tmp.write_text("last message", encoding="utf-8")
            return self.completed(stdout="ignored")

        with mock.patch.object(ri, "_command", return_value="codex"):
            with mock.patch.object(ri.subprocess, "run", side_effect=fake_run):
                with mock.patch.object(ri.os, "replace", wraps=os.replace) as replace:
                    result = ri.invoke({"invocation": "codex-exec", "sandbox": "workspace-write"}, "prompt", out)
        self.assertTrue(result["ok"])
        self.assertEqual(out.read_text(encoding="utf-8"), "last message")
        self.assertEqual(Path(replace.call_args.args[1]), out)

    def test_invoke_codex_falls_back_to_stripped_stdout(self):
        out = self.root / "codex.md"
        with mock.patch.object(ri, "_command", return_value="codex"):
            with mock.patch.object(ri.subprocess, "run", return_value=self.completed(stdout="\x1b[31mfallback\x1b[0m")):
                result = ri.invoke({"invocation": "codex-exec"}, "prompt", out)
        self.assertTrue(result["ok"])
        self.assertEqual(out.read_text(encoding="utf-8"), "fallback")

    def test_invoke_human_writes_prompt_file(self):
        out = self.root / "response.md"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = ri.invoke({"invocation": "human-relay"}, "human prompt", out)
        prompt = self.root / "response-PROMPT.md"
        self.assertTrue(result["waiting_for_human"])
        self.assertEqual(prompt.read_text(encoding="utf-8"), "human prompt")
        self.assertIn(str(out), result["response_path"])

    def test_unsupported_invocation(self):
        out = self.root / "unknown.md"
        result = ri.invoke({"invocation": "bogus"}, "prompt", out)
        self.assertEqual(result["exit_code"], 2)
        self.assertIn("unsupported invocation", result["error"])


if __name__ == "__main__":
    unittest.main()
