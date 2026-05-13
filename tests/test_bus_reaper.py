import contextlib
import io
import re
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import bus_reaper


NOW = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)
BASE_LABELS = ["phase:1.5-git-bus", "neon:target/win-claude-01", "neon:state/claimed"]


def issue(labels=None):
    return {"number": 51, "labels": labels or list(BASE_LABELS)}


def comment(body):
    return {"body": body}


def claim(ts="2026-05-13T11:55:00Z", lease=600):
    return comment(f"<!-- neon-claim:v1 host=win-claude-01 exec=exec-1 claimed_at={ts} lease_seconds={lease} -->")


def hb(ts):
    return comment(f"<!-- neon-hb:v1 exec=exec-1 ts={ts} -->")


class BusReaperTests(unittest.TestCase):
    def setUp(self):
        self.comments = []
        self.updates = []
        self.patchers = [
            patch("tools.bus_reaper.bus_gitea.list_comments", return_value=[]),
            patch("tools.bus_reaper.bus_gitea.update_issue", side_effect=self.fake_update),
            patch("tools.bus_reaper.bus_gitea.comment", side_effect=self.fake_comment),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        bus_reaper._STOP.clear()
        self.addCleanup(bus_reaper._STOP.clear)

    def fake_update(self, number, *, labels=None, state=None):
        self.updates.append({"number": number, "labels": labels, "state": state})
        return {"number": number, "labels": list(labels or []), "state": state or "open"}

    def fake_comment(self, number, body):
        self.comments.append({"number": number, "body": body})
        return {"id": len(self.comments)}

    def patch_comments(self, comments):
        patcher = patch("tools.bus_reaper.bus_gitea.list_comments", return_value=comments)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fresh_heartbeat_is_noop(self):
        self.patch_comments([claim("2026-05-13T10:00:00Z", 600), hb("2026-05-13T11:55:00Z")])

        bus_reaper.process_issue(issue(), NOW)

        self.assertEqual(self.updates, [])
        self.assertEqual(self.comments, [])

    def test_stale_heartbeat_expires(self):
        self.patch_comments([claim("2026-05-13T10:00:00Z", 600), hb("2026-05-13T11:40:00Z")])

        with patch("tools.bus_reaper._now_iso", return_value="2026-05-13T12:00:00Z"):
            bus_reaper.process_issue(issue(), NOW)

        self.assertEqual(len(self.updates), 1)
        self.assertEqual(
            self.updates[0]["labels"],
            ["phase:1.5-git-bus", "neon:target/win-claude-01", "neon:state/expired"],
        )
        self.assertIn("<!-- neon-expired:v1 by=reaper at=2026-05-13T12:00:00Z -->", self.comments[0]["body"])
        self.assertIn("Worker stopped heartbeating after 1200s", self.comments[0]["body"])

    def test_no_heartbeat_uses_claimed_at(self):
        self.patch_comments([claim("2026-05-13T11:40:00Z", 600)])

        bus_reaper.process_issue(issue(), NOW)

        self.assertEqual(len(self.updates), 1)
        self.assertIn("neon:state/expired", self.updates[0]["labels"])

    def test_no_heartbeat_recent_claim_is_noop(self):
        self.patch_comments([claim("2026-05-13T11:50:00Z", 600)])

        bus_reaper.process_issue(issue(), NOW)

        self.assertEqual(self.updates, [])
        self.assertEqual(self.comments, [])

    def test_missing_claim_comment_skips(self):
        self.patch_comments([hb("2026-05-13T11:00:00Z")])
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            bus_reaper.process_issue(issue(), NOW)

        self.assertEqual(self.updates, [])
        self.assertEqual(self.comments, [])
        self.assertIn("has no neon-claim comment", stderr.getvalue())

    def test_label_swap_preserves_other_labels(self):
        self.patch_comments([claim("2026-05-13T10:00:00Z", 600)])
        labels = ["phase:1.5-git-bus", "neon:target/win-claude-01", "custom", "neon:state/in-progress"]

        bus_reaper.process_issue(issue(labels), NOW)

        self.assertEqual(self.updates[0]["labels"], ["phase:1.5-git-bus", "neon:target/win-claude-01", "custom", "neon:state/expired"])

    def test_expire_comment_format(self):
        with patch("tools.bus_reaper._now_iso", return_value="2026-05-13T12:00:00Z"):
            bus_reaper.expire(issue(), "Worker stopped heartbeating after 901s")

        self.assertRegex(
            self.comments[0]["body"],
            re.compile(
                r"^<!-- neon-expired:v1 by=reaper at=2026-05-13T12:00:00Z -->\n"
                r"Worker stopped heartbeating after 901s\n"
                r"<!-- /neon-expired:v1 -->$"
            ),
        )

    def test_already_expired_label_is_skipped(self):
        labels = ["phase:1.5-git-bus", "neon:target/win-claude-01", "neon:state/expired"]

        bus_reaper.process_issue(issue(labels), NOW)

        self.assertEqual(self.updates, [])
        self.assertEqual(self.comments, [])

    def test_picks_latest_hb_when_multiple(self):
        self.patch_comments(
            [
                claim("2026-05-13T10:00:00Z", 600),
                hb("2026-05-13T11:30:00Z"),
                hb("2026-05-13T11:59:00Z"),
                hb("2026-05-13T11:45:00Z"),
            ]
        )

        bus_reaper.process_issue(issue(), NOW)

        self.assertEqual(self.updates, [])
        self.assertEqual(self.comments, [])

    def test_once_mode_runs_one_scan(self):
        stale = issue()
        with patch("tools.bus_reaper.bus_gitea.list_issues", return_value=[stale]) as list_issues:
            self.patch_comments([claim("2026-05-13T10:00:00Z", 600)])
            bus_reaper.run(poll_interval=999, once=True)

        list_issues.assert_called_once_with(state="open", labels=["phase:1.5-git-bus"])
        self.assertEqual(len(self.updates), 1)


if __name__ == "__main__":
    unittest.main()
