@echo off
REM Weekly (Sun @ 03:00) - cross-source validation + confidence backfill.
REM Stage 1: run_validation_engine checks (range/magnitude/source-disagreement)
REM         - inserts validation_issues rows for any fact failing a check.
REM Stage 2: confidence backfill --apply - rescores financial_facts.confidence
REM         and kpi_facts.confidence, folding fresh issue penalties into scores.
REM Both stages are idempotent (~12s total on the prod DB). Recorded in
REM ingestion_runs under directive "weekly_validation" for the cron-health panel.

setlocal
set PROJECT_ROOT=%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\weekly_validation_%TS%.log

cd /d "%PROJECT_ROOT%"
python execution\run_weekly_validation.py > "%LOG_FILE%" 2>&1

endlocal
