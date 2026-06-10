@echo off
REM Weekly (Sun @ 06:30) - grade matured advisor memos/stances (master build P2.5).
REM Socratic stances vs price (SPY-relative via the tracker when up), swap checks
REM vs realized pair margin. Deferrals (immature horizons, price-cache gaps) are
REM normal - the run is idempotent over the pending set.

setlocal
set PROJECT_ROOT=%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\weekly_score_stances_%TS%.log

cd /d "%PROJECT_ROOT%"
python execution\score_advisor_stances.py > "%LOG_FILE%" 2>&1

endlocal
