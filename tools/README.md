# Tools

`tools/` calls things outside the repo — `claude`, `codex`, `opencode`, `git`,
`rg`. Tracker, backend, and dashboard stay stdlib and never dial out. Tools are
still stdlib too, but they can spawn subprocesses to do the dirty work.

See [AGENTS.md](../AGENTS.md) for the repo contracts: append-only event logs,
atomic writes, no committed private data, and no `Co-Authored-By:` trailers.

## Orchestration

### `orchestrate.py`

`orchestrate.py` is the reference orchestrator for declarative role flows. It
reads `roles.toml` when present, falls back to `roles.example.toml`, loads a
manifest such as `prompts/MANIFEST.example.toml`, orders the requested roles,
then writes a run directory under `orchestrate-runs/<run-id>/`. Use it when a
task needs the repo's architect/developer/reviewer/approver handoff instead of
a single direct CLI call. It exits `0` for completed commands, `1` for failed
role execution or unexpected runtime errors, `2` for invalid TOML or manifest
shape, and `78` when a `human-relay` role pauses the run for manual input.

Common commands: `py tools/orchestrate.py run prompts/MANIFEST.example.toml`,
`py tools/orchestrate.py resume <run-id>`, `py tools/orchestrate.py status <run-id>`,
`py tools/orchestrate.py list`, and `py tools/orchestrate.py run --list-roles`.

The run directory is the audit trail. It contains `manifest.used.toml`,
`roles.used.toml`, `state.json`, one markdown deliverable per completed role,
and an `.error.md` file when a role fails. Context files listed in a manifest
are resolved under the project root after realpath checks, so symlink or
junction escapes are rejected.

Related docs: [docs/sample-run/README.md](../docs/sample-run/README.md) shows a
sanitized run directory, and [AGENTS.md](../AGENTS.md) describes the same file
contract from the agent side.

### `role_invoke.py`

`role_invoke.py` is a library used by the orchestrator and hindsight replay,
not a standalone CLI. It maps a role's `invocation` field to one of four
adapters: `claude-cli-headless`, `codex-exec`, `opencode-run`, or
`human-relay`. Use it from Python code that already built a prompt and has an
output path; the public function is `invoke(role_config, prompt, output_path)`.
Unsupported invocations return a result dict with `exit_code=2`, missing CLI
binaries return `127`, and timed-out subprocesses return `124`.

The Claude adapter runs `claude -p --bare --output-format json` and unwraps the
response text before writing the deliverable. The Codex adapter runs
`codex exec` with the configured sandbox and asks Codex to write the last
message to a temporary file, then replaces the final output path. The OpenCode
adapter runs
`opencode run`, injects `OPENROUTER_API_KEY` from `git config --global openrouter.apiKey`
when available, and strips ANSI escape sequences before downstream roles see
the output.

`human-relay` writes a `*-PROMPT.md` file and returns immediately with
`waiting_for_human=true`. The orchestrator turns that into status
`waiting_for_human` and resumes once the response file exists.

### `hindsight.py`

`hindsight.py` runs a second-model critique against completed orchestrator
deliverables. It reads an existing `orchestrate-runs/<run-id>/state.json`,
selects a critic adapter from the original invocation (`codex-exec` usually
gets `opencode-run`, `opencode-run` gets `codex-exec`), and writes
`<role>.hindsight.md` plus a combined `hindsight.md`. Use it after a run
completes when another model should look for missed bugs, privacy issues, or
weak reasoning without rerunning the original task.

Examples: `py tools/hindsight.py --list`, `py tools/hindsight.py <run-id> --dry-run`,
`py tools/hindsight.py <run-id> --role developer`, and
`py tools/hindsight.py --all-pending`. It exits `0` when listed or processed
runs finish cleanly, `1` when a run is not completed or a critic fails, and
`2` for missing required CLI arguments. Each processed deliverable appends an
event to `tracker/hindsight-events.jsonl` with `schema_version`, original
invocation, critic invocation, model, byte counts, status, and output path.

