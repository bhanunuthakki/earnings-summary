@echo off
REM Daily 03:00 — tier-aware FMP refresh queue. Reads FMP_TIER from .env
REM (defaults to basic = 250 calls/day) and drains highest-priority stale
REM endpoints up to the cap. Failed endpoints (403 / tier-restricted) get a
REM 30-day retry window so a downgrade builds backlog automatically; an
REM upgrade catches up over the following days as this script keeps running.
REM
REM Log to .tmp/cron_logs/refresh_cache_<TS>.log.

setlocal
set PYTHONUTF8=1
set PROJECT_ROOT=%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\refresh_cache_%TS%.log

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "refresh_cache" "portfolio-db" execution\refresh_cache.py run > "%LOG_FILE%" 2>&1

endlocal
