@echo off
REM Refresh FMP financial data for a ticker (or all tracked).
REM Pulls income statement / balance sheet / cash flow / segments / key_metrics
REM into data/historical/fmp/<TICKER>_*.json.
REM
REM Routed through run_python.bat for managed runtime, lock, and job health.
REM
REM Usage:
REM   refresh_fmp.bat NU           (one ticker, last 8 quarters)
REM   refresh_fmp.bat --all        (all tracked tickers)
REM   refresh_fmp.bat NU 20        (one ticker, last 20 quarters)

if "%~1"=="" (
  echo Usage: refresh_fmp.bat TICKER [LIMIT]
  echo        refresh_fmp.bat --all [LIMIT]
  exit /b 1
)

set "REPO_ROOT=%~dp0"
if "%REPO_ROOT:~-1%"=="\" set "REPO_ROOT=%REPO_ROOT:~0,-1%"

set LIMIT=%2
if "%LIMIT%"=="" set LIMIT=8

if "%~1"=="--all" (
  "%REPO_ROOT%\cron\run_python.bat" "refresh_fmp" "portfolio-db" "%REPO_ROOT%\execution\fetch_fmp_historical_data.py" --all --limit %LIMIT%
) else (
  "%REPO_ROOT%\cron\run_python.bat" "refresh_fmp" "portfolio-db" "%REPO_ROOT%\execution\fetch_fmp_historical_data.py" --ticker %1 --limit %LIMIT%
)
exit /b %ERRORLEVEL%
