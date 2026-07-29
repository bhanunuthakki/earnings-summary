@echo off
REM Weekly (Thu @ 07:00) - verify every cron/*.task.xml is registered, enabled,
REM and on schedule in Windows Task Scheduler.  Exit 0 = all OK, 1 = mismatch
REM found, 2 = schtasks unreachable.  Output captured to cron_logs for the
REM cron_health panel.

setlocal
set PYTHONUTF8=1
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\verify_cron_%TS%.log

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "verify-cron" "task-scheduler" execution\verify_cron_registration.py > "%LOG_FILE%" 2>&1

endlocal
