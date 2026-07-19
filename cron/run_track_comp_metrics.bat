@echo off
REM Daily 06:45 - comparable-sets Phase 2 daily metrics tracker
REM (docs/design/comparable_sets_bottoms_up.md section 8/11).
REM
REM track_comp_metrics.py computes + upserts scope_type='comparable_set'/
REM 'industry'/'sector' rows into comp_set_metrics_daily -- market_cap moves
REM daily even though quarterly financials don't. Zero LLM - quota-safe at
REM any hour; runs after the 03:00-06:15 daily FMP/transcript chain so
REM caches are fresh for the day.

setlocal
set PYTHONUTF8=1
set PROJECT_ROOT=%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\track_comp_metrics_%TS%.log

cd /d "%PROJECT_ROOT%"
python execution\track_comp_metrics.py --all-tracked > "%LOG_FILE%" 2>&1

endlocal
