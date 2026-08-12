# Directive: Backfill Transcripts + Commitments

## Goal

For policy-authorized companies, keep at most the canonical last 5 reported
fiscal quarters of text Q&A transcripts on disk AND in the database and populated with
extracted forward-looking commitments — so §5 Earnings and §6 Say-Do are
populated in the brief from the moment a new ticker is onboarded, not
weeks later when the user remembers to run the fetchers manually.

Every network attempt is bound to the active stored identity and `list_type`:
portfolio is automatic; evaluation is allowed only by explicit owner `--ticker`
request; watchlist, index, ETF, unknown, malformed, and ambiguous identities fail
closed. Audio/webcast extraction is excluded. An approved manual transcript file
may still enter through the existing ingest path without a network crawl.

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
| 4. Promote | `ingest_transcripts._promote_raw_to_processed` (in-process, same script as Ingest) | After a successful fresh ingest, atomically moves the source file to `transcripts/processed/` and updates `documents.file_path` + index entries. Idempotent on re-runs; opt out with `--no-promote`. |
| 5. Extract commitments | `execution/extract_commitments_from_transcript.py --auto --ticker X` (subprocess, one per ticker) | LLM extracts forward-looking commitments from transcripts not already in `management_commitments` |

## Entry points

| Trigger | Cadence | Scope |
|---|---|---|
| `execution/onboard_ticker.py` | Per company onboarding | Stored-role policy applies; no implied evaluation escalation |
| `cron/backfill_transcripts.task.xml` | Daily 02:00 | Non-archived portfolio only |
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
| Policy denial | Stored identity/role missing, invalid, ambiguous, or too shallow | Structured denial before network access; run continues for other authorized names. |
| Aggregator miss | All text sources return "no transcript found" for (T, Y, Q) | Logged as `aggregator_misses`; run continues. The owner may place an approved text transcript in `transcripts/raw/`. |
| Aggregator error | Network timeout, parse failure on a single source | Tried sources in order, first hit wins; per-source failures are silent and the chain falls through. Script-level exceptions land in `errors` for the JSON summary. |
| Ingest failure | Malformed file, schema drift | Surfaced by `ingest_transcripts.py`'s own per-file error path (logged to stderr; the offending file is skipped). |
| Extract failure | LLM API error, JSON parse error | Logged per-transcript; `extract_commitments_from_transcript.py --auto` continues to the next transcript. |

Per-name misses and transient failures do not halt the run. A child ingest failure
is terminal for that run; authentication and schema/contract failures halt loudly.

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

The script attempts the last N quarter ends that have already passed, where
`1 <= N <= SOURCE_POLICY_CONFIG.reported_quarter_window.max_quarters`,
and lets the aggregator chain tell us via "miss" whether the call
happened yet.
