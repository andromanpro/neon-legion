# Roadmap

> Living document. Captures phases that already shipped, what's planned, and the
> wow-layer modules sketched by the triple-review pipeline (Claude architect +
> Codex CLI + DeepSeek V4-Pro via OpenCode). See [`AGENTS.md`](../AGENTS.md) for
> the orchestration contract and [`docs/architecture.md`](architecture.md) for
> the data flow.

## Shipped

| Phase | What |
|---|---|
| 1.0   | Claude Code tracking hook + cost summary (`hooks/`, `tracker/summary.py`) |
| 1.0.1 | Retroactive backfill from `~/.claude/projects/*/*.jsonl` with `(session_id, message_uuid)` dedup (`tracker/backfill.py`) |
| 1.0.2 | Throttle, skip synthetic, pricing model (`tracker/recost.py`) |
| 1.0.3 | Active-time metric (gap-based ≤ 2 min) inside `summary.py` |
| 1.1   | Codex CLI tracking (`tracker/codex-track.py` + `codex-events.jsonl`) |
| 1.2   | OpenClaw backfill (`tracker/backfill-openclaw-sessions.py`; live tail is still pending — see P0 below) |
| 1.3   | Task complexity estimation pipeline (`tracker/estimate-task.py`, `tracker/run-recent-estimates.py`, `tracker/note-task.py`) |
| 1.4   | Sentiment / profanity tracking (`tracker/backfill-profanity.py`) |
| 2     | Aggregator backend, stdlib HTTP (`backend/server.py`) |
| 3     | Cyberpunk live dashboard (`dashboard/index.html`, `dashboard/page-multi-agent.php`) |
| 3.5   | Snapshot-pipeline for WordPress integration (`backend/server.py --snapshot-once --public`) |
| 4     | Public stats on `androman.pro` — `/labs/` badge + live-block on home + `/neon-legion/` full dashboard (theme `androman` v0.8.28+) |

Pre-publish boring stuff that landed in v0.3:

- 53 unit tests (`tests/`, stdlib `unittest`)
- Release privacy gate (`tools/oss-sanitize.py`, `tools/privacy-scan-snapshot.py`, `tools/release-gate.py`)
- Schema version fields on events / tasks / snapshots (`tracker/schema_migrate.py`)
- Live OpenCode tracking
- Demo trace + fake data generator (`docs/sample-run/`, `tools/gen-fake-events.py`)
- Config TOML wired at runtime (`tools/config.py`, `config.example.toml`)

## Planned

### P0 — boring stuff still missing

These keep the project credible. Anything below a tick here is a quiet
"weekend project" signal to the first GitHub visitor.

1. ~~**Live OpenClaw tracking**~~ ✅ done — `tracker/openclaw-watch.py`
   mirrors the OpenCode watcher (poll the same per-session JSONL tree the
   backfill walks; 30s interval default; `--once` for cron / supervisor;
   dedup by `event_id` + semantic key, so a tight cadence is safe). Wired
   into `deploy-snapshot.sh` ahead of the snapshot regen step, so every
   public-snapshot refresh picks up new sessions without a long-lived
   daemon. Standalone `--once` loop is also available for tighter cadence.
2. ~~**Mojibake retroactive normalization**~~ ✅ done — historical events
   (`claude-events.jsonl` 88k, `codex-events.jsonl` 5k, `openclaw-events.jsonl`
   326, `opencode-events.jsonl` 73) all clean (`tracker/normalize-cp1251.py`
   idempotent, 0 fixes on latest pass). UTF-8 stdin enforcement landed on both
   Stop and SessionStart Claude hooks (`hooks/claude-track-calls.py`,
   `hooks/claude-session-start.py`); Codex/OpenClaw/OpenCode write paths
   already used `ensure_ascii=False` + `encoding="utf-8"`.

### P1 — Phase 1.5 Git bus (next major architectural piece)

Replace SMB inbox/outbox files with **Gitea Issues + labels + webhooks** as the
multi-agent transport. Key decision from the DeepSeek review: bus = **routing
layer, not data layer**.

- Issues carry only a pointer envelope: `task_id`, `target_host`,
  `callback_url`. Payload stays on the NAS share (the "no outbound" constraint
  is preserved).
- Sentinel-delimited envelope `<!-- neon-task:v1 sha256=... -->` so the body
  parses unambiguously.
- `schema_version`, `idempotency_key`, `lease_seconds` on every task.
- **Leases + heartbeats + execution IDs**, not assignee-locks (assignee-lock is
  a race condition on GitHub: multi-assignee allowed, no portable CAS).
- Gitea-only MVP. GitHub adapter behind the same interface afterwards (the
  trade-off: rate limits + abuse detection on GitHub means it is OK for solo
  dev with few tasks/hour, but Gitea/Forgejo is the recommended default).
- Realistic latency target: 5 s on webhook, 30 s on polling. The earlier "≤1 s"
  draft was unrealistic.
