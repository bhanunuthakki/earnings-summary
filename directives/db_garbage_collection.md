# DB Garbage Collection (DRAFT — pending owner ratification)

> Status: PROPOSED 2026-07-30. Not yet ratified; no cron registered. The
> retention windows below are the owner-approval surface — everything else is
> mechanics. Grounded in the 2026-07-30 three-way consumer audit of
> financial_facts, kpi_facts, and the telemetry tables.

## Goal

Keep `data/portfolio.db` bounded without changing what any current consumer
renders or computes. Attack the three real growth vectors: duplicated
validation issues (~10.2k identical re-inserts/day), unbounded per-ticker
fact history far deeper than any reader's window, and (future) telemetry age.

## Tool

`execution/db_gc.py` — single-purpose CLI, dry-run by default, `--apply` to
write. Every deleted row is first copied, schema-identical, into
`data/archive/portfolio_gc_archive.db` (with a `gc_manifest` run log), so any
prune is reversible with one `INSERT ... SELECT`.

## Policies & parameters

| Policy | What | Default | Owner-tunable |
|---|---|---|---|
| `validation-issues` | Collapse duplicate rows per defect key; survivor gets real fingerprint + first/last_seen + occurrence_count | always on | — |
| `telemetry` | Age retention: stage_transitions, source_calls, ingestion_runs | 90 days | `--retention-days` |
| `facts-depth` | Per-ticker window on financial_facts + attempts-grid cascade | 20 quarters / 12 FY; floors 16 / 12 | `--keep-quarters`, `--keep-fy`, `--include-portfolio` |
| `maintenance` | ANALYZE always; VACUUM opt-in | — | `--vacuum` |

Tier behavior (facts-depth): active watchlist / evaluation / index_member →
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

## Cadence & sequencing

- Proposed: weekly, Sunday 06:00 PT (after the 04:00 pipeline; clear of the
  Sun 03:00 git-cleanup / 03:30 memory-streamline / ~10:30 eval rungs).
  VACUUM on the first Sunday of the month only. No LLM leg → the quota
  windows in `llm_quota_scheduling.md` do not apply; DB write-lock contention
  does, hence the slot.
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
