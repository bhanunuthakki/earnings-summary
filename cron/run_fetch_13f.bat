@echo off
REM Quarterly (16th of Feb/May/Aug/Nov @ 08:15) - EDGAR 13F-HR miner (S6
REM discovery, investor lane). Fires one to two days after the 45-day 13F
REM filing deadline (Feb 14 / May 15 / Aug 14 / Nov 14) so the quarter's
REM filings are in. Two steps: (1) fetch_13f.py polls every active rostered
REM manager (discovery_sources rows WITH a cik), diffs its two latest 13F-HRs,
REM and writes investor_13f discovery_signals (untracked names) + news rows
REM (tracked names); (2) recalibrate_investor_weights.py snapshots this
REM quarter's new buys + measures any whose 180d horizon has elapsed +
REM nudges each fund's base_weight by its realized hit-rate (a no-op until
REM ~2 quarters of history accumulate); (3) run_discovery.py re-scores the
REM queue so the fresh investor signals + tuned weights re-rank immediately
REM (the clamp/corroboration math runs there). Best-effort - an unreachable
REM manager contributes nothing.

setlocal
set PYTHONUTF8=1
set PROJECT_ROOT=%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\fetch_13f_%TS%.log

cd /d "%PROJECT_ROOT%"
echo === fetch_13f (mine rostered managers) === > "%LOG_FILE%"
python execution\fetch_13f.py >> "%LOG_FILE%" 2>&1
echo === recalibrate_investor_weights (snapshot + measure + tune weights) === >> "%LOG_FILE%"
python execution\recalibrate_investor_weights.py >> "%LOG_FILE%" 2>&1
echo === run_discovery (re-score with fresh signals + tuned weights) === >> "%LOG_FILE%"
python execution\run_discovery.py >> "%LOG_FILE%" 2>&1

endlocal
