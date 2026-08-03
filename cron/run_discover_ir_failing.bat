@echo off
REM Twice-weekly (Wednesday + Saturday 02:30) — failing-crawler rescan.
REM
REM Re-attempts ONLY the portfolio/evaluation names that currently have ZERO
REM registered IR documents:
REM   python execution/discover_ir_documents_all.py --only-failing
REM
REM --only-failing reads the live document store (documents.source_type='ir_doc'),
REM computes the roster names with no auto-fetched IR docs, and runs just those
REM through the normal discover -> fetch+register -> process chain. So a name that
REM was bot-protected (HTTP 403), HTTP/2-broken, or stalled on a headless load is
REM retried in case the IR site starts cooperating — picked up days sooner than
REM the weekly full sweep, at a fraction of the cost (only the gaps, not all 32).
REM
REM Complements cron\discover_ir_documents.task.xml (the Sunday 01:30 FULL sweep
REM over every name). A name that succeeds here drops out of the gap set and is no
REM longer rescanned; a name that keeps failing is surfaced in the dashboard's
REM "IR Docs" coverage tab with its last crawl reason. Idempotent; the batch never
REM aborts on one ticker's failure. Process exit code = count of FAILED tickers.
REM
REM PREREQUISITE: the optional `ir` extra (headless browser) must be installed in
REM the Python that `python` resolves to:
REM   pip install -e .[ir]  &&  playwright install chromium
REM Without it each ticker's discover child exits non-zero (ImportError) and is
REM logged as a per-ticker failure — the batch itself still completes.

setlocal
set PYTHONUTF8=1
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\discover_ir_failing_%TS%.log

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "discover-ir-failing" "portfolio-db" execution\discover_ir_documents_all.py --only-failing > "%LOG_FILE%" 2>&1
set "RC=%ERRORLEVEL%"

REM Propagate the job's exit code. Without this the script ended on
REM `endlocal` and ALWAYS returned 0, so Task Scheduler recorded
REM "Last Result: 0" even for a job that failed outright.
endlocal & exit /b %RC%
