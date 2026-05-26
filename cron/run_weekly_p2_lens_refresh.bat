@echo off
REM Weekly (Sunday 02:00) — regenerate P2-tier lens artifacts that drifted past cadence.
REM See execution/run_due_lenses.py for the per-tier lens set + cadence rules.
REM Companion to run_daily_fetch_and_brief.bat (which handles P1 daily refresh +
REM the brief_dirty queue) and run_monthly_p3_refresh.bat (P3 monthly).

setlocal
set PROJECT_ROOT=%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\weekly_p2_lens_refresh_%TS%.log

cd /d "%PROJECT_ROOT%"
python execution\run_due_lenses.py --cadence weekly > "%LOG_FILE%" 2>&1

endlocal
