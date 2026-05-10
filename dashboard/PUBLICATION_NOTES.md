# Multi-Agent Tracker — publication notes

Status: prepared for review, not published.
Date: 2026-05-10.

## Market scan

There are already serious tools in the "AI coding usage tracker" space:

- `ccusage` tracks Claude Code usage and also has Codex/OpenCode companion analyzers:
  https://github.com/ryoppippi/ccusage
- `claude-usage` is a local Claude Code dashboard with API-price estimates and subscription caveats:
  https://github.com/phuryn/claude-usage
- `CodexBar` is a macOS menu-bar quota monitor across many coding providers:
  https://github.com/steipete/CodexBar
- `opencode-quota` adds quota/tokens panels and slash commands directly inside OpenCode:
  https://github.com/slkiser/opencode-quota
- OpenRouter documents built-in usage accounting with token counts, cost, cache, and reasoning details:
  https://openrouter.ai/docs/guides/administration/usage-accounting

Conclusion: this should not be positioned as "the first token tracker". The stronger angle is a public case study / personal ops dashboard: multi-agent workflow telemetry, subscription ROI, productivity baselines, and stress signals in one WordPress-ready snapshot.

## Differentiators

- Tracks a mixed human workflow: Claude Code, Codex, OpenClaw, OpenCode.
- Separates "who thought" from "who launched another agent".
- Shows subscription ROI as API equivalent minus prorated subscriptions.
- Keeps productivity/sentiment on Claude orchestrator sessions to avoid double-counting delegated work.
- WordPress page is publishable as a static-ish snapshot, not only a local TUI.

## Privacy checklist

Before public publishing:

1. Generate snapshot with public mode:

   ```cmd
   py -3.14 backend\server.py --snapshot-once --public --snapshot-path "H:\wordpress-androman\wp-data\wp-content\uploads\multi-agent\snapshot.json"
   ```

2. Scan snapshot for local paths, usernames, IDs, and private workspace names:

   ```cmd
   py -3.14 tools\privacy-scan-snapshot.py
   ```

3. Manually review:

   - `today.top_session`
   - `sessions[].desc`
   - `providers[].origins[]`
   - WordPress rendered HTML

4. Do not publish prompts, responses, transcript paths, raw session ids, Telegram ids, API keys, emails, or local filesystem paths.

## Current publication blockers

- Public snapshot should be regenerated with `--public` immediately before publishing.
- Session descriptions are task summaries; they are not personal data after public scrub, but still deserve a human read-through.
- The page explains methodology now, but the article/post around it should say clearly that "saved hours" is an estimate, not laboratory-grade productivity proof.
