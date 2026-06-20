@echo off
REM Monthly (1st @ 01:00) project backup of earnings-summary to the Drive-synced
REM Downloads folder. Runs cron\backup_es_monthly.ps1, which takes a consistent
REM SQLite snapshot of data/portfolio.db, streams a Zip64 archive of the project
REM (default scope=full: incl. the re-fetchable FMP lake + ir_documents +
REM transcripts), HARD-ABORTS if any entry looks like a credential, then
REM atomically OVERWRITES Downloads\earnings-summary-backups\earnings-summary-
REM backup.zip (single file; retention keeps the newest ES_BACKUP_RETAIN, default 1).
REM
REM Log to .tmp/cron_logs/backup_es_monthly_<TS>.log (gitignored).

setlocal
set PROJECT_ROOT=%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\backup_es_monthly_%TS%.log

cd /d "%PROJECT_ROOT%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_ROOT%\cron\backup_es_monthly.ps1" > "%LOG_FILE%" 2>&1

endlocal
