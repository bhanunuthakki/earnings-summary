@echo off
REM Full per-ticker refresh: FMP data + transcripts + IR document processing
REM + KPI extraction + bear case + valuation + news + rebuild workspace report.
REM
REM This is the "one button" path when you want everything regenerated.
REM Each step is independent — if one fails, the rest continue.
REM
REM Usage:
REM   full_refresh.bat NU

if "%~1"=="" (
  echo Usage: full_refresh.bat TICKER
  exit /b 1
)

set "REPO_ROOT=%~dp0"
if "%REPO_ROOT:~-1%"=="\" set "REPO_ROOT=%REPO_ROOT:~0,-1%"
set TICKER=%1

echo ===========================================
echo Full refresh for %TICKER%
echo ===========================================

echo.
echo [1/6] Refresh FMP financial data...
python "%REPO_ROOT%\execution\fetch_fmp_historical_data.py" --ticker %TICKER% --limit 12

echo.
echo [2/6] Backfill missing transcripts...
python "%REPO_ROOT%\execution\backfill_transcripts.py" --ticker %TICKER%

echo.
echo [3/6] LLM-process unprocessed IR documents (transcripts / press releases / decks)...
python "%REPO_ROOT%\execution\process_ir_documents.py" --ticker %TICKER%

echo.
echo [4/6] Extract tier-1 KPIs from summaries...
python "%REPO_ROOT%\execution\extract_kpis_from_summaries.py" --ticker %TICKER% --source earnings --repo-root "%REPO_ROOT%"

echo.
echo [5/6] Rebuild SayDo pairwise commitments...
python "%REPO_ROOT%\execution\build_saydo_pairs.py" --ticker %TICKER% --repo-root "%REPO_ROOT%"

echo.
echo [6/6] Rebuild workspace report with --enable-llm (bear case, valuation, news)...
python "%REPO_ROOT%\execution\build_artifacts.py" --ticker %TICKER% --renderer workspace --enable-llm --repo-root "%REPO_ROOT%"

echo.
echo ===========================================
echo Done. Report at:
echo   %REPO_ROOT%\output\research\%TICKER%\
echo ===========================================