Empty deliverables are treated as trivial and skipped with a short output file.
If no role with the selected critic invocation exists in the current roles
table, the hindsight file records that skip instead of inventing a reviewer.

## Release and privacy

### `release-gate.py`

`release-gate.py` is the hard-fail privacy scanner for public releases. It
scans tracked files, forced ignored files, and recent git commit messages for
configured leak patterns: API-token shapes, absolute paths, LAN addresses,
emails, tracked tracker JSONL, tracked private prompts, cache artifacts, logs,
environment files, customer codenames, and `Co-Authored-By:` trailers. Use it
before tagging or pushing a public release; it does not rewrite anything.

Commands: `python tools/release-gate.py`, `python tools/release-gate.py --json`,
and `python tools/release-gate.py --quiet`. Exit codes are explicit: `0`
clean, `1` violations, `2` config or invocation error. Configuration lives in
[tools/release-gate.toml](release-gate.toml); tests are excluded there because
test fixtures intentionally contain fake private-looking strings.

This gate is stricter than `oss-sanitize.py` because it decides whether
publishing is blocked. Warnings still print in the human output, while
categories configured as `fail` make the command non-zero.

### `oss-sanitize.py`

`oss-sanitize.py` transforms public-facing files before an OSS publish. It
applies generic scrub rules plus optional private rules from
`.oss-sanitize-private.txt`, replaces matches with placeholders, and can show a
diff or rewrite files with backups under `.oss-backup/`. Use it when the repo
contains known personal or customer identifiers that should become stable
placeholders in docs or prompts.

Commands: `python tools/oss-sanitize.py --check`, `python tools/oss-sanitize.py --diff`,
and `python tools/oss-sanitize.py --apply`. Check mode exits `0` when no
substitutions are needed and `2` when violations remain; apply and diff mode
exit `0` after processing matched files. The default scope covers root README
files, prompts, `CLAUDE.md`, dashboard notes, tracker/dashboard docs, selected
tool files, and run command helpers.

Keep blocks can be marked with `<!-- oss:keep -->` and `<!-- /oss:keep -->`.
The sanitizer does not scrub filenames, ignored private prompt directories, or
every possible local Windows path unless the private rules file says exactly
what to match.

### `privacy-scan-snapshot.py`

`privacy-scan-snapshot.py` scans a generated dashboard snapshot before public
promotion. It validates that the snapshot is JSON, then checks the raw text for
local paths, user profile paths, emails, API-token shapes, Telegram IDs, raw
message IDs, raw session UUIDs, and private LAN addresses. Use it after
`backend/server.py --snapshot-once --public` and before copying the snapshot to
WordPress or another public host.

Default command: `py -3.14 tools/privacy-scan-snapshot.py`. Custom inputs use
`py -3.14 tools/privacy-scan-snapshot.py --snapshot dashboard/snapshot.json --extra-terms tracker/private/public-blocklist.txt`.
It exits `0` when no findings are present, `1` when privacy matches are found,
and `2` when the file is not valid JSON.

This scan is a guardrail. [SECURITY.md](../SECURITY.md) still requires a human
review of `today.top_session`, `sessions[].desc`, provider origins, and the
rendered WordPress page.

### `schema_migrate.py`

`schema_migrate.py` checks persisted schema coverage for event logs, run state,
and dashboard snapshots. In v1 it is read-only for `--check`; migrations are
intentionally not implemented until a schema newer than `CURRENT_SCHEMA_VERSION = 1`
exists. Use it after adding new persisted record types or before a release that
depends on every record carrying `schema_version`.

Commands: `python tools/schema_migrate.py --check` and
`python tools/schema_migrate.py --target 2`. Check mode scans candidate
`*.jsonl`, `state.json`, and snapshot files, prints a per-file status, then
exits `0` only when every present non-empty record is current. A target
migration currently prints that migrations are not implemented and exits `2`.

