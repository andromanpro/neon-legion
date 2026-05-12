@echo off
setlocal

python tools\gen-fake-events.py --days 7
if errorlevel 1 exit /b %errorlevel%

python backend\server.py --snapshot-once --snapshot-path dashboard\snapshot.json
if errorlevel 1 exit /b %errorlevel%

echo Open dashboard\index.html in your browser.
