@echo off
REM Weekly (Sun @ 03:00) - confidence backfill.
REM Rescores financial_facts.confidence and kpi_facts.confidence --apply,
REM folding fresh validation-issue penalties into per-fact scores (idempotent).
REM The validation-engine SCAN runs DAILY in run_morning_pipeline (stage 3), so
REM it is NOT repeated here - the backfill reads the issues the daily run already
REM inserted. Recorded in ingestion_runs under directive "weekly_validation".

setlocal
set PYTHONUTF8=1
set PROJECT_ROOT=%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\weekly_validation_%TS%.log

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "weekly-validation" "portfolio-db" execution\run_weekly_validation.py > "%LOG_FILE%" 2>&1

endlocal
