@echo off
REM Daily @ 06:30 - curated podcast-RSS feed poller (S11 diet layer).
REM Polls 7 allowlisted shows (BG2, Invest Like the Best, Acquired,
REM Logan Bartlett, In Good Company, Odd Lots, Stratechery) and writes
REM DIET-lane media_appearance signals for any tracked company or rostered
REM investor (discovery_sources investor_13f) mentioned in an episode title
REM or description within the last 7 days. Re-polling the same episode is a
REM no-op (ux_signals_media_url dedup index). Failures degrade to no new
REM signals -- never block the morning pipeline.

setlocal
set PROJECT_ROOT=%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\fetch_podcast_rss_%TS%.log

cd /d "%PROJECT_ROOT%"
echo === fetch_podcast_rss === > "%LOG_FILE%"
python execution\fetch_podcast_rss.py >> "%LOG_FILE%" 2>&1

endlocal
