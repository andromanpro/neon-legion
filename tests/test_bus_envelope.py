import contextlib
import hashlib
import io
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.bus_envelope import parse, serialize, verify_sha


CLOSE = "<!-- /neon-task:v1 -->"


TASK = {
    "schema_version": 1,
    "task_id": "ulid:01HQZTEST000000000000000",
    "kind": "codex_exec",
    "target_host": "win-claude-01",
    "payload_ref": "smb://nas/neon-bus/payloads/01HQZ.json",
    "payload_sha256": "a" * 64,
    "lease_seconds": 600,
    "idempotency_key": "openclaw-2026-05-13T12:30Z-codex-exec-7",
    "created_at": "2026-05-13T12:30:00Z",
}


def canonical_sha(task):
    body = json.dumps(task, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def wrap(task):
    digest = canonical_sha(task)
    body = json.dumps(task, sort_keys=True, indent=2, ensure_ascii=False)
    return f"<!-- neon-task:v1 sha256={digest} -->\n{body}\n{CLOSE}"


class BusEnvelopeTests(unittest.TestCase):
    def test_roundtrip(self):
        text = serialize(TASK)
        self.assertEqual(parse(text), TASK)

    def test_serialize_sha256_correctness(self):
        text = serialize(TASK)
        expected = "9383ac6a82e3d4d9fd223d95df5c876f923545f363e3a1878ee5ddcb75912836"
        self.assertIn(f"sha256={expected}", text)

    def test_parse_returns_none_on_wrong_sha(self):
        text = serialize(TASK).replace("codex_exec", "codex_exed", 1)
        self.assertIsNone(parse(text))

    def test_parse_returns_none_on_missing_field(self):
        task = dict(TASK)
        del task["task_id"]
        self.assertIsNone(parse(wrap(task)))

    def test_parse_returns_none_on_malformed_json(self):
        text = "<!-- neon-task:v1 sha256=" + "0" * 64 + " -->\n{not json}\n" + CLOSE
        self.assertIsNone(parse(text))

    def test_parse_returns_none_on_v2_schema(self):
        task = dict(TASK, schema_version=2)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = parse(wrap(task))
        self.assertIsNone(result)
        self.assertIn("schema_version", stderr.getvalue())
        self.assertIn("2", stderr.getvalue())

    def test_parse_picks_first_envelope(self):
        first = dict(TASK, task_id="ulid:01FIRST")
        second = dict(TASK, task_id="ulid:01SECOND")
        text = "prefix\n" + wrap(first) + "\nbetween\n" + wrap(second)
        self.assertEqual(parse(text), first)

    def test_parse_returns_none_on_no_sentinel(self):
        self.assertIsNone(parse("plain text without an envelope"))

    def test_verify_sha_true_on_match(self):
        self.assertTrue(verify_sha(serialize(TASK)))

    def test_verify_sha_false_on_mismatch(self):
        text = serialize(TASK).replace("win-claude-01", "win-claude-02", 1)
        self.assertFalse(verify_sha(text))

    def test_serialize_raises_on_missing_field(self):
        task = dict(TASK)
        del task["task_id"]
        with self.assertRaises(ValueError):
            serialize(task)


if __name__ == "__main__":
    unittest.main()
