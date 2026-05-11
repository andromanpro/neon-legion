# Security policy

## What this project handles

`ai-ops-journal` (codename `multi-agent`) is a **local-first personal AI usage
tracker**. It captures the following sensitive data classes:

- AI conversation transcripts (under `~/.claude/projects/`, `~/.codex/`, etc.)
- API call metadata: token counts, model names, costs, timestamps, working
  directories
- Task estimates and sentiment markers (frustration / profanity counts) per
  session, in `tracker/tasks.json`
- Snapshot pipeline writes aggregated metrics to a configured uploads
  directory

By design **no outbound network calls** happen from the backend writer. The
only optional outbound is the public snapshot HTTP server, which lives on
`127.0.0.1` by default.

## What is safe to publish

The `--public` mode (`backend/server.py --public`) produces a sanitized
snapshot:

- `session_id` is hashed with a local 32-byte salt (`blake2b`, 8 hex chars).
  Identical sessions in the same snapshot remain grouped; the original UUID
  is unrecoverable without the salt.
- `desc` and `top_session` text fields are scrubbed of:
  - Absolute Windows/Unix paths
  - Email addresses
  - Token-shaped strings (`sk_`, `ghp_`, `gho_`, `github_pat_`)
  - Customer/project names listed in `--customers-blocklist`
- Salt file (`~/.multi-agent-snapshot-salt`) MUST stay private. Anyone with
  the salt can re-link a public snapshot's hashes to historical hashes.

Always run `tools/privacy-scan-snapshot.py <path>` on a snapshot before
publishing it.

## What you should NOT commit

- `tracker/*.jsonl` — raw event streams
- `tracker/tasks.json` — task descriptions with full identifiers
- `~/.multi-agent-snapshot-salt` — salt file (lives in your home dir; default
  `.gitignore` does not cover home, only the project's tracker dir)
- `CLAUDE.local.md` — your local agent notes
- `config.toml` — environment-specific paths
- Any file in `tracker/private/` if you create one for blocklists, custom
  prompts, etc.

The default `.gitignore` covers the first three categories inside the project
tree.

## Reporting vulnerabilities

If you find a privacy leak, scrub bypass, or other security issue, please:

1. **Preferred — GitHub Security Advisories.** Go to the repo's Security tab
   → "Report a vulnerability". Private, indexed only after disclosure, lets
   us collaborate on the fix in a draft advisory.
2. **Fallback — email** `andromanpro@gmail.com` with subject
   `ai-ops-journal SECURITY:` if you can't use GitHub for some reason.
3. **Never** open a public GitHub issue for a security topic — public issues
   are crawled and indexed within minutes.

Include in your report: a minimal reproduction, the file/function affected,
your assessment of severity, and any proposed mitigation.

You can expect a response within 7 days. If the issue requires a coordinated
fix, we'll agree on disclosure timing together.

## Threat model — what we defend against

- **Accidental publication of personal data** — sanitized snapshot + scrubbing
- **Path traversal via tampered `tasks.json`** — `_profanity_since` validates
  transcript paths are under `~/.claude/projects/` and have a `.jsonl` suffix
- **Cross-site fetch of the snapshot from a different origin** — same-origin
  relative URL in the WordPress page; CORS is not enabled by default on the
  WP host
- **Replay of snapshot data after a public snapshot leak** — salt rotation
  invalidates historical hashes; rotate the salt if a snapshot leaks

## Threat model — what we do NOT defend against

- **Local malware** — anything with read access to your home dir can read
  `tasks.json` and transcripts
- **Compromised AI provider** — if Anthropic/OpenAI keys leak from their side,
  the snapshot pipeline cannot mitigate
- **Cloud hosting of WP page** — if you serve the snapshot from a public
  WordPress instance, anyone with the URL can read the JSON. Use `--public`
  there
- **Active network attacker on LAN** — the backend listens on `127.0.0.1`
  only; if you bind to `0.0.0.0`, LAN attackers can fetch the snapshot
- **Restoring `tasks.json` from untrusted backup** — a tampered file can
  inject malicious text into `brief_description` fields. The `--public`
  scrub layer catches paths/emails/tokens, but a sufficiently crafted string
  may evade matching. Treat `tasks.json` like any other application state:
  back it up from trusted sources only.
- **`git add -f tracker/tasks.json`** — `.gitignore` does not prevent forced
  staging. If you bypass the ignore rule, the full task store (with raw
  descriptions and transcript paths) goes into git history. Use the
  hardening checklist before every release tag.

## Hardening checklist before going public

- [ ] Snapshot generated with `--public` flag
- [ ] `--salt-file` points to a local-only, gitignored path
- [ ] `--customers-blocklist` populated with all sensitive project names
- [ ] `tools/privacy-scan-snapshot.py` reports zero hits
- [ ] WordPress page serves snapshot from same origin
- [ ] No raw `tracker/*.jsonl` committed
- [ ] `CLAUDE.local.md` and `config.toml` are gitignored
