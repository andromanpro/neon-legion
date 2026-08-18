"""The estimator must not outlive its deadline, and must not stack up.

On 2026-08-01 a backfill left 247 estimator processes running: the hook
fired a bare Popen that nothing owned, and the per-session lock aged out
after ten minutes whether or not the run behind it had finished — so the
expiry started a second estimator instead of stopping the first.
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


start = load("claude_session_start", "hooks/claude-session-start.py")
supervisor = load("estimate_supervisor", "tracker/estimate-supervisor.py")


def dead_pid() -> int:
    for candidate in (999999, 999997, 999995):
        if not start.HOOK.process_alive(candidate):
            return candidate
    raise unittest.SkipTest("no reliably dead pid available")


class LockOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._saved = (start.LOG_DIR, start.TASKS_LOCK_FILE, start.HOOK.TRACKER_DIR, start.HOOK.OPS_FILE)
        start.LOG_DIR = self.root
        start.TASKS_LOCK_FILE = self.root / ".tasks.lock"
        start.HOOK.TRACKER_DIR = self.root
        start.HOOK.OPS_FILE = self.root / "ops-events.jsonl"

    def tearDown(self) -> None:
        start.LOG_DIR, start.TASKS_LOCK_FILE, start.HOOK.TRACKER_DIR, start.HOOK.OPS_FILE = self._saved
        self._tmp.cleanup()

    def age(self, path: Path, seconds: float) -> None:
        stamp = time.time() - seconds
        os.utime(path, (stamp, stamp))

    def test_live_owner_keeps_its_lock_past_the_ttl(self) -> None:
        """The old rule deleted the lock on age alone and let a second
        estimator start beside the first."""
        lock = self.root / "s1.lock"
        lock.write_text(str(os.getpid()), encoding="ascii")
        self.age(lock, start.INFLIGHT_TTL_SECONDS * 5)
        self.assertFalse(start.lock_holder_is_gone(lock, start.INFLIGHT_TTL_SECONDS))

    def test_dead_owner_releases_immediately(self) -> None:
        lock = self.root / "s1.lock"
        lock.write_text(str(dead_pid()), encoding="ascii")
        self.assertTrue(start.lock_holder_is_gone(lock, start.INFLIGHT_TTL_SECONDS))

    def test_pidless_lock_falls_back_to_age(self) -> None:
        lock = self.root / "s1.lock"
        lock.write_text("", encoding="ascii")
        self.assertFalse(start.lock_holder_is_gone(lock, start.INFLIGHT_TTL_SECONDS))
        self.age(lock, start.INFLIGHT_TTL_SECONDS + 60)
        self.assertTrue(start.lock_holder_is_gone(lock, start.INFLIGHT_TTL_SECONDS))

    def test_live_count_ignores_dead_holders(self) -> None:
        (self.root / "alive.lock").write_text(str(os.getpid()), encoding="ascii")
        (self.root / "dead.lock").write_text(str(dead_pid()), encoding="ascii")
        self.assertEqual(start.live_estimator_count(), 1)

    def test_inflight_lock_is_refused_while_the_owner_runs(self) -> None:
        (self.root / "s1.lock").write_text(str(os.getpid()), encoding="ascii")
        self.assertFalse(start.create_inflight_lock("s1"))

    def test_inflight_lock_is_taken_over_from_a_dead_owner(self) -> None:
        (self.root / "s1.lock").write_text(str(dead_pid()), encoding="ascii")
        self.assertTrue(start.create_inflight_lock("s1"))

    def test_tasks_lock_survives_a_dead_holder(self) -> None:
        start.TASKS_LOCK_FILE.write_text(str(dead_pid()), encoding="ascii")
        fd = start.acquire_tasks_lock()
        self.assertIsNotNone(fd)
        start.release_tasks_lock(fd)

    def test_tasks_lock_respects_a_live_holder(self) -> None:
        start.TASKS_LOCK_FILE.write_text(str(os.getpid()), encoding="ascii")
        self.assertIsNone(start.acquire_tasks_lock())


class SupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._saved = (
            supervisor.LOG_DIR,
            supervisor.estimator_command,
            supervisor.HOOK.TRACKER_DIR,
            supervisor.HOOK.OPS_FILE,
        )
        supervisor.LOG_DIR = self.root
        supervisor.HOOK.TRACKER_DIR = self.root
        supervisor.HOOK.OPS_FILE = self.root / "ops-events.jsonl"

    def tearDown(self) -> None:
        (
            supervisor.LOG_DIR,
            supervisor.estimator_command,
            supervisor.HOOK.TRACKER_DIR,
            supervisor.HOOK.OPS_FILE,
        ) = self._saved
        self._tmp.cleanup()

    def ops_codes(self) -> list[str]:
        import json

        if not supervisor.HOOK.OPS_FILE.exists():
            return []
        return [
            json.loads(line)["code"]
            for line in supervisor.HOOK.OPS_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_a_run_that_finishes_releases_its_lock(self) -> None:
        supervisor.estimator_command = lambda *_a: ["py", "-3.14", "-c", "pass"]
        (self.root / "s1.lock").write_text("0", encoding="ascii")
        self.assertEqual(supervisor.main(["s1", "transcript.jsonl", "60"]), 0)
        self.assertFalse((self.root / "s1.lock").exists())

    def test_a_run_past_its_deadline_is_killed_and_reported(self) -> None:
        supervisor.estimator_command = lambda *_a: ["py", "-3.14", "-c", "import time; time.sleep(120)"]
        (self.root / "s1.lock").write_text("0", encoding="ascii")
        started = time.monotonic()
        code = supervisor.main(["s1", "transcript.jsonl", "1"])
        elapsed = time.monotonic() - started
        self.assertEqual(code, 1)
        self.assertLess(elapsed, 60, "the deadline must end the run, not the child's own timeout")
        self.assertFalse((self.root / "s1.lock").exists(), "a killed run still has to release its lock")
        self.assertIn("estimator_deadline_exceeded", self.ops_codes())

    def test_a_failing_run_is_reported_not_swallowed(self) -> None:
        supervisor.estimator_command = lambda *_a: ["py", "-3.14", "-c", "raise SystemExit(3)"]
        (self.root / "s1.lock").write_text("0", encoding="ascii")
        supervisor.main(["s1", "transcript.jsonl", "60"])
        self.assertIn("estimator_failed", self.ops_codes())

    def test_supervisor_claims_the_lock_from_the_hook(self) -> None:
        """The lock must name the process actually working, not the hook that
        exited seconds later."""
        lock = self.root / "s1.lock"
        lock.write_text("12345", encoding="ascii")
        supervisor.claim_lock("s1")
        self.assertEqual(lock.read_text(encoding="ascii"), str(os.getpid()))


if __name__ == "__main__":
    unittest.main()
