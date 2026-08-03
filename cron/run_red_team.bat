@echo off
REM Weekly Saturday 10:00, self-gated to the month's FIRST Saturday - the
REM monthly First-Saturday adversarial Red Team review
REM (directives/monthly_red_team.md Phase 2). Windows Task Scheduler has no
REM native "Nth weekday of month" trigger, so this fires every Saturday and
REM run_red_team.py itself no-ops (exit 0) on every non-first Saturday.
REM Idempotent on red_team_{YYYY_MM} - already-generated months are a no-op
REM unless --force. Per-item degrade: a transient LLM failure defers that one
REM item and retries next run; a hard stop (budget cap / missing CLI) halts
REM loudly (non-zero exit, surfaced in the log below).

setlocal
set PYTHONUTF8=1
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\red_team_%TS%.log
cd /d "%PROJECT_ROOT%"

echo === %DATE% %TIME% red_team starting === >> "%LOG_FILE%" 2>&1
call "%PROJECT_ROOT%\cron\run_python.bat" "red-team" "red-team-report" execution\run_red_team.py --repo-root "%PROJECT_ROOT%" >> "%LOG_FILE%" 2>&1
set "RC=%ERRORLEVEL%"
echo === %DATE% %TIME% red_team done (exit %ERRORLEVEL%) === >> "%LOG_FILE%" 2>&1

REM Propagate the job's exit code. Without this the script ended on
REM `endlocal` and ALWAYS returned 0, so Task Scheduler recorded
REM "Last Result: 0" even for a job that failed outright.
endlocal & exit /b %RC%
