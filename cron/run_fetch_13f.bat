@echo off
REM Quarterly (16th of Feb/May/Aug/Nov @ 08:15) - EDGAR 13F-HR miner (S6
REM discovery, investor lane). Fires one to two days after the 45-day 13F
REM filing deadline (Feb 14 / May 15 / Aug 14 / Nov 14) so the quarter's
REM filings are in. Two steps: (1) fetch_13f.py polls every active rostered
REM manager (discovery_sources rows WITH a cik), diffs its two latest 13F-HRs,
REM and writes investor_13f discovery_signals (untracked names) + news rows
REM (tracked names); (2) run_discovery.py re-scores the queue so the fresh
REM investor signals re-rank immediately (the clamp/corroboration math runs
REM there). Best-effort - an unreachable manager contributes nothing.

setlocal
set PROJECT_ROOT=%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\fetch_13f_%TS%.log

cd /d "%PROJECT_ROOT%"
echo === fetch_13f (mine rostered managers) === > "%LOG_FILE%"
python execution\fetch_13f.py >> "%LOG_FILE%" 2>&1
echo === run_discovery (re-score with fresh investor signals) === >> "%LOG_FILE%"
python execution\run_discovery.py >> "%LOG_FILE%" 2>&1

endlocal
