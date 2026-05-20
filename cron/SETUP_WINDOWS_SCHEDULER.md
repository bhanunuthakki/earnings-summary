# Setting up the earnings-summary crons on Windows Task Scheduler

This is the one-time wiring for the scheduled tasks defined in this folder.
All crons run as `InteractiveToken` under `%USERNAME%`, log to
`.tmp/cron_logs/<task>_<TS>.log`, and are registered under the
`\earnings-summary\` namespace so they show up grouped in the Task Scheduler
GUI.

## Active crons

| Task name | Cadence | XML | Wrapper | What it does |
|---|---|---|---|---|
| `earnings-summary\refresh_cache` | Daily 03:00 | `refresh_cache.task.xml` | `run_refresh_cache.bat` | **Tier-aware FMP refresh queue.** Reads `FMP_TIER` from `.env` (defaults to `basic` = 250/day) and drains the highest-priority stale endpoints up to the cap. Failed endpoints (403 / Legacy Endpoint) get a 30-day retry window so a downgrade builds a backlog automatically; an upgrade catches up across following days. See `## Switching FMP tier` below. |
| `earnings-summary\backfill_transcripts` | Daily 04:30 | `backfill_transcripts.task.xml` | `run_backfill_transcripts.bat` | For every active-universe ticker (`db.ACTIVE_LIST_TYPES`), fetches the last 6 fiscal quarters of Q&A from the free aggregator chain, runs ingest, extracts commitments. Idempotent — re-running with no missing quarters is a no-op. |
| `earnings-summary\fetch_fmp_earnings_calendar` | Daily 05:45 | `fetch_fmp_earnings_calendar.task.xml` | `run_fetch_fmp_earnings_calendar.bat` | Refreshes `data/historical/fmp/<TICKER>_earnings_calendar.json` for every portfolio + watchlist + evaluation ticker. On `basic` tier this 403s and logs noise — the `next_earnings_date` adapter in `src/sources/earnings_calendar.py` falls back to yfinance. |
| `earnings-summary\backfill_earnings_surprises` | Daily 06:15 | `backfill_earnings_surprises.task.xml` | `run_backfill_earnings_surprises.bat` | For every active-universe ticker, merges `<TICKER>_earnings_calendar.json` (FMP primary, full EPS + Revenue surprise) with `yfinance.Ticker.earnings_dates` (fallback, EPS-only) into `data/surprise/<TICKER>_surprises.json`, then upserts into `earnings_surprises`. Idempotent. Revenue surprise degrades to NULL when FMP coverage lapses. |
| `earnings-summary\daily_fetch_and_brief` | Daily 06:30 | `daily_fetch_and_brief.task.xml` | `run_daily_fetch_and_brief.bat` | Drains `tracked_companies.brief_dirty` with two gates: **B** material-change hash (skip if content unchanged AND last build < 7d), **C** evaluation cadence (skip if list_type=evaluation AND last build < 7d). For un-skipped tickers, runs thesis evaluator + DCF refresh + brief regen with `--enable-llm` so §8/§9 populate via the Claude CLI (Gemini fallback). |
| `earnings-summary\onboard_pending` | Hourly at :17 | `onboard_pending_tickers.task.xml` | `run_onboard_pending.bat` | Catches up tickers that bypassed `db.track_company`'s auto-onboard hook (raw SQL / external API inserts). Idempotent — no-op when nothing is pending. |

The five daily crons run as a chain: refresh_cache (03:00) drains the FMP
priority queue under the configured tier, backfill_transcripts (04:30) pulls
fresh Q&A transcripts + commitments, fetch_fmp_earnings_calendar (05:45)
refreshes the calendar JSON cache, backfill_earnings_surprises (06:15) writes
the merged EPS/Revenue beat-rate cache + DB, and daily_fetch_and_brief (06:30)
drains `brief_dirty=1` and regenerates briefs (gated by content-change + eval
cadence). The 90/75/30/15-min gaps absorb slow aggregator/FMP responses and
let each step's writes commit before the next reads.

