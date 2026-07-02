@echo off
REM Daily 07:15 - the governed coach-initiation pass (W2 nag governor).
REM
REM run_coach_pings.py collects the three deterministic coaching moments
REM (owner-falsifier breach / annotation-pending stub / open standing intent),
REM runs them through the governor (freshness gate -> frequency caps ->
REM auto-mute) and pushes at most one Telegram ping a day with a Dismiss
REM button; overflow waits quietly in the digest. Zero LLM.

setlocal
set PYTHONUTF8=1
set PROJECT_ROOT=%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\coach_pings_%TS%.log

cd /d "%PROJECT_ROOT%"
python execution\run_coach_pings.py > "%LOG_FILE%" 2>&1

endlocal
