@echo off
REM Daily (06:30) - DIET-lane podcast RSS poller + takeaway summarizer (S11).
REM Step 1: fetch_podcast_rss.py polls 7 curated shows (BG2, Invest Like the
REM   Best, Acquired, Logan Bartlett, In Good Company, Odd Lots, Stratechery),
REM   matches episode titles/descriptions against tracked company names and
REM   rostered investor display names, and writes media_appearance signals in
REM   the DIET lane. Idempotent — re-polling the same episode URL is a no-op.
REM Step 2: summarize_podcast_episodes.py runs a Sonnet pass over any
REM   media_appearance signals whose body is absent or short (< 280 chars),
REM   replacing RSS marketing copy with a 2-4 sentence investment briefing.
REM   Budget-capped at $5/month (migration 0103, on_exceed=skip).

setlocal
set PYTHONUTF8=1
set PROJECT_ROOT=%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\fetch_podcast_rss_%TS%.log

cd /d "%PROJECT_ROOT%"
echo === fetch_podcast_rss (poll + match + write diet signals) === > "%LOG_FILE%"
python execution\fetch_podcast_rss.py >> "%LOG_FILE%" 2>&1
echo === summarize_podcast_episodes (Sonnet takeaway pass) === >> "%LOG_FILE%"
python execution\summarize_podcast_episodes.py >> "%LOG_FILE%" 2>&1

endlocal
