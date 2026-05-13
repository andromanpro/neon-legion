import hashlib
import json
import re
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


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
            patch("tools.bus_worker.bus_gitea.get_issue", return_value={}),
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
        self.comments.append({"number": number, "body": body})
        return {"id": len(self.comments)}

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
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            outside = Path(root).parent / "definitely-outside.json"
            with patch.dict("os.environ", {bus_worker.PAYLOAD_ROOT_ENV: root}):
                with self.assertRaises(bus_worker._WorkerFailure) as raised:
                    bus_worker._payload_path(f"file:///{outside.as_posix()}")
        self.assertEqual(raised.exception.reason, "payload_outside_root")

    def test_payload_path_accepts_path_inside_root(self):
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            inside = Path(root) / "payload.json"
            inside.write_text("{}", encoding="utf-8")
            with patch.dict("os.environ", {bus_worker.PAYLOAD_ROOT_ENV: root}):
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


if __name__ == "__main__":
    unittest.main()
