@echo off
rem Estimate missing Claude session productivity/sentiment baselines via Codex oracle.
rem This DOES call AI. Keep LIMIT moderate. Scheduled task is disabled; run manually.

cd /d "%~dp0.."
set LIMIT=%~1
if "%LIMIT%"=="" set LIMIT=20
echo [%date% %time%] estimate backfill start limit=%LIMIT% >> tracker\estimate-backfill.log
py -3.14 -u tracker\run-recent-estimates.py --limit %LIMIT% >> tracker\estimate-backfill.log 2>&1
set EST_RC=%ERRORLEVEL%
echo [%date% %time%] estimate backfill end rc=%EST_RC% >> tracker\estimate-backfill.log
if "%EST_RC%"=="0" (
  py -3.14 backend\server.py --snapshot-once --public --snapshot-path "<wp_uploads>/multi-agent\snapshot.json" >> tracker\estimate-backfill.log 2>&1
)
exit /b %EST_RC%
