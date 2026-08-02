@echo off
REM Sunday 09:00 - the weekly Senior Partner Brief (PRD §9.1, P2.2).
REM
REM compose_senior_partner_brief.py composes ONE governed structured call per
REM ISO week over the weekly packet, the latest valid Risk Budget (written
REM daily at 04:00 by morning stage 0i - not by this job), the latest
REM Incremental Dollar Recommendation, Investment Decision Cards, Worldview /
REM owner profile, decision + calibration history, and any coach moments the
REM governor routed to the brief. Idempotent per ISO week: a second run in the
REM same week cache-hits the existing artifact rather than spending again.
REM
REM Slot rationale (directives/llm_quota_scheduling.md): clear of the protected
REM 03:00-05:00 morning-pipeline window, AFTER the Sun 08:00 weekly_packet send
REM it composes over, and BEFORE the Sun 10:30 eval rungs.
REM
REM Budget is on_exceed='block' (migration 0201): a budget-exhausted brief must
REM show explicit unavailability, never a degraded or synthesized brief.
REM Exit 2 = hard stop (budget/CLI setup).

setlocal
set PYTHONUTF8=1
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\senior_partner_brief_%TS%.log

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "senior-partner-brief" "portfolio-db" execution\compose_senior_partner_brief.py > "%LOG_FILE%" 2>&1
set "RC=%ERRORLEVEL%"

REM Propagate the job's exit code. Without this the script ended on
REM `endlocal` and ALWAYS returned 0, so Task Scheduler recorded
REM "Last Result: 0" even for a job that failed outright.
endlocal & exit /b %RC%
