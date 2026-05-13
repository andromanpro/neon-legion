from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.git_diff_cost import build_diff_cost


NOW = datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc)
GIT_AVAILABLE = shutil.which("git") is not None


def event(session_id: str, ts: datetime, cost: float = 1.0) -> dict:
    return {
        "session_id": session_id,
        "ts": ts.isoformat(),
        "cost_estimate_usd": cost,
    }


@unittest.skipUnless(GIT_AVAILABLE, "git binary not available")
class GitDiffCostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = ROOT / f"git-diff-cost-{uuid.uuid4().hex[:12]}"
        self.env = os.environ.copy()
        self.env.update(
            {
                "GIT_AUTHOR_NAME": "Test User",
                "GIT_AUTHOR_EMAIL": "test@example.invalid",
                "GIT_COMMITTER_NAME": "Test User",
                "GIT_COMMITTER_EMAIL": "test@example.invalid",
            }
        )
        self._run(["git", "init", str(self.repo)], cwd=ROOT)
        self.git("config", "user.name", "Test User")
        self.git("config", "user.email", "test@example.invalid")

    def tearDown(self) -> None:
        shutil.rmtree(self.repo, ignore_errors=True)

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self._run(["git", *args], cwd=self.repo)

    def _run(self, args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(args, cwd=cwd, env=self.env, text=True, capture_output=True, check=True)
        except subprocess.CalledProcessError as exc:
            self.skipTest(f"git fixture setup failed: {exc.stderr.strip() or exc.stdout.strip()}")

    def commit_file(self, name: str, content: str, message: str, when: datetime) -> str:
        path = self.repo / name
        path.write_text(content, encoding="utf-8")
        self.git("add", name)
        env = self.env.copy()
        env["GIT_AUTHOR_DATE"] = when.isoformat()
        env["GIT_COMMITTER_DATE"] = when.isoformat()
        subprocess.run(["git", "commit", "-m", message], cwd=self.repo, env=env, text=True, capture_output=True, check=True)
        return self.git("rev-parse", "--short", "HEAD").stdout.strip()

    def test_no_events_returns_empty_sessions(self) -> None:
        payload = build_diff_cost([], self.repo, now=NOW)

        self.assertEqual([], payload["sessions"])
        self.assertEqual(0, payload["summary"]["total_sessions_scanned"])

    def test_session_with_one_commit_in_window(self) -> None:
        commit_at = NOW - timedelta(minutes=30)
        short_hash = self.commit_file("a.txt", "one\ntwo\nthree\n", "add lines", commit_at)
        events = [
            event("abc12345-session", NOW - timedelta(hours=1), 0.5),
            event("abc12345-session", NOW, 0.5),
        ]

        payload = build_diff_cost(events, self.repo, now=NOW)

        session = payload["sessions"][0]
        self.assertFalse(session["no_diff"])
        self.assertEqual(1, len(session["commits"]))
        self.assertEqual(short_hash[:7], session["commits"][0]["hash"])
        self.assertEqual(3, session["total_lines_changed"])

    def test_session_with_no_commits_in_window_is_no_diff_bucket(self) -> None:
        self.commit_file("outside.txt", "old\n", "outside", NOW - timedelta(days=3))
        events = [
            event("no-commit-session", NOW - timedelta(hours=1), 1.0),
            event("no-commit-session", NOW, 2.0),
        ]

        payload = build_diff_cost(events, self.repo, now=NOW)

        session = payload["sessions"][0]
        self.assertTrue(session["no_diff"])
        self.assertNotIn("commits", session)
        self.assertNotIn("cost_per_line_usd", session)
        self.assertEqual(1, payload["summary"]["no_diff_count"])

    def test_session_cost_per_line_calculation(self) -> None:
        commit_at = NOW - timedelta(minutes=15)
        self.commit_file("ratio.txt", "a\nb\nc\nd\n", "known ratio", commit_at)
        events = [
            event("ratio-session", NOW - timedelta(hours=1), 1.0),
            event("ratio-session", NOW, 1.0),
        ]

        payload = build_diff_cost(events, self.repo, now=NOW)

        session = payload["sessions"][0]
        self.assertEqual(4, session["total_lines_changed"])
        self.assertAlmostEqual(0.5, session["cost_per_line_usd"])

    def test_expensive_sessions_in_top_decile(self) -> None:
        events = []
        base = NOW - timedelta(hours=10)
        for idx in range(10):
            session_id = f"session-{idx}"
            start = base + timedelta(hours=idx)
            commit_at = start + timedelta(minutes=30)
            cost = 100.0 if idx == 9 else 1.0
            self.commit_file(f"file-{idx}.txt", "line\n", f"commit {idx}", commit_at)
            events.extend([event(session_id, start, cost / 2), event(session_id, start + timedelta(minutes=59), cost / 2)])

        payload = build_diff_cost(events, self.repo, now=NOW)

        self.assertEqual(10, payload["summary"]["sessions_with_commits"])
        self.assertEqual(1, payload["summary"]["expensive_sessions_count"])
        self.assertEqual("session-9", payload["expensive_sessions"][0]["session_id"])

    def test_non_git_repo_path_returns_empty_with_warning(self) -> None:
        non_git = ROOT / f"not-git-{uuid.uuid4().hex[:12]}"
        non_git.mkdir()
        try:
            payload = build_diff_cost([event("sid", NOW, 1.0)], non_git, now=NOW)
        finally:
            shutil.rmtree(non_git, ignore_errors=True)

        self.assertEqual([], payload["sessions"])
        self.assertEqual(0, payload["summary"]["total_sessions_scanned"])


if __name__ == "__main__":
    unittest.main()
