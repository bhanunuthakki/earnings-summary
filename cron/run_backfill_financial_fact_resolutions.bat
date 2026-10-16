@echo off
REM Daily 07:45 - financial-fact resolution ledger backfill (F4c in
REM .tmp/audit/lineage.md). Captures any legacy fact rows that lack immutable
REM observations and resolves their logical keys under policy v2.
REM One bounded batch per run uses the explicit checkpoint under
REM .tmp/financial-fact-resolution-cutover/state.json. The unused selection
REM mirror is not maintained; old selection cursors remain readable.
REM Deterministic local DB work - no LLM leg, no network. Deliberately after
REM the 03:00-07:30 data chain so same-day acquisitions resolve the same day.
REM Log to .tmp/cron_logs/backfill_financial_fact_resolutions_<TS>.log.

setlocal
set PYTHONUTF8=1
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\backfill_financial_fact_resolutions_%TS%.log

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "backfill-financial-fact-resolutions" "portfolio-db" execution\backfill_financial_fact_resolutions.py --apply --batch-size 500 > "%LOG_FILE%" 2>&1
set "RC=%ERRORLEVEL%"

REM Propagate the job's exit code. Without this the script ended on
REM `endlocal` and ALWAYS returned 0, so Task Scheduler recorded
REM "Last Result: 0" even for a job that failed outright.
endlocal & exit /b %RC%
