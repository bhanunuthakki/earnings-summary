@echo off
REM Monthly (15th @ 09:00) - DB restore drill: restore the latest backup_db
REM snapshot to a throwaway path and verify it (gunzip + integrity_check +
REM core-table row-count + schema-version), recording the verdict in
REM ingestion_runs for the cron_health panel. NEVER touches the live DB.
REM Exit 0 = passed, 1 = a check failed, 2 = no snapshot found.

setlocal
set PYTHONUTF8=1
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\restore_drill_%TS%.log

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "restore-drill" "portfolio-db" execution\restore_drill.py > "%LOG_FILE%" 2>&1

endlocal
