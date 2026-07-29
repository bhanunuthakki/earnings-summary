@echo off
REM Daily 05:45 — two steps, one log:
REM   1. Refresh data/historical/fmp/<TICKER>_earnings_calendar.json for every
REM      active ticker. While the FMP subscription is lapsed the stable
REM      /earnings endpoint 402s and this step fails — that is expected; the
REM      cache simply stays at its last good state.
REM   2. Materialize the canonical expected_earnings table from the
REM      FMP-cache -> yfinance preference stack (refresh_expected_earnings.py).
REM      Runs even when step 1 fails, so the home strip / cockpit / portfolio-
REM      tracker keep getting dates. Runs before backfill_earnings_surprises
REM      (06:15) and daily_fetch_and_brief (06:30).
REM Output goes to .tmp/cron_logs/fetch_fmp_earnings_calendar_<TS>.log.

setlocal
set PYTHONUTF8=1
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\fetch_fmp_earnings_calendar_%TS%.log

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "fetch-fmp-earnings-calendar" "portfolio-db" execution\fetch_fmp_earnings_calendar.py --all > "%LOG_FILE%" 2>&1
call "%PROJECT_ROOT%\cron\run_python.bat" "refresh-expected-earnings" "portfolio-db" execution\refresh_expected_earnings.py >> "%LOG_FILE%" 2>&1

endlocal
