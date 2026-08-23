@echo off
REM Every ten minutes: collect bounded Scheduler and managed-service receipts.
REM This wrapper declares the same receipt lane the child validates and borrows,
REM preventing a wrapper/child self-deadlock while preserving cross-run exclusion.

setlocal EnableExtensions
set "PYTHONUTF8=1"
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set "LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"
set "LOG_FILE=%LOG_DIR%\collect_operations_runtime_observations_%TS%.log"

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "collect-operations-runtime-observations" "operations-runtime-receipts" execution\collect_operations_runtime_observations.py --repo-root "%PROJECT_ROOT%" --emit-receipts --json-out > "%LOG_FILE%" 2>&1
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
