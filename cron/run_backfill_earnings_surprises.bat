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
REM Stage 2 only runs if stage 1 exits 0 — partial failures don't poison
REM the DB with half a refresh.
REM
REM ANTHROPIC_API_KEY is unset locally for consistency with the rest of the
REM cron chain. Neither script makes LLM calls today, but the leakage guard
REM (see CLAUDE.md "Unset ANTHROPIC_API_KEY in subshells") is cheap insurance
REM in case future enrichment steps want LLM extraction here.

setlocal
set PROJECT_ROOT=%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

set "ANTHROPIC_API_KEY="

for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\backfill_earnings_surprises_%TS%.log

cd /d "%PROJECT_ROOT%"

echo === backfill_earnings_surprises.py === >> "%LOG_FILE%" 2>&1
python execution\backfill_earnings_surprises.py >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo === backfill FAILED with exit code %errorlevel%; skipping ingest === >> "%LOG_FILE%" 2>&1
    exit /b %errorlevel%
)

echo. >> "%LOG_FILE%" 2>&1
echo === ingest_earnings_surprises.py === >> "%LOG_FILE%" 2>&1
python execution\ingest_earnings_surprises.py >> "%LOG_FILE%" 2>&1

endlocal
