@echo off
REM Weekly Saturday 09:00 - the Tenet-accountability pass (B5, 2026-07-19
REM program overhaul).
REM
REM run_tenet_accountability.py reads every current Worldview Tenet, gathers
REM the owner's own decisions made since the Tenet's as_of (plus best-effort
REM position alpha), and runs each Tenet with evidence through ONE Sonnet
REM call auditing it (a Tenet nobody has acted on since costs zero LLM
REM calls). Results persist onto the Tenet's own row for the Worldview
REM panel's receipts chip and the governor's tenet_challenge moment. Exit
REM 2 = hard stop (budget/CLI); exit 3 = every tenet deferred transient
REM (quota rule 3 - retried next week's sweep).

setlocal
set PYTHONUTF8=1
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\tenet_accountability_%TS%.log

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "tenet-accountability" "portfolio-db" execution\run_tenet_accountability.py > "%LOG_FILE%" 2>&1

endlocal
