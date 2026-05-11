# Contributing

Thanks for considering a contribution. This project started as a personal AI
usage tracker for one user; if you find it useful, we want to keep it lean
and predictable for everyone else too.

## Quick orientation

- **Architecture**: see `README.md` "Architecture" and `docs/architecture.md`.
- **Data model**: each AI provider has its own JSONL events file under
  `tracker/`. `summary.py` reads them at request time, no migration needed
  when adding a new provider.
- **Roles**: this is a multi-agent SDLC project — humans write specs in
  `prompts/*.md`, Codex CLI implements, Claude Code reviews. You are welcome
  to follow the same loop or work directly.

## Adding a new AI provider tracker

Use OpenClaw / OpenCode / Codex as templates. Steps:

1. **Wrapper or hook** that writes JSONL events to
   `tracker/<provider>-events.jsonl` with this schema:
   ```json
   {
     "ts": "ISO8601",
     "session_id": "stable identifier",
     "model": "model id",
     "input_tokens": 0,
     "output_tokens": 0,
     "cached_input_tokens": 0,
     "reasoning_tokens": 0,
     "cost_estimate_usd": 0.0,
     "provider": "openai" | "anthropic" | "<your provider>",
     "working_dir": "optional"
   }
   ```
2. **Backfill script** in `tracker/backfill-<provider>-sessions.py` for
   historical data import.
3. **Summary integration** — add a `read_<provider>_events()` function in
   `tracker/summary.py` and include it in `read_events()`.
4. **Provider mapping** — extend `PROVIDER_KEYS` in `summary.py` so the
   snapshot's `providers` block lists your provider.

There is no central registry yet — each provider adds itself. If the count
grows past 4-5 providers, we'll refactor to a plugin layer.

## Adding a third-opinion reviewer (DeepSeek, GLM, etc)

The default review loop is Claude (architect) + Codex (developer). For
high-risk changes (security, money calculations, privacy) you can route a
third opinion through OpenCode + OpenRouter. See
`prompts/template-deepseek-review.md` for the audit prompt template.

## Code style

- Python 3.11+ syntax (`type | None`, `match`, walrus)
- No third-party dependencies in `tracker/` or `backend/`. The whole pipeline
  must run on stdlib only.
- `tools/` may depend on third-party libraries if there is no stdlib path —
  document them in the file's docstring.
- UTF-8 source files, no BOM.
- Atomic writes (`tmp + os.replace`) for any file that the dashboard reads.

## Testing

There is no formal test suite yet. Before submitting a change:

1. `python -m py_compile <touched files>` — must pass.
2. `python tracker/summary.py --days 30` — must run without errors against
   a representative `tracker/claude-events.jsonl`.
3. `python backend/server.py --snapshot-once --snapshot-path /tmp/test.json` —
   must produce a valid JSON snapshot.
4. If you touch the dashboard, render it locally and verify period filter +
   language switch still work.

## Commit messages

- Imperative present tense: `fix(backend): handle naive timestamps`.
- Conventional commits prefix: `feat`, `fix`, `docs`, `chore`, `refactor`,
  `perf`, `test`.
- Scope is one of: `tracker`, `backend`, `dashboard`, `tools`, `prompts`,
  `docs`.
- **Do not add `Co-Authored-By:` lines for AI tools.** The human submitter is
  the author. AI-assisted contributions are still 100% the contributor's
  responsibility.

## Pull requests

- One topic per PR. If you fix a typo and rewrite the productivity math in
  the same patch, split them.
- Link the relevant `prompts/*.md` if your change implements a phase.
- For privacy-sensitive changes, run `tools/privacy-scan-snapshot.py` on a
  fresh snapshot and paste output in the PR description.
- Maintainer review uses the Codex + Claude loop. Expect comments suggesting
  rewrites — they are not personal, they are part of the workflow.

## What is out of scope

- A SaaS hosted version. This is a local-first tool by design.
- New AI providers without local CLI/API access (the tracker needs to see the
  raw events).
- UI rewrites in frameworks (React, Vue). The dashboard is intentionally
  vanilla HTML + inline JS + CSS, single file, no build step.
- Cloud sync of `tasks.json`. Sentiment data and human corrections stay on
  your machine.

## License

By contributing, you agree your contribution is licensed under the project's
MIT license.
