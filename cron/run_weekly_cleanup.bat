@echo off
REM Sunday 13:00 local: remove allowlisted transient artifacts.
REM Filesystem cleanup uses its own single-flight lane.

setlocal EnableExtensions
set "PYTHONUTF8=1"
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set "LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"
set "LOG_FILE=%LOG_DIR%\weekly_cleanup_%TS%.log"

cd /d "%PROJECT_ROOT%"
> "%LOG_FILE%" echo === weekly cleanup ===
call "%PROJECT_ROOT%\cron\run_python.bat" "weekly-cleanup" "filesystem-maintenance" execution\run_weekly_cleanup.py --apply >> "%LOG_FILE%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"

endlocal & exit /b %EXIT_CODE%
