@echo off
REM Backfill missing earnings-call transcripts from the free-aggregator chain
REM (roic.ai -> stockanalysis.com -> tickertrends.io), then register them into
REM the transcripts table.
REM
REM Routed through run_python.bat for managed runtime, lock, and job health.
REM
REM Usage:
REM   refresh_transcripts.bat NU      (one ticker, last 6 fiscal quarters)
REM   refresh_transcripts.bat --all   (all active tickers, default 6 quarters)

if "%~1"=="" (
  echo Usage: refresh_transcripts.bat TICKER
  echo        refresh_transcripts.bat --all
  exit /b 1
)

set "REPO_ROOT=%~dp0"
if "%REPO_ROOT:~-1%"=="\" set "REPO_ROOT=%REPO_ROOT:~0,-1%"

if "%~1"=="--all" (
  "%REPO_ROOT%\cron\run_python.bat" "refresh_transcripts" "portfolio-db" "%REPO_ROOT%\execution\backfill_transcripts.py"
) else (
  "%REPO_ROOT%\cron\run_python.bat" "refresh_transcripts" "portfolio-db" "%REPO_ROOT%\execution\backfill_transcripts.py" --ticker %1
)
exit /b %ERRORLEVEL%
