@echo off
REM Daily 04:00 — the morning pipeline. One orchestrated run that chains:
REM   1. fetch_news.py     — ingest fresh per-ticker news for the
REM                          material_news trigger to classify.
REM   2. run_triggers.py   — fan registered triggers across the portfolio +
REM                          watchlist + evaluation list, persisting fresh
REM                          alerts + drafted actions into data/portfolio.db.
REM   3. build_alert_feed.py — rebuild the chronological feed HTML
REM                          (data/dashboard/feed.html).
REM   4. run_validation_engine.py --gate — population-level data checks.
REM
REM Supersedes the standalone run_triggers cron (PR #172): the feed is rebuilt
REM in the SAME run that fires the alerts, so a 07:00 read sees fresh HTML
REM instead of yesterday's. Running at 04:00 leaves ample headroom for the
REM trigger stage (cost-capped at $10) to finish before any morning read.
REM (The morning-digest render stage retired with the /digest page 2026-06-11;
REM the live Home rail serves that view straight from the DB.)
REM
REM Resilience: the orchestrator never aborts early. If the trigger stage fails
REM or times out, the feed still rebuilds over whatever alerts already exist
REM (it is a read-only render). The process exit code is the count of failed
REM stages, so a non-zero log line flags partial failure for monitoring while
REM still producing the best-effort feed.

setlocal
set PYTHONUTF8=1
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
REM Retain a bounded-purpose production corpus for the P0 material-news audit.
REM Keep private full-text outside the mirrored repo with bounded retention.
if not defined LLM_CAPTURE_DIR set "LLM_CAPTURE_DIR=%LOCALAPPDATA%\earnings-summary\llm_capture"
if not defined EARNINGS_SUMMARY_CAPTURE_RETENTION_DAYS set EARNINGS_SUMMARY_CAPTURE_RETENTION_DAYS=90
set LLM_CAPTURE_PURPOSES=material_news_classification
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\run_morning_pipeline_%TS%.log

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "morning_pipeline" "portfolio-db" execution\run_morning_pipeline.py --max-cost-usd 10 > "%LOG_FILE%" 2>&1

endlocal
