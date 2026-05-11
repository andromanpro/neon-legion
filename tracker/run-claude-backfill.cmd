@echo off
rem Import recent Claude Code transcript usage into tracker\claude-events.jsonl.
rem Safe to run repeatedly: backfill.py deduplicates by session_id + message_uuid.

cd /d "%~dp0.."
for /f %%I in ('py -3.14 -c "from datetime import date,timedelta; print((date.today()-timedelta(days=14)).isoformat())"') do set FROM_DATE=%%I
echo [%date% %time%] claude backfill start from %FROM_DATE% >> tracker\claude-backfill.log
py -3.14 tracker\backfill.py --from-date %FROM_DATE% >> tracker\claude-backfill.log 2>&1
echo [%date% %time%] claude backfill end >> tracker\claude-backfill.log
