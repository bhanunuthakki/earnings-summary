@echo off
REM Daily 04:30 — backfill aggregator Q&A transcripts + commitments for the
REM active universe (db.ACTIVE_LIST_TYPES: portfolio + watchlist + evaluation).
REM Runs BEFORE fetch_fmp_earnings_calendar (05:45) and earnings_calendar_watcher
REM (06:00) so daily_fetch_and_brief (06:30) sees the freshest §5/§6 inputs.

setlocal
set PYTHONUTF8=1
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
REM Capture only the high-volume commitment extractor's real production
REM exchanges in the private retention-bounded archive outside the repo.
if not defined LLM_CAPTURE_DIR set "LLM_CAPTURE_DIR=%LOCALAPPDATA%\earnings-summary\llm_capture"
if not defined EARNINGS_SUMMARY_CAPTURE_RETENTION_DAYS set EARNINGS_SUMMARY_CAPTURE_RETENTION_DAYS=90
set LLM_CAPTURE_PURPOSES=saydo_commitment_extract
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\backfill_transcripts_%TS%.log

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "backfill-transcripts" "portfolio-db" execution\backfill_transcripts.py > "%LOG_FILE%" 2>&1

endlocal
