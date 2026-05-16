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

from tools.git_diff_cost import build_diff_cost, build_multi_repo_diff_cost


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

    # DeepSeek MED #6 on PR #87: git_errors list is now a structural field.
    def test_payload_carries_git_errors_field(self) -> None:
        # Repo with at least one real commit so git log doesn't fatal-out on
        # "branch has no commits". Session window in the past → no commits in
        # window but log itself succeeds → git_errors stays empty.
        when = NOW - timedelta(days=1)
        self.commit_file("seed.txt", "hello", "seed", when)
        events = [event("sid", when - timedelta(days=10), 1.0)]
        payload = build_diff_cost(events, self.repo, now=NOW)

        self.assertIn("git_errors", payload)
        self.assertIsInstance(payload["git_errors"], list)
        self.assertEqual(0, payload["summary"]["git_errors_count"])

    # DeepSeek MED #9 on PR #87: sessions with commits but zero line changes
    # (merges, renames, chmods) are now flagged structurally + excluded from
    # cost_per_line attribution.
    def test_zero_line_diff_session_excluded_from_cost_per_line_pool(self) -> None:
        # `git commit --allow-empty` creates a commit with 0 file changes.
        when = NOW - timedelta(minutes=5)
        env = self.env.copy()
        env["GIT_AUTHOR_DATE"] = when.isoformat()
        env["GIT_COMMITTER_DATE"] = when.isoformat()
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "empty merge"],
            cwd=self.repo, env=env, text=True, capture_output=True, check=True,
        )
        events = [event("zero", when, 5.0)]
        payload = build_diff_cost(events, self.repo, now=NOW)

        # The session has a commit but 0 lines → cost_per_line_usd is None,
        # session_has_commits_but_zero_lines is True, no_diff True (sharing
        # the bucket).
        self.assertEqual(1, len(payload["sessions"]))
        session = payload["sessions"][0]
        self.assertEqual(session["total_lines_changed"], 0)
        self.assertIsNone(session["cost_per_line_usd"])
        self.assertTrue(session.get("session_has_commits_but_zero_lines"))
        # And the percentile pool excludes it.
        self.assertEqual(0, payload["summary"]["sessions_with_commits"])
        self.assertEqual(0, len(payload["expensive_sessions"]))

    def test_single_repo_payload_top_level_keys_stay_compatible(self) -> None:
        when = NOW - timedelta(minutes=10)
        self.commit_file("compat.txt", "one\ntwo\n", "compat", when)
        payload = build_diff_cost(
            [
                event("compat-session", when - timedelta(minutes=1), 0.5),
                event("compat-session", when + timedelta(minutes=1), 0.5),
            ],
            self.repo,
            now=NOW,
        )

        self.assertEqual(
            {
                "schema_version",
                "generated_at",
                "config",
                "sessions",
                "expensive_sessions",
                "summary",
                "git_errors",
            },
            set(payload.keys()),
        )
        self.assertEqual({"repo_path", "lookback_days", "top_decile_threshold"}, set(payload["config"].keys()))
        self.assertEqual(1, payload["schema_version"])
        self.assertNotIn("per_repo", payload)
        self.assertNotIn("repos", payload["config"])


