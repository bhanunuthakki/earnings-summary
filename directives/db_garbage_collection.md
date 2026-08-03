# DB Garbage Collection

> Status: RATIFIED 2026-07-31 (owner). Windows 20Q/12FY confirmed as the
> standing defaults, and **portfolio IS included** in the facts-depth window
> (`--include-portfolio` is the standing invocation). First apply ran
> 2026-07-31 after a verified snapshot
> (data/archive/pre_gc_20260731_portfolio.db) and a credibility-priors
> re-baseline; full ratified apply (facts-depth incl. portfolio + VACUUM)
> completed 2026-08-02. Weekly cron REGISTERED 2026-08-02 (owner-authorized):
> `cron/db_gc.task.xml` + `cron/run_db_gc.bat`, Sunday 06:00 PT, VACUUM on
> the first Sunday of the month. Grounded in the 2026-07-30 three-way
> consumer audit of financial_facts, kpi_facts, and the telemetry tables.

## Goal

Keep `data/portfolio.db` bounded without changing what any current consumer
renders or computes. Attack the three real growth vectors: duplicated
validation issues (~10.2k identical re-inserts/day), unbounded per-ticker
fact history far deeper than any reader's window, and (future) telemetry age.

## Tool

`execution/db_gc.py` — single-purpose CLI, dry-run by default, `--apply` to
write. Rows deleted from the ARCHIVED tables (financial_facts,
metric_computation_attempts, validation_issues, the telemetry trio) are first
copied, schema-identical, into `data/archive/portfolio_gc_archive.db` (with a
`gc_manifest` per-pass log; UNIQUE identity indexes + INSERT OR IGNORE make
re-archiving idempotent). Two planes are NOT archived and NOT reversible by
INSERT ... SELECT: the 0225 resolution-plane cascade deletes
(fact_observation_revisions / legacy_fact_evidence_match_revisions /
fact_selection_decisions), and the pre-delete `supersedes_id = NULL` rewrite
on SURVIVING facts (the sidecar holds the doomed rows' originals, not the
survivors'). Restore is therefore governed by the fail-closed audit in
`src/provenance/gc_recovery.py` (`execution/audit_gc_recovery.py`), which can
refuse — not by a hand-written INSERT.

That restore is only safe because archiving is idempotent: each sidecar table
carries a UNIQUE index on its identity column (`id`, or the source rowid for
rowid-keyed tables) and the copy is `INSERT OR IGNORE`, so an apply that
aborts after the archive pass and is then retried re-archives nothing. The
sidecar therefore holds **exactly one copy per id** — a second copy would
double-insert on restore, so it is a defect, not noise. `gc_manifest` records
the rows each pass actually added (0 for a re-archive), and a legacy sidecar
carrying duplicates is collapsed once, loudly logged as
`gc_archive_deduplicated`, before its index is created.

## Policies & parameters

| Policy | What | Default | Owner-tunable |
|---|---|---|---|
| `validation-issues` | Collapse duplicates per defect key AND backfill a fingerprint onto EVERY NULL-fingerprint row (singletons included; collisions archived+removed, FK-referenced conflicts logged `gc_validation_fingerprint_stranded`) | always on | — |
| `telemetry` | Age retention: stage_transitions, source_calls, ingestion_runs | 90 days | `--retention-days` |
| `facts-depth` | Per-ticker window on financial_facts + attempts-grid cascade | 20 quarters / 12 FY; floors 16 / 12 | `--keep-quarters`, `--keep-fy`, `--include-portfolio` |
| `maintenance` | ANALYZE on every `--apply`; VACUUM opt-in (15s exclusivity preflight + hard timeout) | — | `--vacuum`, `--vacuum-timeout-min` (30) |
| *(all)* | Rows deleted/updated per committed transaction | 20,000 | `--batch-size` |
| *(all)* | Whole-run wall-clock budget (loud abort past it) | 120 min | `--max-runtime-min` |
| *(all)* | Run-lock wait before yielding | 60 s | `--lock-timeout-s` |

Tier behavior (facts-depth): active watchlist / evaluation / index_member /
etf / `none` →
windowed; tickers with facts but no active `tracked_companies` row → all rows
archived out; **portfolio untouched unless `--include-portfolio`**. TTM rows
and `extracted_by='s1'` rows always survive. Sources are never pruned
selectively (FMP-only pruning would flip `fmp_backpop.sec_covers_well` into a
quota-burning re-fetch loop and blind the source-disagreement tripwires).

Floors exist because report §3 loads 16 quarters (12 display + 4 TTM-1Y CAGR
baseline, `src/report/sections/_common.py`) and renders 10 fiscal years
(+52/53-week-filer margin).

## Explicitly out of scope (audited, do NOT add)

- `kpi_facts` — no reader has a wall-clock filter; as-of replay and
  `HAVING COUNT(*)` catalogs need old rows. Dedup via
  `pipeline.kpi_persistence.purge_duplicate_kpi_facts` only.
- `fmp_endpoint_status`, `metric_computation_attempts` (as tables) —
  current-state grids keyed by PK/unique logical key, not logs.
- `llm_calls` — cost ledger; sealed-Ask audit FKs (`ask_answer_audit_records`,
  ON DELETE NO ACTION) and the all-time eval-coverage gate need it.
- `pipeline_attempts` — `run_accounting.deduplicate_completed` does an
  unbounded any-prior-OK lookup; bound it before any retention here.

## Concurrency contract (2026-07-31 incident, hardened 2026-08-01)

The first prod apply held ONE write transaction for the whole run: WAL grew
past 115 MB, `alembic upgrade head` (then no busy timeout) failed instantly,
a manual `BEGIN IMMEDIATE` could not acquire the write lock for 6+ hours, and
the 04:00 pipeline write legs were at risk — while WAL reads kept the
dashboard looking healthy. Standing rules now built into the tool:

- **Bounded batches**: staging holds no main-DB write lock; archiving writes
  only the sidecar; deletes commit every `--batch-size` rows, so any other
  writer waits at most one batch (seconds) behind its 30s busy_timeout.
- **Run lock**: `--apply` acquires the portfolio write-set run lock
  (`src/run_lock.py`, `data/portfolio.db.write.lock`) that
  `run_morning_pipeline.py` also holds for its whole run. Held lock ⇒ loud
  abort after `--lock-timeout-s` — GC yields, never queues. Stopping the
  services does NOT clear this class of contention; check for a live db_gc
  pid first.
- **Protected window**: the CLI refuses to start inside 03:00–05:00
  America/Los_Angeles and aborts between batches if a run reaches it
  (`--ignore-protected-window` to override, e.g. a supervised recovery).
- **Budgets**: whole run capped at `--max-runtime-min` (default 120); VACUUM
  gets a 15s exclusivity preflight plus a hard `--vacuum-timeout-min`
  (default 30) enforced via connection interrupt. The Task Scheduler XML's
  `ExecutionTimeLimit` is PT3H — deliberately ABOVE the in-tool budgets so
  the tool's own loud aborts fire first (a prior clone carried PT10M, which
  would have hard-killed a first-Sunday VACUUM mid-run); `RestartOnFailure`
  is removed so a failed run is never blindly retried. Committed batches
  stay committed and a re-run resumes from current state.
