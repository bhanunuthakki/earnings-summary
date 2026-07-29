@echo off
REM Daily 06:30 (30 min after the earnings calendar watcher) — drain brief_dirty
REM queue, refresh DCFs, regenerate briefs. Pass --enable-llm so §8 + §9 populate
REM via the Claude CLI; falls back to Gemini if the CLI fails.
REM
REM TIER-AWARENESS (added by the scalability investment):
REM   By default, daily_fetch_and_brief.py now intersects the dirty queue with
REM   the per-tier daily-cadence rule from src/pipeline/tier_runner.py:
REM     P1 (portfolio) always runs.
REM     P2 (watchlist/eval) runs only if last_built_at > 7 days ago.
REM     P3 (index members / etf / none) runs only if last_built_at > 30 days ago.
REM   Pass --ignore-tier to restore the old behavior (run on every dirty ticker).
REM   Companion runs: cron\run_weekly_p2_lens_refresh.bat (Sunday 02:00) and
REM   cron\run_monthly_p3_refresh.bat (1st of month, 03:00).

setlocal
set PYTHONUTF8=1
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\daily_fetch_and_brief_%TS%.log

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "daily_fetch_and_brief" "portfolio-db" execution\daily_fetch_and_brief.py --enable-llm > "%LOG_FILE%" 2>&1

endlocal
