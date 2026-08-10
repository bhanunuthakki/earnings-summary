@echo off
REM Monthly (1st @ 03:00) — compatibility no-op for P3/index catalog names.
REM P3 is deterministic-only and run_due_lenses intentionally returns an empty LLM plan.

setlocal
set PYTHONUTF8=1
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\monthly_p3_refresh_%TS%.log

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "monthly_p3_refresh" "portfolio-db" execution\run_due_lenses.py --cadence monthly > "%LOG_FILE%" 2>&1
set "RC=%ERRORLEVEL%"

REM Propagate the job's exit code. Without this the script ended on
REM `endlocal` and ALWAYS returned 0, so Task Scheduler recorded
REM "Last Result: 0" even for a job that failed outright.
endlocal & exit /b %RC%
