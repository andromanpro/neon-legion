# Task: P2 wow #6 — Git-Diff-Aware Session Cost

Name: codex-developer
Profile: Codex CLI 0.128+ (gpt-5.5, high reasoning, --sandbox workspace-write)
Goal: `tools/git_diff_cost.py` — for each Claude Code session, walk git log within session's time window, get diff stats per commit, attribute session cost proportionally. Emit `diff_cost.json` with per-session breakdown + a "expensive lines" top-N list (sessions where cost_per_line is in the top decile).
Constraints: stdlib + `subprocess` to call `git` only (no GitPython etc), graceful on sessions with no commits (separate `no_diff` bucket), graceful on non-git repos or missing git binary, multi-repo support OPTIONAL (defer to follow-up).
Watches: Gitea issue [#42](http://localhost:3000/androman/neon-legion/issues/42), `tracker/summary.py` (event reader, session aggregation), `tools/cost_regression.py` (sibling shape — atomic write, CLI), `tools/reputation_ledger.py` (sibling — config-driven outputs).
Produces: 1 new file (`tools/git_diff_cost.py` ~180 LOC), 1 new file (`tests/test_git_diff_cost.py` ~180 LOC), modified `config.example.toml` (add `[git_diff_cost]` section), wire-up in `wp-dev/tools/deploy-snapshot.sh` (architect-wired manually if Codex can't reach).

## Operational backstory

P2 wow #6 — last of the recommended three (#39, #41, #42). Author: DeepSeek. Emotional impact: turns abstract cost numbers into "$3 went to renaming a variable" stories.

For each session in `tracker/claude-events.jsonl` (and other provider streams):
1. Determine session window: min(ts) to max(ts) of events with this session_id.
2. Get session total cost (sum of `cost_estimate_usd` for the session's events).
3. Walk `git log --since=<start> --until=<end> --format=...` in the configured repo path. Get commit hashes.
4. For each commit, get diff stats via `git show --stat --format=` (lines added + removed, files touched).
5. Compute `total_lines_changed` = sum across commits.
6. `cost_per_line` = `cost_usd / max(total_lines_changed, 1)` (avoid divide by zero).
7. Sessions with zero commits → `no_diff = true`, `cost_per_line` = null.

Top decile across sessions WITH commits: sessions whose `cost_per_line` is ≥ 90th percentile. These are the "expensive lines" stories.

Tests run on host. Use `subprocess.run` with `cwd=` and `text=True, check=True`.

## Working directory

`F:/WorkAI/multi-agent` (already your `--cd`).

## Project context

Read in order:
1. `tracker/summary.py` — `read_events(start, end)`, `as_int/as_float`, session_id field shape.
2. `tools/cost_regression.py` — sibling for shape (config, atomic write, CLI).
3. `tools/reputation_ledger.py` — sibling.
4. The current working dir is itself a git repo — useful for smoke test.

## `diff_cost.json` schema

```json
{
  "schema_version": 1,
  "generated_at": "2026-05-14T00:30:00+03:00",
  "config": {
    "repo_path": "F:/WorkAI/multi-agent",
    "lookback_days": 30,
    "top_decile_threshold": 0.9
  },
  "sessions": [
    {
      "session_id": "abc12345-...",
      "session_short": "abc12345",
      "start_ts": "2026-05-13T20:00:00+03:00",
      "end_ts": "2026-05-13T21:30:00+03:00",
      "cost_usd": 12.34,
      "commits": [
        {"hash": "abc1234", "insertions": 50, "deletions": 5, "files_changed": 3, "subject": "phase/X.Y: ..."}
      ],
      "total_lines_changed": 55,
      "cost_per_line_usd": 0.224,
      "no_diff": false
    },
    {
      "session_id": "def67890-...",
      "session_short": "def67890",
      "cost_usd": 3.0,
      "no_diff": true
    }
  ],
  "expensive_sessions": [<top 5 by cost_per_line>],
  "summary": {
    "total_sessions_scanned": 47,
    "sessions_with_commits": 32,
    "no_diff_count": 15,
    "expensive_lines_threshold_usd_per_line": 0.5,
    "expensive_sessions_count": 5
  }
}
```

Format the session payload differently for `no_diff` (omit commits, total_lines_changed, cost_per_line_usd).

## API

```python
def build_diff_cost(
    events: list[dict],
    repo_path: Path,
    *,
    lookback_days: int = 30,
    top_decile_threshold: float = 0.9,
    now: datetime | None = None,
) -> dict:
    """Return diff_cost.json payload."""

def write_diff_cost(payload: dict, output_path: Path) -> None:
    """Atomic write."""

def main() -> int:
    """CLI."""
```

CLI:
```
py -3.14 tools/git_diff_cost.py [--repo PATH] [--output PATH] [--lookback-days 30] [--top-decile 0.9]
```

Defaults from `config.toml [git_diff_cost]` section.

## Git invocation

Per-session, run `git -C <repo_path> log --since=<ISO> --until=<ISO> --format=%H` to get commit hashes (cheap). Then for each hash, `git -C <repo_path> show --stat --format= <hash>` to get diff stats. Parse `--stat` output: last line typically `N files changed, X insertions(+), Y deletions(-)`.

Use `subprocess.run(..., text=True, check=False)` (not `check=True` — git failure shouldn't crash the whole tool). On non-zero exit code, log + treat session as `no_diff`.

Edge cases:
- Empty repo / no commits at all: every session has `no_diff = true`.
- Repo not a git repo (no `.git`): emit empty sessions list, log warning.
- Commits authored by someone else but timestamp within session window: include them. The detector is per-user-session, not per-author. (Author filtering optional, default off.)
- Sessions across midnight / DST: use ISO timestamps with timezone, git handles them correctly.

## Tests

≥6 unit tests using a synthetic git repo (via `tempfile.TemporaryDirectory` + `subprocess.run("git init", ...)`):

1. `test_no_events_returns_empty_sessions` — no events → `sessions: []`, `summary.total_sessions_scanned == 0`.
2. `test_session_with_one_commit_in_window` — synthetic events for a session, synthetic git commit within window → session has 1 commit, correct total_lines_changed.
3. `test_session_with_no_commits_in_window_is_no_diff_bucket` — events but no commits → session.no_diff == True.
4. `test_session_cost_per_line_calculation` — known cost + known total_lines → ratio matches.
5. `test_expensive_sessions_in_top_decile` — fixture with 10 sessions, top 1 has very high cost_per_line → it's in expensive_sessions list.
6. `test_non_git_repo_path_returns_empty_with_warning` — repo_path that's not a git repo → graceful empty result.

For test setup, use `subprocess.run(["git", "init", ...], ...)` and `git commit -m "..."` to build the fixture repo. Set `GIT_AUTHOR_NAME`/`GIT_AUTHOR_EMAIL` env vars for reproducibility.

If subprocess `git` binary is not available on the test host, mark tests with `unittest.skipUnless` and skip gracefully.

## Acceptance criteria

- `py -3.14 -c "from tools.git_diff_cost import build_diff_cost, write_diff_cost; print('ok')"` prints `ok`.
- `py -3.14 -m unittest tests.test_git_diff_cost -v` — ≥6 tests pass (or `skipped` if git not available).
- `py -3.14 -m py_compile tools/git_diff_cost.py tests/test_git_diff_cost.py` exits 0.
- `py -3.14 tools/git_diff_cost.py --help` shows config flags.
- Real-data smoke against `F:/WorkAI/multi-agent` repo + production tracker events — produces a valid JSON with at least some sessions (the architect will check).
- Stdlib + git subprocess only. No GitPython or PyGit.
- Full repo suite stays green.

## Out of scope

- Multi-repo support (single `repo_path` for now; can extend to list later).
- Per-author filtering (include all commits in window).
- Per-file cost attribution.
- Markdown report generation alongside JSON.
- Dashboard widget JS/PHP (separate task in theme repo).

## Style / project conventions

- Match shape of `tools/cost_regression.py`.
- `from __future__ import annotations`.
- No `Co-Authored-By:`.
- Atomic write: tmp + os.replace.
- Logging: `print(f"[git-diff-cost] ...", file=sys.stderr)`.

## Self-check before "done"

- Tests pass on host (or skip gracefully if git not in PATH).
- `--help` shows flags.
- Real-data smoke produces valid JSON.
- Sessions with zero commits go to `no_diff` bucket distinguishably.
- Adding `tracker/diff_cost.json` to `.gitignore` (or wherever the default output goes).

## Final report

Conform to schema. Report smoke result: total_sessions_scanned + no_diff_count + expensive_sessions_count + 1-2 lines from expensive_sessions list.
