@echo off
REM Sunday 11:30 PT - the C3 business-factor taxonomy refresh (Workstream C
REM keystone, src/risk_factors.py). One governed Sonnet call per portfolio
REM holding, cached on a per-ticker mix+thesis input hash (unchanged since
REM the last run = zero spend). Exit 2 = hard stop (budget/CLI); exit 3 =
REM every ticker deferred transient this run (quota rule 3 - retried next
REM week's sweep).

setlocal
set PYTHONUTF8=1
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\refresh_business_factors_%TS%.log

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "refresh-business-factors" "portfolio-db" execution\refresh_business_factors.py > "%LOG_FILE%" 2>&1
set "RC=%ERRORLEVEL%"

REM Propagate the job's exit code. Without this the script ended on
REM `endlocal` and ALWAYS returned 0, so Task Scheduler recorded
REM "Last Result: 0" even for a job that failed outright.
endlocal & exit /b %RC%
