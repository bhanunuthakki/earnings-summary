@echo off
REM Weekly (Sunday 01:30) — discover + fetch + register IR documents across the
REM portfolio + evaluation list.
REM
REM Runs execution/discover_ir_documents_all.py, which reads the active-universe
REM roster from the DB (tracked_companies.list_type in portfolio/evaluation) and,
REM per ticker, shells out to two subprocess-isolated stages:
REM   python execution/discover_ir_documents.py --ticker <T>
REM     -> headless-crawl the issuer's IR site, (re-)write its URL manifest
REM   python execution/fetch_ir_documents.py --ticker <T> --categorize --calendar <id>
REM     -> download the manifest's documents into staging and register them at the
REM        canonical path (documents table + ir_narrative-visible layout).
REM
REM Because the roster is read from the DB at run time, newly-added evaluation
REM companies are picked up automatically on the next run. Idempotent — a URL
REM already registered (documents.source_url) is skipped, and the append-only
REM manifest accumulates history across weekly runs even for sites that only
REM expose the current quarter.
REM
REM Resilience: the batch never aborts early — one ticker's failed/hung stage is
REM logged and the next ticker still runs. A ticker with no resolvable IR URL is
REM SKIPPED (not a failure). Process exit code = count of FAILED tickers.
REM
REM Runs Sunday 01:30 — just after refresh_ir_kpis (01:00) and before
REM Sunday synthesis reads. The separate P2 lens sweep runs Friday 22:00.
REM Edit discover_ir_documents.task.xml to change cadence.
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

set LOG_FILE=%LOG_DIR%\discover_ir_documents_%TS%.log

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "discover-ir-documents" "portfolio-db" execution\discover_ir_documents_all.py > "%LOG_FILE%" 2>&1
set "RC=%ERRORLEVEL%"

REM Propagate the job's exit code. Without this the script ended on
REM `endlocal` and ALWAYS returned 0, so Task Scheduler recorded
REM "Last Result: 0" even for a job that failed outright.
endlocal & exit /b %RC%
