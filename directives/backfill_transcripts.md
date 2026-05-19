# Directive: Backfill Transcripts + Commitments

## Goal

For every ticker the user actively analyzes (`db.ACTIVE_LIST_TYPES` =
portfolio + watchlist + evaluation, non-archived), keep the last ~6 fiscal
quarters of Q&A transcripts on disk AND in the database AND populated with
extracted forward-looking commitments — so §5 Earnings and §6 Say-Do are
populated in the brief from the moment a new ticker is onboarded, not
weeks later when the user remembers to run the fetchers manually.

## Why this exists

Before this directive, transcript acquisition was three manual steps:

```
fetch_qa_transcript.py --ticker X --year Y --quarter Q   # one per quarter
ingest_transcripts.py                                    # bulk register
extract_commitments_from_transcript.py --auto --ticker X # one per ticker
```

`db.track_company` spawned `onboard_ticker.py`, which ran the FMP fetch +
parse stages but **never touched transcripts**. That left a permanent gap:
new evaluation tickers (and any catch-up watchlist/portfolio tickers)
shipped briefs with §5 Earnings empty and §6 Say-Do empty, because no
commitments existed in `management_commitments` to render.

## Composed of

| Step | Tool | What changes |
|---|---|---|
| 1. Quarter calendar | `backfill_transcripts.recent_fiscal_quarters()` | Pure function; `(fye_month, today, n) → [(fiscal_year, fiscal_quarter), ...]` |
| 2. Aggregator fetch | `fetch_qa_transcript.fetch_qa()` | Writes `transcripts/raw/<T>_Q<n>_<Y>.txt` if any source in the chain has it |
| 3. Ingest | `execution/ingest_transcripts.py` (subprocess) | Walks `transcripts/{raw,processed}/`, registers files in `transcripts` + `transcript_segments` keyed by sha256 |
| 4. Extract commitments | `execution/extract_commitments_from_transcript.py --auto --ticker X` (subprocess, one per ticker) | LLM extracts forward-looking commitments from transcripts not already in `management_commitments` |

## Entry points

| Trigger | Cadence | Scope |
|---|---|---|
| `execution/onboard_ticker.py` | Per `db.track_company` add into `ACTIVE_LIST_TYPES` | Single ticker, fire-and-forget at the end of the onboard pipeline |
| `cron/backfill_transcripts.task.xml` | Daily 04:30 | Full active universe |
| Manual ad-hoc | On-demand | `python execution/backfill_transcripts.py [--ticker X] [--lookback-quarters N]` |

The daily cron + per-ticker onboard hook are belt-and-braces. The cron
catches:
- Quarters that aggregators didn't have when the onboarder ran but have
  indexed since.
- Tickers that bypassed `track_company`'s auto-spawn (raw SQL inserts).
- Newly-reported quarters as they land in the aggregator chain.

## Idempotency

| Layer | Key |
|---|---|
| Fetch | File presence check: skip if `transcripts/raw/<T>_Q<n>_<Y>.txt` OR `transcripts/processed/<T>_Q<n>_<Y>.txt` exists |
| Ingest | sha256 of the file (`compute.transcript_ingest.ingest_one`) |
| Extract | "transcripts pending extraction" = transcripts with zero `management_commitments` rows |

Running the script three times in a row produces zero new rows the second
and third times.

## Failure-mode policy

| Class | Example | Action |
|---|---|---|
| Aggregator miss | All sources return "no transcript found" for (T, Y, Q) | Logged as `aggregator_misses`, run continues. Common for delisted micro-caps and certain foreign issuers (e.g. NTDOY) — these tickers simply won't get §5/§6 unless the user manually drops a transcript into `transcripts/raw/`. |
| Aggregator error | Network timeout, parse failure on a single source | Tried sources in order, first hit wins; per-source failures are silent and the chain falls through. Script-level exceptions land in `errors` for the JSON summary. |
| Ingest failure | Malformed file, schema drift | Surfaced by `ingest_transcripts.py`'s own per-file error path (logged to stderr; the offending file is skipped). |
| Extract failure | LLM API error, JSON parse error | Logged per-transcript; `extract_commitments_from_transcript.py --auto` continues to the next transcript. |

No failure class halts the run. The script is designed to make
opportunistic progress over time.

## Quarter calendar math

The (fiscal_year, fiscal_quarter) → calendar period_end mapping in
`backfill_transcripts._quarter_end_date()` mirrors the convention used by
the existing aggregator filename pattern `<T>_Q<n>_<Y>.txt`:

- `fiscal_year` is the calendar year the fiscal year **ends in**
- `fiscal_quarter` is 1..4 within that fiscal year
- For FYE month M, period_end of fiscal Q<q> is the last day of month
  `M - 3*(4-q)` (with calendar-year roll-back when that goes < 1)

| FYE month | Q1 ends | Q2 ends | Q3 ends | Q4 ends |
|---|---|---|---|---|
| 12 (Dec, calendar) | Mar Y | Jun Y | Sep Y | Dec Y |
| 9 (Sep, AAPL) | Dec Y-1 | Mar Y | Jun Y | Sep Y |
| 6 (Jun, KLAC) | Sep Y-1 | Dec Y-1 | Mar Y | Jun Y |
| 3 (Mar, HDB/NTDOY) | Jun Y-1 | Sep Y-1 | Dec Y-1 | Mar Y |

The script attempts the last N quarter ends that have already passed
and lets the aggregator chain tell us via "miss" whether the call
happened yet.