## Switching FMP tier

The system reads `FMP_TIER` from `.env` (or `--tier` CLI flag). Tiers:

| Tier | Daily cap | Rate | Use when |
|---|---|---|---|
| `basic` | 250 calls/day | 4/sec | FMP free tier (no paid subscription) |
| `starter` | unlimited | 5/sec | Legacy Starter subscription |
| `premium` | unlimited | 12/sec | Premium / Ultimate subscription |

**When you downgrade FMP** (cancel paid sub → fall to free `basic`):

1. Add `FMP_TIER=basic` to `.env` (or change existing value)
2. The `refresh_cache` cron at 03:00 will automatically drain only the top
   250 highest-priority endpoints per day. Tier-restricted endpoints get a
   30-day retry cooldown so they don't burn budget hammering 403s.
3. The other crons (`fetch_fmp_earnings_calendar`, `backfill_earnings_surprises`,
   `onboard_pending`) will keep firing and generating 403 log noise. That's
   intentional — they degrade gracefully (yfinance fallback in surprises +
   earnings calendar; no new onboards is the right behavior).
4. `daily_fetch_and_brief`'s gates B + C prevent redundant LLM-section
   regeneration when no new data lands, so brief-side costs stay bounded.

**When you upgrade FMP** (resubscribe):

1. Set `FMP_TIER=premium` in `.env` (or `starter`).
2. The 30-day retry window on previously-403'd endpoints expires naturally —
   the cacher's `failed_retry_ok` bucket picks them up over the following
   days. To force immediate catch-up, run once with `--force`:
   ```cmd
   python execution\refresh_cache.py run --tier premium --force
   ```

## Source-call provenance log

Every external source call (FMP, yfinance, SEC XBRL) writes one row to
`source_calls` with `source_name`, `kind`, `ticker`, `called_at`, `status`,
`latency_ms`. This is a write-many / read-rarely log today — the intent is
to inform future intelligent routing (e.g. "yfinance is unreachable for
foreign ADRs since 2026-05-01, prefer FMP cache").

To audit recent calls:

```sql
SELECT source_name, kind, status, COUNT(*)
FROM source_calls
WHERE called_at >= date('now', '-7 days')
GROUP BY source_name, kind, status
ORDER BY COUNT(*) DESC;
```

## Prerequisites

- `python` on PATH and resolves to a Python 3.11+ install with the project's
  `requirements.txt` packages installed (incl. `yfinance` for free-tier fallbacks).
- `.env` next to `pyproject.toml` containing `FMP_API_KEY=...` and optionally
  `FMP_TIER=premium|starter|basic` (defaults to `basic` if unset — set
  `FMP_TIER=premium` when you have a paid sub to unlock the full rate).
- The repo cloned at `%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary`
  — or any path you set in `PROJECT_ROOT` at the top of each `.bat`.
- Claude Code CLI on PATH and authed (only required by `daily_fetch_and_brief`
  for §8/§9 generation; the worker falls back to Gemini if the CLI fails).

## Install

From an **admin** PowerShell or `cmd` window, run one `schtasks /create` per
task:

```cmd
schtasks /create /tn "earnings-summary\refresh_cache" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\refresh_cache.task.xml" ^
  /ru "%USERNAME%"

schtasks /create /tn "earnings-summary\backfill_transcripts" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\backfill_transcripts.task.xml" ^
  /ru "%USERNAME%"

schtasks /create /tn "earnings-summary\fetch_fmp_earnings_calendar" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\fetch_fmp_earnings_calendar.task.xml" ^
  /ru "%USERNAME%"

schtasks /create /tn "earnings-summary\backfill_earnings_surprises" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\backfill_earnings_surprises.task.xml" ^
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
- For `backfill_earnings_surprises`: new/refreshed
  `data/surprise/<TICKER>_surprises.json` files (one per active ticker) +
  rows in the `earnings_surprises` table. The JSON summary at the end of the
  log lists per-ticker insert/update/unchanged counts and which source
  contributed each record (fmp_calendar vs yfinance).
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
