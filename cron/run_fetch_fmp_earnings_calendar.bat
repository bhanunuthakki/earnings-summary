@echo off
REM Daily 05:45 — refresh data/historical/fmp/<TICKER>_earnings_calendar.json
REM for every portfolio + watchlist ticker. Runs 15 min before the
REM earnings_calendar_watcher (06:00) so the watcher reads fresh dates.
REM Output goes to .tmp/cron_logs/fetch_fmp_earnings_calendar_<TS>.log.

setlocal
set PROJECT_ROOT=%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "tokens=2 delims==" %%a in ('wmic OS Get localdatetime /value') do set DT=%%a
set TS=%DT:~0,8%T%DT:~8,6%

set LOG_FILE=%LOG_DIR%\fetch_fmp_earnings_calendar_%TS%.log

cd /d "%PROJECT_ROOT%"
python execution\fetch_fmp_earnings_calendar.py --all > "%LOG_FILE%" 2>&1

endlocal
