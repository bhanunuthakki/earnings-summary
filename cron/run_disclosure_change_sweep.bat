@echo off
REM Saturday 14:00 - resumable disclosure-change detector sweep.
REM Runs only tracked names whose stored SEC accession set has grown since the
REM last fully successful detector pass.  Each child detector is idempotent;
REM a failed ticker is left out of the checkpoint and retried next run.

setlocal
set PYTHONUTF8=1
set PROJECT_ROOT=%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\disclosure_change_sweep_%TS%.log

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "disclosure-change-sweep" "portfolio-db" execution\run_disclosure_change_sweep.py > "%LOG_FILE%" 2>&1

endlocal
