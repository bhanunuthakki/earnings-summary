@echo off
REM Monthly (1st @ 03:00) — regenerate P3-tier (index_member / etf / none) lens artifacts
REM drifted past their cadence. The P3 lens set is minimal (five_min_reread only) so this
REM run is bounded even with 2k+ index constituents — the run_lens cache_inputs hash
REM means stable tickers cost nothing.

setlocal
set PYTHONUTF8=1
set PROJECT_ROOT=%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\monthly_p3_refresh_%TS%.log

cd /d "%PROJECT_ROOT%"
python execution\run_due_lenses.py --cadence monthly > "%LOG_FILE%" 2>&1

endlocal
