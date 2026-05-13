# Task: Phase 1.5 #4 — bus reaper

Name: codex-developer
Profile: Codex CLI 0.128+ (gpt-5.5, high reasoning, --sandbox workspace-write)
Goal: `tools/bus_reaper.py` — side-car that scans `neon:state/claimed` and `neon:state/in-progress` issues, finds their latest heartbeat (or `claimed_at` if no heartbeat yet), and flips any whose lease has elapsed to `neon:state/expired` with an explanation comment.
Constraints: stdlib only, no real network in tests, idempotent (re-running expiration on already-expired issue is a no-op).
Watches: Gitea issue [#51](http://192.168.1.130:3000/androman/neon-legion/issues/51), design doc `docs/phase-1.5-git-bus.md`, just-merged `tools/bus_worker.py` (claim/heartbeat comment formats live there).
Produces: 2 new files (`tools/bus_reaper.py` ~80 LOC + `tests/test_bus_reaper.py` ~120 LOC).

## Operational backstory

You are running with `workspace-write` sandbox in the neon-legion project. Phase 1.5 #1 (envelope), #2 (Gitea client), #3 (worker) all merged. The reaper is independent of the worker — they run as separate processes. The reaper does not own claims; the worker does. The reaper only **expires** stale ones so the next worker poll can pick them up again.

Tests run on host, not in sandbox. Use `unittest` + `unittest.mock.patch` on `bus_gitea.*` + `datetime` if you need a fixed "now". No real sleep, no real network.

## Working directory

`F:/WorkAI/multi-agent` (already your `--cd`).

## Project context

Read in this order:
1. `AGENTS.md`
2. `docs/phase-1.5-git-bus.md` — design context
3. `tools/bus_worker.py` — claim comment format, heartbeat comment format, state labels
4. `tools/bus_gitea.py` — API surface (you'll need `list_issues`, `comment`, `update_issue`; possibly add `list_comments` if absent — note as deviation)

## Sentinel formats to parse

The worker writes these (already on issues you'll see in production):

```
<!-- neon-claim:v1 host=<host> exec=<exec_id> claimed_at=<iso8601> lease_seconds=<int> -->
<!-- neon-hb:v1 exec=<exec_id> ts=<iso8601> -->
```

Parse with simple regex — keys are space-separated, no equals-sign edge cases (no quoted values, no commas). Example regex for hb:

```python
HB_RE = re.compile(r"<!--\s*neon-hb:v1\s+exec=(\S+)\s+ts=(\S+)\s*-->")
```

The reaper does NOT need the envelope body — just the most-recent claim + heartbeat comments.

## Logic

For each open issue with label `phase:1.5-git-bus` AND (`neon:state/claimed` OR `neon:state/in-progress`):

1. Fetch comments (Gitea: `GET /repos/{repo}/issues/{n}/comments` — add a `bus_gitea.list_comments(number)` helper if absent).
2. Find latest `neon-claim:v1` and remember `claimed_at`, `lease_seconds`.
3. Find latest `neon-hb:v1` `ts` (string-sort ISO 8601 works — same lex order as chronological).
4. Reference time: latest hb ts if present, else `claimed_at`.
5. If `now() - reference > lease_seconds * 1.5` → expire. (The 1.5× headroom prevents flapping when a worker is mid-heartbeat-network-blip.)
6. Expire = `update_issue(number, labels=[..., neon:state/expired])` (preserve `phase:1.5-git-bus`, `neon:target/*`, drop the previous `neon:state/*`) + post `<!-- neon-expired:v1 by=reaper at=<iso8601> -->\nWorker stopped heartbeating after <duration>s\n<!-- /neon-expired:v1 -->` comment.
7. Idempotency: if the issue already has `neon:state/expired`, skip. The list filter already excludes it, but a comment-time race is possible.

If no claim comment at all → log and skip (probably a manually-poked test issue, not the reaper's problem).

## CLI

```
py -3.14 tools/bus_reaper.py [--poll-interval 60] [--once] [--lease-grace-multiplier 1.5]
```

`--once` mode runs a single scan and exits — for cron / supervisor. Default loops every 60 s. Lease grace multiplier exposed so the operator can tune flapping vs. responsiveness.

Mirror the SIGINT/SIGTERM/`_STOP` pattern from `tools/bus_worker.py` exactly (responsive shutdown).

## Deliverables

### 1. `tools/bus_reaper.py`

Public surface:
- `run(poll_interval=60, lease_grace_multiplier=1.5)` — main loop
- `process_issue(issue, now, lease_grace_multiplier=1.5)` — single-issue flow, exposed for tests
- `expire(issue, reason)` — performs the label swap + comment, exposed for tests

### 2. `tests/test_bus_reaper.py`

Unit tests (≥8) using `unittest.mock.patch`. Coverage:

1. `test_fresh_heartbeat_is_noop` — hb ts is recent → no expire call.
2. `test_stale_heartbeat_expires` — hb older than `lease_seconds * 1.5` → expire called once, label swap correct, sentinel comment posted.
3. `test_no_heartbeat_uses_claimed_at` — claimed but no hb yet; claimed_at older than threshold → expire.
4. `test_no_heartbeat_recent_claim_is_noop` — claimed_at within threshold → no expire.
5. `test_missing_claim_comment_skips` — issue has state=claimed but no `neon-claim:v1` comment → log + skip (no API write).
6. `test_label_swap_preserves_other_labels` — `phase:1.5-git-bus`, `neon:target/win-claude-01` survive; only `neon:state/*` changes.
7. `test_expire_comment_format` — comment body matches `<!-- neon-expired:v1 by=reaper at=... -->\n...\n<!-- /neon-expired:v1 -->`.
8. `test_already_expired_label_is_skipped` — issue already has `neon:state/expired` → no API write (idempotency guard).
9. `test_picks_latest_hb_when_multiple` — three hb comments at different times → uses the newest.
10. `test_once_mode_runs_one_scan` — `run(--once)` calls `list_issues` exactly once and exits.

Tests must run in <2 s total.

## If `bus_gitea.list_comments` is missing

If it's not in `tools/bus_gitea.py`, **add it** there (not duplicate in reaper):

```python
def list_comments(number: int) -> list[dict]:
    """GET /repos/{repo}/issues/{number}/comments — paginated."""
```

Plus a unit test in `tests/test_bus_gitea.py` for the new function. Note this as a deviation in the final report.

## Acceptance criteria

- `py -3.14 -c "import sys; sys.path.insert(0, '.'); from tools.bus_reaper import run, process_issue, expire; print('ok')"` prints `ok`.
- `py -3.14 -m unittest tests.test_bus_reaper -v` — all ≥8 tests pass.
- `py -3.14 -m py_compile tools/bus_reaper.py tests/test_bus_reaper.py` exits 0.
- `py -3.14 tools/bus_reaper.py --help` shows usage.
- Stdlib only. No new dependencies.

## Out of scope

- Cleanup of orphaned NAS payload files (separate cron).
- Notification (email/Slack/Telegram) on expiration — defer.
- Resurrecting the issue back to `pending` (the worker can do that or the reaper can — for MVP, just `expired`; operators can manually re-trigger).

## Style / project conventions

- Match shape of `tools/bus_worker.py` (docstring → constants → public → private → `__main__` smoke).
- `from __future__ import annotations`.
- No `Co-Authored-By:`.
- Logging: `print(f"[bus-reaper] ...", file=sys.stderr)`.

## Self-check before "done"

- All ≥8 tests pass.
- `py_compile` clean.
- `bus_gitea` calls are patched in every test.
- ISO 8601 parsed via `datetime.fromisoformat` (stdlib).
- `_STOP` handler installed for SIGINT/SIGTERM.

## Final report

Conform to schema (`files_created`, `summary`, `tested`, `test_results`, `open_questions`, `deviations_from_spec`). If you add `list_comments` to `bus_gitea`, mention it under `files_created` (modified, not new — but list it explicitly).
