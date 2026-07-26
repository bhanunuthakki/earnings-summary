@echo off
REM Shared scheduler/interactive seam: selects an explicit Python 3.11+ runtime,
REM serializes overlapping portfolio writes, and records JSON job health.
setlocal EnableExtensions
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"

set "PYTHON_EXE=%PROJECT_ROOT%\venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=%PROJECT_ROOT%\.venv\Scripts\python.exe"
if exist "%PYTHON_EXE%" goto run

where py >nul 2>nul
if errorlevel 1 goto missing_python
py -3.11 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if errorlevel 1 goto missing_python
set "PYTHON_EXE=py -3.11"
goto run

:missing_python
echo ERROR: Python 3.11+ runtime not found. Install it or create venv\Scripts\python.exe. 1>&2
exit /b 1

:run
if "%PYTHON_EXE%"=="py -3.11" (
  py -3.11 "%PROJECT_ROOT%\cron\job_runtime.py" --scheduler-wrapper --python-executable py --python-arg=-3.11 -- %*
) else (
  "%PYTHON_EXE%" "%PROJECT_ROOT%\cron\job_runtime.py" --scheduler-wrapper --python-executable "%PYTHON_EXE%" -- %*
)
exit /b %ERRORLEVEL%
