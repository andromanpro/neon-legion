#!/usr/bin/env python
"""Thin Gitea issue API client for the Phase 1.5 Git bus.

This module performs outbound HTTP calls to the configured local Gitea server.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request


TOKEN_PATH = os.path.expanduser(os.environ.get("GITEA_TOKEN_PATH", "~/.gitea-token"))
# No hardcoded default — was a LAN-IP disclosure (secret-scan 2026-05-16).
# Must be set explicitly via env; _request() guards against empty.
BASE_URL = os.environ.get("GITEA_BASE_URL", "").rstrip("/")
REPO = os.environ.get("GITEA_REPO", "androman/neon-legion").strip("/")
API_ROOT = f"{BASE_URL}/api/v1"


class BusGiteaError(Exception):
    """4xx / 5xx error from Gitea. Carries response body."""

    def __init__(self, status: int, body: str):
        super().__init__(f"Gitea API error {status}: {body}")
        self.status = status
        self.body = body


def create_issue(title: str, body: str, labels: list[int], milestone: int | None = None) -> dict:
    """POST /repos/{repo}/issues - returns issue dict."""
    payload = {"title": title, "body": body, "labels": labels}
    if milestone is not None:
        payload["milestone"] = milestone
    return _request("POST", f"/repos/{_repo_path()}/issues", payload)


def update_issue(number: int, *, labels: list[int | str] | None = None, state: str | None = None) -> dict:
    """PATCH /repos/{repo}/issues/{number} - return updated issue."""
    payload = {}
    if labels is not None:
        payload["labels"] = labels
    if state is not None:
        payload["state"] = state
    if not payload:
        raise ValueError("labels or state must be provided")
    return _request("PATCH", f"/repos/{_repo_path()}/issues/{number}", payload)


def comment(number: int, body: str) -> dict:
    """POST /repos/{repo}/issues/{number}/comments - returns comment dict."""
    return _request("POST", f"/repos/{_repo_path()}/issues/{number}/comments", {"body": body})


def list_comments(number: int, page: int = 1) -> list[dict]:
    """GET /repos/{repo}/issues/{number}/comments across all pages."""
    comments = []
    current_page = page
    while True:
        query = {"page": current_page}
        path = f"/repos/{_repo_path()}/issues/{number}/comments?{urllib.parse.urlencode(query)}"
        batch = _request("GET", path)
        if not batch:
            return comments
        comments.extend(batch)
        current_page += 1


def list_issues(state: str = "open", labels: list[str] | None = None, page: int = 1) -> list[dict]:
    """GET /repos/{repo}/issues across all pages."""
    issues = []
    current_page = page
    while True:
        query = {"state": state, "page": current_page}
        if labels:
            query["labels"] = ",".join(labels)
        path = f"/repos/{_repo_path()}/issues?{urllib.parse.urlencode(query, safe=':,')}"
        batch = _request("GET", path)
        if not batch:
            return issues
        issues.extend(batch)
        current_page += 1


def get_issue(number: int) -> dict:
    """GET /repos/{repo}/issues/{number}."""
    return _request("GET", f"/repos/{_repo_path()}/issues/{number}")


def _request(method: str, path: str, body=None, *, retry: bool = True):
    if not BASE_URL:
        raise RuntimeError(
            "GITEA_BASE_URL is not set — export it (e.g. http://<host>:3000) "
            "before using bus_gitea. No default is shipped to avoid leaking "
            "internal host topology."
        )
    data = None
    headers = {"Accept": "application/json", "Authorization": f"token {_read_token()}"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(f"{API_ROOT}{path}", data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = getattr(response, "status", response.getcode())
            text = response.read().decode("utf-8")
            return _handle_response(status, text, getattr(response, "headers", {}), method, path, body, retry)
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read().decode("utf-8", errors="replace")
        finally:
            exc.close()
        return _handle_response(exc.code, text, exc.headers, method, path, body, retry)


def _handle_response(status: int, text: str, headers, method: str, path: str, body, retry: bool):
    if status == 204:
        return {}
    if status in (200, 201):
        return json.loads(text) if text else {}
    if status == 429 and retry:
        time.sleep(_retry_after_reset(headers))
        return _request(method, path, body, retry=False)
    if status >= 500 and retry:
        time.sleep(2)
        return _request(method, path, body, retry=False)
    raise BusGiteaError(status, text)


def _retry_after_reset(headers) -> float:
    try:
        reset_at = int(headers.get("X-RateLimit-Reset", "0"))
    except (TypeError, ValueError):
        return 60.0
    return min(max(reset_at - time.time(), 0.0), 60.0)


def _read_token() -> str:
    with open(TOKEN_PATH, "r", encoding="utf-8") as token_file:
        return token_file.readline().strip()


def _repo_path() -> str:
    return urllib.parse.quote(REPO, safe="/")


if __name__ == "__main__":
    from unittest.mock import patch

    class _SmokeResponse:
        status = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def getcode(self):
            return self.status

        def read(self):
            return b'{"number": 1}'

    with patch(__name__ + "._read_token", return_value="smoke"), \
         patch(__name__ + ".BASE_URL", "http://gitea.smoke:3000"), \
         patch(__name__ + ".API_ROOT", "http://gitea.smoke:3000/api/v1"), \
         patch("urllib.request.urlopen", return_value=_SmokeResponse()):
        assert get_issue(1) == {"number": 1}
    print("ok")
