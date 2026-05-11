@echo off
rem Self-locating wrapper — works regardless of where the repo is cloned.
rem %~dp0 expands to this script's directory (with trailing backslash).
py -3.14 "%~dp0codex-track.py" %*
