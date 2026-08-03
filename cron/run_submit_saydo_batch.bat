@echo off
REM Weekly Saturday 02:00 — process any DUE SayDo verdict pairs through the
REM Anthropic Message Batches API (the 50%% off-hours discount lane). Two steps:
REM
REM   1. build_saydo_pairs.py --all --prepare-batch  — for every portfolio
REM      holding, find management commitments whose check-date has arrived and
REM      write the pending (say, do) verdict requests to a JSONL under
REM      .tmp/saydo_batch/. No-op (prints []) when nothing is due.
REM   2. submit_saydo_batch.py — submit that JSONL, poll until the batch ends,
REM      write each verdict to .tmp/, and ledger every result at the 50%% batch
REM      discount. A pre-flight cost gate hard-halts if the projection would
REM      breach the pairwise_analysis cap. No-op (no_jsonl_found) when step 1
REM      wrote nothing.
REM
REM Runs in its own task (not folded into weekly_synthesis) because the batch
REM poll can outlast that task's 2h cap — this one allows 6h.

setlocal
set PYTHONUTF8=1
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\submit_saydo_batch_%TS%.log
cd /d "%PROJECT_ROOT%"

echo === %TIME% Preparing SayDo batch JSONL === >> "%LOG_FILE%" 2>&1
call "%PROJECT_ROOT%\cron\run_python.bat" "submit-saydo-batch-prepare" "portfolio-db" execution\build_saydo_pairs.py --all --prepare-batch >> "%LOG_FILE%" 2>&1

echo === %TIME% Submitting + polling SayDo batch === >> "%LOG_FILE%" 2>&1
call "%PROJECT_ROOT%\cron\run_python.bat" "submit-saydo-batch-submit" "portfolio-db" execution\submit_saydo_batch.py >> "%LOG_FILE%" 2>&1
set "RC=%ERRORLEVEL%"

REM Propagate the job's exit code. Without this the script ended on
REM `endlocal` and ALWAYS returned 0, so Task Scheduler recorded
REM "Last Result: 0" even for a job that failed outright.
endlocal & exit /b %RC%
