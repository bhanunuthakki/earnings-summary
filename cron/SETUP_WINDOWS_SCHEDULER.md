# Setting up the monthly earnings-release check on Windows Task Scheduler

This is the one-time wiring for the cron job defined in
`directives/check_quarterly_releases.md`. After this, on the 15th of every
month the scheduler will run the orchestrator, write transcripts for any new
quarterly releases into `transcripts/raw/`, and drop a per-run report at
`.tmp/cron_runs/<run_id>.json`.

## Files in this folder

| File | Purpose |
|---|---|
| `run_check_quarterly_releases.bat` | Wrapper invoked by the scheduler |
| `check_quarterly_releases.task.xml` | Declarative task definition (calendar trigger, retry policy) |

## Prerequisites

- `python` on PATH and resolves to a Python 3.11+ install with the project's
  `requirements.txt` packages installed.
- `.env` next to `pyproject.toml` containing `FMP_API_KEY=...` (the key is
  what the orchestrator uses to pull the earnings calendar + income statements).
- `ffmpeg` at `C:\ffmpeg\bin` (only needed if you turn on `--with-audio-fallback`).
- The repo cloned at `%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary`
  — or any path you set in `PROJECT_ROOT` at the top of the `.bat`.

## Install

From an **admin** PowerShell or `cmd` window:

```cmd
schtasks /create /tn "earnings-summary\check_quarterly_releases" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\check_quarterly_releases.task.xml" ^
  /ru "%USERNAME%"
```

The task is registered under the namespace `\earnings-summary\` so it shows
up grouped in Task Scheduler GUI.

## Verify

```cmd
schtasks /query /tn "earnings-summary\check_quarterly_releases" /v /fo LIST
```

You should see `Status: Ready`, `Next Run Time: <15th of next month> 06:00:00`.

## Test fire (without waiting for the 15th)

```cmd
schtasks /run /tn "earnings-summary\check_quarterly_releases"
```

Then check:
- `.tmp\cron_logs\check_quarterly_*.log` — full stdout/stderr of the run
- `.tmp\cron_runs\check_quarterly_*.json` — structured run report
- `transcripts\raw\` — any new `<TICKER>_Q<N>_<YEAR>.txt` files

You can also run the wrapper directly to bypass the scheduler entirely:

```cmd
%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\run_check_quarterly_releases.bat
```

## Uninstall

```cmd
schtasks /delete /tn "earnings-summary\check_quarterly_releases" /f
```

## Edit the schedule

Open Task Scheduler → Task Scheduler Library → `earnings-summary` →
`check_quarterly_releases` → Properties → Triggers tab. Or edit the XML and
re-import.

## Tuning knobs (edit `run_check_quarterly_releases.bat`)

- `--days 45` — how far back to look. Default catches anything reported in
  the last ~6 weeks. Bump to 60-90 if you want a wider net.
- `--with-audio-fallback` — adds a Whisper smart-search pass when the
  aggregator chain misses. **Off by default** because ytsearch5 occasionally
  picks the wrong upload; turn on once you're comfortable the QA validator
  catches anything bad (it does — `qa_status=failed` keeps audio cached and
  refuses to overwrite).
- `--limit-quarters 4` — how many quarterly reports per ticker to pull from
  FMP before applying the date filter. 4 is enough for a monthly cadence.

## Manual one-offs

```cmd
REM Single ticker
python execution\check_quarterly_releases.py --ticker NOW --days 90

REM Dry-run (no fetch, just report what would happen)
python execution\check_quarterly_releases.py --dry-run --days 90
```
