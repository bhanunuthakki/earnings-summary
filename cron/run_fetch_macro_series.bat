@echo off
REM Daily 05:35 - populate macro_series (timeout-bounded Yahoo candidates;
REM direct FMP macro calls disabled pending shared recovery admission) and recompute
REM portfolio macro_sensitivities, the substrate of the next-dollar panel's
REM macro tilt factor (directives/next_dollar_model.md). The local compute runs
REM only after every requested series is fresh or explicitly cached-degraded;
REM partial/failed acquisition preserves its warning/failure exit code. Output:
REM .tmp/cron_logs/fetch_macro_series_<TS>.log.

setlocal
set PYTHONUTF8=1
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\fetch_macro_series_%TS%.log

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "fetch-macro-series" "portfolio-db" execution\fetch_macro_series.py > "%LOG_FILE%" 2>&1
set "FETCH_RC=%ERRORLEVEL%"

REM Only an all-series fresh or explicitly cached-degraded acquisition may feed
REM the sensitivity compute. A partial/failed fetch must remain visible instead
REM of being overwritten by a later successful compute exit code.
if "%FETCH_RC%"=="0" goto compute
if "%FETCH_RC%"=="2" goto compute
set "RC=%FETCH_RC%"
goto done

:compute
call "%PROJECT_ROOT%\cron\run_python.bat" "compute-macro-sensitivities" "portfolio-db" execution\compute_macro_sensitivities.py --portfolio >> "%LOG_FILE%" 2>&1
set "COMPUTE_RC=%ERRORLEVEL%"
if not "%COMPUTE_RC%"=="0" (
  set "RC=%COMPUTE_RC%"
) else (
  set "RC=%FETCH_RC%"
)

:done

REM Propagate the job's exit code. Without this the script ended on
REM `endlocal` and ALWAYS returned 0, so Task Scheduler recorded
REM "Last Result: 0" even for a job that failed outright.
endlocal & exit /b %RC%
