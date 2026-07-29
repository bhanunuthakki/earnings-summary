@echo off
REM Shared scheduler/interactive seam: requires the checkout's managed Python runtime,
REM serializes overlapping portfolio writes, and records JSON job health.
setlocal EnableExtensions
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set "PYTHON_EXE=%PROJECT_ROOT%\venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=%PROJECT_ROOT%\.venv\Scripts\python.exe"
if exist "%PYTHON_EXE%" goto verify_python

:missing_python
echo ERROR: Managed Python runtime not found. Create venv\Scripts\python.exe or .venv\Scripts\python.exe in this checkout. 1>&2
exit /b 1

:verify_python
"%PYTHON_EXE%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if errorlevel 1 goto missing_python

"%PYTHON_EXE%" "%PROJECT_ROOT%\cron\job_runtime.py" --scheduler-wrapper --python-executable "%PYTHON_EXE%" -- %*
exit /b %ERRORLEVEL%
