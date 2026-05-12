# Sample Orchestrator Run

This directory is a sanitized copy of one local `tools/orchestrate.py` run for readers who want the file contract without running AI CLIs.
The original run used human-relay adapters for architect, developer, and reviewer.
That keeps the sample deterministic and free of prompt or model transcript data.

`state.json` is the orchestrator ledger.
It has `schema_version`, `run_id`, lifecycle timestamps, planned `flow`, and steps.
Each step records the role name, status, output path, and adapter result.
Published paths are placeholders under `<project_root>/orchestrate-runs/<run_id>`.
In a live run those paths point at the ignored `orchestrate-runs/` directory.

`manifest.used.toml` is the task definition copied into the run directory.
It shows the title, description, role flow, context files, and acceptance criteria.
`roles.used.toml` is the exact role table used when the run started.
Keeping both files makes a run auditable even if repo defaults change later.

`01-architect.md`, `02-developer.md`, and `03-reviewer.md` are role deliverables; real runs can contain specs, implementation notes, or audits.
Downstream roles receive earlier deliverables as context.
That handoff is the main behavior this sample demonstrates.

To replay the pattern, create a manifest with `[task]`, `flow`, and criteria.
Copy `roles.example.toml` to local `roles.toml` and choose real invocations.
Run `python tools/orchestrate.py run your-manifest.toml`.
Use `python tools/orchestrate.py list` to find the new run id, then `python tools/orchestrate.py status <run_id>` to inspect progress.
If a human-relay step pauses, write the requested markdown file and resume.
Resume with `python tools/orchestrate.py resume <run_id>`.

Before publishing a run, copy only needed files and scrub local paths.
Run `python tools/oss-sanitize.py --check --globs docs/sample-run/**` and `python tools/release-gate.py`.
