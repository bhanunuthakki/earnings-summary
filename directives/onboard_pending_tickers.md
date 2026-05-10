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
| Per-ticker onboarder (called per pending ticker) | `execution/onboard_ticker.py` |
| Thesis evaluator (called per pending ticker) | `execution/run_thesis_evaluator.py` |
| DCF runner (called per pending ticker) | `execution/batch_dcf.py` |
| Cron wrapper | `cron/run_onboard_pending.bat` |
| Scheduled-task definition | `cron/onboard_pending_tickers.task.xml` |

## "Pending" definition (immutable)

A row in `tracked_companies` is pending when ALL of:

- `list_type IN ('portfolio', 'watchlist')`

AND ANY of:

| Signal | Meaning |
|---|---|
| `instrument_type IS NULL` | `track_company` would have set this; missing → bypassed the hook |
| `0 financial_facts rows` | parse stage never ran |
| `0 dcf_runs rows` | analysis stage never ran |

Index/ETF/'none' rows are deliberately excluded — they're not in the analytical
universe.

## Per-ticker pipeline

```
onboard_ticker.py (FMP fetch + parse) → run_thesis_evaluator.py → batch_dcf.py
```

Each stage is a subprocess. The eval + DCF stages are best-effort: they exit
non-zero when a ticker has no holdings JSON or insufficient facts, but that
does not abort the chain — those stages get marked `failed` in the structured
result and the ticker advances to the next.

## Idempotency

- `save_fmp_data --skip-existing` (called by `onboard_ticker.py`) is a no-op
  on already-fetched endpoints.
- `run_thesis_evaluator` always recomputes from current facts — repeated runs
  produce identical outputs given identical inputs.
- `batch_dcf` returns `skipped` when a ticker already has a current-quarter
  DCF run.

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

## Coordination with autopilot

The monthly autopilot (`cron/autopilot.task.xml`) refreshes already-onboarded
tickers' analytical state from new transcripts/IR docs. It assumes facts already
exist. When a new ticker is added between autopilot runs, this hourly catch-up
gets it to the "facts present" state in time for the next autopilot tick.

Net: no edits needed to `directives/autopilot.md` — the two directives
compose cleanly.
