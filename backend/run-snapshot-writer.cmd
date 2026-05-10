@echo off
rem Multi-agent snapshot writer. Run manually when live snapshots are needed.
rem Historical Task Scheduler setup is disabled; this process stays alive until stopped.

cd /d "F:\WorkAI\multi-agent"
py -3.14 backend\server.py ^
  --port 8089 ^
  --host 127.0.0.1 ^
  --snapshot-path "H:\wordpress-androman\wp-data\wp-content\uploads\multi-agent\snapshot.json" ^
  --snapshot-interval 900 ^
  --snapshot-days 62 ^
  >> backend\server.log 2>&1
