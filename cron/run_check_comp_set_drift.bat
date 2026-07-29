@echo off
REM Weekly Sunday 12:15 - comparable-set drift QA check (Phase 2,
REM docs/design/comparable_sets_bottoms_up.md #7). Ingests the FMP
REM sector/industry PE snapshot cache files as fmp_snapshot reference
REM rows, then compares bottoms-up industry/sector median PE against
REM them; |drift| > 25% logs a warning + a validation_issues row
REM (source_disagreement). Deterministic local math - no LLM leg, no
REM network. Deliberately off the Sunday-morning eval-rung slot and the
REM 03:00-05:00 morning-pipeline window.
REM Log to .tmp/cron_logs/check_comp_set_drift_<TS>.log.

setlocal
set PYTHONUTF8=1
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\check_comp_set_drift_%TS%.log

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "check-comp-set-drift" "portfolio-db" execution\check_comp_set_drift.py > "%LOG_FILE%" 2>&1

endlocal
