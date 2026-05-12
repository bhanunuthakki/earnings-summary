@echo off
REM Daily 04:30 — backfill aggregator Q&A transcripts + commitments for the
REM active universe (db.ACTIVE_LIST_TYPES: portfolio + watchlist + evaluation).
REM Runs BEFORE fetch_fmp_earnings_calendar (05:45) and earnings_calendar_watcher
REM (06:00) so daily_fetch_and_brief (06:30) sees the freshest §5/§6 inputs.
REM
REM ANTHROPIC_API_KEY is unset locally so the commitment-extraction LLM call
REM (Claude Code CLI) bills against the subscription, not the metered API.

setlocal
set PROJECT_ROOT=%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

set "ANTHROPIC_API_KEY="

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\backfill_transcripts_%TS%.log

cd /d "%PROJECT_ROOT%"
python execution\backfill_transcripts.py > "%LOG_FILE%" 2>&1

endlocal
