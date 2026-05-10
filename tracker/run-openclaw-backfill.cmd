@echo off
rem Import OpenClaw/OpenRouter local session usage into tracker\openclaw-events.jsonl.
rem Safe to run repeatedly: backfill-openclaw-sessions.py uses deterministic event_id.

cd /d "F:\WorkAI\multi-agent"
echo [%date% %time%] openclaw backfill start >> tracker\openclaw-backfill.log
py -3.14 tracker\backfill-openclaw-sessions.py >> tracker\openclaw-backfill.log 2>&1
py -3.14 backend\server.py --snapshot-once --public --snapshot-path "H:\wordpress-androman\wp-data\wp-content\uploads\multi-agent\snapshot.json" >> tracker\openclaw-backfill.log 2>&1
echo [%date% %time%] openclaw backfill end >> tracker\openclaw-backfill.log
