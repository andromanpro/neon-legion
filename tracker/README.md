# Claude Code tracker

`hooks/claude-track-calls.py` is a Claude Code `Stop` hook that reads the hook payload from stdin, finds the latest assistant transcript event with token usage, estimates API cost from a hardcoded pricing table, and appends one deduplicated JSONL event for the turn. It exits silently on missing or malformed inputs so it does not block Claude Code.

## Register the hook

Add this to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "py -3.14 F:/WorkAI/multi-agent/hooks/claude-track-calls.py"
          }
        ]
      }
    ]
  }
}
```

## Data file

Events are stored locally in:

```text
tracker/claude-events.jsonl
```

## Summary

Run the markdown summary from the project root:

```bash
py -3.14 tracker/summary.py
```

Optional period filters:

```bash
py -3.14 tracker/summary.py --days 7
py -3.14 tracker/summary.py --from 2026-05-01 --to 2026-05-09
```

## Verify the hook

After a Claude Code session stops, watch the event log:

```bash
tail -f tracker/claude-events.jsonl
```
