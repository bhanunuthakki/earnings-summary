@echo off
REM Monthly (2nd @ 07:30) - calibration scorecard (close_the_loops L8): the
REM compounding loop's personal readout. The deterministic substrate (hit-rate
REM trend + selection/sizing/timing skill split) plus eval-gated coach prose
REM (named biases + a falsifiable behavioural experiment), grounded only in the
REM owner's own graded history. Below the min-n floor it renders the substrate
REM with an honest "too thin to coach yet" note and makes no LLM call. The
REM synthesised prose is judged inline before saving (suppressed below
REM threshold). Persists to data/calibration_scorecard/<period>.json.

setlocal
set PYTHONUTF8=1
set PROJECT_ROOT=%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\monthly_calibration_scorecard_%TS%.log

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "monthly-calibration-scorecard" "portfolio-db" execution\run_calibration_scorecard.py > "%LOG_FILE%" 2>&1

endlocal
