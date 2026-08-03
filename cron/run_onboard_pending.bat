@echo off
REM ---------------------------------------------------------------------------
REM Hourly cron wrapper for execution/onboard_pending_tickers.py.
REM
REM Catches up tickers that bypassed db.track_company's auto-onboard hook
REM (raw SQL inserts / external API writes / direct DB edits). Idempotent —
REM safe to run on any cadence; a no-op when no tickers are pending.
REM
REM Setup: cron\SETUP_WINDOWS_SCHEDULER.md for the install pattern.
REM
REM PROJECT_ROOT auto-resolved from this .bat's own location.
REM ---------------------------------------------------------------------------

setlocal EnableDelayedExpansion

if not defined PROJECT_ROOT (
    for %%I in ("%~dp0..") do set "PROJECT_ROOT=%%~fI"
)

cd /d "%PROJECT_ROOT%" || (echo PROJECT_ROOT not found: %PROJECT_ROOT% & exit /b 2)

set "LOGDIR=%PROJECT_ROOT%\.tmp\cron_logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set "LOGFILE=%LOGDIR%\onboard_pending_%TS%.log"

echo [%TS%] PROJECT_ROOT=%PROJECT_ROOT% > "%LOGFILE%"

set PYTHONUTF8=1
REM Write set "onboard-pending", NOT "portfolio-db". A full run regularly
REM outlives its hourly trigger (2h observed), and wrapper-holding the DB write
REM set for that long starved every other scheduled writer (13 jobs
REM skipped_locked on 2026-08-03). The script now claims portfolio-db itself,
REM per ticker, releasing between tickers; the wrapper's job here is only to
REM stop hourly runs stacking on each other -- which "onboard-pending" does.
call "%PROJECT_ROOT%\cron\run_python.bat" "onboard-pending" "onboard-pending" -u "execution\onboard_pending_tickers.py" >> "%LOGFILE%" 2>&1
set RC=%ERRORLEVEL%

echo [exit %RC%] %LOGFILE%
endlocal & exit /b %RC%
