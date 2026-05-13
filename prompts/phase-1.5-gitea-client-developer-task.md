# Task: Phase 1.5 #2 — Gitea client wrapper

Name: codex-developer
Profile: Codex CLI 0.128+ (gpt-5.5, high reasoning, --sandbox workspace-write)
Goal: Thin `tools/bus_gitea.py` exposing the Gitea API surface the bus needs (create_issue, update_issue, comment, list_issues, get_issue) with consistent error handling and rate-limit awareness. Stdlib only.
Constraints: stdlib only (`urllib.request`), UTF-8 everywhere, atomic write where applicable, no third-party deps.
Watches: Gitea issue [#49](http://localhost:3000/androman/neon-legion/issues/49), design doc `docs/phase-1.5-git-bus.md`, the just-merged `tools/bus_envelope.py` (style reference, not import).
Produces: 2 new files (`tools/bus_gitea.py` ~100 LOC + `tests/test_bus_gitea.py` ~150 LOC).

## Operational backstory

You are running with `workspace-write` sandbox in the neon-legion project. Phase 1.5 #1 (envelope library) merged at f323b70. #2 (this task) is the second foundation module — three later issues (#50 worker, #51 reaper, #52 adapter) all depend on it.

**Tests run on host**, not in sandbox. Codex does `py_compile` + table-driven assertions inside `if __name__ == "__main__"` for smoke. Unit tests use only stdlib `unittest` + `unittest.mock.patch` — no live network calls inside tests.

**Stderr noise**: PowerShell wrapping on Windows may emit warnings for here-docs / multiline strings. Use Python files for any tooling you need to write (the `tools/` dir is writable). The just-merged `tools/bus_envelope.py` is a good style reference for module shape.

## Working directory

`F:/WorkAI/multi-agent` (already your `--cd`).

## Project context

Read:
- `AGENTS.md` (project conventions, no `Co-Authored-By:`, stdlib-only)
- `docs/phase-1.5-git-bus.md` (full bus design)
- `tools/bus_envelope.py` (style reference — same module shape expected)

## API surface

Module `tools/bus_gitea.py` with these public functions, all stdlib (`urllib.request` + `json` + `os`):

```python
class BusGiteaError(Exception):
    """4xx / 5xx error from Gitea. Carries response body."""
    def __init__(self, status: int, body: str): ...

def create_issue(title: str, body: str, labels: list[int], milestone: int | None = None) -> dict:
    """POST /repos/{repo}/issues — returns issue dict."""

def update_issue(number: int, *, labels: list[int] | None = None, state: str | None = None) -> dict:
    """PATCH /repos/{repo}/issues/{number} — return updated issue.
    Either or both of labels (replaces full set) and state ('open'/'closed') can be passed."""

def comment(number: int, body: str) -> dict:
    """POST /repos/{repo}/issues/{number}/comments — returns comment dict."""

def list_issues(state: str = "open", labels: list[str] | None = None, page: int = 1) -> list[dict]:
    """GET /repos/{repo}/issues — paginated. labels = label NAMES (not ids) per Gitea API.
    Returns flat list across pages (auto-paginates until empty)."""

def get_issue(number: int) -> dict:
    """GET /repos/{repo}/issues/{number}."""
```

## Configuration

All calls read these at module load:

| Env var | Default | Used for |
|---|---|---|
| `GITEA_TOKEN_PATH` | `~/.gitea-token` | Read API token from this file (single line) |
| `GITEA_BASE_URL` | `http://localhost:3000` | API base URL |
| `GITEA_REPO` | `androman/neon-legion` | `{owner}/{repo}` slug |

The token is read from the file path (not directly from env) so the same module works for both local dev and any future supervisor that reuses the env conventions of `tools/config.py`.

## Rate-limit / backoff

Gitea returns `429 Too Many Requests` with `X-RateLimit-Remaining: 0` and `X-RateLimit-Reset: <unix-ts>` headers. The client should:

- On 429: sleep until `X-RateLimit-Reset` (cap at 60 s), retry ONCE.
- On 5xx: retry ONCE after 2 s.
- On 4xx (other than 429): raise `BusGiteaError(status, body)`.
- On 200/201/204: return parsed JSON (or `{}` for 204).

## Deliverables

### 1. `tools/bus_gitea.py`

Public API as above. Internal helper `_request(method, path, body=None, *, retry=True)` does the actual HTTP call.

### 2. `tests/test_bus_gitea.py`

Unit tests using `unittest` + `unittest.mock.patch('urllib.request.urlopen', ...)`. Coverage (≥10 tests):

1. `test_create_issue_posts_correct_body` — assert URL, method, body shape, returns parsed dict.
2. `test_update_issue_labels_only` — PATCH with `{"labels": [...]}` when only labels passed.
3. `test_update_issue_state_only` — PATCH with `{"state": "closed"}` when only state passed.
4. `test_update_issue_both` — PATCH with combined dict when both passed.
5. `test_comment_posts_body` — POST comment endpoint, body in payload.
6. `test_list_issues_single_page` — one page of results, returns list.
7. `test_list_issues_paginates` — first page returns 50 items, second returns 3, third returns 0 → returns 53 combined.
8. `test_list_issues_label_filter` — assert query string contains `labels=phase:1.5-git-bus`.
9. `test_get_issue_returns_dict`.
10. `test_4xx_raises_BusGiteaError` — 404 raises with body included.
11. `test_429_retries_after_reset` — first call 429 with reset header, second succeeds. Use a fake `time.sleep` to avoid real wait.
12. `test_5xx_retries_once` — 500 then 200 → returns parsed dict.

Tests must run in <1 s total (no real network, no real sleep).

## Acceptance criteria

- `py -3.14 -c "import sys; sys.path.insert(0, '.'); from tools.bus_gitea import create_issue, update_issue, comment, list_issues, get_issue, BusGiteaError; print('ok')"` prints `ok`.
- `py -3.14 -m unittest tests.test_bus_gitea -v` passes ≥10 tests.
- `py -3.14 -m py_compile tools/bus_gitea.py tests/test_bus_gitea.py` exits 0.
- Stdlib only. No new dependencies.

## Out of scope

- Webhook receiver (issue #50 worker owns the polling).
- Caching / read-model (issue #53).
- GitHub adapter (deferred to next milestone).
- Async / threaded HTTP. Synchronous `urllib.request` is the right call here.

## Style / project conventions

- Per `AGENTS.md`: stdlib only, no `Co-Authored-By:` trailers, privacy by default (no token logging).
- Match the module shape of the just-merged `tools/bus_envelope.py`:
  - Module docstring at top.
  - Public API at the top of the file, helpers below.
  - `from __future__ import annotations` if you use `X | None` style hints.
  - `if __name__ == "__main__":` block with smoke assertions (use mocked urlopen here too — no real network).

## Test plan / self-check

1. Read `AGENTS.md`, `docs/phase-1.5-git-bus.md`, `tools/bus_envelope.py`.
2. Implement `tools/bus_gitea.py`.
3. Implement `tests/test_bus_gitea.py` (≥10 unit tests as above).
4. `py -3.14 -m py_compile tools/bus_gitea.py tests/test_bus_gitea.py`.
5. `py -3.14 -m unittest tests.test_bus_gitea -v` — all pass.
6. `py -3.14 -c "from tools.bus_gitea import *"` import smoke.

## Final report

Conform to schema (`files_created`, `summary`, `tested`, `test_results`, `open_questions`, `deviations_from_spec`). State explicitly whether tests run inside sandbox succeeded; architect will re-run on host.
