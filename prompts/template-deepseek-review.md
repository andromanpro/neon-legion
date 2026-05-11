# Template: DeepSeek v4 final audit (via OpenCode + OpenRouter)

Use this template when running a third-opinion review via OpenCode after
Claude (architect) + Codex (developer) have already iterated. Fill in
`<TASK>` placeholders and pipe through `opencode run --model deepseek/deepseek-v4 ...`.

## Inputs to provide

- Working tree: `<project_root>` (read-only)
- Recent commits: `git log --oneline -20`
- Focus files: comma-separated paths or globs
- Risk profile: `security` | `privacy` | `money-math` | `architecture` | `release-readiness`
- Constraints: time-box (max 20 min equivalent reasoning)

## Prompt body

You are a third-opinion reviewer for an open-source project that is about
to be published to GitHub. Two prior reviewers (Claude as architect, Codex
as developer) have already iterated on this code. Your job is to find
**residual issues** they missed.

## Working directory

`<project_root>` (read-only). Read but do not modify.

## Risk profile

`<security | privacy | money-math | architecture | release-readiness>`

## What changed in the recent batch

```
<paste `git log --oneline -20` here>
```

## Focus files

```
<paste comma-separated paths or globs here>
```

## Review checklist — go in this order

### A. Privacy / public-publish blockers

1. Search tracked files for personal identifiers: IPs, hostnames, email
   addresses, usernames, absolute paths, customer/project codenames in any
   language (incl. Cyrillic inflections).
2. Verify `tools/oss-sanitize.py` rule set covers all real findings (suggest
   missing rules).
3. Check `.gitignore` covers: salt files, config.toml, CLAUDE.local.md,
   runtime JSONL, OAuth tokens, screenshots/.tmp.
4. Verify `--public` mode in `backend/server.py` actually scrubs all
   text fields exposed in `snapshot.json` (paths, emails, tokens, customer
   names, bidi/ZWJ control chars).
5. Confirm `tools/privacy-scan-snapshot.py` is wired into the publish
   workflow (referenced in README/SECURITY.md).

### B. Security correctness

1. Path traversal: every function that reads a file using a path from
   `tasks.json`, config, or external input — does it validate the path is
   under an allowed root?
2. Atomic writes: every file the dashboard reads — written via
   `tmp + os.replace` with a unique tmp name per writer?
3. Locks: every concurrent writer to `tasks.json` / events JSONL — does it
   acquire a lock, and refuse to proceed if lock acquisition fails (no
   silent unlocked writes)?
4. Subprocess invocations: prompt injection via filename/arg, shell=True
   misuse, encoding bugs on Windows (cp1251 mojibake).
5. Snapshot URL in WP page: same-origin or absolute? If absolute, mixed-
   content / CORS risk in preview.

### C. Money / math sanity

1. Cost calculation formulas: input + cached + output + reasoning tokens —
   correct rates per model? No double-counting of cache reads (which are
   outside the rate-limit budget for Anthropic Max)?
2. Productivity multiplier formula: `1 + saved/with-AI` — like-with-like?
   active hours and baselines computed over the SAME session subset?
3. Subscription pro-rate: `MONTHLY_USD * period_days / PRORATE_DAYS` —
   correct for partial months?
4. Period filter math: linear pro-rate vs real per-period — documented? UX
   expectations match?
5. Edge cases: empty coverage (0/N covered sessions), single-event session
   (active_hours ~ 0), future timestamps (clock skew).

### D. Release readiness

1. README.md: hero clear in 3 lines? Quick-start runs from scratch? Privacy
   section explicit? Comparison with alternatives present?
2. LICENSE / SECURITY.md / CONTRIBUTING.md present and consistent?
3. config.example.toml covers all hardcoded paths in the codebase?
4. Screenshots in `docs/screenshots/` referenced from README? Captions don't
   leak data?
5. Demo data: is there a sample `snapshot-demo.json` users can preview
   without installing?
6. Dependencies: stdlib-only claim true? Run `grep -rn "^import\|^from" tracker/ backend/`
   to verify.
7. Python version: declared minimum (3.11 / 3.12)? Syntax (`type | None`,
   match statements) consistent with that floor?

### E. Architecture sanity

1. Are the 4 provider event streams symmetric in schema? Any field one has
   but another lacks would surface as `0` or `undefined` in the dashboard.
2. `events_for_task_metrics` excludes Codex events — is the rationale
   documented (avoid double-counting Codex calls inside Claude orchestrator
   sessions)?
3. Snapshot JSON schema versioned? Adding a new field is backward-compatible
   (old WP page rendering doesn't crash on missing fields), but removing or
   renaming would break consumers — flag if any change is breaking.

## Output format

```markdown
# DeepSeek v4 audit report

Risk profile: <name>
Reviewer model: deepseek/deepseek-v4
Audit time: ~<minutes>

## A. Privacy
| # | Severity | File:line | Issue | Suggested fix |
|---|---|---|---|---|

## B. Security
| # | Severity | File:line | Issue | Suggested fix |

## C. Money / math
| # | Severity | File:line | Issue | Suggested fix |

## D. Release readiness
| # | Severity | File:line | Issue | Suggested fix |

## E. Architecture
| # | Severity | File:line | Issue | Suggested fix |

## Summary

- Issues found: HIGH=X MED=Y LOW=Z
- Blockers for public release: <list>
- Recommended fixes before push: <top 5>
- Verdict: ship-ready / needs fixes / blocked
```

Be terse. Cite file:line for every issue. If you check something and find
nothing — don't include it (no "all clean" noise).

## Hard constraints

- Read-only. Do not modify any file.
- Stay within 20 min equivalent reasoning. Stop and report partial findings
  if you hit the cap.
- Do not invent issues for narrative balance. Real findings only.
- If you disagree with a prior reviewer's decision (architect or developer),
  explicitly say so — that's the value of an independent third opinion.
