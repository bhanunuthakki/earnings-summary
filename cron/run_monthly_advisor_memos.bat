@echo off
REM Monthly (1st @ 07:30) - advisor memo run (master build P2.3): the next-dollar
REM allocation memo + swap-discipline checks. Runs after the morning pipeline so
REM FMP prices/DCFs are fresh; the tracker being offline degrades gracefully
REM (memos still generate from DB-side evidence). Each memo persists to
REM advisor_memos + an analyst note (+ ledger entry when ticker-scoped).

setlocal
set PYTHONUTF8=1
set PROJECT_ROOT=%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\monthly_advisor_memos_%TS%.log

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "monthly-advisor-memos" "portfolio-db" execution\run_advisor_memos.py --kind all > "%LOG_FILE%" 2>&1

endlocal