@unittest.skipUnless(GIT_AVAILABLE, "git binary not available")
class MultiRepoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / f"git-diff-cost-multi-{uuid.uuid4().hex[:12]}"
        self.repo_a = self.root / "repo-a"
        self.repo_b = self.root / "repo-b"
        self.env = os.environ.copy()
        self.env.update(
            {
                "GIT_AUTHOR_NAME": "Test User",
                "GIT_AUTHOR_EMAIL": "test@example.invalid",
                "GIT_COMMITTER_NAME": "Test User",
                "GIT_COMMITTER_EMAIL": "test@example.invalid",
            }
        )
        self.root.mkdir()
        for repo in (self.repo_a, self.repo_b):
            self._run(["git", "init", str(repo)], cwd=self.root)
            self.git(repo, "config", "user.name", "Test User")
            self.git(repo, "config", "user.email", "test@example.invalid")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def git(self, repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return self._run(["git", *args], cwd=repo)

    def _run(
        self,
        args: list[str],
        cwd: Path,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                args,
                cwd=cwd,
                env=env or self.env,
                text=True,
                capture_output=True,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            self.skipTest(f"git fixture setup failed: {exc.stderr.strip() or exc.stdout.strip()}")

    def commit_lines(self, repo: Path, name: str, line_count: int, message: str, when: datetime) -> str:
        path = repo / name
        path.write_text("".join(f"{name}-{idx}\n" for idx in range(line_count)), encoding="utf-8")
        self.git(repo, "add", name)
        env = self.env.copy()
        env["GIT_AUTHOR_DATE"] = when.isoformat()
        env["GIT_COMMITTER_DATE"] = when.isoformat()
        self._run(["git", "commit", "-m", message], cwd=repo, env=env)
        return self.git(repo, "rev-parse", "--short", "HEAD").stdout.strip()

    def sparse_payload(self) -> dict:
        both_start = NOW - timedelta(hours=4)
        a_only_start = NOW - timedelta(hours=3)
        no_repo_start = NOW - timedelta(hours=2)
        b_expensive_start = NOW - timedelta(hours=1)

        self.commit_lines(self.repo_a, "both-a.txt", 2, "both A", both_start + timedelta(minutes=20))
        self.commit_lines(self.repo_b, "both-b.txt", 4, "both B", both_start + timedelta(minutes=30))
        self.commit_lines(self.repo_a, "a-only.txt", 3, "A only", a_only_start + timedelta(minutes=30))
        self.commit_lines(self.repo_b, "b-expensive.txt", 3, "B expensive", b_expensive_start + timedelta(minutes=30))

        events = [
            event("both-repos-session", both_start, 6.0),
            event("both-repos-session", both_start + timedelta(minutes=59), 6.0),
            event("a-only-session", a_only_start, 1.5),
            event("a-only-session", a_only_start + timedelta(minutes=59), 1.5),
            event("no-repo-session", no_repo_start, 2.5),
            event("no-repo-session", no_repo_start + timedelta(minutes=59), 2.5),
            event("b-expensive-session", b_expensive_start, 7.5),
            event("b-expensive-session", b_expensive_start + timedelta(minutes=59), 7.5),
        ]
        return build_multi_repo_diff_cost(
            events,
            [("A", self.repo_a), ("B", self.repo_b)],
            top_decile_threshold=0.5,
            now=NOW,
        )

    def session(self, payload: dict, session_id: str) -> dict:
        matches = [session for session in payload["sessions"] if session["session_id"] == session_id]
        self.assertEqual(1, len(matches))
        return matches[0]

    def test_session_touching_both_repos_counts_cost_once(self) -> None:
        payload = self.sparse_payload()

        session = self.session(payload, "both-repos-session")
        self.assertFalse(session["no_diff"])
        self.assertEqual(6, session["total_lines_changed"])
        self.assertAlmostEqual(2.0, session["cost_per_line_usd"])
        self.assertNotAlmostEqual(6.0, session["cost_per_line_usd"])
        self.assertNotAlmostEqual(3.0, session["cost_per_line_usd"])
        self.assertEqual({"A", "B"}, {commit["repo"] for commit in session["commits"]})
        lines_by_repo = {
            repo: sum(commit["insertions"] + commit["deletions"] for commit in session["commits"] if commit["repo"] == repo)
            for repo in ("A", "B")
        }
        self.assertEqual({"A": 2, "B": 4}, lines_by_repo)

    def test_session_touching_only_one_repo_uses_that_repo_lines(self) -> None:
        payload = self.sparse_payload()

        session = self.session(payload, "a-only-session")
        self.assertFalse(session["no_diff"])
        self.assertEqual(3, session["total_lines_changed"])
        self.assertAlmostEqual(1.0, session["cost_per_line_usd"])
        self.assertEqual({"A"}, {commit["repo"] for commit in session["commits"]})

    def test_session_touching_no_repo_stays_no_diff(self) -> None:
        payload = self.sparse_payload()

        session = self.session(payload, "no-repo-session")
        self.assertTrue(session["no_diff"])
        self.assertNotIn("commits", session)
        self.assertNotIn("cost_per_line_usd", session)

    def test_per_repo_breakdown_tracks_lines_not_cost(self) -> None:
        payload = self.sparse_payload()

        self.assertEqual(["A", "B"], payload["config"]["repos"])
        self.assertEqual(str(self.repo_a), payload["config"]["repo_path"])
        self.assertEqual({"A", "B"}, set(payload["per_repo"].keys()))
        self.assertEqual(5, payload["per_repo"]["A"]["total_lines"])
        self.assertEqual(7, payload["per_repo"]["B"]["total_lines"])
        self.assertEqual(2, payload["per_repo"]["A"]["sessions_with_commits"])
        self.assertEqual(2, payload["per_repo"]["B"]["sessions_with_commits"])
        self.assertEqual(1, payload["per_repo"]["A"]["expensive_sessions_count"])
        self.assertEqual(2, payload["per_repo"]["B"]["expensive_sessions_count"])
        self.assertNotIn("cost_usd", payload["per_repo"]["A"])

    def test_expensive_sessions_use_cross_repo_cost_per_line_and_sort_desc(self) -> None:
        payload = self.sparse_payload()

        self.assertEqual(3, payload["summary"]["sessions_with_commits"])
        self.assertEqual(1, payload["summary"]["no_diff_count"])
        self.assertEqual(2.0, payload["summary"]["expensive_lines_threshold_usd_per_line"])
        self.assertEqual(
            ["b-expensive-session", "both-repos-session"],
            [session["session_id"] for session in payload["expensive_sessions"]],
        )


if __name__ == "__main__":
    unittest.main()
