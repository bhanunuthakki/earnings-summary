@echo off
REM Daily 02:45: capture verified encrypted snapshots, upload all required
REM families, then keep the latest completed backup per family (default 1).
REM Failures and unclassified recovery copies remain intact. No legacy sweep.
setlocal
set PYTHONUTF8=1
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set "LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
if not defined ES_DB_BACKUP_RETAIN set "ES_DB_BACKUP_RETAIN=1"
if not defined ES_ARCHIVE_BACKUP_RETAIN set "ES_ARCHIVE_BACKUP_RETAIN=1"
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"
set "LOG_FILE=%LOG_DIR%\backup_db_%TS%.log"
cd /d "%PROJECT_ROOT%"
REM A backup is a reader; db-backup excludes concurrent snapshot writers.
call "%PROJECT_ROOT%\cron\run_python.bat" "backup_db" "db-backup" cron\backup_db.py > "%LOG_FILE%" 2>&1
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto done

REM New, skipped_unchanged, and already_done runs emit the same hash-validated
REM current-invocation JSON receipt. No historical log or filename guess.
set "BACKUP_DIR="
for /f "usebackq delims=" %%p in (`powershell -NoProfile -Command "$r = Get-Content -LiteralPath $env:LOG_FILE | ForEach-Object { try { $v = $_ | ConvertFrom-Json -ErrorAction Stop; if ($v.policy -eq 'backup-retention-v1' -and $v.status -eq 'ready') { $v } } catch {} } | Select-Object -Last 1; if (-not $r -or -not (Test-Path -LiteralPath $r.backup_dir -PathType Container)) { exit 1 }; Write-Output $r.backup_dir"`) do set "BACKUP_DIR=%%p"
if not defined BACKUP_DIR (
  echo ERROR: no hash-validated structured backup receipt.>> "%LOG_FILE%"
  set "RC=1"
  goto done
)
if "%BACKUP_DIR:~-1%"=="\" set "BACKUP_DIR=%BACKUP_DIR:~0,-1%"

REM Both upload stages must succeed before any local or remote retirement.
call "%PROJECT_ROOT%\cron\run_python.bat" "backup-drive-upload" "backup-drive-upload" execution\upload_drive_backups.py --source-dir "%BACKUP_DIR%" --pattern "portfolio.db.*.gz.enc" --folder "earnings-summary-db-backups" --backup-set "portfolio-db" --retain %ES_DB_BACKUP_RETAIN% --latest-only --defer-retention >> "%LOG_FILE%" 2>&1
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto done
call "%PROJECT_ROOT%\cron\run_python.bat" "backup-drive-upload" "backup-drive-upload" execution\upload_drive_backups.py --source-dir "%BACKUP_DIR%" --pattern "portfolio_gc_archive.db.*.gz.enc" --folder "earnings-summary-db-backups" --backup-set "portfolio-gc-archive" --retain %ES_ARCHIVE_BACKUP_RETAIN% --allow-empty --latest-only --defer-retention >> "%LOG_FILE%" 2>&1
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto done

call "%PROJECT_ROOT%\cron\run_python.bat" "backup-drive-upload" "backup-drive-upload" execution\upload_drive_backups.py --source-dir "%BACKUP_DIR%" --pattern "portfolio.db.*.gz.enc" --folder "earnings-summary-db-backups" --backup-set "portfolio-db" --retain %ES_DB_BACKUP_RETAIN% --latest-only --finalize-only >> "%LOG_FILE%" 2>&1
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto done
call "%PROJECT_ROOT%\cron\run_python.bat" "backup-drive-upload" "backup-drive-upload" execution\upload_drive_backups.py --source-dir "%BACKUP_DIR%" --pattern "portfolio_gc_archive.db.*.gz.enc" --folder "earnings-summary-db-backups" --backup-set "portfolio-gc-archive" --retain %ES_ARCHIVE_BACKUP_RETAIN% --allow-empty --latest-only --finalize-only >> "%LOG_FILE%" 2>&1
set "RC=%ERRORLEVEL%"
:done
endlocal & exit /b %RC%
