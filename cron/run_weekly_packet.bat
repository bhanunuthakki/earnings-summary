@echo off
REM Sunday 08:00 - the Sunday packet (navigation_ia.md section 3, PR2).
REM
REM run_weekly_packet.py assembles everything waiting on the owner's judgment
REM (unreconciled notes/themes, proposed tenets, open research proposals, stub
REM decisions) into ONE finite Telegram sequence with one-tap verdict buttons
REM and a packet-clear receipt. Idempotent per ISO week (re-running the same
REM week updates, never duplicates). LLM leg: weekly_packet_predraft (Haiku,
REM per item, per-item degrade) - window registered in
REM directives/llm_quota_scheduling.md. Exits cleanly if the bot is
REM unconfigured or the owner has never messaged it.

setlocal
set PYTHONUTF8=1
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\weekly_packet_%TS%.log

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "weekly-packet" "portfolio-db" execution\run_weekly_packet.py > "%LOG_FILE%" 2>&1

endlocal
