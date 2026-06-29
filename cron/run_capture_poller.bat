@echo off
REM The Ledger capture poller — long-poll Telegram and ingest musings.
REM Runs continuously (LogonTrigger); RestartOnFailure keeps it alive. Reads the
REM bot token from data\secrets\telegram_bot_token; exits cleanly if unconfigured.
REM One instance only (IgnoreNew) — a second getUpdates poller would 409.

setlocal
set PROJECT_ROOT=%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\capture_poller_%TS%.log

cd /d "%PROJECT_ROOT%"
python execution\capture_poller.py >> "%LOG_FILE%" 2>&1

endlocal
