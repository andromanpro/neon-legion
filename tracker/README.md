# Claude Code tracker

`hooks/claude-track-calls.py` is a Claude Code `Stop` hook that reads the hook payload from stdin, finds the latest assistant transcript event with token usage, estimates API cost from a hardcoded pricing table, and appends one deduplicated JSONL event for the turn. It exits silently on missing or malformed inputs so it does not block Claude Code.

`hooks/claude-session-start.py` is a `SessionStart` hook for Phase 1.3. On each new Claude Code session it looks for recent sessions in `tracker/claude-events.jsonl` that do not yet have complexity estimates in `tracker/tasks.json`, then starts detached estimator workers. The hook exits immediately; real estimation runs in the background through `tracker/estimate-task.py`.

## Register the hooks

Add this to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "py -3.14 F:/WorkAI/multi-agent/hooks/claude-session-start.py"
          }
        ]
      }
    ],
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

## Data files

Events are stored locally in:

```text
tracker/claude-events.jsonl
```

Task complexity estimates are stored locally in:

```text
tracker/tasks.json
```

### tasks.json fields (Phase 1.4 extended)

- `profanity_count` — int, regex match count of swear words in user messages
- `frustration_score` — float 0-1, AI assessment
- `appreciation_score` — float 0-1, AI assessment
- `mood_arc` — string (max 30 chars), brief emotional trajectory
- `sentiment_intensity` — "low"|"medium"|"high"

Estimator logs are written to:

```text
tracker/.estimation-logs/
```

These runtime files are ignored by git.

## Complexity workflow

1. `Stop` records Claude Code usage events in `tracker/claude-events.jsonl`.
2. The next `SessionStart` finds sessions from the last 24 hours that are missing from `tracker/tasks.json`.
3. For each pending session with a transcript, the hook launches `tracker/estimate-task.py` in the background.
4. The estimator builds a truncated transcript summary and calls:

```bash
claude -p --bare --output-format json
```

5. The estimator writes `ai_baseline_hours`, description, confidence, and review flags into `tracker/tasks.json`.
6. Manual corrections use `human_corrected_hours`, which takes priority over the AI baseline in summaries.

The oracle prompt lives in `tracker/oracle-prompt.txt` so the estimation rubric can be edited without touching Python code.

## Manual task notes

Set or update a manual override:

```bash
py -3.14 tracker/note-task.py --session-id <session_id> --hours 4.5 --description "implemented tracking hook"
```

List all task entries:

```bash
py -3.14 tracker/note-task.py --list
```

List sessions in the event log that still have no task entry:

```bash
py -3.14 tracker/note-task.py --pending
```

Delete an entry so the next `SessionStart` hook can estimate it again:

```bash
py -3.14 tracker/note-task.py --reestimate <session_id>
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

When the selected period contains task estimates, the summary adds a Phase 1.3 Productivity block with wall-clock AI time, estimated without-AI hours, saved hours, and multiplier.

## Verify the hooks

After a Claude Code session stops, watch the event log:

```bash
tail -f tracker/claude-events.jsonl
```

After starting a later Claude Code session, watch estimator logs:

```bash
ls tracker/.estimation-logs/
```
