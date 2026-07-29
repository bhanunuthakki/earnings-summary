@echo off
REM Daily 05:35 - populate macro_series (12-series FMP fetch) and recompute
REM portfolio macro_sensitivities, the substrate of the next-dollar panel's
REM macro tilt factor (directives/next_dollar_model.md). Scheduled right
REM after the FMP daily quota reset (00:00 UTC = 05:30 local) and BEFORE
REM fetch_fmp_earnings_calendar (05:45) + daily_fetch_and_brief (06:30)
REM burn the day's request budget - the whole run is ~25 calls. The beta
REM recompute is local CPU only (reads the cached price charts + the rows
REM the fetch just wrote). Output: .tmp/cron_logs/fetch_macro_series_<TS>.log.

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
call "%PROJECT_ROOT%\cron\run_python.bat" "compute-macro-sensitivities" "portfolio-db" execution\compute_macro_sensitivities.py --portfolio >> "%LOG_FILE%" 2>&1

endlocal
