@echo off
REM Weekly (Sun @ 06:00 PT) - DB garbage collection per directives/db_garbage_collection.md.
REM Standing invocation keeps only non-fact policies. Destructive facts-depth
REM apply is disabled pending immutable archive-generation migration/cutover.
REM VACUUM runs only on the FIRST Sunday of the month (day-of-month <= 7).
REM db_gc self-guards: write-set run lock, bounded batches, protected-window
REM refusal, and a wall-clock budget.

setlocal
set PYTHONUTF8=1
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"
for /f "usebackq tokens=*" %%d in (`powershell -NoProfile -Command "(Get-Date).Day"`) do set "DOM=%%d"

set "VACUUM_FLAG="
if %DOM% LEQ 7 set "VACUUM_FLAG=--vacuum"

set LOG_FILE=%LOG_DIR%\db_gc_%TS%.log

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "db-gc" "portfolio-db" execution\db_gc.py --apply --policies validation-issues,telemetry,llm-artifacts,maintenance %VACUUM_FLAG% > "%LOG_FILE%" 2>&1
set "RC=%ERRORLEVEL%"

REM Propagate the job's exit code. Without this the script ended on
REM `endlocal` and ALWAYS returned 0, so Task Scheduler recorded
REM "Last Result: 0" even for a job that failed outright.
endlocal & exit /b %RC%
