@echo off
REM Daily 06:00 — populate expected_earnings from the FMP cache.
REM Output goes to .tmp/cron_logs/earnings_calendar_watcher_<TS>.log.

setlocal
set PROJECT_ROOT=%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\earnings_calendar_watcher_%TS%.log

cd /d "%PROJECT_ROOT%"
python execution\earnings_calendar_watcher.py > "%LOG_FILE%" 2>&1

endlocal
