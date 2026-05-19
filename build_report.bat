@echo off
REM Build the workspace report for a ticker.
REM Usage:
REM   build_report.bat NU              (workspace renderer, no LLM)
REM   build_report.bat NU --enable-llm (full pipeline: bear case, news, valuation)

if "%~1"=="" (
  echo Usage: build_report.bat TICKER [--enable-llm]
  exit /b 1
)

set "REPO_ROOT=%~dp0"
if "%REPO_ROOT:~-1%"=="\" set "REPO_ROOT=%REPO_ROOT:~0,-1%"

python "%REPO_ROOT%\execution\build_artifacts.py" --ticker %1 --renderer workspace --repo-root "%REPO_ROOT%" %2 %3 %4
