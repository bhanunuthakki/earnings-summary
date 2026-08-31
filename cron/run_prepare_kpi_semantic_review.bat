@echo off
REM Every ten minutes: publish bounded read-only KPI semantic-review artifacts.
REM The canonical database is opened read-only. Mutable output is restricted to
REM the configured product-state root's content-addressed review export lane.

setlocal EnableExtensions
set "PYTHONUTF8=1"
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set "LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"
set "LOG_FILE=%LOG_DIR%\prepare_kpi_semantic_review_%TS%.log"

if not defined EARNINGS_SUMMARY_DB_PATH (
  echo ERROR: EARNINGS_SUMMARY_DB_PATH is required for the product-state authority. 1>&2
  exit /b 1
)

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "prepare-kpi-semantic-review" "kpi-semantic-review-export" execution\prepare_kpi_semantic_review.py --db "%EARNINGS_SUMMARY_DB_PATH%" --code-root "%PROJECT_ROOT%" --publish > "%LOG_FILE%" 2>&1
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
