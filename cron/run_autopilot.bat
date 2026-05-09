@echo off
REM ---------------------------------------------------------------------------
REM Monthly cron wrapper for execution/autopilot.py.
REM Replaces the older run_check_quarterly_releases.bat (autopilot already
REM calls check_quarterly_releases.py as its first stage).
REM
REM Setup: cron\SETUP_WINDOWS_SCHEDULER.md (same flow, swap the wrapper file
REM in the schtasks /create line if migrating).
REM
REM PROJECT_ROOT auto-resolved from this .bat's own location.
REM ---------------------------------------------------------------------------

setlocal EnableDelayedExpansion

if not defined PROJECT_ROOT (
    for %%I in ("%~dp0..") do set "PROJECT_ROOT=%%~fI"
)
if not defined FFMPEG_LOCATION set "FFMPEG_LOCATION=C:\ffmpeg\bin"

cd /d "%PROJECT_ROOT%" || (echo PROJECT_ROOT not found: %PROJECT_ROOT% & exit /b 2)

set "LOGDIR=%PROJECT_ROOT%\.tmp\cron_logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set "LOGFILE=%LOGDIR%\autopilot_%TS%.log"

echo [%TS%] PROJECT_ROOT=%PROJECT_ROOT% > "%LOGFILE%"
echo [%TS%] FFMPEG_LOCATION=%FFMPEG_LOCATION% >> "%LOGFILE%"

REM --pull-days 45 covers monthly cron with 2-week safety overlap.
REM Add --with-audio-fallback if you want PULL to escalate to ytsearch when
REM aggregators miss a recent call (off by default — see directive).
python -u "execution\autopilot.py" --pull-days 45 --pull-limit-quarters 4 --memo-news-days 14 >> "%LOGFILE%" 2>&1
set RC=%ERRORLEVEL%

echo [exit %RC%] %LOGFILE%
endlocal & exit /b %RC%
