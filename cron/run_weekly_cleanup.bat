@echo off
REM Sunday 13:00 local: expire allowlisted transient artifacts, then stale research state.
REM Filesystem cleanup uses its own single-flight lane; the state-expiry step is
REM bounded DB-only work and retains portfolio-db. Cleanup failure prevents expiry.

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
call "%PROJECT_ROOT%\cron\run_python.bat" "weekly-cleanup" "portfolio-db" execution\run_weekly_cleanup.py --apply >> "%LOG_FILE%" 2>&1
if not errorlevel 1 goto expire_research
set "EXIT_CODE=%ERRORLEVEL%"
goto done

:expire_research
>> "%LOG_FILE%" echo === stale research expiry ===
call "%PROJECT_ROOT%\cron\run_python.bat" "weekly-cleanup-expire-research" "portfolio-db" execution\expire_stale_research.py --apply >> "%LOG_FILE%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"

:done
endlocal & exit /b %EXIT_CODE%
