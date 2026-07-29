@echo off
REM The Ledger capture poller — long-poll Telegram and ingest musings.
REM Runs continuously (LogonTrigger); RestartOnFailure keeps it alive. Reads the
REM bot token from the external earnings-summary secrets directory; exits cleanly if unconfigured.
REM One instance only (IgnoreNew) — a second getUpdates poller would 409.

setlocal
set PYTHONUTF8=1
REM small.en transcribes proper nouns (ticker names: Nubank, MercadoLibre) far
REM better than base.en, so the deterministic matcher auto-links them. ~480MB
REM one-time model download on the first voice memo.
set CAPTURE_WHISPER_MODEL=small.en
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\capture_poller_%TS%.log

cd /d "%PROJECT_ROOT%"
REM Continuous-worker exception: a portfolio-db lock here would starve every
REM bounded job; capture-poller provides singleton/runtime health while each
REM short SQLite write still uses the centralized connection policy.
call "%PROJECT_ROOT%\cron\run_python.bat" "capture-poller" "capture-poller" execution\capture_poller.py >> "%LOG_FILE%" 2>&1

endlocal
