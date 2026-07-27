@echo off
REM Daily 02:45 — SQLite online backup of data/portfolio.db to the configured
REM backup directory (ES_DB_BACKUP_DIR, default: C:\Users\bhanu\My Drive\
REM earnings-summary-db-backups). Runs BEFORE the 03:00 refresh_cache chain
REM so a consistent snapshot exists before the day's writes begin. Keeps the
REM most recent ES_DB_BACKUP_RETAIN snapshots (default 14). Drive receives only
REM authenticated AES-256-GCM .gz.enc files; the recovery key stays outside
REM the repo in the external secrets directory.
REM
REM Log to .tmp/cron_logs/backup_db_<TS>.log.

setlocal
set PYTHONUTF8=1
set PROJECT_ROOT=%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\backup_db_%TS%.log

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "backup_db" "portfolio-db" cron\backup_db.py > "%LOG_FILE%" 2>&1

endlocal