- **Exit codes** (what Task Scheduler / job_health show): 0 ok; **2** loud
  GC abort (`gc_aborted` on stderr: lock held past `--lock-timeout-s`,
  protected window, runtime budget, recycled-rowid fail-closed, schema-shape
  or hardlink preflight); **75** = the job_runtime write-set lock
  (`data/.job_locks/portfolio-db-*.lock`, a SECOND lock distinct from
  `<db>.write.lock`) was busy — the cron yielded, benign, that week's run is
  skipped; **1** = unexpected error (e.g. `SchemaRevisionMismatch` if a
  migration lands mid-run, or a `ValueError` from a floor violation).
- `alembic/env.py` now sets `busy_timeout=30000` on SQLite, so migrations
  ride out a batch instead of failing instantly.

## Cadence & sequencing

- REGISTERED (2026-08-02, owner-authorized): weekly, Sunday 06:00 PT via
  `cron/db_gc.task.xml` → `cron/run_db_gc.bat` (after the 04:00 pipeline;
  `weekly_validation` — the other Sunday portfolio-db writer — runs 03:00
  inside db_gc's own protected window, and the ~10:30 eval rung is clear). The wrapper adds `--vacuum` only when day-of-month <= 7 (first
  Sunday). Standing invocation: `--apply --include-portfolio` (all four
  policies). No LLM leg → the quota windows in `llm_quota_scheduling.md` do
  not apply; DB write-lock contention does, hence the slot — and the in-tool
  protected-window guard + run lock above are the backstop if the schedule
  ever drifts.
- Idempotency: a second run over the same DB is a no-op; idempotency key is
  the DB state itself (row-level predicates), logged per run in `gc_manifest`.
- Failure mode: halt loud (non-zero exit, JSON events on stderr). No retries.

## One-time sequencing before the FIRST `--apply` of facts-depth

1. Re-baseline credibility priors (`execution/build_confidence_observations.py`)
   so measured priors aren't later rebuilt from a truncated disagreement
   population.
2. Expect one full re-run of the thesis evaluator and segment derivers
   (their `db_snapshots` fingerprints hash `SELECT *`).
3. Run with `--vacuum` to reclaim freed pages (~281 MB freelist + freed index
   pages; measured 1.77 GB file at proposal time).

## Open decisions for the owner

1. Include portfolio in the window (`--include-portfolio`)? Audit verdict:
   nothing coded reads deeper than 16 quarters / 10 FY for decisions; deeper
   history only lengthens chart tails and LLM anchor context. Portfolio
   pre-2016 is only ~40k rows, so the space win is minor — this is a
   philosophy call, not a size one.
2. Ratify 20Q / 12FY as the standing windows, or widen.
3. Register the weekly cron (separate authorization).
