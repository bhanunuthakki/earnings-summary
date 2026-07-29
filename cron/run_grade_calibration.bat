@echo off
REM Weekly Sun 10:30 America/Los_Angeles - feed the prompt-calibration loop.
REM (Re-registered from a drifted 03:30 on 2026-07-13 -- see
REM directives/llm_quota_scheduling.md's incident note; this comment had
REM gone stale relative to the live Task Scheduler entry.)
REM
REM run_calibration_grading.py runs the three calibration graders in sequence
REM (predictions -> decisions -> bear_cases), each scoring matured LLM outputs
REM against realized data and writing prompt_calibration_scores rows tagged with
REM the central prompt-version registry. The orchestrator never aborts early:
REM every grader is attempted even if an earlier one fails, and the process exit
REM code is the count of failed graders for monitoring.
REM
REM This was the missing machinery: the three graders were manual CLIs that
REM nothing ran, so prompt_calibration_scores stayed empty (v6 re-grade).

setlocal
set PYTHONUTF8=1
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\grade_calibration_%TS%.log

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "grade-calibration" "portfolio-db" execution\run_calibration_grading.py > "%LOG_FILE%" 2>&1

endlocal
