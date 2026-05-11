# Directive: Onboard Pending Tickers (recurring catch-up)

## Goal

Belt-and-suspenders for `db.track_company`'s auto-onboard subprocess hook. When
a ticker enters `tracked_companies` via raw SQL / external API / direct DB
edit, the spawn-`onboard_ticker.py` codepath inside `track_company` is bypassed
and the ticker stays at "documents only / no facts / no DCF" forever.

This directive scans for those orphans every hour and runs the rest of the
onboard pipeline against them.

## Tools / Scripts

| Purpose | Path |
|---|---|
| Catch-up CLI | `execution/onboard_pending_tickers.py` |
| Per-ticker onboarder | `execution/onboard_ticker.py` |
| Thesis evaluator | `execution/run_thesis_evaluator.py` |
| DCF runner | `execution/batch_dcf.py` |
| **Auto commitment extractor** | `execution/extract_commitments_from_transcript.py --auto` |
| Cron wrapper | `cron/run_onboard_pending.bat` |
| Scheduled-task definition | `cron/onboard_pending_tickers.task.xml` |

## "Pending" definition (immutable)

A row in `tracked_companies` is pending when ALL of:

- `list_type IN ('portfolio', 'watchlist')`

AND ANY of:

| Reason code | Signal | Meaning |
|---|---|---|
| `no_instrument_type` | `instrument_type IS NULL` | `track_company` would have set this; missing → bypassed the hook |
| `no_financial_facts` | 0 `financial_facts` rows | parse stage never ran |
| `no_dcf_run` | 0 `dcf_runs` rows | analysis stage never ran |
| `no_commitments` | has ≥1 `transcripts` row but 0 `management_commitments` | LLM extractor never ran for this ticker's transcripts |

Reason precedence is the order above — first matching condition wins. Index /
ETF / `'none'` rows are deliberately excluded.

A fully-onboarded ticker with NO transcripts is intentionally NOT pending —
nothing for the extractor to chew on. The `no_commitments` signal only fires
when transcripts exist.

## Per-ticker pipeline

Stage subset depends on `pending_reason`:

| pending_reason | Stages run |
|---|---|
| `no_instrument_type`, `no_financial_facts`, `no_dcf_run` | `onboard_ticker` → `run_thesis_evaluator` → `batch_dcf` → `extract_commitments` |
| `no_commitments` | `extract_commitments` only (heavy stages skipped because the ticker is already onboarded) |

```
no_instrument_type / no_financial_facts / no_dcf_run:
  onboard_ticker (FMP fetch + parse)
    -> run_thesis_evaluator
    -> batch_dcf
    -> extract_commitments_from_transcript --auto

no_commitments:
  extract_commitments_from_transcript --auto
```

Each stage is a subprocess. The eval / DCF / extraction stages are
best-effort: they exit non-zero when a ticker has no holdings JSON,
insufficient facts, no transcripts, or LLM auth is misconfigured — but
that does not abort the chain. Each stage's outcome is recorded in the
structured result and the ticker advances to the next.

The `--skip-commitments` flag suppresses the extraction stage globally
(useful for runs where LLM auth is unavailable or you want to keep
runtime predictable).

## Idempotency

- `save_fmp_data --skip-existing` (called by `onboard_ticker.py`) is a no-op
  on already-fetched endpoints.
- `run_thesis_evaluator` always recomputes from current facts — repeated runs
  produce identical outputs given identical inputs.
- `batch_dcf` returns `skipped` when a ticker already has a current-quarter
  DCF run.
- `extract_commitments --auto` skips transcripts that already have ≥1
  `management_commitments` row. New transcripts arriving between cron
  ticks get extracted on the next run.

Net: re-running the catch-up while no tickers are pending exits 0 with an
empty `results` array. Safe at any cadence.

## Rate-limit budget

FMP free-tier limit applies: 250 requests/min, 750 calls/day. `save_fmp_data`
fetches ~30 endpoints per ticker, so the script can comfortably onboard ~25
fresh tickers per hour without hitting the daily cap. The hourly cron
self-paces by definition — it walks pending tickers serially and exits when
done.

## Failure-mode policy

| Class | Example | Action |
|---|---|---|
| FMP transient | 5xx, 429 | `save_fmp_data` retries with backoff. Onboard chain continues regardless of FMP exit code. |
| Schema/contract | parse stage barfs on unexpected FMP shape | Mark stage `failed`, continue chain. Surface in structured report so the user sees the ticker count vs. successes. |
| No holdings JSON | `run_thesis_evaluator` skips the ticker | Stage `failed` (rc=non-zero); does not block DCF. |
| Hard halt | DB schema drift, alembic mismatch | Whole script fails non-zero; cron emits a non-zero exit so Task Scheduler marks the run failed in its history. |

## Output

- stdout: a JSON `RunReport` containing `run_id`, per-ticker stage outcomes,
  log file path. Designed for downstream parsing.
- stderr: structured logs via Python `logging` (one line per ticker).
- Per-run log: `.tmp/cron_logs/onboard_pending_<UTC>.log` with the full
  subprocess stdout/stderr concatenated.

## Cadence

Hourly is the default — the cost is negligible (a single `SELECT` on an empty
result set when nothing is pending) and an hourly cadence means a newly-added
ticker is onboarded within ~1 hour of being added regardless of which path
added it. Daily would also be defensible; sub-hourly is overkill.

## Verification

After each scheduled run:
- [ ] `.tmp/cron_logs/onboard_pending_<UTC>.log` exists and starts with
      `PROJECT_ROOT=…`.
- [ ] Task Scheduler history shows the run with exit code 0.
- [ ] `python execution/onboard_pending_tickers.py --dry-run` returns
      `pending_count: 0`.

## Coordination with the daily worker

The daily worker (`cron/daily_fetch_and_brief.task.xml`) drains
`tracked_companies.brief_dirty` and runs the synth→publish slice
(`run_thesis_evaluator → match_commitments → refresh_dcf → build_artifacts`)
on each dirty ticker. It assumes facts already exist.

When a new ticker is added between worker ticks via raw SQL / external API /
direct DB edit, this hourly catch-up gets it to the "facts present" state
within ~1 hour. Once the catch-up writes fact rows, the SQL triggers from
migration 0026 flip `brief_dirty=1`, and the daily worker picks the ticker
up on its next tick (06:30 local time).

The two crons compose cleanly — no shared state beyond `brief_dirty`.
