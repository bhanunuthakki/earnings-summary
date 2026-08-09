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
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\backup_db_%TS%.log

cd /d "%PROJECT_ROOT%"
REM Write set is "db-backup", NOT "portfolio-db". The snapshot is a READER --
REM SQLite's online-backup API is safe under concurrent writes -- so claiming the
REM database's exclusive write set only made the backup lose races it did not
REM need to enter. It serializes against other backup runs (which share the
REM destination directory and its retention prune); it does not serialize
REM against ordinary DB writers.
call "%PROJECT_ROOT%\cron\run_python.bat" "backup_db" "db-backup" cron\backup_db.py > "%LOG_FILE%" 2>&1
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto done

REM Destructive file retention is authorized only after THIS invocation proves
REM it produced an encrypted snapshot that still exists. A zero exit alone is
REM insufficient because backup_db can exit successfully on an idempotent skip.
set "BACKUP_RECEIPT="
for /f "usebackq delims=" %%p in (`powershell -NoProfile -Command "$line = Get-Content -LiteralPath '%LOG_FILE%' ^| Where-Object { $_ -like 'OK backup -> *.gz.enc*' } ^| Select-Object -Last 1; if (-not $line) { exit 1 }; $path = $line -replace '^OK backup ->\s*','' -replace '\s+\([^)]*\)\s+retained=.*$',''; if ($path -notlike '*.gz.enc' -or -not (Test-Path -LiteralPath $path -PathType Leaf)) { exit 1 }; Write-Output $path"`) do set "BACKUP_RECEIPT=%%p"
if not defined BACKUP_RECEIPT (
  echo ERROR: backup succeeded without a verifiable encrypted .gz.enc receipt.>> "%LOG_FILE%"
  set "RC=1"
  goto done
)

call "%PROJECT_ROOT%\cron\run_python.bat" "backup-file-gc" "backup-file-gc" execution\backup_file_gc.py --root "%PROJECT_ROOT%" --apply >> "%LOG_FILE%" 2>&1
set "RC=%ERRORLEVEL%"

REM Propagate the job's exit code. Without this the script ended on `endlocal`
REM and ALWAYS returned 0, so Task Scheduler recorded "Last Result: 0" while
REM the backup was failing — three days of missed snapshots looked healthy in
REM every place an operator would check. A failed backup must read as failed.
:done
endlocal & exit /b %RC%
