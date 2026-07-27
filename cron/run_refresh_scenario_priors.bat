@echo off
REM Monthly (1st @ 03:00) - re-propose per-name DCF scenario priors only for
REM names whose thesis/bear-case anchor text changed since the last call
REM (--only-changed hashes the anchor block and skips the LLM when unchanged -
REM priors are stable quarter-to-quarter, so a quiet month costs nothing).
REM Owner-set priors (set_by=owner) are never touched either way.

setlocal
set PYTHONUTF8=1
REM Resolve from this wrapper so a runtime registration cannot jump back into
REM the mutable scratch checkout.
for %%I in ("%~dp0..") do set "PROJECT_ROOT=%%~fI"
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\refresh_scenario_priors_%TS%.log
cd /d "%PROJECT_ROOT%"

echo === %DATE% %TIME% refresh_scenario_priors starting === >> "%LOG_FILE%" 2>&1
call "%PROJECT_ROOT%\cron\run_python.bat" "refresh_scenario_priors" "portfolio-db" execution\set_scenario_priors.py --only-changed --apply --repo-root "%PROJECT_ROOT%" >> "%LOG_FILE%" 2>&1
echo === %DATE% %TIME% refresh_scenario_priors done (exit %ERRORLEVEL%) === >> "%LOG_FILE%" 2>&1

endlocal
