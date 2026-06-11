@echo off
REM Weekly (Sun @ 02:00) — model-downgrade evaluation sweep.
REM
REM Evaluates whether cheaper candidate models (Haiku, Gemini) reach parity
REM with the incumbent pin for each active LLM purpose, using real prompt cases
REM from the capture JSONL files accumulated during the previous week.
REM
REM Prerequisites:
REM   1. LLM_CAPTURE_DIR must have been set during recent build runs to
REM      accumulate capture_*.jsonl files under data/llm_capture/.
REM   2. Run AFTER the weekly_synthesis cron (23:00 Sat) so the capture window
REM      includes Sunday's synthesis calls — this cron at 02:00 Sun catches them.
REM
REM Writes to: model_eval_verdicts table in data/portfolio.db
REM            (rolling INSERT history — never upserts).
REM
REM The sweep is ADVISORY: SWITCH_DOWN recommendations require a human PR to
REM apply (editing LLM_MODELS in src/llm/cli.py + citing this run's verdicts).

setlocal
set PROJECT_ROOT=%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\model_eval_sweep_%TS%.log
cd /d "%PROJECT_ROOT%"

echo === %DATE% %TIME% model_eval_sweep starting === >> "%LOG_FILE%" 2>&1
python execution\run_model_eval_sweep.py ^
    --capture-dir data\llm_capture ^
    --repo-root "%PROJECT_ROOT%" >> "%LOG_FILE%" 2>&1
echo === %DATE% %TIME% model_eval_sweep done (exit %ERRORLEVEL%) === >> "%LOG_FILE%" 2>&1

endlocal
