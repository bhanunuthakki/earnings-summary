@echo off
setlocal EnableExtensions
set "PYTHONUTF8=1"
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"

if not defined EARNINGS_SUMMARY_DB_PATH (
  echo ERROR: EARNINGS_SUMMARY_DB_PATH is required for the product-state authority. 1>&2
  exit /b 1
)
for %%I in ("%EARNINGS_SUMMARY_DB_PATH%") do set "DB_DIR=%%~dpI"
for %%I in ("%DB_DIR%..") do set "PRODUCT_STATE_ROOT=%%~fI"
set "ES_JOB_RUNTIME_REPO_ROOT=%PRODUCT_STATE_ROOT%"
set "ES_JOB_RUNTIME_CODE_ROOT=%PROJECT_ROOT%"

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "reconcile-recorded-trades" "portfolio-db" "%PROJECT_ROOT%\execution\reconcile_recorded_trades.py" --apply
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
