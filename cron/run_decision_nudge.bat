@echo off
REM Daily 17:00 - the point-of-intent decision nudge (navigation_ia.md
REM section 3, PR2).
REM
REM run_decision_nudge.py scans decisions logged without conviction or
REM falsifier in the lookback window and sends AT MOST ONE Telegram prompt
REM per decision EVER (UNIQUE decision_nudges ledger). "Fill in now" captures
REM a two-line reply straight onto the decision row via the shared
REM pending-reply gate. Zero LLM - quota-safe at any hour; 17:00 chosen
REM because the owner answers Telegram in the evening. Exits cleanly if the
REM bot is unconfigured.

setlocal
set PYTHONUTF8=1
set PROJECT_ROOT=%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\decision_nudge_%TS%.log

cd /d "%PROJECT_ROOT%"
python execution\run_decision_nudge.py > "%LOG_FILE%" 2>&1

endlocal
