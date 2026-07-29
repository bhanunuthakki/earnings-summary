@echo off
REM The Ledger synthesis — daily per-holding stance consolidation from musings.
REM Incremental (only scopes with new musings call the LLM); budget-capped.

setlocal
set PYTHONUTF8=1
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\ledger_synthesis_%TS%.log

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "ledger-synthesis" "portfolio-db" execution\run_ledger_synthesis.py > "%LOG_FILE%" 2>&1

endlocal
