@echo off
REM Weekly Sun 03:30 - feed the prompt-calibration loop.
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
set PROJECT_ROOT=%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\grade_calibration_%TS%.log

cd /d "%PROJECT_ROOT%"
python execution\run_calibration_grading.py > "%LOG_FILE%" 2>&1

endlocal
