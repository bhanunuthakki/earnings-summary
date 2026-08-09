@echo off
REM Process all open comments on a ticker's latest report.
REM
REM Routed through run_python.bat for managed runtime, lock, and job health.
REM
REM Usage:
REM   process_comments.bat NU              (dry-run preview)
REM   process_comments.bat NU --apply      (actually mutate files)
REM   process_comments.bat NU --apply --clear   (also drop addressed)

if "%~1"=="" (
  echo Usage: process_comments.bat TICKER [--apply] [--clear]
  exit /b 1
)

set "REPO_ROOT=%~dp0"
if "%REPO_ROOT:~-1%"=="\" set "REPO_ROOT=%REPO_ROOT:~0,-1%"

"%REPO_ROOT%\cron\run_python.bat" "process_comments" "portfolio-db" "%REPO_ROOT%\execution\process_report_comments.py" --ticker %1 --repo-root "%REPO_ROOT%" %2 %3 %4
exit /b %ERRORLEVEL%
