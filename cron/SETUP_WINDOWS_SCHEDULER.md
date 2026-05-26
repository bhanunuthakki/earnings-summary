# Setting up the earnings-summary crons on Windows Task Scheduler

This is the one-time wiring for the scheduled tasks defined in this folder.
All crons run as `InteractiveToken` under `%USERNAME%`, log to
`.tmp/cron_logs/<task>_<TS>.log`, and are registered under the
`\earnings-summary\` namespace so they show up grouped in the Task Scheduler
GUI.

## Active crons

Nine scheduled tasks total. The five daily ones run as a chain (03:00 → 06:30); the hourly catch-up is independent; the two weekly + one monthly run off-cycle and refresh the synthesis / lens layer.

### Daily chain (P1 tier — portfolio refreshed every day)

| Task name | Cadence | XML | Wrapper | What it does |
|---|---|---|---|---|
| `earnings-summary\refresh_cache` | Daily 03:00 | `refresh_cache.task.xml` | `run_refresh_cache.bat` | **Tier-aware FMP refresh queue.** Reads `FMP_TIER` from `.env` (defaults to `basic` = 250/day) and drains the highest-priority stale endpoints up to the cap. Failed endpoints (403 / Legacy Endpoint) get a 30-day retry window so a downgrade builds a backlog automatically; an upgrade catches up across following days. See `## Switching FMP tier` below. |
| `earnings-summary\backfill_transcripts` | Daily 04:30 | `backfill_transcripts.task.xml` | `run_backfill_transcripts.bat` | For every active-universe ticker (`db.ACTIVE_LIST_TYPES`), fetches the last 6 fiscal quarters of Q&A from the free aggregator chain, runs ingest, extracts commitments. Idempotent — re-running with no missing quarters is a no-op. |
| `earnings-summary\fetch_fmp_earnings_calendar` | Daily 05:45 | `fetch_fmp_earnings_calendar.task.xml` | `run_fetch_fmp_earnings_calendar.bat` | Refreshes `data/historical/fmp/<TICKER>_earnings_calendar.json` for every portfolio + watchlist + evaluation ticker. On `basic` tier this 403s and logs noise — the `next_earnings_date` adapter in `src/sources/earnings_calendar.py` falls back to yfinance. |
| `earnings-summary\backfill_earnings_surprises` | Daily 06:15 | `backfill_earnings_surprises.task.xml` | `run_backfill_earnings_surprises.bat` | For every active-universe ticker, merges `<TICKER>_earnings_calendar.json` (FMP primary, full EPS + Revenue surprise) with `yfinance.Ticker.earnings_dates` (fallback, EPS-only) into `data/surprise/<TICKER>_surprises.json`, then upserts into `earnings_surprises`. Idempotent. Revenue surprise degrades to NULL when FMP coverage lapses. |
| `earnings-summary\daily_fetch_and_brief` | Daily 06:30 | `daily_fetch_and_brief.task.xml` | `run_daily_fetch_and_brief.bat` | Drains `tracked_companies.brief_dirty` with three gates: **A** tier cadence (P1 daily, P2 if >7d old, P3 if >30d old), **B** material-change hash (skip if content unchanged AND last build < 7d), **C** evaluation cadence (skip if list_type=evaluation AND last build < 7d). For un-skipped tickers, runs thesis evaluator + DCF refresh + brief regen with `--enable-llm` so §8/§9 populate via the Claude CLI (Gemini fallback). |

The five daily crons run as a chain: refresh_cache (03:00) drains the FMP
priority queue under the configured tier, backfill_transcripts (04:30) pulls
fresh Q&A transcripts + commitments, fetch_fmp_earnings_calendar (05:45)
refreshes the calendar JSON cache, backfill_earnings_surprises (06:15) writes
the merged EPS/Revenue beat-rate cache + DB, and daily_fetch_and_brief (06:30)
drains `brief_dirty=1` and regenerates briefs (gated by content-change + eval
cadence). The 90/75/30/15-min gaps absorb slow aggregator/FMP responses and
let each step's writes commit before the next reads.

### Hourly catch-up

| Task name | Cadence | XML | Wrapper | What it does |
|---|---|---|---|---|
| `earnings-summary\onboard_pending` | Hourly at :17 | `onboard_pending_tickers.task.xml` | `run_onboard_pending.bat` | Catches up tickers that bypassed `db.track_company`'s auto-onboard hook (raw SQL / external API inserts). Idempotent — no-op when nothing is pending. |

