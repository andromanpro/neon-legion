# Claude Code hooks

Register the hooks in `~/.claude/settings.json` for the checkout that should be
tracked.

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "py -3.14 \"<project_root>/hooks/claude-session-start.py\""
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
            "command": "py -3.14 \"<project_root>/hooks/claude-track-calls.py\""
          }
        ]
      }
    ]
  }
}
```

Replace `<project_root>` with this repo's absolute path. Keep the quotes around
the command path on Windows.

## `claude-track-calls.py`

`claude-track-calls.py` is the `Stop` hook. It reads the Claude Code hook JSON
from stdin, opens the transcript path from that payload, finds the latest
assistant event with usage, estimates API cost from the local pricing table,
and appends one deduplicated row to `tracker/claude-events.jsonl`. Missing
payload fields, malformed JSON, unknown models, absent transcripts, duplicate
message UUIDs, and other hook failures exit silently with code `0` so Claude
Code is not blocked.

The hook also writes `tracker/.last-uuids.json` and uses
`tracker/.claude-events.lock` while appending. It stores metadata and token
usage, not prompt or assistant text.

## `claude-session-start.py`

`claude-session-start.py` is the `SessionStart` hook. It reads recent Claude
sessions from `tracker/claude-events.jsonl`, skips the newly opened session,
finds sessions from the last 24 hours that are missing from
`tracker/tasks.json`, and starts detached `tracker/estimate-task.py` workers.
The hook dispatches at most five estimators per fire and exits quickly.

Estimator logs and inflight locks live under `tracker/.estimation-logs/`.
When a transcript cannot be found, the hook records a low-confidence manual
review entry instead of retrying forever.

## Encoding

`claude-track-calls.py` forces UTF-8 on stdin, stdout, and stderr. Bug #20:
Cyrillic paths in `cwd` arrive through Windows console defaults as mojibake
unless the hook decodes stdin as UTF-8 before writing JSONL.

`claude-session-start.py` also reconfigures stdout and stderr to UTF-8. It
does not currently reconfigure stdin because it reads only the JSON session id
from the hook payload.

## Per-project overrides

Machine-specific hook commands belong in Claude Code settings, not in the
committed hook scripts. If one checkout needs different paths or extra flags,
put that override in the checkout's `.claude/settings.local.json` and keep the
file untracked. Use `.claude/settings.json` only for a project-wide setting
that is safe to commit.

Runtime path changes belong in ignored `config.toml` when the code supports a
config key. Existing hook-related config is documented in
[config.example.toml](../config.example.toml).
