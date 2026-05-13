# Phase 1.5 — Git bus

> **Status**: design, pre-implementation. Milestone
> [v0.5](http://192.168.1.130:3000/androman/neon-legion/milestones/) on Gitea.
> Triple-reviewed (Claude + Codex + DeepSeek) — the residual issues from that
> pass are captured under "decisions" below.

## What replaces what

Today's cross-machine coordination is in
[`tools/openclaw-codex-bridge.py`](../tools/openclaw-codex-bridge.py): an SMB
share with `inbox/`, `outbox/`, `jobs/` directories that one side polls and
the other side writes to. It works, but it has three real costs:

| Pain | SMB-inbox today | Git bus tomorrow |
|---|---|---|
| Audit trail | files get archived → diff is hard to follow | issue timeline is the trail |
| Replay / debugging | no per-task history | `gh issue view N` / Gitea UI |
| Dead-worker recovery | manual rm of stale inbox file | lease + heartbeat |
| Multi-host coordination | single share | any host on the LAN with Gitea credentials |

## What the bus IS

A **routing layer**, not a data layer:

- Issues carry a tiny pointer envelope (`task_id`, `target_host`,
  `callback_url`, `payload_ref`, `sha256`, `lease_seconds`).
- Big payloads stay on the NAS share — bus just points at them.
- Labels are the state machine.
- The no-outbound-network constraint is preserved.

## What the bus is NOT

- Not a message queue (no FIFO order guarantees beyond "create order").
- Not a data store (issue body is metadata; everything ≥1 KB goes to a
  payload file).
- Not a webhook gateway (webhooks accelerate polling, they don't replace it).
- Not for high-throughput. Realistic target: 5 s on webhook, 30 s on polling
  — well under the rate-limit floor on either Gitea or GitHub.

## Envelope format

Issue body, single comment, or update — always wrapped in a sentinel so the
parser is unambiguous:

```
<!-- neon-task:v1 sha256=abcdef… -->
{
  "schema_version": 1,
  "task_id": "ulid:01HQZ…",
  "kind": "codex_exec",
  "target_host": "win-claude-01",
  "payload_ref": "smb://nas/neon-bus/payloads/01HQZ…json",
  "payload_sha256": "…",
  "lease_seconds": 600,
  "idempotency_key": "openclaw-2026-05-13T12:30Z-codex-exec-7",
  "created_at": "2026-05-13T12:30:00Z"
}
<!-- /neon-task:v1 -->
```

The `sha256` in the opening sentinel is over the JSON body, so a worker can
detect comment-vandalism / wrong-issue bleed-over in one comparison.

## Lifecycle

Labels drive the state machine. One label per state — workers transition by
**adding** the new label and removing the previous one in a single API call
where possible.

```
neon:state/pending     ← created
neon:state/claimed     ← worker acquired the lease (also adds neon:lease/<exec_id>)
neon:state/in-progress ← worker posted "running" heartbeat
neon:state/done        ← worker posted result envelope + closed the issue
neon:state/failed      ← worker posted failure envelope + closed the issue
neon:state/expired     ← lease elapsed without heartbeat (reaper re-opens)
```

Identity (the **execution ID**) lives on the *lease* label, not the
*assignee*. Multi-assignee on GitHub means assignee-lock is a race condition
with no portable CAS — leases via labels avoid it. Each claim writes
`neon:lease/<worker_host>/<exec_id>` so a reaper can spot it.

## Heartbeat

`in-progress` workers post a one-line comment every `lease_seconds / 3`:

```
<!-- neon-hb:v1 exec=… ts=… -->
```

A reaper polling every 60 s flips an issue to `expired` if no heartbeat is
within `lease_seconds`. The next polling worker can claim it (idempotency
key + payload_sha256 protect against double-run side effects).

## Idempotency

Every task carries an `idempotency_key`. The worker SHOULD record the
(idempotency_key → result) mapping in its local read-model cache so a
re-issue from the same originator (e.g. dropped network ack) returns the
prior result instead of re-running.

## Read-model

The backend keeps an in-memory SQLite cache built from the canonical JSONL
event store at startup. SQLite is **never** primary storage — it is a query
accelerator for the dashboard. Reasons:

- Append-only JSONL stays canonical.
- SQLite rebuilt on every backend start — no migration risk.
- Bus events (`tracker/bus-events.jsonl`) follow the same shape as the other
  provider event streams (provider="bus", new event kinds).

## Scope (MVP)

In: Gitea-only adapter. Out: GitHub adapter (next milestone, same interface).

| # | Issue | LOC | Depends on |
|---|---|---|---|
| 1 | Envelope library (parse/serialize + sentinel) | ~50 | — |
| 2 | Gitea client (issues + labels + comments) | ~80 | — |
| 3 | Worker loop (poll, claim, lease, heartbeat) | ~100 | #1, #2 |
| 4 | Reaper (expire stale leases) | ~30 | #2 |
| 5 | Adapter: port `openclaw-codex-bridge` actions to the bus | ~50 | #1, #2, #3 |
| 6 | SQLite read-model rebuilt at backend startup | ~80 | bus-events JSONL |

Estimated total ~390 LOC, slightly above the ~200 LOC the DeepSeek pass
estimated. The extra ~190 LOC is heartbeat + reaper + read-model, which
were called out as P1 in the review.

## Decisions worth re-stating

- **Bus is routing, not data**. Big payloads stay on NAS share, bus points
  at them. This keeps the no-outbound-network constraint.
- **Labels for state, not assignees**. Assignees on GitHub allow multi-set,
  no CAS — race condition. Labels with leases are portable.
- **Sentinel-wrapped envelopes**. Avoid ambiguous comment vs body parsing.
  `<!-- neon-task:v1 sha256=… -->` is the only thing the parser looks for.
- **Polling 30 s + webhook 5 s**. The earlier "≤1 s" draft was unrealistic.
- **SQLite is a read-model**, never the source of truth.
- **GitHub adapter is deferred**. Solo dev with few tasks/hour fits within
  GitHub abuse limits, but Gitea/Forgejo is the safe default.

## What this replaces in practice

Once Phase 1.5 lands, `tools/openclaw-codex-bridge.py` becomes a thin
adapter: read the action from the bus, dispatch to the existing handlers
(`action_list`, `action_read`, `action_rg`, `action_codex_exec`), post the
result envelope back to the bus. The actions themselves don't change.

## What is explicitly out of scope

- High-throughput task fan-out (not a queue replacement).
- Multi-tenancy / per-tenant rate limits.
- Encryption at rest of payloads (NAS share is already inside the LAN
  trust boundary; if you need this, encrypt the share, not the bus).
- Cross-cluster federation. One Gitea, one bus, one trust zone.

## Trust model

Recorded after the DeepSeek pass surfaced two HIGH findings (#A1 path
traversal, #B1 claim race) that are exploitable only by trusted-side
adversaries. Documenting the assumptions explicitly so a future operator
doesn't deploy the bus across a wider trust boundary than it can carry.

1. **Bus tasks are assumed to originate from trusted issuers.** The
   envelope `payload_sha256` guards against transport corruption and
   comment vandalism *within* a task — but anyone with Gitea write
   access can mint a fresh envelope pointing at any payload they
   control. There is no end-to-end signature beyond the sha256 over
   the envelope body itself. Operating outside a single-user / single-
   admin Gitea instance requires adding signatures or moving payload
   validation upstream.

2. **Payload reads are confined to `BUS_PAYLOAD_ROOT`.** The worker
   refuses to read any path that is not under the value of the
   `BUS_PAYLOAD_ROOT` environment variable (which must be set
   explicitly — there is no default). This prevents `payload_ref`
   from being used as an arbitrary-file-read oracle. The sha256
   mismatch failure result no longer echoes the actual hash, removing
   the file-content fingerprint leak.

3. **CAS via claim-comment monotonic ID** (was: "One worker per
   `neon:target/<host>` label"). After the label PATCH, the worker posts
   a `neon-claim:v1` comment, re-fetches comments, and verifies the
   highest-ID claim comment is its own `exec_id`. Gitea assigns
   monotonic IDs to issue comments, so even simultaneous claim-POSTs
   resolve to exactly one canonical winner. Lost claims do not revert
   the label; the reaper's lease-expiry path picks up orphan `claimed`
   state.

4. **`expired` is terminal AND closed.** The reaper passes
   `state="closed"` alongside the label swap so terminal issues do
   not accumulate in `list_issues(state="open")` queries.

5. **Finalise transitions are non-fatal.** If the very last
   `update_issue` to `done`/`failed` fails (network blip, 5xx), the
   result envelope is still posted to the issue; the worker logs an
   "orphaned" warning and lets the reaper expire the leftover state.
   No double-posting under contradictory reasons.
