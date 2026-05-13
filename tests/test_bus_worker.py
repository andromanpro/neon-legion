import hashlib
import json
import re
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import bus_envelope, bus_worker


HOST = "win-codex-01"
PAYLOAD = {"message": "hello"}
BASE_LABELS = ["phase:1.5-git-bus", f"neon:target/{HOST}", "neon:state/pending"]


def payload_sha(payload):
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def task(**overrides):
    data = {
        "schema_version": 1,
        "task_id": "ulid:01HQZWORKER000000000000",
        "kind": "echo",
        "target_host": HOST,
        "payload_ref": "file:///F:/tmp/payload.json",
        "payload_sha256": payload_sha(PAYLOAD),
        "lease_seconds": 600,
        "idempotency_key": "worker-test",
        "created_at": "2026-05-13T12:30:00Z",
    }
    data.update(overrides)
    return data


def issue(body=None, labels=None):
    return {"number": 50, "body": bus_envelope.serialize(task()) if body is None else body, "labels": labels or list(BASE_LABELS)}


class FakeDone:
    def __init__(self, stop_after):
        self.calls = 0
        self.stop_after = stop_after
        self.set_called = False

    def wait(self, timeout):
        self.calls += 1
        return self.calls > self.stop_after

    def set(self):
        self.set_called = True