### Weekly + monthly synthesis layer

These regenerate the LLM "lens" artifacts (`five_min_reread`, `thesis_drift_qoq`, `bull_case`, `mgmt_credibility_score`, `cross_portfolio_synthesis`, etc.) that the analytical dashboard and per-section briefs consume. Tier-cadence rule: **P1 = daily, P2 = weekly, P3 = monthly.** The daily chain handles P1; these three tasks handle P2 + P3.

| Task name | Cadence | XML | Wrapper | What it does |
|---|---|---|---|---|
| `earnings-summary\weekly_p2_lens_refresh` | Weekly, Sunday 02:00 | `weekly_p2_lens_refresh.task.xml` | `run_weekly_p2_lens_refresh.bat` | Regenerates P2-tier (watchlist + evaluation) lens artifacts drifted past their cadence. Wraps `python execution/run_due_lenses.py --cadence weekly`. Idempotent via `artifact_store.cached_inputs` hash dedup — stable tickers cost nothing. |
| `earnings-summary\weekly_synthesis` | Weekly, Sunday 23:00 | `weekly_synthesis.task.xml` | `run_weekly_synthesis.bat` | **The "Sunday-night portfolio review" pipeline.** Five steps in order: (1) `refresh_dirty_artifacts.py --manifest-only` drains the LLM-artifact dirty queue so lens reads see fresh facts; (2) `run_lens.py --tickers AMZN,BN,GOOG,MELI,META,NOW,NU,NVO,RBRK,VEEV,WIX --all` regenerates every per-ticker lens for the full portfolio; (3) `run_lens.py --lens cross_portfolio_synthesis` runs the Opus cross-portfolio convergence read (~$0.25); (4) `build_analytical_dashboard.py` rebuilds `output/dashboard/<DATE>_portfolio_dashboard.html` with the new artifacts; (5) `grade_bear_cases.py --all-portfolio` grades predictions whose `target_period` has passed. Sequential — any step's failure halts the rest. |
| `earnings-summary\monthly_p3_refresh` | Monthly, 1st @ 03:00 | `monthly_p3_refresh.task.xml` | `run_monthly_p3_refresh.bat` | Regenerates P3-tier (index_member / etf / `none`) lens artifacts drifted past their 90-day cadence. Wraps `python execution/run_due_lenses.py --cadence monthly`. The P3 lens set is minimal (`five_min_reread` only) so the run stays bounded even with 2k+ index constituents — and the `cache_inputs` hash means stable tickers cost nothing. |

The two weekly tasks deliberately bracket the trading week: `weekly_p2_lens_refresh` runs Sunday 02:00 (early) so any P2-tier reads are fresh before the analyst checks in, then `weekly_synthesis` runs Sunday 23:00 (late) so the portfolio dashboard reflects everything that landed during the week, ahead of Monday open. They don't depend on each other — `weekly_synthesis` step 1 (`refresh_dirty_artifacts`) is what guarantees current data, not the earlier weekly run.

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

schtasks /create /tn "earnings-summary\weekly_p2_lens_refresh" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\weekly_p2_lens_refresh.task.xml" ^
  /ru "%USERNAME%"

schtasks /create /tn "earnings-summary\weekly_synthesis" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\weekly_synthesis.task.xml" ^
  /ru "%USERNAME%"

schtasks /create /tn "earnings-summary\monthly_p3_refresh" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\monthly_p3_refresh.task.xml" ^
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
- For `weekly_p2_lens_refresh`: new rows in the `llm_artifacts` table for
  P2-tier (watchlist + evaluation) tickers whose previous lens runs had
  drifted past the weekly cadence. Stable tickers log "cache hit — skipping"
  via the `cache_inputs` hash; that's the desired no-op behavior.
- For `weekly_synthesis`: five `=== <TIME> ...` step markers in the log
  (drain dirty → per-ticker lenses → cross-portfolio synthesis → dashboard
  rebuild → bear-case grading). On success, expect a fresh
  `output/dashboard/<DATE>_portfolio_dashboard.html`, new `llm_artifacts`
  rows for the eleven portfolio tickers + one `cross_portfolio_synthesis`
  artifact, and updated grades on any bear-case predictions whose
  `target_period` has passed.
- For `monthly_p3_refresh`: new `llm_artifacts` rows for P3-tier
  (index_member / etf / `none`) tickers that drifted past their 90-day
  cadence. Bounded — the P3 lens set is `five_min_reread` only.

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
