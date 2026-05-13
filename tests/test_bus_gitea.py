import io
import json
import sys
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import bus_gitea
from tools.bus_gitea import BusGiteaError, comment, create_issue, get_issue, list_comments, list_issues, update_issue


class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status = status
        self._body = json.dumps(body, ensure_ascii=False).encode("utf-8") if not isinstance(body, bytes) else body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self):
        return self.status

    def read(self):
        return self._body


def http_error(status, body, headers=None):
    data = body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
    return urllib.error.HTTPError(
        url="http://gitea.local/api/v1/repos/androman/neon-legion/issues",
        code=status,
        msg="error",
        hdrs=headers or {},
        fp=io.BytesIO(data),
    )


class BusGiteaTests(unittest.TestCase):
    def setUp(self):
        self.token_patch = patch("tools.bus_gitea._read_token", return_value="test-token")
        self.token_patch.start()
        self.addCleanup(self.token_patch.stop)

    def request_json(self, request):
        return json.loads(request.data.decode("utf-8"))

    def test_create_issue_posts_correct_body(self):
        seen = {}

        def fake_urlopen(request, timeout):
            seen["request"] = request
            seen["timeout"] = timeout
            return FakeResponse(201, {"number": 49})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = create_issue("Task", "Body", [1, 2], milestone=3)

        request = seen["request"]
        self.assertEqual(result, {"number": 49})
        self.assertEqual(request.full_url, bus_gitea.API_ROOT + "/repos/androman/neon-legion/issues")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(self.request_json(request), {"title": "Task", "body": "Body", "labels": [1, 2], "milestone": 3})
        self.assertEqual(seen["timeout"], 30)

    def test_update_issue_labels_only(self):
        with patch("urllib.request.urlopen", return_value=FakeResponse(200, {"number": 49})) as urlopen:
            update_issue(49, labels=[4, 5])

        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "PATCH")
        self.assertEqual(request.full_url, bus_gitea.API_ROOT + "/repos/androman/neon-legion/issues/49")
        self.assertEqual(self.request_json(request), {"labels": [4, 5]})

    def test_update_issue_state_only(self):
        with patch("urllib.request.urlopen", return_value=FakeResponse(200, {"state": "closed"})) as urlopen:
            update_issue(49, state="closed")

        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "PATCH")
        self.assertEqual(self.request_json(request), {"state": "closed"})

    def test_update_issue_both(self):
        with patch("urllib.request.urlopen", return_value=FakeResponse(200, {"state": "closed"})) as urlopen:
            update_issue(49, labels=[6], state="closed")

        request = urlopen.call_args.args[0]
        self.assertEqual(self.request_json(request), {"labels": [6], "state": "closed"})

    def test_comment_posts_body(self):
        with patch("urllib.request.urlopen", return_value=FakeResponse(201, {"id": 7})) as urlopen:
            result = comment(49, "running")

        request = urlopen.call_args.args[0]
        self.assertEqual(result, {"id": 7})
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, bus_gitea.API_ROOT + "/repos/androman/neon-legion/issues/49/comments")
        self.assertEqual(self.request_json(request), {"body": "running"})

    def test_list_comments_paginates(self):
        pages = [
            FakeResponse(200, [{"id": 1}]),
            FakeResponse(200, [{"id": 2}]),
            FakeResponse(200, []),
        ]

        with patch("urllib.request.urlopen", side_effect=pages) as urlopen:
            result = list_comments(49)

        self.assertEqual(result, [{"id": 1}, {"id": 2}])
        urls = [call.args[0].full_url for call in urlopen.call_args_list]
        self.assertEqual(urls[0], bus_gitea.API_ROOT + "/repos/androman/neon-legion/issues/49/comments?page=1")
        self.assertEqual(urls[1], bus_gitea.API_ROOT + "/repos/androman/neon-legion/issues/49/comments?page=2")
        self.assertEqual(urls[2], bus_gitea.API_ROOT + "/repos/androman/neon-legion/issues/49/comments?page=3")

    def test_list_issues_single_page(self):
        with patch("urllib.request.urlopen", side_effect=[FakeResponse(200, [{"number": 1}]), FakeResponse(200, [])]):
            self.assertEqual(list_issues(), [{"number": 1}])

    def test_list_issues_paginates(self):
        pages = [
            FakeResponse(200, [{"number": number} for number in range(50)]),
            FakeResponse(200, [{"number": 50}, {"number": 51}, {"number": 52}]),
            FakeResponse(200, []),
        ]

        with patch("urllib.request.urlopen", side_effect=pages) as urlopen:
            result = list_issues()

        self.assertEqual(len(result), 53)
        self.assertEqual(result[-1], {"number": 52})
        urls = [call.args[0].full_url for call in urlopen.call_args_list]
        self.assertIn("page=1", urls[0])
        self.assertIn("page=2", urls[1])
        self.assertIn("page=3", urls[2])

    def test_list_issues_label_filter(self):
        with patch("urllib.request.urlopen", return_value=FakeResponse(200, [])) as urlopen:
            list_issues(labels=["phase:1.5-git-bus"])

        request = urlopen.call_args.args[0]
        self.assertIn("labels=phase:1.5-git-bus", request.full_url)

    def test_get_issue_returns_dict(self):
        with patch("urllib.request.urlopen", return_value=FakeResponse(200, {"number": 49})):
            self.assertEqual(get_issue(49), {"number": 49})

    def test_4xx_raises_BusGiteaError(self):
        with patch("urllib.request.urlopen", side_effect=http_error(404, "missing")):
            with self.assertRaises(BusGiteaError) as raised:
                get_issue(404)

        self.assertEqual(raised.exception.status, 404)
        self.assertIn("missing", raised.exception.body)

    def test_429_retries_after_reset(self):
        reset_at = int(time.time()) + 10
        responses = [
            http_error(429, "limited", {"X-RateLimit-Reset": str(reset_at)}),
            FakeResponse(200, {"number": 49}),
        ]

        with patch("urllib.request.urlopen", side_effect=responses) as urlopen, patch("time.sleep") as sleep:
            result = get_issue(49)

        self.assertEqual(result, {"number": 49})
        self.assertEqual(urlopen.call_count, 2)
        self.assertGreaterEqual(sleep.call_args.args[0], 0)
        self.assertLessEqual(sleep.call_args.args[0], 60)

    def test_5xx_retries_once(self):
        responses = [http_error(500, "boom"), FakeResponse(200, {"number": 49})]

        with patch("urllib.request.urlopen", side_effect=responses) as urlopen, patch("time.sleep") as sleep:
            result = get_issue(49)

        self.assertEqual(result, {"number": 49})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(2)


if __name__ == "__main__":
    unittest.main()
