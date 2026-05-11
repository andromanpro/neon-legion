# CLAUDE.md — Claude-specific entry point

**Canonical project context lives in [`AGENTS.md`](AGENTS.md).** That file
is read by every agent (Codex, OpenCode, OpenClaw, Aider, Cody). Claude
Code reads this file (`CLAUDE.md`); to avoid drift, this file is a thin
pointer. If you edit one, edit the other.

Personal notes (memory, customer references, history) belong in
`CLAUDE.local.md` (gitignored). See `CLAUDE.local.md.example` as a starting
template if you want to bootstrap your own local agent notes.

---

The sections below duplicate the high-signal parts of `AGENTS.md` so
Claude has them inline; for the full contract (sandbox boundaries, file
protocols, bridge details, "any agent can be orchestrator") — read
`AGENTS.md`.

## Project identity

**neon-legion** (codename `multi-agent`) — a personal, local-first AI
usage tracker and dashboard. It captures token/cost/time metrics across
Claude Code, Codex CLI, OpenClaw, and OpenCode; runs a sentiment+productivity
oracle; renders a cyberpunk dashboard locally and (optionally) on a
WordPress page through a snapshot pipeline that never opens an outbound
port.

The multi-agent piece is twofold:
1. The pipeline aggregates events from multiple AI providers in one
   dashboard (provider tracking).
2. Optional bridges (e.g. `tools/openclaw-codex-bridge.py`) let one agent
   on one machine ask another agent on another machine to run a task — for
   instance, OpenClaw on a NAS asking Codex on Windows for a `codex_exec`
   inside a sandbox.

## Repo layout

```
neon-legion/
├── hooks/          # Claude Code Stop/SessionStart hooks
├── tracker/        # Event ingestion (wrappers + backfills) + summary.py
├── backend/        # HTTP API + snapshot writer (stdlib only)
├── dashboard/      # Local cyberpunk HTML + WordPress page template
├── tools/          # Sandbox bridges, sanitization, privacy scanners
├── prompts/        # Task specs for AI developers (audit history + templates)
├── schemas/        # JSON schemas for codex --output-schema strict mode
└── docs/           # Architecture, diagrams, screenshots
```

## Conventions for any agent

1. **No outbound network calls** from `tracker/`, `backend/`, or `dashboard/`.
   The whole pipeline runs locally. `tools/` may call out (e.g. fetching
   currency rates) — flag it in the module docstring.
2. **Stdlib only** in `tracker/`, `backend/`, `dashboard/`. Third-party
   dependencies belong in `tools/` and must be optional.
3. **Atomic writes** for any file that the dashboard reads:
   `*.tmp.<pid>.<tid>` → `os.replace`. Never leave partially-written JSON
   visible to the UI.
4. **No `Co-Authored-By:`** trailers in commits — AI tools are not authors.
   The human submitter is responsible for the change.
5. **Privacy by default** — assume any sample data you commit will end up
   on a public Pages site. If unsure, run `tools/oss-sanitize.py --check`.
6. **Append-only events** — never rewrite `tracker/*-events.jsonl`. To
   correct mistakes, write a compensating event with a `correction_of`
   field.

## Multi-agent roles

| Role | Who | Responsibility |
|---|---|---|
| Architect | Claude Code (interactive) | Design decisions, code review, narrative |
| Developer | Codex CLI (headless, `codex exec`) | Implementation, refactors, tests |
| Reviewer (third opinion) | OpenCode + DeepSeek v4 via OpenRouter | Security audit, money-math sanity, residual issue scan |
| Approver | Human submitter | Final merge gate; pushes to public main |

The standard flow is described in the `/multi-agent` skill. In short:
architect writes a task spec in `prompts/`, developer implements, architect
reviews. For high-stakes batches (release prep, security PRs), add a third
DeepSeek pass before merge.

## Trust boundaries

- **`workspace-write` sandbox** is allowed for Codex implementing under
  spec. Tests run on host, not inside sandbox (the sandbox blocks `pip
  install`).
- **`danger-full-access`** for any agent requires explicit human approval
  per invocation. Bridges (`tools/openclaw-codex-bridge.py`) reject it.
- **`read-only`** for all third-opinion reviewers.

## When in doubt

Read `README.md` for the user-facing story, `docs/architecture.md` for the
data flow, and `SECURITY.md` for what is considered sensitive. Then check
the `/multi-agent` skill for SDLC patterns.