Related one-off history lives in `tracker/backfill-schema-version.py`, which
retroactively adds `schema_version=1` to existing tracker JSONL and
orchestrator state files.

## Demo data

### `gen-fake-events.py`

`gen-fake-events.py` writes deterministic demo telemetry for a clean checkout.
It creates fake Claude, Codex, OpenClaw, and OpenCode event streams plus
Claude task summaries, all under the local tracker directory, and never calls
the network. Use it when the dashboard needs data before real hooks and
wrappers have produced events.

Command: `python tools/gen-fake-events.py --days 7`. Add `--dry-run` to print
planned counts without writing files, or `--force` to replace non-empty demo
targets in a disposable checkout. The command exits `0` on success or dry run,
`1` when non-empty tracker targets made the write incomplete, and `2` for
argument or internal generation errors.

`make demo` runs this generator, writes one dashboard snapshot with
`backend/server.py --snapshot-once`, and tells the reader to open
`dashboard/index.html`.

## Bridge

### `openclaw-codex-bridge.py`

`openclaw-codex-bridge.py` is the Windows-side bridge for an OpenClaw workspace
that cannot see the Windows worktree directly. It watches a shared folder with
`inbox/`, `outbox/`, `archive/`, and `jobs/`, processes constrained JSON
requests, and can launch Codex CLI jobs through `tracker/codex-track.py`. Use
it when another machine needs controlled read/search/status access or needs to
queue a sandboxed Codex job against this checkout.

Commands: `py tools/openclaw-codex-bridge.py --once` and
`py tools/openclaw-codex-bridge.py --watch --sleep 2`. Supported actions
include `ping`, `list`, `read`, `rg`, `git_status`, `handoff_to_codex`,
`codex_exec`, `codex_status`, and `codex_cancel`. The bridge rejects sensitive
paths, prevents path escapes outside `WORKAI_ROOT`, limits read/search output,
and allows only `read-only` or explicit `workspace-write` Codex sandboxes.

The normal inbox loop returns `0` after processing requests. Internal Codex job
runners use the Codex exit code, `124` for timeout, and `2` for invalid job
requests. [AGENTS.md](../AGENTS.md) documents the same bridge protocol from the
agent contract side.

## Configuration

### `config.py`

`config.py` is the runtime configuration loader used by backend, tracker, and
tools code. It is a library, not a CLI, and exposes `load_config()`,
`get(dotted_key, default, convert)`, and
`get_legacy_env(name, default, convert)`. Use it when new code needs a setting
that should respect `config.toml`, `config.example.toml`, and environment
overrides in one place.

Precedence is: caller-supplied CLI flag, environment variable, local
`config.toml`, committed `config.example.toml`, then code default. The env-var
mapping is explicit in `ENV_OVERRIDES`; legacy variables that do not belong in
the TOML schema stay available through `get_legacy_env()`. Invalid local TOML
or conversion failures fall back instead of crashing import-time code.

The config template is [config.example.toml](../config.example.toml). Keep
machine paths, salts, and local subscription overrides in ignored
`config.toml`.

## Quick chooser

| Need | Run |
|---|---|
| Start a role-based task flow | `py tools/orchestrate.py run <manifest.toml>` |
| Resume a human-relay run | `py tools/orchestrate.py resume <run-id>` |
| Critique completed role output with another model | `py tools/hindsight.py <run-id>` |
| Generate first-run dashboard data | `python tools/gen-fake-events.py --days 7` |
| Check schema coverage | `python tools/schema_migrate.py --check` |
| Sanitize docs before public OSS work | `python tools/oss-sanitize.py --check` |
| Block a release on privacy leaks | `python tools/release-gate.py` |
| Scan a public snapshot | `py -3.14 tools/privacy-scan-snapshot.py` |
| Process bridge requests once | `py tools/openclaw-codex-bridge.py --once` |
