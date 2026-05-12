import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
SPEC = importlib.util.spec_from_file_location("oss_sanitize_under_test", TOOLS_DIR / "oss-sanitize.py")
oss = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(oss)


class OssSanitizeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        os.chmod(self.root, 0o777)
        self.old = oss.PROJECT_ROOT, oss.BACKUP_DIR
        oss.PROJECT_ROOT = self.root
        oss.BACKUP_DIR = self.root / ".oss-backup"

    def tearDown(self):
        oss.PROJECT_ROOT, oss.BACKUP_DIR = self.old
        shutil.rmtree(self.root)

    def run_main(self, *args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(sys, "argv", ["oss-sanitize.py", *args]):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = oss.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def write_private_rule(self, pattern="customer-name-x", repl="<client>", desc="customer rule"):
        (self.root / ".oss-sanitize-private.txt").write_text(
            f"{pattern} | {repl} | {desc}\n",
            encoding="utf-8",
        )

    def test_check_mode_finds_violations(self):
        self.write_private_rule()
        (self.root / "README.md").write_text("customer-name-x is private\n", encoding="utf-8")
        code, stdout, stderr = self.run_main("--check", "--globs", "README.md")
        self.assertEqual(code, 2)
        self.assertIn("README.md", stdout)
        self.assertIn("customer rule", stdout)
        self.assertIn("violations remain", stderr)

    def test_check_mode_zero_exit_clean(self):
        (self.root / "README.md").write_text("public text\n", encoding="utf-8")
        code, stdout, _stderr = self.run_main("--check", "--globs", "README.md")
        self.assertEqual(code, 0)
        self.assertIn("Files with hits: 0", stdout)

    def test_check_mode_nonzero_exit_dirty(self):
        (self.root / "README.md").write_text("host=192.168.1.130\n", encoding="utf-8")
        code, stdout, _stderr = self.run_main("--check", "--globs", "README.md")
        self.assertEqual(code, 2)
        self.assertIn("Private LAN IP", stdout)

    def test_apply_creates_backup(self):
        self.write_private_rule()
        readme = self.root / "README.md"
        readme.write_text("customer-name-x\n", encoding="utf-8")
        code, _stdout, _stderr = self.run_main("--apply", "--globs", "README.md")
        self.assertEqual(code, 0)
        self.assertEqual((oss.BACKUP_DIR / "README.md").read_text(encoding="utf-8"), "customer-name-x\n")
        self.assertEqual(readme.read_text(encoding="utf-8"), "<client>\n")

    def test_apply_idempotent(self):
        self.write_private_rule()
        readme = self.root / "README.md"
        readme.write_text("customer-name-x\n", encoding="utf-8")
        self.assertEqual(self.run_main("--apply", "--globs", "README.md")[0], 0)
        code, stdout, _stderr = self.run_main("--apply", "--globs", "README.md")
        self.assertEqual(code, 0)
        self.assertIn("Total substitutions: 0", stdout)

    def test_diff_mode_doesnt_modify(self):
        readme = self.root / "README.md"
        original = "host=192.168.1.130\n"
        readme.write_text(original, encoding="utf-8")
        code, stdout, _stderr = self.run_main("--diff", "--globs", "README.md")
        self.assertEqual(code, 0)
        self.assertIn("-host=192.168.1.130", stdout)
        self.assertIn("+host=localhost", stdout)
        self.assertEqual(readme.read_text(encoding="utf-8"), original)

    def test_generic_rules_rfc1918_ip(self):
        new, hits = oss.sanitize_text("api=http://192.168.1.130:8000\n")
        self.assertIn("api=http://localhost:8000", new)
        self.assertNotIn("192.168.1.130", new)
        self.assertEqual(hits[0][0], "Private LAN IP (RFC1918)")

    def test_generic_rules_home_dir(self):
        new, hits = oss.sanitize_text("path=/home/alice/project/file.txt\n")
        self.assertIn("<user_home>", new)
        self.assertNotIn("/home/alice", new)
        self.assertEqual(hits[0][0], "Unix user home")

    def test_private_rules_file_loaded(self):
        self.write_private_rule("secret-client", "<client>", "secret client")
        new, hits = oss.sanitize_text("secret-client ships here\n")
        self.assertEqual(new, "<client> ships here\n")
        self.assertEqual(hits, [("secret client", 1)])

    def test_default_excluded_files_skipped(self):
        security = self.root / "SECURITY.md"
        security.write_text("host=192.168.1.130\n", encoding="utf-8")
        code, _stdout, stderr = self.run_main("--apply", "--globs", "SECURITY.md")
        self.assertEqual(code, 0)
        self.assertIn("No files matched", stderr)
        self.assertEqual(security.read_text(encoding="utf-8"), "host=192.168.1.130\n")

    def test_keep_marker_preserves_block(self):
        text = (
            "before 192.168.1.1\n"
            "<!-- oss:keep -->\n"
            "inside 192.168.1.2\n"
            "<!-- /oss:keep -->\n"
            "after /home/alice/project\n"
        )
        new, _hits = oss.sanitize_text(text)
        self.assertIn("before localhost", new)
        self.assertIn("inside 192.168.1.2", new)
        self.assertIn("after <user_home>", new)


if __name__ == "__main__":
    unittest.main()
