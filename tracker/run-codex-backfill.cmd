@echo off
rem Import Codex Desktop/TUI local session usage into tracker\codex-events.jsonl.
rem Safe to run repeatedly: backfill-codex-sessions.py uses deterministic event_id.

cd /d "%~dp0.."
echo [%date% %time%] codex backfill start >> tracker\codex-backfill.log
py -3.14 tracker\backfill-codex-sessions.py >> tracker\codex-backfill.log 2>&1
echo [%date% %time%] codex backfill end >> tracker\codex-backfill.log
