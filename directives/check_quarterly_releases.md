# Directive: Monthly Quarterly-Release Check (Cron)

## Goal

On a monthly schedule, walk the portfolio, detect any new quarterly earnings releases via FMP, and pull the corresponding Q&A transcript into `transcripts/raw/` so the say-do, summary, and master-PDF pipelines pick it up automatically. Designed to run unattended via Windows Task Scheduler — see `cron/SETUP_WINDOWS_SCHEDULER.md`.

## Tools / Scripts

| Purpose | Script |
|---|---|
| Orchestrator (Layer 3) | `execution/check_quarterly_releases.py` |
| Portfolio + fiscal config | `src/portfolio.py` |
| Wrapper (.bat) | `cron/run_check_quarterly_releases.bat` |
| Task definition | `cron/check_quarterly_releases.task.xml` |

## Inputs

| Input | Where |
|---|---|
| Portfolio | `src/portfolio.get_portfolio()` — DB rows from `tracked_companies`, falling back to a hardcoded baseline of 11 names |
| FMP API key | `.env` → `FMP_API_KEY` |
| Date window | `--days <N>` (default 45 — covers a monthly cron with 2-week safety overlap) |
| Audio fallback | `--with-audio-fallback` flag (off by default) |

## Pipeline (per cron run)

```
For each ticker in portfolio:
  1. FMP /stable/income-statement?symbol=T&period=quarter&limit=4
       → list[(fiscal_year, period_label, period_end, filing_date)]
  2. FMP /stable/earnings?symbol=T&limit=12
       → augmentation: announcement_date per quarter (matched by filing_date)
  3. Filter to reports where announcement_date or filing_date is within --days of today
       AND <= today (i.e. actually filed, not forward-looking)
  4. For each surviving report:
       a) If transcript_index has qa_status=ok → "skipped_have_qa_ok"
       b) Try fetch_qa_transcript.fetch_qa()  →  if qa=ok → "aggregator_ok"
       c) (Optional, --with-audio-fallback) try fetch_audio_transcripts.fetch_and_transcribe() with smart search → "audio_ok"
       d) Else → "miss" (logged in run report for human review)

Write a structured run report to .tmp/cron_runs/<run_id>.json.
```

## Why this shape

- **FMP `/stable/income-statement` is the authoritative source of fiscal labels.** Quarter announcement dates from a global earnings calendar don't carry fiscal-year/quarter labels for fiscal-Q1-end-Jan tickers (RBRK, VEEV); the income-statement endpoint returns `period: "Q4"`, `fiscalYear: 2026` directly, exactly matching what aggregator URLs expect (verified for roic.ai).
- **Augmentation with `/stable/earnings` adds the announcement date** so the `--days` window filter is precise even when filing-date and announcement-date diverge (common for foreign filers like NVO — 6-K filing happens shortly after the announcement).
- **Aggregator-first, audio-as-fallback** matches the documented priority chain: `fetch_qa_transcript.py` (free, ~1s/quarter) before `fetch_audio_transcripts.py` (CPU-heavy, ~25 min/quarter, occasional false hits via ytsearch).
- **Skip via `qa_status=ok` (not just file presence)** — re-runs after a partial failure correctly retry only the qa=failed entries; everything else is a no-op.

## Outputs

| Artifact | Path | Shape |
|---|---|---|
| Per-quarter transcript | `transcripts/raw/<TICKER>_Q<N>_<YEAR>.txt` | Synthesizer-banner + Q&A (or Whisper timestamps if audio path) |
| Run report | `.tmp/cron_runs/<run_id>.json` | `RunReport` Pydantic model: per-quarter status, action, source, qa_status, errors |
| Wrapper log | `.tmp/cron_logs/check_quarterly_*.log` | Full stdout/stderr of the run |

`run_id` format: `check_quarterly_<YYYYMMDD>T<HHMMSS>Z`.

## Schedule

Default trigger (per `cron/check_quarterly_releases.task.xml`): **15th of every month, 06:00 local time.** Chosen because most US-listed Q4 reports land in the first 3 weeks of February, Q1 reports in the first 3 weeks of May, etc. — so by the 15th the aggregators have indexed everything.

If a fire is missed (machine off): `StartWhenAvailable=true` + `RestartOnFailure` settings ensure the scheduler runs the missed task as soon as the machine is back online.

## Edge cases & known constraints

- **NVO foreign-filer lag**: Novo Nordisk announces under Danish stock-exchange rules and files a 6-K to the SEC. FMP's `filingDate` may lag the announcement by 1-3 days. The augmentation pull from `/stable/earnings` catches the announcement date so the `--days` window doesn't miss the report.
- **Most-recent-quarter-not-yet-indexed**: If the cron fires within 12-24 hours of a call, aggregators may not have indexed it yet. Outcome: `action="miss"` in the run report. Next monthly fire will catch it.
- **FMP free-tier rate**: ~22 calls per run (2 endpoints × 11 tickers). Comfortably within the daily limit.
- **Idempotency**: each step is idempotent. Re-running with the same window is safe — already-`qa=ok` entries are skipped; failed-QA transcripts keep their cached audio for human re-attempt.
- **Portfolio drift**: when you add/remove a ticker via the front-end, the DB-backed `tracked_companies` updates and the next cron picks it up automatically. The hardcoded `_DEFAULT_PORTFOLIO` in `src/portfolio.py` only fires on a fresh clone with empty DB.

## Verification (after a real fire)

- [ ] `.tmp/cron_runs/check_quarterly_<latest>.json` exists and has `quarters_examined: list[QuarterStatus]` with one entry per recently-filed quarter.
- [ ] `[summary]` line in the wrapper log shows non-zero `aggregator_ok` count for any new quarters.
- [ ] No `action="error"` entries in the run report.
- [ ] `transcripts/raw/` has new `<TICKER>_Q<N>_<YEAR>.txt` files for the quarters reported in the window; each has `qa_status=ok` per `python execution/qa_transcripts.py --report`.
- [ ] Re-running the orchestrator immediately produces all-`skipped_have_qa_ok` (idempotency check).
