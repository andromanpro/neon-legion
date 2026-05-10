@echo off
rem Import OpenCode local SQLite usage into tracker\opencode-events.jsonl.
rem Safe to run repeatedly: backfill-opencode-sessions.py uses deterministic event_id.

cd /d "F:\WorkAI\multi-agent"
echo [%date% %time%] opencode backfill start >> tracker\opencode-backfill.log
py -3.14 tracker\backfill-opencode-sessions.py >> tracker\opencode-backfill.log 2>&1
py -3.14 backend\server.py --snapshot-once --snapshot-path "H:\wordpress-androman\wp-data\wp-content\uploads\multi-agent\snapshot.json" >> tracker\opencode-backfill.log 2>&1
echo [%date% %time%] opencode backfill end >> tracker\opencode-backfill.log
