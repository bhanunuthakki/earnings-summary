@echo off
REM Weekly Saturday 11:00 - whole-book thesis-collision audit (program review
REM 2026-07-19, phase A4). The LLM clustering pass that finds shared-driver
REM concentrations ("Brazilian consumer credit normalizes" hitting MELI+NU at
REM once) and thesis contradictions across the book. The engine, validation
REM and the Risk-tab cache reader all shipped months ago; this schedule is the
REM missing invocation (0 runs ever in prod before this). One governed LLM
REM call, cached on the thesis-set hash - an unchanged book re-runs as a cache
REM hit with zero LLM spend. Deliberately outside the 03:00-05:00 morning
REM window and off the Sunday eval-rung / Sat 10:00 red-team slots.
REM Log to .tmp/cron_logs/thesis_collision_<TS>.log.

setlocal
set PYTHONUTF8=1
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\thesis_collision_%TS%.log

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "thesis-collision" "portfolio-db" execution\run_thesis_collision.py > "%LOG_FILE%" 2>&1
set "RC=%ERRORLEVEL%"

REM Propagate the job's exit code. Without this the script ended on
REM `endlocal` and ALWAYS returned 0, so Task Scheduler recorded
REM "Last Result: 0" even for a job that failed outright.
endlocal & exit /b %RC%