class BusWorkerTests(unittest.TestCase):
    def setUp(self):
        self.original_handlers = dict(bus_worker.HANDLERS)
        self.comments = []
        self.updates = []
        self.patchers = [
            patch("tools.bus_worker.bus_gitea.list_issues", return_value=[]),
            patch("tools.bus_worker.bus_gitea.get_issue", return_value={"labels": [{"name": bus_worker.CLAIMED}]}),
            patch("tools.bus_worker.bus_gitea.list_comments", side_effect=self.fake_list_comments),
            patch("tools.bus_worker.bus_gitea.update_issue", side_effect=self.fake_update),
            patch("tools.bus_worker.bus_gitea.comment", side_effect=self.fake_comment),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.restore_handlers)
        bus_worker._STOP.clear()
        self.addCleanup(bus_worker._STOP.clear)

    def restore_handlers(self):
        bus_worker.HANDLERS.clear()
        bus_worker.HANDLERS.update(self.original_handlers)

    def fake_update(self, number, *, labels=None, state=None):
        self.updates.append({"number": number, "labels": labels, "state": state})
        return {"number": number, "labels": list(labels or []), "state": state or "open"}

    def fake_comment(self, number, body):
        comment = {"id": len(self.comments) + 1, "number": number, "body": body}
        self.comments.append(comment)
        return {"id": comment["id"]}

    def fake_list_comments(self, number):
        return [comment for comment in self.comments if comment["number"] == number]

    def result_body(self):
        for comment in reversed(self.comments):
            if "neon-result:v1" in comment["body"]:
                return comment["body"]
        self.fail("result comment was not posted")

    def states(self):
        return [call["labels"][-1] for call in self.updates if call["labels"]]

    def test_process_issue_happy_path(self):
        with patch("tools.bus_worker._payload_read", return_value=PAYLOAD):
            bus_worker.process_issue(issue(), HOST)

        self.assertEqual(self.states(), ["neon:state/claimed", "neon:state/in-progress", "neon:state/done"])
        self.assertEqual(self.updates[-1]["state"], "closed")
        self.assertIn("neon-claim:v1", self.comments[0]["body"])
        self.assertIn("neon-result:v1", self.result_body())
        self.assertIn('"status":"done"', self.result_body())

    def test_claim_win_when_no_concurrent_claim(self):
        with patch("tools.bus_worker._new_exec_id", return_value="mine-100"), \
             patch("tools.bus_worker._payload_read", return_value=PAYLOAD):
            bus_worker.process_issue(issue(), HOST)

        self.assertEqual(self.states(), ["neon:state/claimed", "neon:state/in-progress", "neon:state/done"])
        self.assertIn("exec=mine-100", self.comments[0]["body"])

    # DeepSeek audit on PR #71 (HIGH): "lowest id wins" — first poster is the
    # canonical winner. These tests cover the inverted-from-spec semantics.

    def test_claim_lost_when_other_worker_posted_before(self):
        # Other posted first (id=100) → other wins. We posted second (id=101) → we lose.
        handler = Mock()
        bus_worker.register_handler("echo", handler)
        mine_body = "<!-- neon-claim:v1 host=win-codex-01 exec=mine-100 claimed_at=2026-05-13T12:30:01Z lease_seconds=600 -->"
        other_body = "<!-- neon-claim:v1 host=win-codex-01 exec=other-099 claimed_at=2026-05-13T12:30:00Z lease_seconds=600 -->"

        with patch("tools.bus_worker._new_exec_id", return_value="mine-100"), \
             patch("tools.bus_worker.bus_gitea.comment", return_value={"id": 101}), \
             patch("tools.bus_worker.bus_gitea.list_comments", return_value=[
                 {"id": 100, "number": 50, "body": other_body},
                 {"id": 101, "number": 50, "body": mine_body},
             ]), \
             patch("tools.bus_worker._payload_read", return_value=PAYLOAD), \
             patch("tools.bus_worker.log") as log:
            bus_worker.process_issue(issue(), HOST)

        self.assertEqual(self.states(), ["neon:state/claimed"])
        self.assertFalse(any(call["labels"] and call["labels"][-1] == "neon:state/in-progress" for call in self.updates))
        handler.assert_not_called()
        self.assertTrue(any("claim lost" in call.args[0] for call in log.call_args_list))

    def test_claim_won_when_we_posted_first(self):
        # We posted first (id=100) → we win. Other posted second (id=101) → other loses.
        mine_body = "<!-- neon-claim:v1 host=win-codex-01 exec=mine-100 claimed_at=2026-05-13T12:30:00Z lease_seconds=600 -->"
        other_body = "<!-- neon-claim:v1 host=win-codex-01 exec=other-101 claimed_at=2026-05-13T12:30:01Z lease_seconds=600 -->"

        with patch("tools.bus_worker._new_exec_id", return_value="mine-100"), \
             patch("tools.bus_worker.bus_gitea.comment", return_value={"id": 100}) as comment, \
             patch("tools.bus_worker.bus_gitea.list_comments", return_value=[
                 {"id": 100, "number": 50, "body": mine_body},
                 {"id": 101, "number": 50, "body": other_body},
             ]), \
             patch("tools.bus_worker._payload_read", return_value=PAYLOAD):
            bus_worker.process_issue(issue(), HOST)

        self.assertEqual(self.states(), ["neon:state/claimed", "neon:state/in-progress", "neon:state/done"])
        self.assertTrue(any("neon-result:v1" in call.args[1] for call in comment.call_args_list))

    def test_interleaved_race_only_first_poster_wins(self):
        # DeepSeek MED #4: the genuinely hard race window. Worker A posts (id=100),
        # A verifies (sees only its own comment), under "highest id" A would win;
        # then B posts (id=101), B verifies (sees both), under "highest id" B also wins.
        # Under "lowest id wins" with my_comment_id guard, A's verify confirms its
        # own comment is the lowest AND matches my_comment_id → A wins. We simulate
        # B's path here: B posts second (gets id=101), but list_comments returns A's
        # earlier comment too. B must lose because lowest_id=100 != B's my_comment_id=101.
        a_body = "<!-- neon-claim:v1 host=win-codex-01 exec=worker-a claimed_at=2026-05-13T12:30:00Z lease_seconds=600 -->"
        b_body = "<!-- neon-claim:v1 host=win-codex-01 exec=worker-b claimed_at=2026-05-13T12:30:00Z lease_seconds=600 -->"
        handler = Mock()
        bus_worker.register_handler("echo", handler)

        # We are worker B — posted second (id=101). list_comments returns both.
        with patch("tools.bus_worker._new_exec_id", return_value="worker-b"), \
             patch("tools.bus_worker.bus_gitea.comment", return_value={"id": 101}), \
             patch("tools.bus_worker.bus_gitea.list_comments", return_value=[
                 {"id": 100, "number": 50, "body": a_body},
                 {"id": 101, "number": 50, "body": b_body},
             ]), \
             patch("tools.bus_worker._payload_read", return_value=PAYLOAD), \
             patch("tools.bus_worker.log"):
            bus_worker.process_issue(issue(), HOST)

        self.assertEqual(self.states(), ["neon:state/claimed"])
        handler.assert_not_called()

    def test_claim_lost_when_stale_old_cycle_claim_present(self):
        # DeepSeek MED #2: my_comment_id guard handles stale claims from prior cycles.
        # A leftover neon-claim:v1 comment with our same host but old exec_id has
        # a lower id (it was posted earlier in time / earlier in the issue history).
        # Without my_comment_id == lowest_id, lowest_exec matches my_exec only if
        # exec strings collide — but the lowest_id check fails our gate.
        my_exec = "mine-new"
        stale = f"<!-- neon-claim:v1 host=win-codex-01 exec={my_exec} claimed_at=2026-05-12T00:00:00Z lease_seconds=600 -->"
        fresh = f"<!-- neon-claim:v1 host=win-codex-01 exec={my_exec} claimed_at=2026-05-13T12:30:00Z lease_seconds=600 -->"

        with patch("tools.bus_worker._new_exec_id", return_value=my_exec), \
             patch("tools.bus_worker.bus_gitea.comment", return_value={"id": 200}), \
             patch("tools.bus_worker.bus_gitea.list_comments", return_value=[
                 {"id": 5, "number": 50, "body": stale},
                 {"id": 200, "number": 50, "body": fresh},
             ]), \
             patch("tools.bus_worker._payload_read", return_value=PAYLOAD), \
             patch("tools.bus_worker.log"):
            bus_worker.process_issue(issue(), HOST)

        # lowest is the stale one (id=5), exec matches but id != my_comment_id (200) → lost.
        self.assertEqual(self.states(), ["neon:state/claimed"])

    def test_claim_verify_handles_list_comments_failure(self):
        from tools.bus_gitea import BusGiteaError

        handler = Mock()
        bus_worker.register_handler("echo", handler)

        with patch("tools.bus_worker._new_exec_id", return_value="mine-100"), \
             patch("tools.bus_worker.bus_gitea.list_comments", side_effect=BusGiteaError(500, "comments down")), \
             patch("tools.bus_worker._payload_read", return_value=PAYLOAD), \
             patch("tools.bus_worker.log") as log:
            bus_worker.process_issue(issue(), HOST)

        self.assertEqual(self.states(), ["neon:state/claimed"])
        handler.assert_not_called()
        self.assertTrue(any("list_comments failed" in call.args[0] for call in log.call_args_list))

    def test_claim_lost_does_not_revert_label(self):
        # Other posted earlier (id=99), mine posted later (id=100) → mine loses
        # under lowest-id-wins. The lost worker must NOT revert label to pending —
        # that's the reaper's job via lease expiry.
        mine_body = "<!-- neon-claim:v1 host=win-codex-01 exec=mine-100 claimed_at=2026-05-13T12:30:01Z lease_seconds=600 -->"
        other_body = "<!-- neon-claim:v1 host=win-codex-01 exec=other-099 claimed_at=2026-05-13T12:30:00Z lease_seconds=600 -->"

        with patch("tools.bus_worker._new_exec_id", return_value="mine-100"), \
             patch("tools.bus_worker.bus_gitea.comment", return_value={"id": 100}), \
             patch("tools.bus_worker.bus_gitea.list_comments", return_value=[
                 {"id": 99, "number": 50, "body": other_body},
                 {"id": 100, "number": 50, "body": mine_body},
             ]), \
             patch("tools.bus_worker._payload_read", return_value=PAYLOAD):
            bus_worker.process_issue(issue(), HOST)

        # Only the initial pending → claimed PATCH; no revert.
        self.assertEqual(len(self.updates), 1)
        self.assertEqual(self.updates[0]["labels"][-1], "neon:state/claimed")
        self.assertNotIn("neon:state/pending", self.updates[0]["labels"])

    def test_process_issue_payload_sha_mismatch(self):
        bad_body = bus_envelope.serialize(task(payload_sha256="0" * 64))

        with patch("tools.bus_worker._payload_read", return_value=PAYLOAD):
            bus_worker.process_issue(issue(body=bad_body), HOST)

        self.assertIn("neon:state/failed", self.states())
        self.assertEqual(self.updates[-1]["state"], "closed")
        self.assertIn("payload_sha_mismatch", self.result_body())

    def test_process_issue_unsupported_scheme(self):
        body = bus_envelope.serialize(task(payload_ref="http://example.invalid/payload.json"))

        with patch("tools.bus_worker._payload_read", return_value=PAYLOAD):
            bus_worker.process_issue(issue(body=body), HOST)

        self.assertIn("neon:state/failed", self.states())
        self.assertIn("unsupported_payload_scheme", self.result_body())

    def test_process_issue_handler_raises(self):
        def boom(_envelope, _payload):
            raise ValueError("bad payload")

        bus_worker.register_handler("boom", boom)
        body = bus_envelope.serialize(task(kind="boom"))

        with patch("tools.bus_worker._payload_read", return_value=PAYLOAD):
            bus_worker.process_issue(issue(body=body), HOST)

        self.assertIn("neon:state/failed", self.states())
        self.assertIn("ValueError", self.result_body())
        self.assertIn("handler_exception", self.result_body())

    def test_process_issue_unknown_kind(self):
        body = bus_envelope.serialize(task(kind="missing-kind"))

        with patch("tools.bus_worker._payload_read", return_value=PAYLOAD):
            bus_worker.process_issue(issue(body=body), HOST)

        self.assertIn("neon:state/failed", self.states())
        self.assertIn("unknown_kind", self.result_body())

    def test_process_issue_malformed_envelope(self):
        with patch("tools.bus_worker._payload_read", return_value=PAYLOAD):
            bus_worker.process_issue(issue(body="plain issue body"), HOST)

        self.assertEqual(self.updates, [])
        self.assertEqual(self.comments, [])

    def test_process_issue_skips_already_claimed(self):
        labels = ["phase:1.5-git-bus", f"neon:target/{HOST}", "neon:state/claimed"]

        with patch("tools.bus_worker._payload_read", return_value=PAYLOAD):
            bus_worker.process_issue(issue(labels=labels), HOST)

        self.assertEqual(self.updates, [])
        self.assertEqual(self.comments, [])

    def test_register_handler_adds_to_registry(self):
        def custom(_envelope, payload):
            return {"custom": payload["message"]}

        bus_worker.register_handler("custom", custom)
        body = bus_envelope.serialize(task(kind="custom"))

        with patch("tools.bus_worker._payload_read", return_value=PAYLOAD):
            bus_worker.process_issue(issue(body=body), HOST)

        self.assertIn('"custom":"hello"', self.result_body())

    def test_echo_handler_returns_wrapped_payload(self):
        self.assertEqual(bus_worker.echo_handler(task(), PAYLOAD), {"echo": PAYLOAD})

    def test_heartbeat_thread_posts_comments_then_stops(self):
        done = FakeDone(stop_after=2)
        with patch("time.sleep"):
            thread = threading.Thread(target=bus_worker._heartbeat_loop, args=(50, "exec-1", 3, done), daemon=True)
            thread.start()
            thread.join(timeout=2)
            done.set()

        heartbeat_comments = [comment for comment in self.comments if "neon-hb:v1" in comment["body"]]
        self.assertGreaterEqual(len(heartbeat_comments), 2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(done.set_called)

    def test_exec_id_format(self):
        with patch("tools.bus_worker.time.time", return_value=1770000000), patch("tools.bus_worker.secrets.token_hex", return_value="abc123"):
            exec_id = bus_worker._new_exec_id(HOST)

        self.assertEqual(exec_id, f"{HOST}-1770000000-abc123")
        self.assertRegex(exec_id, re.compile(rf"^{re.escape(HOST)}-\d+-[0-9a-f]{{6}}$"))

    def test_wait_or_stop_returns_early_on_stop(self):
        with patch.object(bus_worker._STOP, "is_set", side_effect=[False, True]), patch("tools.bus_worker.time.sleep") as sleep:
            started = time.monotonic()
            bus_worker._wait_or_stop(30)
            elapsed = time.monotonic() - started

        self.assertLessEqual(elapsed, 1.0)
        sleep.assert_called_once_with(1.0)

    # DeepSeek audit A1 — payload root confinement
    def test_payload_path_raises_when_root_unset(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(bus_worker._WorkerFailure) as raised:
                bus_worker._payload_path("file:///F:/tmp/payload.json")
        self.assertEqual(raised.exception.reason, "payload_root_unset")

    def test_payload_path_raises_when_outside_root(self):
        root = ROOT
        outside = root.parent / "definitely-outside.json"
        with patch.dict("os.environ", {bus_worker.PAYLOAD_ROOT_ENV: str(root)}):
            with self.assertRaises(bus_worker._WorkerFailure) as raised:
                bus_worker._payload_path(f"file:///{outside.as_posix()}")
        self.assertEqual(raised.exception.reason, "payload_outside_root")

    def test_payload_path_accepts_path_inside_root(self):
        root = ROOT
        inside = root / "payload.json"
        with patch.dict("os.environ", {bus_worker.PAYLOAD_ROOT_ENV: str(root)}):
            resolved = bus_worker._payload_path(f"file:///{inside.as_posix()}")
        self.assertEqual(resolved, inside.resolve())

    def test_sha_mismatch_result_omits_actual_hash(self):
        # A1 leak: the failure comment must NOT echo the actual sha — that
        # would turn the worker into a content-fingerprint oracle.
        bad_body = bus_envelope.serialize(task(payload_sha256="0" * 64))
        with patch("tools.bus_worker._payload_read", return_value=PAYLOAD):
            bus_worker.process_issue(issue(body=bad_body), HOST)

        result = self.result_body()
        self.assertIn("payload_sha_mismatch", result)
        self.assertIn('"expected":"0000', result)
        self.assertNotIn('"actual":', result)

    def test_finalise_failure_does_not_propagate(self):
        # C1: a transient Gitea error on the final state transition must not
        # bubble out of process_issue, and must not cause the result envelope
        # to be re-posted under a contradictory reason.
        from tools.bus_gitea import BusGiteaError

        call_counter = {"set_state": 0}
        original_update = self.fake_update

        def flaky_update(number, *, labels=None, state=None):
            for label in labels or []:
                if label == bus_worker.DONE:
                    call_counter["set_state"] += 1
                    raise BusGiteaError(500, "transient 5xx")
            return original_update(number, labels=labels, state=state)

        with patch("tools.bus_worker._payload_read", return_value=PAYLOAD), \
             patch("tools.bus_worker.bus_gitea.update_issue", side_effect=flaky_update):
            try:
                bus_worker.process_issue(issue(), HOST)
            except BusGiteaError:
                self.fail("BusGiteaError on finalise leaked out of process_issue")

        self.assertEqual(call_counter["set_state"], 1)
        # Result envelope was posted exactly once, with status=done.
        result_comments = [c for c in self.comments if "neon-result:v1" in c["body"]]
        self.assertEqual(len(result_comments), 1)
        self.assertIn('"status":"done"', result_comments[0]["body"])

    def test_finalise_skipped_when_issue_expired_during_run(self):
        with patch("tools.bus_worker._new_exec_id", return_value="mine-100"), \
             patch("tools.bus_worker._payload_read", return_value=PAYLOAD), \
             patch("tools.bus_worker.bus_gitea.get_issue", return_value={"labels": [{"name": "neon:state/expired"}]}), \
             patch("tools.bus_worker._post_result") as post_result, \
             patch("tools.bus_worker.log") as log:
            bus_worker.process_issue(issue(), HOST)

        post_result.assert_not_called()
        self.assertNotIn("neon:state/done", self.states())
        self.assertNotIn("neon:state/failed", self.states())
        self.assertTrue(any("lease lost" in call.args[0] for call in log.call_args_list))

    def test_finalise_skipped_when_another_worker_reclaimed(self):
        mine_body = "<!-- neon-claim:v1 host=win-codex-01 exec=mine-100 claimed_at=2026-05-13T12:30:01Z lease_seconds=600 -->"
        other_body = "<!-- neon-claim:v1 host=win-codex-01 exec=other-099 claimed_at=2026-05-13T12:30:00Z lease_seconds=600 -->"
        calls = {"list_comments": 0}

        def list_comments(_number):
            calls["list_comments"] += 1
            if calls["list_comments"] == 1:
                return [{"id": 101, "number": 50, "body": mine_body}]
            return [
                {"id": 100, "number": 50, "body": other_body},
                {"id": 101, "number": 50, "body": mine_body},
            ]

        with patch("tools.bus_worker._new_exec_id", return_value="mine-100"), \
             patch("tools.bus_worker.bus_gitea.comment", return_value={"id": 101}), \
             patch("tools.bus_worker.bus_gitea.list_comments", side_effect=list_comments), \
             patch("tools.bus_worker.bus_gitea.get_issue", return_value={"labels": [{"name": bus_worker.CLAIMED}]}), \
             patch("tools.bus_worker._payload_read", return_value=PAYLOAD), \
             patch("tools.bus_worker._post_result") as post_result, \
             patch("tools.bus_worker.log") as log:
            bus_worker.process_issue(issue(), HOST)

        post_result.assert_not_called()
        self.assertNotIn("neon:state/done", self.states())
        self.assertNotIn("neon:state/failed", self.states())
        self.assertTrue(any("lease lost" in call.args[0] for call in log.call_args_list))

    def test_finalise_proceeds_when_lease_still_held(self):
        mine_body = "<!-- neon-claim:v1 host=win-codex-01 exec=mine-100 claimed_at=2026-05-13T12:30:00Z lease_seconds=600 -->"
        other_body = "<!-- neon-claim:v1 host=win-codex-01 exec=other-101 claimed_at=2026-05-13T12:30:01Z lease_seconds=600 -->"

        with patch("tools.bus_worker._new_exec_id", return_value="mine-100"), \
             patch("tools.bus_worker.bus_gitea.comment", return_value={"id": 100}), \
             patch("tools.bus_worker.bus_gitea.list_comments", return_value=[
                 {"id": 100, "number": 50, "body": mine_body},
                 {"id": 101, "number": 50, "body": other_body},
             ]), \
             patch("tools.bus_worker.bus_gitea.get_issue", return_value={"labels": [{"name": bus_worker.IN_PROGRESS}]}), \
             patch("tools.bus_worker._payload_read", return_value=PAYLOAD):
            bus_worker.process_issue(issue(), HOST)

        self.assertEqual(self.states(), ["neon:state/claimed", "neon:state/in-progress", "neon:state/done"])

    def test_finalise_skipped_on_get_issue_error(self):
        from tools.bus_gitea import BusGiteaError

        with patch("tools.bus_worker._new_exec_id", return_value="mine-100"), \
             patch("tools.bus_worker._payload_read", return_value=PAYLOAD), \
             patch("tools.bus_worker.bus_gitea.get_issue", side_effect=BusGiteaError(500, "issue down")), \
             patch("tools.bus_worker._post_result") as post_result, \
             patch("tools.bus_worker.log") as log:
            bus_worker.process_issue(issue(), HOST)

        post_result.assert_not_called()
        self.assertNotIn("neon:state/done", self.states())
        self.assertNotIn("neon:state/failed", self.states())
        self.assertTrue(any("lease lost" in call.args[0] for call in log.call_args_list))

    # DeepSeek audit on PR #73 (MED #1): _verify_lease_held must gate on
    # my_comment_id, not just my_exec, to be symmetric with _verify_claim_won.
    # This is defense in depth — currently no resurrection mechanism exists
    # (reaper closes the issue, expired issues stay closed), but if/when
    # resurrection lands, this guard prevents a zombie worker from finalising
    # over a re-claim where exec_id happens to collide.
    def test_finalise_skipped_when_lowest_claim_id_differs_from_mine(self):
        # Worker A posts claim at fake_comment id=1 (registered in setUp).
        # We mock list_comments at lease-check time to return a DIFFERENT
        # neon-claim:v1 with the same exec text but a different (lower) id —
        # this would have passed the my_exec-only check. With my_comment_id
        # gate, it correctly reports lost.
        my_exec = "win-codex-01-1700000000-abc123"
        # The lease check sees an "older" claim with the same exec but id=0
        # (somehow predating ours). Without the my_comment_id gate, this
        # would pass lowest_exec == my_exec. With the gate, lowest_id (0) !=
        # my_comment_id (1) → lost.
        ghost = f"<!-- neon-claim:v1 host=win-codex-01 exec={my_exec} claimed_at=2026-05-12T00:00:00Z lease_seconds=600 -->"

        # Patch list_comments to return the ghost-only on the LEASE check
        # (second call). The first call (claim verify) sees the worker's
        # own posted comment normally.
        original_list = self.fake_list_comments
        call_count = {"n": 0}

        def list_comments_seq(number):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return original_list(number)
            return [{"id": 0, "number": number, "body": ghost}]

        with patch("tools.bus_worker._new_exec_id", return_value=my_exec), \
             patch("tools.bus_worker._payload_read", return_value=PAYLOAD), \
             patch("tools.bus_worker.bus_gitea.list_comments", side_effect=list_comments_seq), \
             patch("tools.bus_worker._post_result") as post_result, \
             patch("tools.bus_worker.log") as log:
            bus_worker.process_issue(issue(), HOST)

        post_result.assert_not_called()
        self.assertNotIn("neon:state/done", self.states())
        self.assertTrue(any("lease lost" in call.args[0] for call in log.call_args_list))


if __name__ == "__main__":
    unittest.main()
