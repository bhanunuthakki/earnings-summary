@echo off
REM Daily 07:10 - bottoms-up comparable-set daily metrics (Phase 2,
REM docs/design/comparable_sets_bottoms_up.md #8). Two steps: resolve/
REM refresh set membership (idempotent no-op unless the rule ladder
REM resolves differently) then persist the day's comparable_set +
REM industry/sector scope rows into comp_set_metrics_daily. Pure local
REM math over the per-ticker FMP cache - no LLM leg, no network, no
REM FMP-quota spend - scheduled after daily_fetch_and_brief (06:30) so
REM the day's market caps are current, and clear of the 03:00-05:00
REM morning-pipeline protected window.
REM Log to .tmp/cron_logs/track_comp_metrics_<TS>.log.

setlocal
set PYTHONUTF8=1
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\track_comp_metrics_%TS%.log

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "track-comp-metrics-build-sets" "portfolio-db" execution\build_comparable_sets.py --all-tracked > "%LOG_FILE%" 2>&1
call "%PROJECT_ROOT%\cron\run_python.bat" "track-comp-metrics-record" "portfolio-db" execution\track_comp_metrics.py --all-tracked >> "%LOG_FILE%" 2>&1

endlocal
