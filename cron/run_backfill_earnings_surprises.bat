@echo off
REM Daily 06:15 — backfill EPS/Revenue surprise records and ingest into the
REM earnings_surprises table for every active-universe ticker.
REM
REM Runs AFTER fetch_fmp_earnings_calendar (05:45) and earnings_calendar_watcher
REM (06:00) so the FMP earnings_calendar.json files on disk are fresh, and
REM BEFORE daily_fetch_and_brief (06:30) so any brief that consumes the
REM scorecard sees the freshest beat-rate signal.
REM
REM Two-stage flow:
REM   1. backfill_earnings_surprises.py    walks FMP cache + yfinance fallback,
REM                                        writes data/surprise/<TICKER>_surprises.json
REM   2. ingest_earnings_surprises.py      upserts the JSON into the
REM                                        earnings_surprises table
REM
REM Stage 2 runs on exit 0 or exit 2 (partial_failure — healthy tickers already
REM wrote valid JSON files). Hard failures (other non-zero exit codes) skip
REM Stage 2; final exit code retains Stage 1 failure when Stage 2 succeeds.

setlocal EnableDelayedExpansion
set PYTHONUTF8=1
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set "LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set "LOG_FILE=%LOG_DIR%\backfill_earnings_surprises_%TS%.log"

cd /d "%PROJECT_ROOT%"

echo === backfill_earnings_surprises.py === >> "%LOG_FILE%" 2>&1
call "%PROJECT_ROOT%\cron\run_python.bat" "backfill-earnings-surprises-fetch" "portfolio-db" execution\backfill_earnings_surprises.py >> "%LOG_FILE%" 2>&1
set "STAGE1_RC=!ERRORLEVEL!"
set "RUN_INGEST=0"
if "!STAGE1_RC!"=="0" set "RUN_INGEST=1"
if "!STAGE1_RC!"=="2" set "RUN_INGEST=1"
if "!RUN_INGEST!"=="0" (
    echo === backfill FAILED hard with exit code !STAGE1_RC!; skipping ingest === >> "%LOG_FILE%" 2>&1
    endlocal & exit /b %STAGE1_RC%
)
if "!STAGE1_RC!"=="2" (
    echo === backfill exited 2 partial_failure; continuing to ingest written caches === >> "%LOG_FILE%" 2>&1
)

echo. >> "%LOG_FILE%" 2>&1
echo === ingest_earnings_surprises.py === >> "%LOG_FILE%" 2>&1
call "%PROJECT_ROOT%\cron\run_python.bat" "backfill-earnings-surprises-ingest" "portfolio-db" execution\ingest_earnings_surprises.py >> "%LOG_FILE%" 2>&1
set "STAGE2_RC=!ERRORLEVEL!"
set "RC=!STAGE2_RC!"
if not "!STAGE1_RC!"=="0" if "!STAGE2_RC!"=="0" set "RC=!STAGE1_RC!"

endlocal & exit /b %RC%
