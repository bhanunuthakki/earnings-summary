@echo off
REM Sunday 09:00 - comparable-sets Phase 2 QA drift check
REM (docs/design/comparable_sets_bottoms_up.md section 7/11).
REM
REM check_comp_set_drift.py compares our bottoms-up industry/sector PE
REM against FMP's cached sector/industry PE snapshot and flags a >25% drift
REM as a data-quality signal (never a hard failure, no DB writes). Zero LLM
REM - quota-safe at any hour.

setlocal
set PYTHONUTF8=1
set PROJECT_ROOT=%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\check_comp_set_drift_%TS%.log

cd /d "%PROJECT_ROOT%"
python execution\check_comp_set_drift.py > "%LOG_FILE%" 2>&1

endlocal
