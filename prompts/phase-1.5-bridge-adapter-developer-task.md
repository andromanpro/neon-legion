# Task: Phase 1.5 #5 — bridge adapter (port openclaw-codex-bridge to bus)

Name: codex-developer
Profile: Codex CLI 0.128+ (gpt-5.5, high reasoning, --sandbox workspace-write)
Goal: Wire the existing `tools/openclaw-codex-bridge.py` action handlers (`action_list`, `action_read`, `action_rg`, `action_handoff_to_codex`, `action_codex_exec`) into the Phase 1.5 bus worker via a thin adapter. The action *handlers* stay byte-identical; only the transport changes from SMB inbox/outbox files to Gitea Issues.
Constraints: stdlib only, **do not** modify any `action_*` function bodies, preserve file-watch mode as the default (`--bus` is opt-in).
Watches: Gitea issue [#52](http://localhost:3000/androman/neon-legion/issues/52), design doc `docs/phase-1.5-git-bus.md`, just-merged `tools/bus_worker.py` (handler contract), `tools/openclaw-codex-bridge.py` (action sources).
Produces: 1 new file (`tools/bus_adapter_openclaw.py` ~80 LOC + tests `tests/test_bus_adapter_openclaw.py` ~100 LOC), 2 modified files (`tools/openclaw-codex-bridge.py` — add `--bus` flag wiring; `tools/run-openclaw-codex-bridge.cmd` — switch to `--bus`).

## Operational backstory

You are running with `workspace-write` sandbox in the neon-legion project. Phase 1.5 #1–#4 merged. This adapter is the last piece of the bus replacement: once it lands, the SMB inbox/outbox watcher and the bus-driven worker can run side by side, and operators can flip via the `--bus` flag.

**Crucially: do not change any `action_*` function body in `openclaw-codex-bridge.py`.** Their semantics (sandbox boundaries, sensitive-path checks, MAX_READ_BYTES, etc.) are battle-tested. The adapter wraps, never edits.

Tests run on host, not in sandbox. Use `unittest` + `unittest.mock.patch` to stub out the action functions and `bus_worker.register_handler` — no real codex run, no real SMB read.

## Working directory

`<project-dir>` (already your `--cd`).

## Project context

Read in this order:
1. `AGENTS.md`
2. `docs/phase-1.5-git-bus.md`
3. `tools/bus_worker.py` — `HANDLERS`, `register_handler`, handler signature `(envelope: dict, payload: dict) -> dict`
4. `tools/openclaw-codex-bridge.py` — focus on `action_list`, `action_read`, `action_rg`, `action_handoff_to_codex`, `action_codex_exec` plus the existing `main()` arg parsing

The bridge module's filename has a dash — use `importlib.util.spec_from_file_location` to import it (mirror the pattern in `tracker/openclaw-watch.py`).

## Handler contract

Bus worker handler signature: `(envelope: dict, payload: dict) -> dict`.

Existing bridge action signature: `(request: dict, workai_root: Path, bridge_root: Path | None) -> dict`.

Adapter wraps: payload **is** the request dict (operators write the same JSON shape as today, just to a NAS share file instead of an SMB inbox file). `workai_root` and `bridge_root` are bound at register-time from env/CLI.

Mapping (5 kinds — match the issue body):

| Bus kind | Bridge action |
|---|---|
| `bus.list` | `action_list` |
| `bus.read` | `action_read` |
| `bus.rg` | `action_rg` |
| `bus.handoff_to_codex` | `action_handoff_to_codex` |
| `bus.codex_exec` | `action_codex_exec` |

`action_ping`, `action_git_status`, `action_codex_status`, `action_codex_cancel` are intentionally out-of-scope for MVP — they are operational, not work-doing, and the SMB path covers them.

## Failure handling

The bridge actions raise `BridgeError(message)` on validation failure. The adapter must:

- Catch `BridgeError` → re-raise as `bus_worker._WorkerFailure("bridge_error", message=str(exc))` so the worker sets `neon:state/failed` with a structured reason.
- Let other exceptions propagate to the worker's generic exception handler (which already wraps them as `handler_exception`).

Note: `_WorkerFailure` is private to `bus_worker`. Re-export it (or duplicate the minimal shape) — your call. Recommend re-export with `from tools.bus_worker import _WorkerFailure as WorkerFailure` and use `WorkerFailure` locally.

## Deliverables

### 1. `tools/bus_adapter_openclaw.py`

Public surface:

```python
def load_bridge_module(bridge_path: Path = None):
    """Import openclaw-codex-bridge.py via importlib (filename has a dash).
    Default path: this directory / 'openclaw-codex-bridge.py'."""

def register_all(workai_root: Path, bridge_root: Path | None = None, *, bridge_module=None) -> dict[str, callable]:
    """Register the 5 bus.* handlers with bus_worker.HANDLERS.
    Returns the {kind: handler} mapping for caller inspection / tests."""

def make_handler(action_fn, workai_root: Path, bridge_root: Path | None):
    """Wrap a bridge action_fn into a (envelope, payload) -> dict bus handler."""
```

### 2. `tests/test_bus_adapter_openclaw.py`

Unit tests (≥8) using `unittest.mock.patch`. Coverage:

1. `test_register_all_returns_5_handlers` — keys: `bus.list, bus.read, bus.rg, bus.handoff_to_codex, bus.codex_exec`.
2. `test_register_all_writes_to_bus_worker_HANDLERS` — `HANDLERS["bus.list"]` is callable after `register_all`.
3. `test_make_handler_passes_payload_as_request` — handler called with mock envelope + payload; the wrapped action_fn receives payload (not envelope) + correct workai_root + bridge_root.
4. `test_make_handler_returns_action_result` — handler return value == action_fn return value (no transformation).
5. `test_make_handler_translates_BridgeError_to_WorkerFailure` — action_fn raises `BridgeError("invalid id")` → handler raises `WorkerFailure` with `reason="bridge_error"` + `message="invalid id"`.
6. `test_make_handler_lets_other_exceptions_propagate` — action_fn raises `RuntimeError("oops")` → handler re-raises `RuntimeError` (no wrapping).
7. `test_load_bridge_module_imports_dash_named_file` — uses a temp dir with a fake module file, asserts `BridgeError` symbol is importable from the loaded module.
8. `test_register_all_uses_injected_bridge_module` — `register_all(..., bridge_module=stub)` does NOT load the real file; uses `stub.action_*` instead.

Stub the bridge module in tests — do not import the real one, which would import the action handlers, regex globs, codex subprocess etc.

### 3. `tools/openclaw-codex-bridge.py` modifications

Add a `--bus` flag to the existing arg parser (next to `--watch`). When `--bus` is passed:

```python
if args.bus:
    from tools.bus_adapter_openclaw import register_all
    from tools import bus_worker
    register_all(args.workai_root, args.bridge_root)
    return bus_worker.main(["--host", args.host, "--poll-interval", str(args.poll_interval)])
```

Add `--host <name>` and `--poll-interval <int>` args (required when `--bus`; ignored otherwise, default 30). Default mode (no `--bus`) MUST be unchanged — `--watch` still does inbox/outbox file polling.

**Do not modify any existing `action_*` function or the file-watcher loop.** Only:
- Add the new CLI flags.
- Add the new branch at the top of `main()` (before existing branches).
- Optionally print a deprecation hint when the file-watch mode starts: `print("[bridge] file-watch mode (legacy); --bus available for Phase 1.5 transport", file=sys.stderr)`.

### 4. `tools/run-openclaw-codex-bridge.cmd`

Replace `--watch` with `--bus --host <hostname>` (use `%COMPUTERNAME%` for the hostname so the cmd works on any Windows host without per-machine editing):

```cmd
@echo off
setlocal
py -3.14 "%~dp0openclaw-codex-bridge.py" --bus --host %COMPUTERNAME%
```

## Acceptance criteria

- `py -3.14 -c "import sys; sys.path.insert(0, '.'); from tools.bus_adapter_openclaw import register_all, make_handler, load_bridge_module; print('ok')"` prints `ok`.
- `py -3.14 -m unittest tests.test_bus_adapter_openclaw -v` — all ≥8 tests pass.
- `py -3.14 -m py_compile tools/bus_adapter_openclaw.py tests/test_bus_adapter_openclaw.py tools/openclaw-codex-bridge.py` exits 0.
- `py -3.14 tools/openclaw-codex-bridge.py --help` shows `--bus`, `--host`, `--poll-interval`.
- Existing openclaw-codex-bridge tests (if any in `tests/`) still pass — full suite stays green.
- Stdlib only.

## Out of scope

- Webhook receiver (polling is the MVP transport).
- HTTP payload schemes (worker already fails them).
- New action kinds.
- Modifying any `action_*` body (sandbox / size limits / sensitive-path checks).

## Style / project conventions

- Match shape of `tools/bus_worker.py`.
- `from __future__ import annotations`.
- No `Co-Authored-By:`.
- Logging: `print(f"[bus-adapter-openclaw] ...", file=sys.stderr)`.

## Self-check before "done"

- Tests pass, py_compile clean.
- `register_all` is the only public function that mutates `bus_worker.HANDLERS`; no side effects on module import.
- `--bus` flag works; default `--watch` flag unchanged (sanity-check by reading `main()` diff).
- `run-openclaw-codex-bridge.cmd` uses `%COMPUTERNAME%`, not a hardcoded host.

## Final report

Conform to schema. Note any deviations explicitly (e.g. if `--watch` needed adjustment to accept new flags without breaking).
