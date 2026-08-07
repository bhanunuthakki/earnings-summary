@echo off
REM Full per-ticker refresh: FMP data + transcripts + IR document processing
REM + KPI extraction + SayDo pairs + report rebuild.
REM
REM Delegates to refresh_dispatch.py --mode full, routed through run_python.bat
REM for managed Python runtime, serialized portfolio writes, and JSON job health.
REM
REM Usage:
REM   full_refresh.bat NU

if "%~1"=="" (
  echo Usage: full_refresh.bat TICKER
  exit /b 1
)

set "REPO_ROOT=%~dp0"
if "%REPO_ROOT:~-1%"=="\" set "REPO_ROOT=%REPO_ROOT:~0,-1%"

"%REPO_ROOT%\cron\run_python.bat" "%REPO_ROOT%\execution\refresh_dispatch.py" --ticker %1 --mode full
exit /b %ERRORLEVEL%
