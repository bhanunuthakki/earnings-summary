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
| DCF runner | `execution/refresh_dcf.py` |
| **Auto commitment extractor** | `execution/extract_commitments_from_transcript.py --auto` |
| Cron wrapper | `cron/run_onboard_pending.bat` |
| Scheduled-task definition | `cron/onboard_pending_tickers.task.xml` |

## "Pending" definition (immutable)

A row in `tracked_companies` is pending when ALL of:

- `list_type IN ('portfolio', 'watchlist', 'evaluation')` (the `db.ACTIVE_LIST_TYPES` set)

AND ANY of:

| Reason code | Signal | Meaning |
|---|---|---|
| `no_instrument_type` | `instrument_type IS NULL` | `onboard_ticker` classifies this from the FMP profile (`set_instrument_type_from_fmp`); NULL means the onboard chain never ran for this ticker |
| `no_financial_facts` | 0 `financial_facts` rows | parse stage never ran |
| `no_dcf_run` | 0 `dcf_runs` rows | analysis stage never ran |
| `no_commitments` | has ≥1 *extractable* `transcripts` row but 0 `management_commitments` | LLM extractor still has work to do for this ticker's transcripts |

Reason precedence is the order above — first matching condition wins. Index /
ETF / `'none'` rows are deliberately excluded.

A fully-onboarded ticker with NO transcripts is intentionally NOT pending —
nothing for the extractor to chew on. The `no_commitments` signal only fires
when transcripts exist.

**"Extractable" mirrors the extractor's own target selection**
(`compute.say_do_extractor.transcripts_pending_extraction`). Two additional
guards, both using existing durable state:

- A transcript with a `commitment_scan_log` row is done — a recorded
  zero-commitment scan is a real outcome, not a retry candidate.
- A ticker with an empty `kpi_definitions` catalog is excluded — the
  extraction outcome is predetermined (zero commitments, no LLM call, no scan
  marker), so it stays out of the queue until a catalog is seeded, at which
  point it becomes pending again automatically.

Any predicate looser than the extractor's re-queues tickers whose
`--auto` run returns `targets=0` — an hourly no-op subprocess forever (the
2026-07-16 MELI/AGX/DASH/FIGR eternal-churn). On a pre-0129 DB (no
`commitment_scan_log` table) the scan-log guard is omitted, matching the
extractor's own graceful degrade.

## Per-ticker pipeline

Stage subset depends on `pending_reason`:

| pending_reason | Stages run |
|---|---|
| `no_instrument_type`, `no_financial_facts`, `no_dcf_run` | `onboard_ticker` → `run_thesis_evaluator` → `refresh_dcf` → `extract_commitments` |
| `no_commitments` | `extract_commitments` only (heavy stages skipped because the ticker is already onboarded) |

```
no_instrument_type / no_financial_facts / no_dcf_run:
  onboard_ticker (FMP fetch + parse)
    -> run_thesis_evaluator
    -> refresh_dcf  (seeds dcf/<TICKER>.xlsx if absent, then refreshes Historicals + PV)
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

## Identity and repeat safety

- **Logical Idempotency Key:** `(ticker, pending_reason, required_stage_set)` at the
  current required Observation Version.
- **Content Identity:** digests of the fetched source documents, holdings input, and
  generated DCF/artifact bytes used by each stage.
- **Observation Version:** current tracked-company state plus the source filing,
  transcript, facts, and configuration revisions that caused the ticker to be pending.
- **Attempt Identity:** the report's `run_id`; retries always receive a distinct value.

- `save_fmp_data --skip-existing` (called by `onboard_ticker.py`) is a no-op
  on already-fetched endpoints.
- `run_thesis_evaluator` always recomputes from current facts — repeated runs
  produce identical outputs given identical inputs.
- `refresh_dcf` seeds `dcf/<TICKER>.xlsx` from a template if absent and
  refreshes the Historicals sheet from FMP on every run; the PV calc is
  re-run from the user-owned Valuation sheet. Tickers without a `wacc` in
  their holdings JSON exit `skipped` (no dcf_runs write).
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

## Recently-IPO'd backoff (daily cadence)

A ticker flagged `recently_ipod: true` in `micro_thesis/holdings/<T>.json` has
almost no FMP coverage until its first 10-Q is ingested — often months after
IPO. The pending SQL flags it `no_financial_facts` every run, so left unguarded
the hourly cron would re-run the full ~60-endpoint onboard for it hourly,
burning ~720 FMP calls/day against the 750/day cap for a ticker that has no new
data to fetch.

`apply_ipo_backoff` (in `onboard_pending_tickers.py`) defers such tickers to a
**daily** cadence instead of skipping them:

- A recently-IPO'd ticker with a heavy-chain reason is deferred only if its most
  recent FMP fetch (`MAX(fmp_endpoint_status.last_pulled)`) was **< 24 h** ago.
- A ticker that was **never** fetched (no `fmp_endpoint_status` rows) is NOT
  deferred — its first onboard always runs.
- Once FMP ingests the company's first filings, the next daily re-check onboards
  it normally. The cadence is lowered, never eliminated.
- Non-IPO tickers and the `no_commitments` reason (cheap, FMP-free) are never
  deferred — the rest of the universe keeps its hourly cadence.

Deferred tickers are surfaced under the report's `deferred` array (also in
`--dry-run` output) so they're visible, not silently dropped.

This pairs with the `save_fmp_data` fix that records an accessible-but-empty
`/stable` endpoint (HTTP 200 + `[]`) as `empty` rather than `forbidden` — the
two together make "has FMP started covering this IPO yet?" answerable from
`fmp_endpoint_status`.

## Failure-mode policy

| Class | Example | Action |
|---|---|---|
| FMP transient | 5xx, 429 | `save_fmp_data` retries with backoff. Onboard chain continues regardless of FMP exit code. |
| Schema/contract | parse stage barfs on unexpected FMP shape | Mark stage `failed`, continue chain. Surface in structured report so the user sees the ticker count vs. successes. |
| No holdings JSON | `run_thesis_evaluator` skips the ticker | Stage `failed` (rc=non-zero); does not block DCF. |
| Hard halt | DB schema drift, alembic mismatch | Whole script fails non-zero; cron emits a non-zero exit so Task Scheduler marks the run failed in its history. |

## Output

- stdout: a JSON `RunReport` containing `run_id` (the Attempt Identity), per-ticker stage outcomes,
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
