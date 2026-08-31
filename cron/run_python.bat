@echo off
REM Shared scheduler/interactive seam: requires the checkout's managed Python runtime,
REM applies the runtime's closed job-to-lane single-flight policy, and records JSON
REM job health. SQLite transactions, not this wrapper, serialize database writes for
REM approved long-running network/browser/LLM jobs.
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

set "TRIGGER_ARG="
if /I "%ES_JOB_TRIGGER_KIND%"=="service" set "TRIGGER_ARG=--trigger-kind service"
set "ES_JOB_TRIGGER_KIND="

set "JOB_RUNTIME_REPO_ROOT="
set "JOB_RUNTIME_CODE_ROOT="
if /I not "%~1"=="prepare-kpi-semantic-review" goto clear_runtime_root_env

if defined ES_JOB_RUNTIME_REPO_ROOT if not defined ES_JOB_RUNTIME_CODE_ROOT (
  set "ES_JOB_RUNTIME_REPO_ROOT="
  set "ES_JOB_RUNTIME_CODE_ROOT="
  echo ERROR: ES_JOB_RUNTIME_CODE_ROOT is required with ES_JOB_RUNTIME_REPO_ROOT. 1>&2
  exit /b 1
)
if defined ES_JOB_RUNTIME_CODE_ROOT if not defined ES_JOB_RUNTIME_REPO_ROOT (
  set "ES_JOB_RUNTIME_REPO_ROOT="
  set "ES_JOB_RUNTIME_CODE_ROOT="
  echo ERROR: ES_JOB_RUNTIME_REPO_ROOT is required with ES_JOB_RUNTIME_CODE_ROOT. 1>&2
  exit /b 1
)
if defined ES_JOB_RUNTIME_REPO_ROOT set "JOB_RUNTIME_REPO_ROOT=%ES_JOB_RUNTIME_REPO_ROOT%"
if defined ES_JOB_RUNTIME_CODE_ROOT set "JOB_RUNTIME_CODE_ROOT=%ES_JOB_RUNTIME_CODE_ROOT%"

:clear_runtime_root_env
set "ES_JOB_RUNTIME_REPO_ROOT="
set "ES_JOB_RUNTIME_CODE_ROOT="
if defined JOB_RUNTIME_REPO_ROOT goto explicit_roots

"%PYTHON_EXE%" -u "%PROJECT_ROOT%\execution\sqlite_bootstrap.py" "%PROJECT_ROOT%\cron\job_runtime.py" %TRIGGER_ARG% --scheduler-wrapper --python-executable "%PYTHON_EXE%" --python-bootstrap "%PROJECT_ROOT%\execution\sqlite_bootstrap.py" -- %*
exit /b %ERRORLEVEL%

:explicit_roots
"%PYTHON_EXE%" -u "%PROJECT_ROOT%\execution\sqlite_bootstrap.py" "%PROJECT_ROOT%\cron\job_runtime.py" %TRIGGER_ARG% --repo-root "%JOB_RUNTIME_REPO_ROOT%" --code-root "%JOB_RUNTIME_CODE_ROOT%" --scheduler-wrapper --python-executable "%PYTHON_EXE%" --python-bootstrap "%PROJECT_ROOT%\execution\sqlite_bootstrap.py" -- %*
exit /b %ERRORLEVEL%
