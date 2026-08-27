# DB Garbage Collection

> Status: RATIFIED 2026-07-31 (owner), amended 2026-08-03. The historical
> 20Q/12FY facts-depth policy remains available for read-only measurement, but
> destructive facts-depth apply is disabled pending an immutable
> archive-generation migration and explicit cutover. Weekly GC remains
> registered Sunday 06:00 PT for validation issues, telemetry, superseded LLM
> artifacts, maintenance,
> and first-Sunday VACUUM only.

## Goal

Keep `data/portfolio.db` bounded without changing what any current consumer
renders or computes. Attack the three real growth vectors: duplicated
validation issues (~10.2k identical re-inserts/day), superseded LLM artifacts
that no durable decision/provenance edge needs, unbounded per-ticker fact
history far deeper than any reader's window, and telemetry age.

This runbook implements the retention boundaries owned by `llm_calls.md` and
`data_pipeline_dag.md`, the preservation boundary in `data_provenance.md`, and
the scheduled-operation rules in `operations_governance_surface.md`. It owns
execution mechanics, not new retention policy or a second schedule.

## Tool

`execution/db_gc.py` — single-purpose CLI, dry-run by default. `--apply`
writes only the validation-issues, telemetry, llm-artifacts, and maintenance policies. Any
apply containing `facts-depth` aborts before a run lock, archive sidecar, or DB
mutation. Historical facts-depth implementation evidence remains covered by
tests, but is not an authorized production entry point.

Rows deleted by the supported policies are first copied into
`data/archive/portfolio_gc_archive.db`. The archive is **append-only and
run-keyed** (2026-08-03 redesign): each table mirrors the live columns BY NAME
plus `gc_run_id` (the Attempt Identity, represented by the archiving attempt's
`run_at`; rowid-keyed tables also carry `gc_source_rowid`).
`UNIQUE(gc_run_id, key)` is an attempt-scoped duplicate guard, not a Logical
Idempotency Key, and each pass is logged in `gc_manifest`.

