# Setting up the earnings-summary crons on Windows Task Scheduler

This is the one-time wiring for the scheduled tasks defined in this folder.
All crons run as `InteractiveToken` under `%USERNAME%`, log to
`.tmp/cron_logs/<task>_<TS>.log`, and are registered under the
`\earnings-summary\` namespace so they show up grouped in the Task Scheduler
GUI.

## Active crons

| Task name | Cadence | XML | Wrapper | What it does |
|---|---|---|---|---|
| `earnings-summary\backfill_transcripts` | Daily 04:30 | `backfill_transcripts.task.xml` | `run_backfill_transcripts.bat` | For every active-universe ticker (`db.ACTIVE_LIST_TYPES`), fetches the last 6 fiscal quarters of Q&A from the free aggregator chain, runs ingest, extracts commitments. Idempotent — re-running with no missing quarters is a no-op. |
| `earnings-summary\fetch_fmp_earnings_calendar` | Daily 05:45 | `fetch_fmp_earnings_calendar.task.xml` | `run_fetch_fmp_earnings_calendar.bat` | Refreshes `data/historical/fmp/<TICKER>_earnings_calendar.json` for every portfolio + watchlist + evaluation ticker. Source of truth for the watcher 15 min later. |
| `earnings-summary\earnings_calendar_watcher` | Daily 06:00 | `earnings_calendar_watcher.task.xml` | `run_earnings_calendar_watcher.bat` | Scans the FMP earnings calendar cache and populates the `expected_earnings` table for the daily worker to drain. |
| `earnings-summary\daily_fetch_and_brief` | Daily 06:30 | `daily_fetch_and_brief.task.xml` | `run_daily_fetch_and_brief.bat` | Drains `tracked_companies.brief_dirty`, runs the thesis evaluator + DCF refresh + brief regen per ticker. Runs with `--enable-llm` so §8/§9 populate via the Claude CLI (Gemini fallback). |
| `earnings-summary\onboard_pending` | Hourly at :17 | `onboard_pending_tickers.task.xml` | `run_onboard_pending.bat` | Catches up tickers that bypassed `db.track_company`'s auto-onboard hook (raw SQL / external API inserts). Idempotent — no-op when nothing is pending. |

The four daily crons run as a chain: backfill (04:30) pulls fresh Q&A
transcripts + commitments, fetch (05:45) refreshes the JSON cache, watcher
(06:00) reads it into `expected_earnings`, worker (06:30) drains
`brief_dirty=1` and regenerates briefs. The 75-min / 15-min / 30-min gaps
absorb slow aggregator/FMP responses and let each step's writes commit
before the next reads.

## Prerequisites

- `python` on PATH and resolves to a Python 3.11+ install with the project's
  `requirements.txt` packages installed.
- `.env` next to `pyproject.toml` containing `FMP_API_KEY=...`.
- The repo cloned at `%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary`
  — or any path you set in `PROJECT_ROOT` at the top of each `.bat`.
- Claude Code CLI on PATH and authed (only required by `daily_fetch_and_brief`
  for §8/§9 generation; the worker falls back to Gemini if the CLI fails).

## Install

From an **admin** PowerShell or `cmd` window, run one `schtasks /create` per
task:

```cmd
schtasks /create /tn "earnings-summary\backfill_transcripts" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\backfill_transcripts.task.xml" ^
  /ru "%USERNAME%"

schtasks /create /tn "earnings-summary\fetch_fmp_earnings_calendar" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\fetch_fmp_earnings_calendar.task.xml" ^
  /ru "%USERNAME%"

schtasks /create /tn "earnings-summary\earnings_calendar_watcher" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\earnings_calendar_watcher.task.xml" ^
  /ru "%USERNAME%"

schtasks /create /tn "earnings-summary\daily_fetch_and_brief" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\daily_fetch_and_brief.task.xml" ^
  /ru "%USERNAME%"

schtasks /create /tn "earnings-summary\onboard_pending" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\onboard_pending_tickers.task.xml" ^
  /ru "%USERNAME%"
```

The `/tn` value is the registered task name (used by all `schtasks` commands
below); the `/xml` value is the file in this folder. Note that the
`onboard_pending` task name doesn't match its XML filename — that's fine, the
filename is just for humans.

## Verify

```cmd
schtasks /query /tn "earnings-summary\<task>" /v /fo LIST
```

You should see `Status: Ready` and a `Next Run Time` consistent with the
cadence in the table above.

## Test fire (without waiting for the schedule)

```cmd
schtasks /run /tn "earnings-summary\<task>"
```

Then check:

- `.tmp\cron_logs\<task>_<TS>.log` — full stdout/stderr of the run.
- For `backfill_transcripts`: new `transcripts/raw/<TICKER>_Q<n>_<YYYY>.txt`
  files (for quarters the aggregators had) + new rows in `transcripts`
  table + new rows in `management_commitments` for the freshly-ingested
  transcripts. The JSON summary at the end of the log lists per-ticker
  fetched/skipped/miss counts.
- For `fetch_fmp_earnings_calendar`: file mtimes on
  `data/historical/fmp/*_earnings_calendar.json` updated to the run time.
- For `earnings_calendar_watcher`: row count in the `expected_earnings`
  table (rows for the watcher's [today−30d, today+14d] window).
- For `daily_fetch_and_brief`: `output/research/<TICKER>/<DATE>_report.html`
  for any tickers that had `brief_dirty=1`.
- For `onboard_pending`: the script exits 0 with an empty results array when
  nothing is pending; otherwise it logs each onboarded ticker.

You can also run any wrapper directly to bypass the scheduler entirely:

```cmd
%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\run_<task>.bat
```

## Uninstall

```cmd
schtasks /delete /tn "earnings-summary\<task>" /f
```

## Edit the schedule

Open Task Scheduler → Task Scheduler Library → `earnings-summary` →
`<task>` → Properties → Triggers tab. Or edit the XML and re-import with
`/create /f` to overwrite.
