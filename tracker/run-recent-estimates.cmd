@echo off
rem Daily catchup for Phase 1.3 oracle estimation — runs estimate-task.py for
rem the 5 most recent sessions without ai_baseline_hours. Codex-quota friendly.

cd /d "%~dp0.."
"C:\Windows\py.exe" -3.14 tracker\run-recent-estimates.py --limit 5 ^
  >> tracker\estimates.log 2>&1
