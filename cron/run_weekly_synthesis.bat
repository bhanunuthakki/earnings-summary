@echo off
REM Weekly Sunday 23:00 — regenerate the cross-portfolio synthesis lens +
REM run all ticker-scoped lenses for every portfolio holding. This is the
REM "Sunday-night portfolio review" the synthesis layer powers — produces
REM fresh per-holding 5-min rereads + cross-ticker convergence reads ahead
REM of Monday open.
REM
REM Plus: drain any dirty llm_artifacts so the per-section briefs reflect
REM whatever new facts landed during the week.

setlocal
set PYTHONUTF8=1
set PROJECT_ROOT=%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\weekly_synthesis_%TS%.log
cd /d "%PROJECT_ROOT%"

REM 1. Refresh dirty artifacts FIRST so the lens reads have current data.
echo === %TIME% Draining dirty artifacts === >> "%LOG_FILE%" 2>&1
python execution\refresh_dirty_artifacts.py --manifest-only >> "%LOG_FILE%" 2>&1

REM 2. Regenerate all per-ticker lenses for every portfolio holding.
echo === %TIME% Running per-ticker lenses === >> "%LOG_FILE%" 2>&1
python execution\run_lens.py --tickers AMZN,BN,GOOG,MELI,META,NOW,NU,NVO,RBRK,VEEV,WIX --all >> "%LOG_FILE%" 2>&1

REM 3. Regenerate the cross-portfolio synthesis (Opus call, ~$0.25).
echo === %TIME% Running cross-portfolio synthesis === >> "%LOG_FILE%" 2>&1
python execution\run_lens.py --lens cross_portfolio_synthesis >> "%LOG_FILE%" 2>&1

REM 4. Rebuild the analytical dashboard so the new artifacts surface.
echo === %TIME% Rebuilding analytical dashboard === >> "%LOG_FILE%" 2>&1
python execution\build_analytical_dashboard.py >> "%LOG_FILE%" 2>&1

REM Bear-case grading is owned by the dedicated weekly grade_calibration cron
REM (Sun 03:30 -> execution/run_calibration_grading.py, bear_cases rung), so it
REM is intentionally NOT duplicated here. Grading is idempotent (only
REM outcome='pending' predictions are touched), so a single weekly pass suffices.

endlocal
