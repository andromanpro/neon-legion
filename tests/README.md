# Tests

Run the suite from the repo root with `python -m unittest discover -s tests -v`.

The suite is stdlib `unittest`. It uses `tempfile` fixtures for isolated
worktrees and mocks subprocess calls instead of launching Claude, Codex,
OpenCode, git, or release tooling.

## Coverage

| File | Covers |
|---|---|
| `test_orchestrate.py` | Role loading, manifest validation, context path safety, DAG ordering, resume, human relay, atomic state writes. |
| `test_role_invoke.py` | CLI adapter result shapes, Claude JSON unwrap, ANSI stripping, Codex temp output replacement, human relay, unsupported invocations. |
| `test_hindsight.py` | Critic selection, pending-run listing, dry run behavior, event shape, manifest fallback, trivial deliverable skips. |
| `test_oss_sanitize.py` | Generic/private scrub rules, check/apply/diff modes, backups, keep blocks, default exclusions. |

## Conventions

New tests go next to the code they exercise by behavior, not by release phase.
Use temporary directories and patch module-level `PROJECT_ROOT` values when a
tool normally touches repo state.

Mock subprocess boundaries. Tests should not require installed AI CLIs, network
access, real tracker data, or a user-specific config file.

For a new tool in `tools/`, add focused tests to an existing file when the
behavior is adjacent. Create a new `test_<tool_name>.py` when the tool has its
own CLI contract, file format, or failure modes.

Keep fixtures disposable. If a test needs tracker logs, run directories, or
config files, create them under `tempfile.mkdtemp()` and remove them in
`tearDown()`.
