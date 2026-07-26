@echo off
REM Daily 04:00 - drain the LLM artifact queue.
REM
REM Picks up every llm_artifacts row that is either dirty=1 (an upstream
REM fact-change trigger flipped it) OR whose expires_at < now (the per-purpose
REM TTL in src/llm_artifact_store._DEFAULT_TTL_DAYS has elapsed). For each
REM (ticker, purpose), shells out to the regenerator command defined in
REM execution/refresh_dirty_artifacts._PURPOSE_TO_REGENERATOR. Halts gracefully
REM (exit 0) once accumulated LLM spend in this run reaches --max-cost-usd.
REM
REM Cost cap: $5.00 USD per daily run. Sized so that even pathological queues
REM (large drift after a long outage) can't blow the subscription / API budget
REM in one cron firing.

setlocal
set PYTHONUTF8=1
set PROJECT_ROOT=%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\refresh_dirty_artifacts_%TS%.log

cd /d "%PROJECT_ROOT%"
call "%PROJECT_ROOT%\cron\run_python.bat" "refresh-dirty-artifacts" "portfolio-db" execution\refresh_dirty_artifacts.py --execute --max-cost-usd 5 > "%LOG_FILE%" 2>&1

endlocal
