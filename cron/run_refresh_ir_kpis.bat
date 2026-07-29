@echo off
REM Weekly (Sunday 01:00) — refresh every configured ticker's IR-spreadsheet KPIs.
REM
REM Runs execution/refresh_ir_kpis_all.py, which loops every ticker that has a
REM parser config (micro_thesis/ir_config/<T>.json) and, per ticker, shells out to
REM   python execution/refresh_ir_kpis.py --ticker <T> --discover
REM to headless-render the issuer's IR results-center, download the current
REM historical-data spreadsheet, parse it, and ingest the KPI series at IR_DOC tier
REM (superseding the lower-tier LLM brief values in the report). Idempotent — an
REM unchanged spreadsheet re-ingests as a no-op (the document is sha256-keyed), so
REM a weekly poll simply catches each new quarter's file within a week.
REM
REM Resilience: the batch never aborts early — one ticker's failed/hung discovery
REM is logged and the next ticker still runs. Process exit code = count of failed
REM tickers, so a non-zero log line flags partial failure for monitoring.
REM
REM Runs Sunday 01:00 so freshly-ingested IR KPIs precede the Sunday-night
REM weekly_synthesis (23:00). Edit refresh_ir_kpis.task.xml to change the cadence
REM (e.g. monthly) — the script is cadence-agnostic and idempotent either way.
REM
REM PREREQUISITE: the optional `ir` extra (headless browser) must be installed in
REM the Python that `python` resolves to:
REM   pip install -e .[ir]  &&  playwright install chromium
REM Without it each ticker's --discover child exits non-zero (ImportError) and is
REM logged as a per-ticker failure — the batch itself still completes.

setlocal
set PYTHONUTF8=1
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\refresh_ir_kpis_%TS%.log

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "refresh-ir-kpis" "portfolio-db" execution\refresh_ir_kpis_all.py > "%LOG_FILE%" 2>&1

endlocal