- Estimated scope: ~200 LOC for the routing-layer flavour, vs ~800 for a full
  payload-transport one.

Read-model on the same data: **in-memory SQLite cache** rebuilt from JSONL at
backend startup. SQLite is *never* primary store — append-only JSONL stays
canonical, SQLite gives query speed without persistence risk.

### P2 — Wow layer (12 modules)

Each is one self-contained file under `tools/`, removable, optional. The first
two cross-validated by both reviewers; #1 already shipped in v0.3.

| # | Feature | Origin | Status | What it does |
|---|---|---|---|---|
| 1 | **Hindsight Replay** | Both | ✅ shipped v0.3 | Closed session → second model "what would you have done differently?" — local corpus of lessons |
| 2 | **Consensus Matrix** | Codex | planned | Compares architect / dev / reviewer outputs — where they agreed, where they argued, whose concerns aged well |
| 3 | **Cost Regression Detector** | DeepSeek | planned | 7-day vs 30-day rolling per output token — catches silent vendor degradation ("Anthropic quietly lowered cache hit rate on Tuesday") |
| 4 | **Model Slippage Detector** | Codex | planned | Fingerprints task shape; alerts when the same task class starts costing more or needing retries |
| 5 | **Agent Reputation Ledger** | DeepSeek | planned | Per-agent scoring; the orchestrator re-sorts the DAG by cost-efficiency on its own |
| 6 | **Git-Diff-Aware Session Cost** | DeepSeek | planned | "15-line refactor cost $0.30. 200-line feature — $4.20. The session where $3 went to renaming a variable — here it is." |
| 7 | **Capability Cards** | Codex | planned | Per-role success metrics → `roles.suggested.toml` (never auto-apply) |
| 8 | **Slop Score** | Codex | planned | Heuristic on repeated n-grams, generic advice, low action density — when a model is "filling" rather than "solving" |
| 9 | **Reverse Autopilot** | Codex | planned | Mines repeated manual patterns → proposes scripts / manifest templates |
| 10 | **Disagreement-Driven Routing** | Codex | planned | Before running a manifest: "do we need a reviewer here based on historical risk for this task shape?" |
| 11 | **Auto-README Live Stats** | DeepSeek | planned | `<!-- START_STATS -->` markers in `README.md`, refreshed by cron — "last 7 days: 342 calls, $87 saved, Wednesday was the most productive day" |
| 12 | **Release Privacy Gate** | Codex | ✅ shipped v0.3 | Hard-fail scanner *before* `git push`, covers the easily-forgotten places: pycache, `.oss-backup/`, force-added ignored files |

Suggested order if you only do three: #1 (done), #3 Cost Regression Detector,
#5 Agent Reputation Ledger. Both touch the same `tracker/*` data, both are
"would-buy-it" features per the review pass.

### Phase 5 — 3D conversation viz

Vendored static copy under `dashboard/viz/` (not a submodule, not an iframe).

- Source export: `neon.graph.v1.json`, polling with ETag, **not WebSocket**.
- Refresh cadence: 30 s + CSS fade — "looks live" without a socket.
- Two graph modes: `orchestrate` (debug — DAG with edges as handoffs) and
  `sessions` (overview — vertices = sessions, edges = handoffs, color by agent).
- 2D fallback for non-WebGL clients (Linux WSL, low-end mobile).
- DeepSeek's 4-day breakdown (data export → static page → polling → 2D fallback)
  is the working plan when this phase opens.

Companion existing project: [`ai-conversation-viz`](https://github.com/andromanpro/ai-conversation-viz)
already renders the force-directed graph for individual transcripts. Phase 5
swaps the data source from raw transcripts to neon-legion snapshots.

### Phase 6+ — pet/sci-fi

- **6 — AR overlay**: Xreal/Quest cyberpunk HUD in the corner of vision, voice
  channel through OpenClaw bridge. No work scheduled.
- **7 — Productization**: competitor research, packaging (OSS / SaaS / hybrid),
  pricing for the AI productivity audience.

## Decisions worth keeping in mind

- **Boring before wow.** Before adding more wow-tier features (#2–#11), the P0
  list above wins ROI per hour. The triple-review concluded this twice
  independently: "If you do nothing else — write tests, fix mojibake, finish
  live tracking, ship demo, schema versions." Wow layer reads as serious only
  on a fundament that doesn't smell.
- **Gitea is staging, GitHub is production.** Both stay in sync via push from
  the human submitter. Direct GitHub-only orchestration is a P3 concern.
- **No `Co-Authored-By:` trailers.** AI tools are not authors. The human
  submitter owns the change.
- **Privacy by default.** Anything in `tracker/` is gitignored. Public mode
  (`--public`) salts session IDs, scrubs paths and emails, and applies a
  customer blocklist before writing the snapshot. Anything pushed to a public
  repo passes `tools/oss-sanitize.py --check` and `tools/release-gate.py`.
