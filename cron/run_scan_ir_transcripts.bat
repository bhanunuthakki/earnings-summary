@echo off
REM Daily 04:15 — post-earnings IR-transcript scan for the active universe.
REM For each tracked ticker within 14 days of its last earnings date, re-checks
REM the issuer's own IR site (issuer_ir source) for the latest reported quarter's
REM transcript and fetches + ingests it. Idempotent on
REM transcripts/processed/<T>_Q<n>_<Y>.txt — stops once that quarter is ingested.
REM Runs BEFORE backfill_transcripts (04:30) so the broad sweep finds the file
REM already on disk, and before daily_fetch_and_brief (06:30) so the brief sees it.

setlocal
set PYTHONUTF8=1
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\scan_ir_transcripts_%TS%.log

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "scan-ir-transcripts" "portfolio-db" execution\scan_ir_transcripts.py > "%LOG_FILE%" 2>&1

endlocal
