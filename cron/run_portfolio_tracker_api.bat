@echo off
REM SYSTEM-owned BootTrigger for the sole Portfolio Tracker API listener.
REM The explicit root and loopback URL are configured outside this repository;
REM never infer a sibling checkout, Python environment, or bind address.

setlocal EnableExtensions
set "PYTHONUTF8=1"
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set "LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"
set "LOG_FILE=%LOG_DIR%\portfolio_tracker_api_%TS%.log"

if not defined PORTFOLIO_TRACKER_ROOT (
  echo ERROR: PORTFOLIO_TRACKER_ROOT is required for portfolio-tracker-service. 1>&2
  exit /b 1
)
if not defined PORTFOLIO_TRACKER_API_URL (
  echo ERROR: PORTFOLIO_TRACKER_API_URL is required for portfolio-tracker-service. 1>&2
  exit /b 1
)
if not defined EARNINGS_SUMMARY_DB_PATH (
  echo ERROR: EARNINGS_SUMMARY_DB_PATH is required for the product-state receipt root. 1>&2
  exit /b 1
)

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "portfolio-tracker-api" "portfolio-tracker-api" execution\serve_portfolio_tracker.py --tracker-root "%PORTFOLIO_TRACKER_ROOT%" --api-url "%PORTFOLIO_TRACKER_API_URL%" > "%LOG_FILE%" 2>&1
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
