# Data Pipeline DAG

**Status**: Layer 1 baseline. Defines the 8 stages every per-task directive composes from. Immutable without explicit user authorization.

**Why this exists**: The pipeline as it stood was ad-hoc script-calls-script with `.tmp/` JSON as the only inter-stage contract. That doesn't scale to the FMP × SEC × IR × audio × manual matrix. Every per-task directive (`fetch_transcripts`, `fetch_ir_documents`, `quarterly_refresh`, etc.) is now expressed as a slice of these 8 stages with explicit per-stage status, contracts, and resumption.

## Stages

```
INGEST → TRANSCRIBE → PARSE → VALIDATE → PERSIST → COMPUTE → SYNTHESIZE → PUBLISH
```

Each stage:
- Has typed inputs and outputs (Pydantic models in `src/models/`).
- Writes one row per `(run_id, ticker, period_end, stage)` to `stage_transitions`; `run_id` is the Attempt Identity.
- Is logically idempotent: rerunning the same Logical Idempotency Key at the same Observation Version is a no-op unless `--force`.
- Is resumable: a failed Attempt Identity restarts from its first `(ticker, period_end, stage)` where `status != ok`.

## Per-stage contract

| Stage | Input | Output | Terminal status (subset) |
|---|---|---|---|
| INGEST | `(ticker, source_type, doc_type, period_end)` | `documents` row + raw file in `data/raw/{source}/{ticker}/` | `OK`, `FAILED` |
| TRANSCRIBE | `documents.id` of audio | `documents` row of transcript + `transcript_segments` rows | `OK`, `SKIPPED`, `FAILED` |
| PARSE | `documents.id` | Pydantic-validated payload in `.tmp/parsed/{run_id}/{document_id}.json` | `OK`, `FAILED` |
| VALIDATE | parsed payload | `validation_issues` rows (severity `warn` or `halt`) | `OK`, `NEEDS_REVIEW`, `FAILED` |
| PERSIST | validated payload | rows in `financial_facts` / `segment_facts` / `kpi_facts` | `OK`, `FAILED` |
| COMPUTE | facts | derived metrics (ROE, ROA, ROIC, FCF, segment incremental margin, DCF) into `metric_facts` and `dcf_runs` | `OK`, `SKIPPED`, `FAILED` |
| SYNTHESIZE | facts + transcripts + thesis | LLM summary, Say-Do, thesis-state delta | `OK`, `FAILED` |
| PUBLISH | summaries + facts | master PDF, thesis tracker update, frontend feed | `OK`, `FAILED` |

The full enum lives in `src.models.runs.StageStatus`.

## Identity and repeat safety

- **Logical Idempotency Key:** `(directive_name, ticker_scope, period_end, stage)`;
  this is the stable business effect used to prevent duplicate work.
- **Attempt Identity:** `run_id = {directive_name}_{ticker_scope}_{period_end}_{started_at_iso}`;
  this changes on every execution and owns logs, costs, and checkpoints.
- **Observation Version:** the source-side version plus Content Identity described in
  `directives/data_provenance.md`; changed source content may legitimately rerun the same
  Logical Idempotency Key and append a new version.

Resumption queries one Attempt Identity and proceeds from its first non-`ok` stage.
Cross-attempt skip logic compares the Logical Idempotency Key and required Observation Version,
never the timestamp-bearing `run_id`.

## Intermediate and telemetry lifecycle

Pipeline checkpoints and `.tmp/` files are disposable only after they are no longer
needed for exact resumption. Active checkpoint trees, malformed or unrecognized
`state.json` files, locks, database/recovery material, and unverified temporary audio
fail closed and are never cleanup candidates.

The current retention defaults are:

- 30 days for completed checkpoint trees and general disposable `.tmp/` artifacts;
- seven days for rebuildable caches, including news and Python tool caches; and
- 90 days for bounded pipeline telemetry (`stage_transitions`, `source_calls`, and
  `ingestion_runs`).

`execution/run_weekly_cleanup.py` and `execution/db_gc.py` are the executable
allowlists and exact cutoff implementations. Their already-registered weekly jobs are
the only cleanup writers; this lifecycle does not authorize a new operation, schedule,
or generic directory sweep. A retention change belongs here first, followed by its
executable constant and focused tests.

## Failure-mode policy

| Class | Example | Action |
|---|---|---|
| Transient | 5xx, network timeout, 429 | Retry with exponential backoff (jittered, max 3 attempts). On final failure, mark stage `FAILED` and halt the run. |
| Schema/contract | Missing field, enum value not in `DocType` | Mark stage `FAILED` with `validation_issues` rule `SCHEMA_DRIFT`. Do not retry. Surface to the user — the directive needs updating. |
| Auth | 401, 403 | Halt immediately. Do not retry. |
| Validation halt | Severity `halt` issue (range, currency, period) | Stage `FAILED`. Do not advance to PERSIST. |

## Resumption

After failure, `python execution/daily_fetch_and_brief.py --ticker <T>` (or re-run the relevant per-ticker CLI):
1. Reads `stage_transitions` for the run.
2. Identifies the first `(ticker, period_end, stage)` with `status != ok`.
3. Re-runs from there.
4. Never silently restarts from stage 0.

## Routing at INGEST

Per `directives/data_provenance.md`, routing is decided at INGEST based on `(instrument_type, kpi_definitions)`:

- If `companies.instrument_type == 'etf'`: sources = `{etf_endpoint_set}` only.
- Else if any `kpi_definitions` row for this ticker has `primary_source != 'fmp'`: sources = `{fmp, ir_doc, sec_xbrl}` (the union of what's needed).
- Else: sources = `{fmp}`.

Routing is data-driven — no hardcoded ticker logic. Adding a name to the IR-override registry (a `kpi_definitions` row) automatically opts that name into IR-fetch on the next run.

**Manual IR uploads are an additional, orthogonal source.** The user typically uploads IR PDFs/XLSX only for portfolio names + a subset of watchlist names, not for every tracked ticker. `categorize_ir_uploads.py` picks them up from the root of `ir_documents/`, classifies them, and registers `documents` rows with `source_type='ir_doc'` independent of the routing rules above. Tickers with manual uploads but no `kpi_definitions` IR override still get IR rows in `documents` — downstream consumers should query `documents` directly rather than gating on the routing decision. Tickers with neither auto-fetch routing nor manual uploads simply have no `ir_doc` rows; downstream queries `LEFT JOIN`, and synthesis proceeds with whatever facts are present.

## Refresh cadence

Per `directives/data_provenance.md` §6 (per-source overrides) and the project memory:

- One-time backfill: full history per name on entry into the book.
- Quarterly refresh: triggered by either (a) a new period appearing in `FMP_FINANCIAL_REPORTS_DATES`, or (b) elapsed wall-clock quarter, whichever fires first.
- Stage 3 IR-override fetches run on the same trigger as the FMP refresh.

## What this rules out

- No transformation logic in Layer 2 (orchestration). Layer 2 sequences and reads stdout/stderr; it does not parse, validate, or compute.
- No long-running monolithic scripts that cross stages. Each stage is its own executable in `execution/`.
- No "this run partially succeeded so we'll just keep going." A `HALT` validation kills the run.
- No skipping VALIDATE. PERSIST never reads from PARSE directly.
