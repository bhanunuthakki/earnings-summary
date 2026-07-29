@echo off
REM Weekly (Saturday 02:00) — SEC EDGAR companyfacts refresh across the tracked
REM universe (portfolio + evaluation + watchlist).
REM
REM Runs execution\fetch_sec_xbrl.py, which reads the roster from the DB at run
REM time (tracked_companies filtered to pipeline.sec_xbrl.CIK_MAP), so newly
REM added names are picked up automatically on the next run. Per ticker it:
REM   - fetches data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json
REM     (rate-limited ~5 req/sec per SEC fair-use policy; identifying User-Agent)
REM   - writes the raw payload to data\historical\sec\{T}_companyfacts.json
REM   - registers one documents row per accession + inserts financial_facts for
REM     the curated GAAP/IFRS tag ladders (idempotent INSERT OR IGNORE)
REM   - flags silent staleness (accessions landed, zero facts) via brief_dirty
REM
REM WHY: EDGAR is the continuous free freshness source; the paid FMP key is a
REM ~6-monthly bulk backpopulation (directives/edgar_pipeline.md). SEC facts
REM outrank FMP in the tier-aware loader, so disagreements resolve to the
REM issuer's own filed number.
REM
REM Runs Saturday 02:00 — clear of the Sunday maintenance block (refresh_ir_kpis
REM 01:00, discover_ir_documents 01:30, git cleanup 03:00). Edit
REM fetch_sec_xbrl.task.xml to change cadence, then re-register from the MAIN
REM checkout (editing the XML alone does NOT update the live task):
REM   schtasks /create /tn "\earnings-summary\fetch_sec_xbrl" /xml "%~dp0fetch_sec_xbrl.task.xml" /f
REM
REM Exit code: non-zero when any ticker failed (transient network failures are
REM per-ticker; the run continues to the next ticker).

setlocal
set PYTHONUTF8=1
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\fetch_sec_xbrl_%TS%.log

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "fetch-sec-xbrl" "portfolio-db" execution\fetch_sec_xbrl.py > "%LOG_FILE%" 2>&1

endlocal
