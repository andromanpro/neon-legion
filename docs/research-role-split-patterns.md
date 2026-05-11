# Role-split patterns in top OSS agent-orchestration repos

Research dump — input to multi-agent skill v2 + naming/positioning decision.

## CrewAI (51k★)

**Model:** Crew = team of Agents, each with `role`, `goal`, `backstory`. Tasks
flow between agents via `process` (sequential / hierarchical / consensual).

**Killer concept:** every agent is a Pydantic model with explicit prompt
scaffolding for role/goal/backstory. Reduces "who am I" drift.

**Take for neon-legion:**
- Codify roles in YAML/TOML, not in code
- Each role has explicit `goal` field that gets prepended to every prompt
- "Backstory" = system-prompt context that persists

## MetaGPT (68k★)

**Model:** Simulated software-company hierarchy — `ProductManager`, `Architect`,
`ProjectManager`, `Engineer`, `QA`. Sequential SDLC pass.

**Killer concept:** SOP encoded — each role has standard deliverables
(PRD → tech-design → tasks → code → tests).

**Take for neon-legion:**
- We already do this with prompts/phase-*.md — but ours is informal
- Adopt MetaGPT's deliverable-per-role convention: architect writes Issue,
  developer outputs PR-spec, reviewer outputs audit report
- Add a `roles.toml` that defines what each role MUST produce

## OpenHands / OpenDevin (73k★)

**Model:** Single autonomous agent with tool registry — file editing, bash,
browsing, IPython. No multi-agent — instead an event-stream of tool calls.

**Killer concept:** runtime event store. Every action logged as event,
replayable, auditable.

**Take for neon-legion:**
- Our `tracker/*-events.jsonl` is similar — events of agent activity
- Could add `tool` field to events to distinguish bash vs file-read vs LLM-call
- Replayability = unique angle vs token-counter trackers

## SWE-agent (19k★)

**Model:** Agent + Agent-Computer Interface (ACI). Restricted tool set
optimized for code-fix tasks: open, edit, scroll, run-tests.

**Killer concept:** task scoping — SWE-Bench style: take issue, produce diff.
Doesn't try to be general.

**Take for neon-legion:**
- Define narrow task patterns where a sub-agent is invoked
- Codex CLI already does this (codex exec --sandbox)
- We could add "task templates" — issue→PR, refactor, audit, write-tests

## aider (45k★)

**Model:** Single agent, pair-programming framing, multiple LLM backends.
Repo-aware via file map + treesitter.

**Killer concept:** repo-map injection into every prompt — agent always knows
codebase structure.

**Take for neon-legion:**
- We already inject project context per-session via CLAUDE.md
- Adopt aider's "repo-map" idea: auto-generated outline at session start

## claude-squad (7k★)

**Model:** TUI managing N parallel terminal agents (Claude/Codex/etc) in
separate tmux + git worktrees. Each agent has isolated workspace.

**Killer concept:** git worktree per agent = no merge conflicts mid-run.

**Take for neon-legion:**
- We use worktrees ad-hoc for big Codex tasks (per memory)
- Could systematize: each multi-agent task auto-creates a worktree
- This is THE pattern for parallel multi-agent without stepping on each other

## opencode (158k★)

**Model:** Single coding agent, plugin-based providers (OpenAI/Anthropic/local).
Headless `opencode run`, TUI, MCP support.

**Killer concept:** provider-agnostic, plugin ecosystem.

**Take for neon-legion:**
- We're already provider-aware (4 providers tracked)
- Could expose a plugin interface for "add your AI CLI tracker" — but tracker
  is simpler than agent runtime, less plugin surface needed

## cline (62k★)

**Model:** Autonomous agent in VS Code, browser preview, executes commands
with user approval per step.

**Killer concept:** approval gates per action — user sees diff/command before
execution.

