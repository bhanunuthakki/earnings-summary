@echo off
REM Daily 04:00 — the morning pipeline. One orchestrated run that chains:
REM   1. run_triggers.py   — fan registered triggers across the portfolio +
REM                          watchlist + evaluation list, persisting fresh
REM                          alerts + drafted actions into data/portfolio.db.
REM   2. build_morning_digest.py — rebuild today's digest HTML from those
REM                          persisted alerts (data/dashboard/morning/).
REM   3. build_alert_feed.py     — rebuild the chronological feed HTML
REM                          (data/dashboard/feed.html).
REM
REM Supersedes the standalone run_triggers cron (PR #172): the digest + feed
REM are now rebuilt in the SAME run that fires the alerts, so a 07:00 read sees
REM fresh HTML instead of yesterday's. Running at 04:00 leaves ample headroom
REM for the trigger stage (cost-capped at $10) to finish before any morning
REM read.
REM
REM Resilience: the orchestrator never aborts early. If the trigger stage fails
REM or times out, the digest + feed still rebuild over whatever alerts already
REM exist (they are read-only renders). The process exit code is the count of
REM failed stages, so a non-zero log line flags partial failure for monitoring
REM while still producing the best-effort digest.

setlocal
set PROJECT_ROOT=%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\run_morning_pipeline_%TS%.log

cd /d "%PROJECT_ROOT%"
python execution\run_morning_pipeline.py --max-cost-usd 10 > "%LOG_FILE%" 2>&1

endlocal
