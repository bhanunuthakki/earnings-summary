@echo off
REM Weekly (Friday 22:00) — regenerate narrow P2 monitoring lenses only.
REM See execution/run_due_lenses.py for the per-tier lens set + cadence rules.
REM Companion: run_daily_fetch_and_brief.bat handles P1 daily refresh and the brief_dirty queue.

setlocal
set PYTHONUTF8=1
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\weekly_p2_lens_refresh_%TS%.log

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "weekly-p2-lens-refresh" "portfolio-db" execution\run_due_lenses.py --cadence weekly --max-plan-pairs 128 --window-opens-local 21:30 --stop-before-local 01:35 > "%LOG_FILE%" 2>&1
set "RC=%ERRORLEVEL%"

REM Propagate the job's exit code. Without this the script ended on
REM `endlocal` and ALWAYS returned 0, so Task Scheduler recorded
REM "Last Result: 0" even for a job that failed outright.
endlocal & exit /b %RC%