**Take for neon-legion:**
- Not directly relevant (we track usage, don't execute)
- BUT: openclaw-codex-bridge has sandbox modes which match this pattern

## block/goose (45k★)

**Model:** AI agent + "recipe" system (workflows defined in markdown).
Provider-agnostic. Extensions for git/email/etc.

**Killer concept:** recipes = reusable workflow templates committed to repo.

**Take for neon-legion:**
- Our prompts/*.md are similar — task templates
- Goose elevates them as first-class artifacts. We could too.

---

## Synthesis: what to steal for neon-legion v0.2 (post-publish)

| Pattern | Source | Applied where |
|---|---|---|
| **Role-as-YAML/TOML** with explicit `goal` field | CrewAI | Add `roles/<role>.toml` with persistent context |
| **Deliverable-per-role** SOP | MetaGPT | Codify in `/multi-agent` skill what each role MUST produce |
| **Event store with `tool` field** | OpenHands | Extend `tracker/*-events.jsonl` schema for tool-level events |
| **Task templates as recipes** | goose / aider | Promote `prompts/*.md` to first-class «recipe» artifacts |
| **Git worktree per parallel agent** | claude-squad | Automate worktree creation for parallel Codex/Claude runs |
| **Repo-map auto-injection** | aider | Generate project outline at session start, inject into agent prompts |
| **Approval gates** | cline | Already done in openclaw-codex-bridge sandbox modes |

## What NOT to steal

- CrewAI's `Process` orchestration — too abstract for our use, we delegate
  via codex exec / human-relay, no need for own scheduler
- MetaGPT's role hierarchy as code — too rigid, prompts/*.md is more flexible
- OpenHands's runtime — we're a tracker, not a runtime
- opencode's plugin system — overkill for 4 providers

## Differentiation slots (our wedges)

Three slots where top-10 OSS doesn't compete:

### 1. Reflection > execution
All top tools focus on **executing** agents. We focus on **auditing** their
work. Adding a "third opinion" review (DeepSeek) on top of architect (Claude)
+ developer (Codex) is built-in, not bolted on.

### 2. Cross-machine controlled bridge
`tools/openclaw-codex-bridge.py` (28KB) is unique: a file-protocol bridge that
lets one agent on one machine (e.g. OpenClaw on NAS Docker) invoke another
agent on a different machine (Codex on Windows) without opening any port.

Top-10 stays single-machine. claude-squad runs parallel agents but all on
local tmux. CrewAI / AutoGen run in one process. We solve the "I'm on the
couch with my phone, agents are on my workstation upstairs" problem with
file-shared inbox/outbox + sandbox modes (read-only / workspace-write /
controlled-elevation).

This is **military logistics**: distributed units, secured communication
channel, chain of command. Maps perfectly onto the `neon-legion` frame.

### 3. Sentiment overlay
ZERO competitors track frustration / profanity / appreciation per session.
We do (Phase 1.4). For an "army" framing this is **morale tracking** —
classic military command function.

## The army narrative writes itself

| Project layer | Military equivalent |
|---|---|
| Multi-vendor tracking (Claude+Codex+OpenClaw+OpenCode) | Different units (infantry / artillery / signals / supply) — one HQ |
| OpenClaw NAS-Docker → Windows Codex bridge | Secured comms across forward operating bases |
| Productivity oracle (hours saved) | Force-multiplier metric |
| Sentiment tracking | Morale + combat-stress reports |
| 5h-budget rate-limit gauge | Logistics / ammunition counter |
| `--public` snapshot with salt-hash | OPSEC: redacted situation reports for public release |
| Multi-agent SDLC (architect/developer/reviewer) | Three-officer review chain — CO, XO, OPSO |

Slogan candidates with this scaffolding:

- *«The other tools are weapons. We are the after-action report.»*
- *«Distributed agents need distributed command. Welcome to the legion.»*
- *«AI is the weapon. We track the campaign.»*
- *«Recruit, deploy, audit. Repeat.»*
- *«Every keystroke logged. Every dollar accounted. Every swear word counted.»*

The last one — that's the meme-sarcastic Andriyanov line.
