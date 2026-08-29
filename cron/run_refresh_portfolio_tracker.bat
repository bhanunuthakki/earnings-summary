@echo off
REM Daily read-only Portfolio Tracker evidence producer. This task never owns
REM the API listener and does not mutate the tracker database.

setlocal EnableExtensions
set "PYTHONUTF8=1"
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set "LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"
set "LOG_FILE=%LOG_DIR%\refresh_portfolio_tracker_%TS%.log"

if not defined PORTFOLIO_TRACKER_API_URL (
  echo ERROR: PORTFOLIO_TRACKER_API_URL is required for daily tracker evidence. 1>&2
  exit /b 1
)
if not defined EARNINGS_SUMMARY_DB_PATH (
  echo ERROR: EARNINGS_SUMMARY_DB_PATH is required for the product-state receipt root. 1>&2
  exit /b 1
)

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "refresh-portfolio-tracker" "portfolio-tracker-refresh" execution\refresh_portfolio_tracker.py --code-root "%PROJECT_ROOT%" --api-url "%PORTFOLIO_TRACKER_API_URL%" --scheduled-task > "%LOG_FILE%" 2>&1
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