Why run-keyed, not one-copy-per-id: `financial_facts` has no AUTOINCREMENT,
so SQLite recycles ids after a prune lowers `max(id)` — an id is not a stable
identity across time. Under the run key, a retry within a run adds nothing,
an identical payload is never duplicated across runs (payload guard), and a
recycled id becomes a **run-attributed variant** instead of either silent
loss (#1130's OR IGNORE) or a permanent prune dead-end (#1140's abort). The
cross-run payload guard excludes the columns the GC rewrites on live rows
after archiving (`_RECYCLED_ID_IGNORED_COLUMNS`, currently
`financial_facts.supersedes_id`) so a post-null re-archive is not mistaken for
a new variant (adversarial review #1). Schema drift is handled at archive
time: a live column added by a migration is mirrored into the archive (NULL on
older rows), and a DROPPED live column is a loud abort — never a positional
copy, which silently misfiles values when a drop+add keeps the column count.

Two planes are NOT archived and NOT reversible by restore: the 0225
resolution-plane cascade deletes (fact_observation_revisions /
legacy_fact_evidence_match_revisions / fact_selection_decisions), and the
pre-delete `supersedes_id = NULL` rewrite on SURVIVING facts (the sidecar
holds the doomed rows' originals, not the survivors'). Restore is therefore
governed by the fail-closed audit in `src/provenance/gc_recovery.py`
(`execution/audit_gc_recovery.py`), which can refuse.

**Restore = `execution/gc_restore.py`**, never a bare `INSERT ... SELECT`
(rows can legitimately appear under multiple runs). It classifies each
archived row — *restorable* (absent from main, restored verbatim),
*identical* (skipped), *conflict* (present with a different payload; NEVER
touched, exit 4) — restoring the latest variant per key or `--run <id>`
exactly. `--apply` runs under the same run-lock / schema-preflight /
protected-window guards as db_gc; `--drill` proves restorability into a
throwaway schema-clone without touching main and is exercised on a schedule
by `restore_drill.py` — a backup you have never restored is not a backup.

**Legacy sidecar owner decision (2026-08-03):** an existing archive table
without `gc_run_id` is preservation-only forensic evidence. `gc_restore`
must not upgrade that artifact in place or bulk-reinsert it into
`portfolio.db`; its scheduled drill verifies SQLite integrity, foreign-key
health, Content Identity hash, and table inventory without mutation. Emergency
`gc_restore --apply` remains available only for fully run-keyed archives.
Future deep-history access must use the immutable, sealed read-only archive
generation boundary below. This decision does not authorize facts-depth
pruning, which remains disabled, and does not disable the currently supported
validation-issues/telemetry GC policies from creating run-keyed archive rows.

**Archive retention**: keep every run. Growth is slow (tens of MB/quarter
steady-state after the one-off deep prunes); revisit with a deliberate
age-out policy only if the sidecar passes ~1 GB, and never age out runs the
restore drill has not verified since.

For `llm-artifacts`, eligibility is defined by `llm_calls.md`. The shared
archive-first mechanism remains a safety copy, but backup availability is not
a precondition for disposing of an eligible superseded row.

## Policies & parameters

| Policy | What | Default | Owner-tunable |
|---|---|---|---|
| `validation-issues` | Collapse duplicates per defect key AND backfill a fingerprint onto EVERY NULL-fingerprint row (singletons included; collisions archived+removed, FK-referenced conflicts logged `gc_validation_fingerprint_stranded`) | always on | — |
| `telemetry` | Apply the bounded pipeline-telemetry lifecycle from `data_pipeline_dag.md` | canonical default | `--retention-days` |
| `llm-artifacts` | Apply the superseded, unreferenced artifact lifecycle from `llm_calls.md` | canonical default | `--artifact-retention-days` |
| `facts-depth` | Read-only measurement of the former per-ticker window | 20 quarters / 12 FY; floors 16 / 12 | dry-run only; apply is disabled |
| `maintenance` | ANALYZE on every `--apply`; VACUUM opt-in (15s exclusivity preflight + hard timeout) | — | `--vacuum`, `--vacuum-timeout-min` (30) |
| *(all)* | Rows deleted/updated per committed transaction | 20,000 | `--batch-size` |
| *(all)* | Whole-run wall-clock budget (loud abort past it) | 120 min | `--max-runtime-min` |
| *(all)* | Run-lock wait before yielding | 60 s | `--lock-timeout-s` |

Dry-run tier behavior (facts-depth): active watchlist / evaluation /
index_member / etf / `none` →
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
  inside db_gc's own protected window, and the ~10:30 eval rung is clear).
  The wrapper adds `--vacuum` only when day-of-month <= 7 (first
  Sunday). Repository standing invocation: `--apply --policies
  validation-issues,telemetry,llm-artifacts,maintenance`; facts-depth is
  excluded. No LLM
  leg → the quota windows in `llm_quota_scheduling.md` do
  not apply; DB write-lock contention does, hence the slot — and the in-tool
  protected-window guard + run lock above are the backstop if the schedule
  ever drifts.
- **Logical Idempotency Key:** canonical database identity, enabled policy set and
  policy version, and eligibility cutoff. A second apply over the same eligible
  state is a no-op.
- **Content Identity:** canonical payload hash for each archived row and the sealed
  archive-generation digest where applicable.
- **Observation Version:** schema revision, policy cutoff, and bounded source-state
  snapshot observed for the run.
- **Attempt Identity:** `gc_run_id`; every retry receives a new value and receipt in
  `gc_manifest`.
- Failure mode: halt loud (non-zero exit, JSON events on stderr). No retries.

## Immutable archive-generation boundary

`execution/seal_archive_generation.py` builds or verifies a no-clobber,
hash-sealed manifest for one quiesced non-live SQLite generation.
`execution/register_archive_generation.py` re-verifies the database and
manifest under artifact and run locks, then atomically publishes the small
operational receipt introduced by migration `0272_archive_generation_catalog`.
Readers must use `provenance.archive_catalog.open_archive_generation`, which
selects only receipted generations and re-verifies both artifacts around every
read-only session. Archive publication precedes catalog registration; no
cross-file `ATTACH` transaction is treated as atomic.

Archive creation and catalog registration are code-only until a live-derived,
quiesced clone passes corpus completeness, cross-generation reference,
production-scale latency, restore, and rollback proofs. No retention deletion
is authorized by a seal or receipt alone.

## Open decisions for the owner

1. Ratify archive root, generation interval, and operational hot-window bounds.
2. Approve a sealed live-derived clone rehearsal and production-scale proof.
3. Approve a separate live migration/cutover and rollback window.
4. Define the retention and backup horizon for the preservation-only legacy GC
   sidecar after sealed read-only archive access is operational.
