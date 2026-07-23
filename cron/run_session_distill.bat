@echo off
REM Daily 18:00 - the session-distill sweep (B4 keystone).
REM
REM run_session_distill.py reads every idle/undistilled Ask thread and every
REM landed/undistilled bridged Claude-session transcript, distils each into
REM at most 5 deterministically-grounded candidates, and auto-adopts any
REM tenet/stance revision (announced with a one-tap Telegram Revert). Exit
REM 2 = hard stop (budget/CLI); exit 3 = every candidate deferred transient
REM (quota rule 3 - retried next run).

setlocal
set PYTHONUTF8=1
set PROJECT_ROOT=%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\session_distill_%TS%.log

cd /d "%PROJECT_ROOT%"
python execution\run_session_distill.py > "%LOG_FILE%" 2>&1

endlocal
