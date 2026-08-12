@echo off
REM Weekly Sunday 23:00 — regenerate the cross-portfolio synthesis lens +
REM run all ticker-scoped lenses for every portfolio holding. This is the
REM "Sunday-night portfolio review" the synthesis layer powers — produces
REM fresh per-holding 5-min rereads + cross-ticker convergence reads ahead
REM of Monday open.
REM
REM Plus: drain any dirty llm_artifacts so the per-section briefs reflect
REM whatever new facts landed during the week.

setlocal
set PYTHONUTF8=1
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\weekly_synthesis_%TS%.log
cd /d "%PROJECT_ROOT%"

REM One research-synthesis lane owner spans the checkpointed, fail-fast stages;
REM SQLite transactions serialize each bounded database write phase.
echo === %TIME% Running weekly synthesis === >> "%LOG_FILE%" 2>&1
call "%PROJECT_ROOT%\cron\run_python.bat" "weekly-synthesis" "portfolio-db" execution\run_weekly_synthesis.py >> "%LOG_FILE%" 2>&1
set "RC=%ERRORLEVEL%"


REM Bear-case grading is owned by the dedicated weekly grade_calibration cron
REM (Sun 03:30 -> execution/run_calibration_grading.py, bear_cases rung), so it
REM is intentionally NOT duplicated here. Grading is idempotent (only
REM outcome='pending' predictions are touched), so a single weekly pass suffices.

REM Propagate the job's exit code. Without this the script ended on
REM `endlocal` and ALWAYS returned 0, so Task Scheduler recorded
REM "Last Result: 0" even for a job that failed outright.
endlocal & exit /b %RC%
