@echo off
REM Daily 06:00 — populate expected_earnings from the FMP cache.
REM Output goes to .tmp/cron_logs/earnings_calendar_watcher_<TS>.log.

setlocal
set PROJECT_ROOT=%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "tokens=2 delims==" %%a in ('wmic OS Get localdatetime /value') do set DT=%%a
set TS=%DT:~0,8%T%DT:~8,6%

set LOG_FILE=%LOG_DIR%\earnings_calendar_watcher_%TS%.log

cd /d "%PROJECT_ROOT%"
python execution\earnings_calendar_watcher.py > "%LOG_FILE%" 2>&1

endlocal
