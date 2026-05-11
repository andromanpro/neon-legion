# AGENTS.md — universal agent contract for neon-legion

This file is the **canonical project context for any AI agent**. It is read
automatically by Codex CLI, OpenCode, Aider, Sourcegraph Cody, OpenClaw, and
similar tools at session startup. Claude Code reads `CLAUDE.md`, which is a
thin pointer back to this file — keep the two in sync if you edit either.

If you are an agent and you can see this file, you are inside the legion.
Read this whole document once, then proceed.

## Project in one sentence

**neon-legion** is a personal, local-first AI usage tracker and a multi-agent
orchestrator. It captures token/cost/time across Claude Code, Codex CLI,
OpenClaw, OpenCode + DeepSeek; routes tasks across them via a declarative
`roles.toml`; and ships a cyberpunk dashboard nobody asked for.

Slogan: *Your personal AI legion. Almost no fucking swearing. (Yes, we count yours.)*

## Architecture in six layers

```
1. Hooks         hooks/                      — capture events from Claude Code
2. Tracker       tracker/*-events.jsonl      — append-only fact ledger (stdlib)
3. Backend       backend/                    — read-only HTTP + snapshot writer
4. Dashboard     dashboard/                  — local HTML, optional WP snapshot
5. Orchestrator  tools/orchestrate.py        — roles.toml → DAG → invoke role CLIs
6. Bridge        tools/openclaw-codex-bridge — cross-machine inbox/outbox over SMB
```

Any agent is allowed to become the **orchestrator** — the loop is just
"read manifest, run task, write deliverable, repeat". `tools/orchestrate.py`
is the reference implementation; equivalent implementations in Codex or
OpenCode are welcome as long as they respect the same file contracts.

## Roles

Defined in `roles.example.toml` (copy to `roles.toml`, gitignored):

| Role | Default invocation | Typical job |
|---|---|---|
| architect | `claude-cli-headless` | Design, spec, narrative, review |
| developer | `codex-exec` (workspace-write) | Implementation, refactor, tests |
| reviewer | `opencode-run` (DeepSeek v4 via OpenRouter, read-only) | Third opinion, security audit, money-math |
| approver | `human-relay` | Final merge gate |

Roles are **not hard-wired to vendors**. If you want Codex to be the
architect and Claude to be the developer — edit `roles.toml`. The
orchestrator only knows about `invocation` strings, not models.

## File contracts every agent must respect

1. **Append-only events.** Never rewrite `tracker/*-events.jsonl`. To fix a
   prior event, write a compensating row with `correction_of: <event_id>`.
2. **Atomic writes** for anything the dashboard or another agent reads:
   `tmp.<pid>.<rand>` → `os.replace`. Half-written JSON breaks consumers.
3. **No outbound network calls** from `tracker/`, `backend/`, `dashboard/`.
   `tools/` may call out and must declare it in its module docstring.
4. **Stdlib only** in `tracker/`, `backend/`, `dashboard/`. Anything pip-ish
   goes in `tools/` and must be optional.
5. **No `Co-Authored-By:` trailers** in commit messages. The human submitter
   is the only author.
6. **Privacy by default.** Sample data committed to git is assumed public.
   `tools/oss-sanitize.py --check` before any push.

## Sandbox boundaries (read this before running anything)

| Mode | Who | What's allowed |
|---|---|---|
| `read-only` | reviewers, third-opinion audits, exploration | Read everything except secrets list in `tools/openclaw-codex-bridge.py` |
| `workspace-write` | developer implementing under a spec | Write under repo only; **no `pip install`** (tests run on host) |
| `danger-full-access` | requires per-invocation human ok | Bridges **reject** this — must run locally |

`workspace-write` requires explicit `allow_workspace_write: true` when
invoked through the bridge. Bridges silently downgrading anything to
`danger` is a bug — file an issue.

## How to be the orchestrator (any agent)

1. Read `roles.example.toml` and `prompts/MANIFEST.example.toml` to learn
   the schema.
2. Write a manifest describing the task — flow (`architect → developer →
   reviewer → approver`), context_files, acceptance criteria, skip rules.
3. Resolve dependencies; for each task, render prompt = role.backstory +
   role.goal + manifest.task + context_files.
4. Invoke the role's CLI (the `invocation` string maps to an adapter:
   `claude-cli-headless`, `codex-exec`, `opencode-run`, `human-relay`).
5. Write the deliverable atomically, log to `orchestrate-runs/<run_id>/`.
6. Move to next task. On failure, exit 78 (EX_CONFIG) so a human can resume.

If you don't want to reimplement step 1-6 — just call
`python tools/orchestrate.py run <manifest.toml>`. The reference
implementation handles resume, DAG, dry-run, and `--list-roles`.

## Bridge protocol (cross-machine)

The bridge in `tools/openclaw-codex-bridge.py` is a polling watcher, not a
daemon. Either side drops a JSON file into `inbox/<id>.json`; the watcher
on the other side processes it sequentially (sorted by filename), writes
the response to `outbox/<id>.json`, moves the request to `archive/`. Codex
jobs run as detached subprocesses with `state.json` per job, so the watcher
itself never blocks on a long-running task.

**Parallelism today:** the inbox loop is sequential (one request at a
time), but long-running Codex jobs (`codex_exec`) are launched as
background `Popen` and tracked via `jobs/<job_id>/state.json`. So many
Codex jobs can run in parallel — the bottleneck is only the dispatch of
new requests, which is millisecond-scale per file. If you need actual
parallel dispatch (rare), wrap `process_once` in a thread pool — but
beware: SMB shares hate concurrent writes to the same filename, and
`bridge-state.json` is single-writer.

**When to use the bridge instead of direct orchestrate:**
- The target agent lives on another machine (OpenClaw on NAS, Codex on a
  workstation, etc.)
- You want sandboxed cross-machine handoff with audit trail
- You want the orchestrator and the worker to fail independently

When you don't need cross-machine, prefer `tools/orchestrate.py` — fewer
moving parts.

## Where to look first

| Need | File |
|---|---|
| User-facing story | `README.md` |
| Data flow + diagrams | `docs/architecture.md` |
| What's considered sensitive | `SECURITY.md` |
| Active phases | Gitea issues with `phase:*` labels |
| SDLC patterns | `/multi-agent` skill (or `prompts/private/` private specs) |
| Reviewer prompt template | `prompts/template-deepseek-review.md` |

## When in doubt

Ask. Either a human reviewer or, if you are an autonomous agent, a peer
agent through the bridge. Silent guessing in a multi-agent system is the
single worst failure mode — it produces work that looks plausible and is
wrong.
