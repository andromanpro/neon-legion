# Task: Phase 1.5 #3 — bus worker loop

Name: codex-developer
Profile: Codex CLI 0.128+ (gpt-5.5, high reasoning, --sandbox workspace-write)
Goal: `tools/bus_worker.py` — long-running poller that picks up `neon:state/pending` issues targeted at this host, claims them, dispatches to a handler registry, heartbeats every `lease_seconds/3`, and finalises with done/failed state.
Constraints: stdlib only, atomic file ops, threading.Thread for heartbeat (daemon), SIGINT/SIGTERM clean shutdown.
Watches: Gitea issue [#50](http://localhost:3000/androman/neon-legion/issues/50), design doc `docs/phase-1.5-git-bus.md`, the just-merged `tools/bus_envelope.py` + `tools/bus_gitea.py`.
Produces: 2 new files (`tools/bus_worker.py` ~180 LOC + `tests/test_bus_worker.py` ~200 LOC).

## Operational backstory

You are running with `workspace-write` sandbox in the neon-legion project. Phase 1.5 #1 (envelope) merged at 4bf9fa2 and #2 (Gitea client) at 243f4af. Two foundation modules ready. **Three later issues** (#51 reaper, #52 adapter, plus follow-ups) depend on this worker module.

**Tests run on host**, not in sandbox. Use only stdlib `unittest` + `unittest.mock.patch`. No real network, no real sleep — patch `bus_gitea` functions and `time.sleep`.

The architect made one design adjustment from the doc: **lease tracking via sentinel-wrapped comments**, not dynamic per-host-exec-id labels. Reason: dynamic label creation is API-noisy and Gitea label-filter doesn't support wildcards anyway, so the reaper would have to fetch issues + parse comments regardless. Single source of truth = the claim comment.

## Working directory

`F:/WorkAI/multi-agent` (already your `--cd`).

## Project context

Read in this order:
1. `AGENTS.md` (conventions: stdlib-only, no `Co-Authored-By:`, privacy by default)
2. `docs/phase-1.5-git-bus.md` (overall bus design)
3. `tools/bus_envelope.py` (envelope parse/serialize, style reference)
4. `tools/bus_gitea.py` (Gitea API surface)
5. `tracker/openclaw-watch.py` (sister polling-loop with SIGINT/SIGTERM handling — copy that exit pattern)

## State machine

State labels (created in advance by the architect — assume they exist):

```
neon:state/pending      ← issue creator sets this
neon:state/claimed      ← worker acquired the lease
neon:state/in-progress  ← worker started running the handler
neon:state/done         ← handler succeeded, issue closed
neon:state/failed       ← handler raised, issue closed
neon:state/expired      ← (reaper #51 sets this; worker does not)
```

Transitions (each via `bus_gitea.update_issue(number, labels=[...])` — replaces the full label set, so keep the `phase:1.5-git-bus`, `neon:target/<host>`, and `neon:state/*` labels):

```
pending → claimed       (atomic: try update; if Gitea returns no-op or label conflict, skip the issue and continue)
claimed → in-progress   (after payload download + sha verify)
in-progress → done      (handler returned cleanly)
in-progress → failed    (handler raised)
```

## Sentinel-wrapped artefacts

The worker writes three kinds of sentinel comments on the issue:

```
<!-- neon-claim:v1 host=<host> exec=<exec_id> claimed_at=<iso8601> lease_seconds=<int> -->
<!-- neon-hb:v1 exec=<exec_id> ts=<iso8601> -->
<!-- neon-result:v1 exec=<exec_id> status=<done|failed> -->
<canonical JSON result>
<!-- /neon-result:v1 -->
```

`exec_id` is `f"{host}-{int(time.time())}-{secrets.token_hex(3)}"` (host + epoch + 6 hex chars). Recorded once per claim, reused for hb + result.

## Payload handling

The task envelope's `payload_ref` is a string URI. For MVP:

- `smb://...` and `file://...` → read from local FS (use `pathlib.Path` with `urllib.parse.urlparse(...).path`; SMB share is auto-mounted at OS level so it looks like a file path on Windows).
- `http://...` / `https://...` → skip with a `failed` result and reason `unsupported_payload_scheme` (these come later — kept out of MVP).
- Verify sha256 of payload bytes against envelope's `payload_sha256`. Mismatch → fail.

## Handler registry

```python
HANDLERS: dict[str, Callable[[dict, dict], dict]] = {
    "echo": echo_handler,  # built-in stub for smoke testing
}

def echo_handler(envelope: dict, payload: dict) -> dict:
    """Returns the payload unchanged. Smoke handler for the worker itself."""
    return {"echo": payload}

def register_handler(kind: str, handler) -> None:
    HANDLERS[kind] = handler
```

Issue #52 (adapter) will add `bus.list`, `bus.read`, etc. via `register_handler`.

## Heartbeat thread

While the handler runs, a daemon thread posts `neon-hb:v1` comments every `lease_seconds // 3` seconds. The thread exits when the main thread sets a `done` event (`threading.Event`). Use `event.wait(timeout=interval)` instead of `time.sleep` so shutdown is responsive.

## Loop

```python
def run(host: str, poll_interval: int = 30) -> None:
    while not _STOP:
        try:
            issues = bus_gitea.list_issues(
                state="open",
                labels=["phase:1.5-git-bus", "neon:state/pending", f"neon:target/{host}"],
            )
        except BusGiteaError as exc:
            log(f"poll failed: {exc}", level="error")
            issues = []

        for issue in sorted(issues, key=lambda i: i["number"]):
            if _STOP:
                break
            try:
                process_issue(issue, host)
            except Exception as exc:
                log(f"unhandled exception on #{issue['number']}: {exc}", level="error")

        # responsive sleep — wakes early on SIGINT
        _wait_or_stop(poll_interval)
```

`_wait_or_stop` polls `_STOP` every second, same as `tracker/openclaw-watch.py`.

## CLI

```
py -3.14 tools/bus_worker.py --host <name> [--poll-interval 30]
```

Required `--host`. Default poll interval 30 s.

## Deliverables

### 1. `tools/bus_worker.py`

Public surface:
- `run(host, poll_interval=30)` — main loop
- `process_issue(issue, host)` — single-issue flow (claim → run → finalize). Exposed for tests.
- `HANDLERS: dict[str, Callable]`
- `register_handler(kind, handler)`
- `echo_handler(envelope, payload) -> dict`
- `_STOP: threading.Event` — module-level; set by SIGINT/SIGTERM

### 2. `tests/test_bus_worker.py`

Unit tests (≥12) using `unittest.mock.patch` on `bus_gitea.*` and `time.sleep`. Patch `_payload_read` to return a fixed dict (don't touch the FS). Coverage:

1. `test_process_issue_happy_path` — pending → claimed → in-progress → done; assert label transitions, claim comment posted, result comment posted, issue closed.
2. `test_process_issue_payload_sha_mismatch` — wrong sha256 in envelope → state goes to `failed`, reason in result comment.
3. `test_process_issue_unsupported_scheme` — `http://...` payload_ref → fails with `unsupported_payload_scheme`.
4. `test_process_issue_handler_raises` — handler raises ValueError → state `failed`, exception type in result.
5. `test_process_issue_unknown_kind` — envelope `kind` not in registry → fails with `unknown_kind`.
6. `test_process_issue_malformed_envelope` — issue body has no sentinel → log + skip (no state change).
7. `test_process_issue_skips_already_claimed` — label set lacks `neon:state/pending` (someone else claimed first) → no-op.
8. `test_register_handler_adds_to_registry` — register a custom handler, dispatch picks it up.
9. `test_echo_handler_returns_wrapped_payload`.
10. `test_heartbeat_thread_posts_comments_then_stops` — start hb with `lease_seconds=3` (interval=1), let it tick twice, then `done.set()`, assert ≥2 hb comments and thread joins within 2 s.
11. `test_exec_id_format` — assert pattern `<host>-<epoch>-<6 hex>`.
12. `test_wait_or_stop_returns_early_on_stop` — patch `_STOP.is_set` to return True mid-wait; method returns within ≤1 s.

Tests must run in <5 s total.

## Acceptance criteria

- `py -3.14 -c "import sys; sys.path.insert(0, '.'); from tools.bus_worker import run, process_issue, register_handler, HANDLERS, echo_handler; print('ok')"` prints `ok`.
- `py -3.14 -m unittest tests.test_bus_worker -v` — all ≥12 tests pass.
- `py -3.14 -m py_compile tools/bus_worker.py tests/test_bus_worker.py` exits 0.
- `py -3.14 tools/bus_worker.py --help` shows usage.
- Stdlib only. No new dependencies.

## Out of scope

- Reaper (#51, separate module).
- Real handlers (#52 adapter wires those in).
- HTTP payload fetcher (deferred — see scheme handling above).
- Webhook listener — polling is the MVP transport.

## Style / project conventions

- Match the module shape of `tools/bus_envelope.py` and `tools/bus_gitea.py`: docstring → constants → public functions → private helpers → `__main__` smoke.
- `from __future__ import annotations` at the top.
- No `Co-Authored-By:` in commits.
- SIGINT/SIGTERM handling: mirror `tracker/openclaw-watch.py` exactly (module-level `_STOP`, signal handlers set it, loops poll it).
- Logging: `print(f"[bus-worker] ...", file=sys.stderr)` — same style as `openclaw-watch.py`. No `logging` module.

## Self-check before reporting "done"

- All ≥12 tests pass.
- `py_compile` clean.
- `bus_gitea` is patched in every test — no real network call from a test ever.
- `time.sleep` is patched in heartbeat test — test runs in <2 s.
- SIGINT/SIGTERM signals registered.
- The `__main__` smoke at the bottom is mocked, not live.

## Final report

Conform to schema (`files_created`, `summary`, `tested`, `test_results`, `open_questions`, `deviations_from_spec`).
