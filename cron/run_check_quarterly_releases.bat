@echo off
REM ---------------------------------------------------------------------------
REM Monthly cron wrapper for execution/check_quarterly_releases.py.
REM Invoked by Windows Task Scheduler (or manually).
REM
REM Setup instructions: cron\SETUP_WINDOWS_SCHEDULER.md
REM
REM PROJECT_ROOT is derived from this .bat's own location (the file lives at
REM <project_root>\cron\run_check_quarterly_releases.bat) — works regardless
REM of where the repo is checked out. Override by exporting PROJECT_ROOT
REM before the call if you need to point at a different checkout.
REM ---------------------------------------------------------------------------

setlocal EnableDelayedExpansion

if not defined PROJECT_ROOT (
    REM %~dp0 = directory of this .bat (always ends with a backslash).
    REM Strip the trailing 'cron\' to get the project root.
    set "_BAT_DIR=%~dp0"
    for %%I in ("%~dp0..") do set "PROJECT_ROOT=%%~fI"
)
if not defined FFMPEG_LOCATION set "FFMPEG_LOCATION=C:\ffmpeg\bin"

cd /d "%PROJECT_ROOT%" || (echo PROJECT_ROOT not found: %PROJECT_ROOT% & exit /b 2)

set "LOGDIR=%PROJECT_ROOT%\.tmp\cron_logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

REM Build a sortable timestamp YYYYMMDDTHHMMSS using PowerShell (locale-safe).
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set "LOGFILE=%LOGDIR%\check_quarterly_%TS%.log"

echo [%TS%] PROJECT_ROOT=%PROJECT_ROOT% > "%LOGFILE%"
echo [%TS%] FFMPEG_LOCATION=%FFMPEG_LOCATION% >> "%LOGFILE%"

REM --days 45 covers a monthly cron with 2-week safety overlap.
REM Add --with-audio-fallback if you want the orchestrator to also try
REM ytsearch5 when an aggregator misses.
python "execution\check_quarterly_releases.py" --days 45 >> "%LOGFILE%" 2>&1
set RC=%ERRORLEVEL%

echo [exit %RC%] %LOGFILE%
endlocal & exit /b %RC%
