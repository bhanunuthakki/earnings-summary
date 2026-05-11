@echo off
REM Daily 06:30 (30 min after the earnings calendar watcher) — drain brief_dirty
REM queue, refresh DCFs, regenerate briefs. Pass --enable-llm so §8 + §9 populate
REM via the Claude CLI; falls back to Gemini if the CLI fails.

setlocal
set PROJECT_ROOT=%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary
set LOG_DIR=%PROJECT_ROOT%\.tmp\cron_logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM wmic was removed from Windows 11 24H2+; use PowerShell for the UTC stamp.
for /f "usebackq tokens=*" %%t in (`powershell -NoProfile -Command "(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')"`) do set "TS=%%t"

set LOG_FILE=%LOG_DIR%\daily_fetch_and_brief_%TS%.log

cd /d "%PROJECT_ROOT%"
python execution\daily_fetch_and_brief.py --enable-llm > "%LOG_FILE%" 2>&1

endlocal
