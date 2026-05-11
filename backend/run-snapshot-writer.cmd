@echo off
rem Snapshot writer — runs the backend with periodic snapshot writes.
rem Self-locating via %~dp0 (this script's directory). Override the snapshot
rem destination by setting MA_SNAPSHOT_PATH in your environment, or copy this
rem file to your own .cmd and edit.

setlocal
set "SCRIPT_DIR=%~dp0"
set "PROJECT_ROOT=%SCRIPT_DIR%.."

if "%MA_SNAPSHOT_PATH%"=="" set "MA_SNAPSHOT_PATH=%PROJECT_ROOT%\dashboard\snapshot.json"

cd /d "%PROJECT_ROOT%"
py -3.14 backend\server.py ^
  --port 8089 ^
  --host 127.0.0.1 ^
  --snapshot-path "%MA_SNAPSHOT_PATH%" ^
  --snapshot-interval 900 ^
  --snapshot-days 62 ^
  --public ^
  >> backend\server.log 2>&1
