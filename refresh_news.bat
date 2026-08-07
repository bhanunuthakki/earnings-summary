@echo off
REM Force-refresh the §News tab with a fresh WebSearch. Bypasses the 7-day
REM cache and regenerates the brief wholesale.
REM
REM Routed through run_python.bat for managed runtime, lock, and job health.
REM
REM Usage:
REM   refresh_news.bat NU              (default 7-day lookback)
REM   refresh_news.bat NU 14           (14-day lookback)
REM   refresh_news.bat --all-tracked

if "%~1"=="" (
  echo Usage: refresh_news.bat TICKER [DAYS]
  echo        refresh_news.bat --all-tracked
  exit /b 1
)

set "REPO_ROOT=%~dp0"
if "%REPO_ROOT:~-1%"=="\" set "REPO_ROOT=%REPO_ROOT:~0,-1%"

if "%~1"=="--all-tracked" (
  "%REPO_ROOT%\cron\run_python.bat" "%REPO_ROOT%\execution\refresh_news.py" --all-tracked
) else (
  if "%~2"=="" (
    "%REPO_ROOT%\cron\run_python.bat" "%REPO_ROOT%\execution\refresh_news.py" --ticker %1
  ) else (
    "%REPO_ROOT%\cron\run_python.bat" "%REPO_ROOT%\execution\refresh_news.py" --ticker %1 --news-days %2
  )
)
exit /b %ERRORLEVEL%
