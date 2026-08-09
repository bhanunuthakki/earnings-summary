@echo off
REM Start the workspace report's comments + chat server.
REM Bound to localhost:7421. Keep this window open while reviewing reports.
REM Stop with Ctrl+C.

REM Locate the repo root from this script's own directory so the .bat
REM keeps working if you move it.
set "REPO_ROOT=%~dp0"
if "%REPO_ROOT:~-1%"=="\" set "REPO_ROOT=%REPO_ROOT:~0,-1%"
set "PYTHON_EXE=%REPO_ROOT%\venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=%REPO_ROOT%\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
  echo ERROR: Managed Python runtime not found under venv or .venv. 1>&2
  exit /b 1
)

echo Repo root: %REPO_ROOT%
echo Starting comments server on http://localhost:7421
echo Press Ctrl+C to stop.
echo.

"%PYTHON_EXE%" -u "%REPO_ROOT%\execution\sqlite_bootstrap.py" "%REPO_ROOT%\execution\comments_server.py" --port 7421 --repo-root "%REPO_ROOT%"
