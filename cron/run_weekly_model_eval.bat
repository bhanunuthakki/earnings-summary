@echo off
REM Weekly (Sat @ 20:00) — model-eval loop: harvest -> sweep -> AUTO-SWITCH.
REM
REM The activated, closed-loop model-downgrade eval (directives/model_eval_loop.md):
REM   1. Harvest a rotating 2-ticker sample (force-refresh build + company_desc
REM      refresh) with LLM_CAPTURE_DIR set, so fresh prompt cases are captured.
REM   2. Sweep every cheaper candidate per active purpose -> model_eval_verdicts
REM      (rolling history, full per-case rationale in summary_json).
REM   3. Auto-switch: after 3 consecutive SWITCH_DOWN verdicts for a (purpose,
REM      candidate), write a model_pin_overrides row so _model_for routes there;
REM      revert after 3 consecutive KEEP_INCUMBENT. Every decision -> alerts.
REM
REM Conservative + reversible: a streak (not one run) triggers a switch, and the
REM whole loop is auditable from model_eval_verdicts + model_pin_overrides + alerts.
REM
REM To run advisory-only (no auto-switch), append --dry-run-switches below.

setlocal
set PYTHONUTF8=1
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\weekly_model_eval_%TS%.log
cd /d "%PROJECT_ROOT%"

echo === %DATE% %TIME% weekly_model_eval starting === >> "%LOG_FILE%" 2>&1
call "%PROJECT_ROOT%\cron\run_python.bat" "weekly-model-eval" "portfolio-db" execution\run_weekly_model_eval.py ^
    --repo-root "%PROJECT_ROOT%" >> "%LOG_FILE%" 2>&1
set "RC=%ERRORLEVEL%"
echo === %DATE% %TIME% weekly_model_eval done (exit %RC%) === >> "%LOG_FILE%" 2>&1

REM Propagate the job's exit code. Without this the script ended on
REM `endlocal` and ALWAYS returned 0, so Task Scheduler recorded
REM "Last Result: 0" even for a job that failed outright.
endlocal & exit /b %RC%
