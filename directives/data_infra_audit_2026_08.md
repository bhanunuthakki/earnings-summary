# Data Infrastructure Audit & Simplification Roadmap — 2026-08

Full-repo data-infra audit (2026-08-03, five parallel deep audits: database layer, execution
pipeline, scheduling, storage/backup/caching, data quality/provenance). This directive is the
consolidated findings + the phased simplification roadmap. It supersedes nothing — it is the
first infra-wide debt inventory; feature roadmaps live in `master_build_2026_06.md` and
`platform_backlog.md`.

## Headline numbers

| Metric | Value |
|---|---:|
| Tracked files / Python LOC | 2,882 / ~365k (`src/` alone) |
| `src/provenance/` | **78 files / 88k LOC = ~24% of the repo** |
| …of which completed-migration scaffolding | ~17k LOC (12.5k imported by no product code) |
| Alembic migrations (cannot build from `base`) | 262 steps; 5 duplicate numbers; 8 branch points |
| Live tables / views / triggers | 279 / 52 / 263 |
| …dead or write-only tables / unread views | 16 / 22 (42% of views) |
| `execution/` scripts | 320 / 94k LOC; 108 verb prefixes |
| …one-shot or zero-reference candidates for retirement | ~60 files / ~8k LOC |
| Cron tasks / wrappers | 44 / 45 (.bat), ~900 of 1,486 wrapper lines copy-paste |
| Run-lock systems guarding `portfolio.db` | **2** (mutually invisible) |
| DB-path resolvers | **3** (each parses `EARNINGS_SUMMARY_DB_PATH` independently) |
| Caching conventions | **6** (2 invalidate on content, 4 on clock or never) |
| Duplicated-state mechanisms needing reconcilers | 9 (48 sync/reconcile/repair/backfill scripts = 15% of `execution/`) |
| Largest unowned stores | `ir_documents/` 6.2 GB; `fmp_snapshots/` 1.2 GB / 30.7k files |
| Test files replaying the full 262-step chain | 177 of 230 DB-building files (18–56s each vs 13.5ms copy) |

## What is healthy — do not churn

- `connect_sqlite` adoption: ~6 raw `sqlite3.connect` in production, all defensible.
- `JobLock` (`src/runtime/job_runtime.py`): PID-reuse-safe, named-mutex transitions,
  inheritance proof, wraps 100% of the cron fleet.
- The exit-78 schema-drift preflight + `SchemaRevisionMismatch` re-raise discipline
  (post-#1154 "drift is not absence" pattern).
- `db_gc.py` concurrency hardening (bounded batches, protected window, budget) and the
  kpi_facts/llm_calls exclusion policy — keep as-is.
- `cron/task_manifest.json` governance (XML/PS1/MD generated + validated).
- `save_fmp_data.py` + `refresh_cache.py`: budget files, schema-drift dumps, staleness gates —
  the Layer-3 contract implemented in full. The problem is nothing else inherited it.
- KPI persist path: real pre-persist unit/range/monotonicity gates.
- `llm_artifacts` store: the one cache with a real invalidation protocol (input_sha256 +
  supersede + dirty-drain).
- `tests/conftest.py::migrated_db` session-template fixture (adoption incomplete, design right).

## The four systemic patterns behind the debt

1. **Completed migrations never demolish their scaffolding.** The latest-state/cutover project
   left 22 execution scripts + ~17k LOC in `src/provenance/` + 10 built-but-unread
   `latest_governed_*` tables + 7 unread `_v2` views. 31 single-commit backfills sit in the
   active namespace. `0031_drop_dead_tables` proves the accumulation recurs.
2. **Conventions are enforced by discipline, not structure.** 42 hand-typed `_log()` helpers;
   43/44 wrappers have the exit tail (the 44th silently reports success on failure); 25 call
   sites downgrade `busy_timeout` after the factory set it; 35/320 scripts emit JSON stderr
   events. Every one of these has a generator/lint/convention-test fix.
3. **Duplicate mechanisms for one concern.** 2 locks, 3 DB-path resolvers, 2 validation-issue
   stores (3 names for one table), 6 caching conventions, 3 daily orchestrators, 11 copy-pasted
   store `_open()` functions, 9 duplicated-state planes with reconcilers.
4. **Safety machinery exists but is unwired.** `backup_file_gc.py` (293 LOC, correct) is
   scheduled nowhere while 2.4 GB of its targets sit on disk; the GC archive sidecar — the ONLY
   copy of every pruned row — has never reached Drive; daily backup cadence had a 6-day hole;
   no WAL-size tripwire exists despite the 2026-07-31 starvation incident.

---

## Roadmap

Ordered by (risk removed ÷ effort), then by dependency. Each phase is a small number of PRs.
Items marked **[DECISION]** need an owner call before work starts; everything else is
execute-on-schedule. LLM-window rules (`llm_quota_scheduling.md`) apply to any new cron entry.

### Phase 0 — Stop active bleeding (small PRs, this week)

Correctness/safety fixes where current behavior silently lies.

1. **Back up the GC archive sidecar off-box.** Verify `_backup_archive_sidecar`
   (`cron/backup_db.py:214`) produces `portfolio_gc_archive.db.*.gz.enc` in Drive on the next
   run; add a cron-health assertion: sidecar snapshot exists and is newer than the last
   `gc_manifest` entry. Until green, "reversible prune" is an unproven claim. *(Done when the
   restore drill also verifies the sidecar — #1159 covered drill; this covers presence/freshness.)*
2. **Investigate the backup cadence hole** (snapshots 07-21→07-27, gap, 08-02) and add a
   freshness alarm: no Drive snapshot < 48h old → cron-health red.
3. **Schedule `backup_file_gc.py`** chained after `backup_db` (same wrapper, sequential steps)
   so pruning never races the snapshot. Reclaims ~2.4 GB immediately.
4. **Fix `cron/run_backfill_earnings_surprises.bat`** (bare `endlocal`, returns 0 on failure)
   and add the `run_track_comp_metrics.bat` RC-aggregation idiom to the other 5 multi-step
   wrappers. Extend `scheduler_manifest.validate_source_tree()` with a wrapper-content
   assertion (last non-empty line must be the `endlocal & exit /b` tail) so #1132 can't regress.
5. **WAL-size tripwire**: report `portfolio.db-wal` bytes + oldest-open-transaction age in the
   cron-health panel with a threshold alarm. Converts the 2026-07-31 failure mode from 10
   silent hours into an alert. No manual checkpoint CLI needed.
6. **Triage `onboard-pending`** — 135 failures / 153 hourly runs is the fleet's dominant
   signal; fix or unschedule. While it fails hourly, Last Result is untrustable everywhere.
7. **Fix `src/synthesis/theme_synth.py:103`** bare `except Exception` (swallows hard stops,
   violating quota rule 3 in both directions); adopt the `deferred_transient` tally in
   `thesis_collision.py`.
8. Small manifest/doc reconciliations: "42 tasks" → assert real count in
   `generate_cron_artifacts.py --check`; re-encode `weekly_cleanup.task.xml` to UTF-16 + add a
   BOM assertion; fix `refresh_scenario_priors.task.xml` runtime-path `<Command>` + add a
   path-prefix assertion; registry says 03:00 but task fires 03:40 — reconcile.

### Phase 1 — One lock, one launcher (concurrency unification)

The scariest live hazard: interactive and scheduled writers do not exclude each other.

1. **Retire `src/run_lock.py`; `JobLock` becomes the only lock.** Convert its 4 consumers
   (`db_gc`, `gc_restore`, `repair_llm_call_attempt_columns`, `run_morning_pipeline`) to the
   write-set lock. Export named `LOCK_HELD_EXIT_CODE = 75` / `SCHEMA_DRIFT_EXIT_CODE = 78`
   constants; kill the 1/2/3 lock-exit divergence; audit the ~10 scripts that `return 75` for
   unrelated meanings.
2. **Route the 7 root `.bat` entrypoints through `cron/run_python.bat`.** One-line change each;
   interactive runs instantly get the managed venv, the lock, the exit-78 preflight, env
   loading, and a job-health record. Closes the `full_refresh.bat`-vs-04:00-pipeline race that
   `AGENTS.md` already prohibits. Then delete `full_refresh.bat` in favor of
   `refresh_dispatch.py --mode full` (its docstring admits it reimplements the same 6-step chain).
3. **Unshadow the 04:00 window**: `scan_ir_transcripts` (04:15) and `backfill_transcripts`
   (04:30) are structurally starved by the pipeline's 900s lock hold (skipped_locked 4× each).
   Either move past ~05:30 or fold in as pipeline stages under `allow_nested_job_locks()`.

### Phase 2 — Delete the dead 20% (pure deletion, biggest LOC win)

No behavior change; each deletion PR must confirm zero product-code readers first.

1. **[DECISION] Fate of the `latest_governed_*` plane.** 852-line migration, 10 tables, FTS,
   3 sync triggers, ~9 `src/provenance/` modules, 288 internal references — readers never cut
   over (its own docstring says so). It was built for the render-read-amplification problem, so
   this is *finish the cutover* vs *delete the plane* — not unilateral deletion. Carrying both
   is the single largest duplicated-state cost in the repo. Decide, then execute in this phase.
2. **Retire `src/provenance/` scaffolding (~17k LOC).** The
   benchmark/rehearsal/parity/shadow/bootstrap/backfill/cutover modules; start with the 12.5k
   LOC that no product code imports (16 modules). Delete module + tests together, one cluster
   per PR.
3. **Retire ~60 `execution/` scripts.** The 9 zero-reference scripts; the 22-script
   cutover cluster (after item 1's decision); the 31 single-commit one-shot backfills. Anything
   that must stay auditable moves to `execution/archive/` (exempt from Layer-3 conventions)
   rather than sitting in the active namespace.
4. **Delete dead schema** (needs its own migration, mind the FTS/batch_alter trigger trap):
   9 zero-reference tables (`capital_actions`, `litigation_matters`, `numerical_claims`,
   `critical_accounting_estimates`, `fx_rates`, `kpi_aliases`, `segment_aliases`,
   `exec_holdings`, `tracked_companies_new`), 7 write-only tables, and the unread views —
   including the whole 7-view `_v2` fact-plane family (gated on item 1's decision where they
   overlap). Add a CI check failing on any table/view with no `FROM`/`JOIN` in product code —
   `0031_drop_dead_tables` already proved one-off cleanups don't stick.
5. Move `panel_activation_counts` DDL (`comments_server.py:2092`, request-time CREATE, no
   reader) into a migration or delete it.

### Phase 3 — Collapse the bases (shared primitives; unlocks everything downstream)

1. **Squash the migration baseline.** Generate a real `NNNN_squashed_baseline` from a head-DB
   schema dump; make `alembic upgrade head` work on a bare file; delete the `init_db()` import
   side effect in `src/db.py` and the 3-step stamp ritual. Risks to plan for: FTS triggers
   dropped by table rebuilds (known incident class), the two `sqlite_master` view-restore
   dances, and the 25 test stamp points. This is the root-cause fix for the test-suite cost
   memory ("no universal template — 0000_baseline fails on tracked_companies").
2. **Finish the `migrated_db` fixture migration** (58/230 files adopted; 177 replay the full
   chain). After the squash, collapse 25 stamp points to 2–3 anchors; re-point everything
   stamping at either `0213_*` revision (two same-numbered revisions are the two hottest
   fixture anchors — latent wrong-schema bug). Expected test-suite wall-clock win is minutes,
   not seconds.
3. **One DB-path resolver.** `resolve_db_path` becomes the sole `EARNINGS_SUMMARY_DB_PATH`
   parser; `db.DB_PATH` a thin alias; `portfolio_db_path` delegates. (Two of the three exist
   only to dodge the import side effect Phase 3.1 removes.)
4. **Build the missing HTTP layer**: `src/net/client.py` (session factory: timeouts, retries,
   backoff, per-host token-bucket — SEC 0.25s, FMP tier budget; `sec_user_agent()` wired;
   error classification reusing the `filings/models.py` taxonomy; auto `log_call()`), then one
   `FmpClient` replacing 9 base-URL declarations, 15 `FMP_API_KEY` reads, 17 hand-rolled
   sessions — and making `refresh_cache.py`'s tier budget non-bypassable.
5. **`execution/_lib.py`** (or `src/cli/`): `PROJECT_ROOT`, `resolve_db_path`, `log_event()`
   (deletes 42 private `_log`s), `standard_parser()` with
   `--db-path/--repo-root/--ticker/--force/--apply/--dry-run`; add `execution/__init__.py` and
   drop 300 `sys.path.insert` hacks. Then a parametrized convention test over `execution/*.py`
   (argparse-or-registered-library, shared logger, no literal `"data" / "portfolio.db"`,
   `--force` on DB-writing scripts) with a shrink-only baseline, so the layer stops drifting.
6. **One `store_conn()` helper** (extend `user_state._db.open_conn`, which already has
   `schema_preflight`); delete the 11 copy-pasted store `_open()`s — `wealth_context_store`
   skipping `resolve_db_path` is a latent bug, not style. Merge the two validation-issue
   stores + `record_validation_issue` into one module. Rename `src/model_provenance/` →
   `src/valuation_lineage/` (8 import sites). Collapse the 3 materialized-JSON cache modules
   (~880 LOC) into one generic `materialized_cache` with a read-time `generated_at` staleness
   check — a failed Stage 0f then surfaces instead of serving stale numbers; rename them off
   the misleading `*_cache` suffix (no `*_cache` table exists in the DB).
7. **`connect_sqlite` as sole policy authority**: delete 307 redundant `row_factory`
   assignments and the ~25 `busy_timeout` downgrades (5000/10000 vs the canonical 30000 the
   lock design depends on); lint-ban both outside `sqlite_runtime.py`.

### Phase 4 — Lifecycle & data-quality hardening

1. **Retention owners for the big unowned stores.**
   - `ir_documents/` (6.2 GB): keep newest N periods per ticker for non-portfolio names;
     content-addressed so dedupe is free.
   - **[DECISION]** `fmp_snapshots/` (1.2 GB, 30.7k files, grows daily forever): age-thin
     daily→weekly→monthly. Changes as-of answer granularity, so owner sign-off required.
   - `.tmp/`: extend `run_weekly_cleanup.py` from 4 allowlisted roots to a default rule —
     completed checkpoint dirs >30d + the 1,097 loose root files; safety scaffolding already
     exists.
   - Fix the absolute `C:\...` paths in `documents.file_path` (210 rows) — not portable across
     checkouts.
2. **Close the plausibility gap on `financial_facts`.** Finish the
   `insert_with_restatement_detection` wiring (explicitly "opt-in until extractor-by-extractor
   wiring lands") so the bulk-load path gets a pre-persist gate; today AGENTS.md promises
   "halt before persist" and the implementation is "warn after persist, in the stage that runs
   last." Widen the two narrow allowlists (`_FINANCIAL_FACT_RANGES`: 3 items;
   `SOURCE_DISAGREEMENT`: 4 line items — balance sheet and cash flow currently unchecked).
3. **Extend FMP validation to fallback rungs** (v3-shaped models): a degraded fetch currently
   also loses its schema gate — the highest-risk combination.
4. **Shared row-drop helper with a drift threshold** for the 6 fetchers that silently drop
   `ValidationError` rows: dump to `.tmp/`, escalate when the drop *rate* crosses a bar, so
   "1 bad row" and "schema changed, dropped all 200" stop looking identical.
5. **Generate the 44 cron wrappers** from `task_manifest.json` (like the XML/PS1/MD already
   are): removes ~900 copy-paste lines and 44 PowerShell-subprocess timestamp spawns, makes
   `SCHEMA_DRIFT_TOLERANT_JOBS` checkable against real job names, normalizes naming, and makes
   exit-tail regressions structurally impossible. (Supersedes the Phase-0 tail lint once landed.)
6. **Declarative, resumable morning pipeline**: move the 19 `_Stage` literals + timeouts +
   `skip_triggers` gates into a data table (`key, script, argv, timeout, depends_on`); `--only`
   / `--from` fall out free; a `.tmp/morning_pipeline/state.json` checkpoint makes a 04:00
   failure resumable instead of re-running up to 2h25m — which AGENTS.md Layer-2 already
   requires. Fold the freshness-bar stragglers (`diet_panel` 48h, `command_center_shell` 7d)
   into `pipeline.freshness` while in the area.
7. Consolidate the `extract_*` CLI cluster (6 files at 0.62–0.87 containment → one
   `extract.py --kind`), the segment-extraction trio, and the alert-cleanup pair (~700 LOC).

### Explicitly out of scope / rejected

- Age-deleting `kpi_facts` / `llm_calls` — standing policy, unchanged.
- Replacing SQLite / moving to Postgres — `postgres_shadow` scaffolding gets deleted in
  Phase 2, not revived.
- A universal WAL checkpoint CLI — tripwire only (Phase 0.5); structural fix already lives in
  `db_gc` batching.
- Rewriting the 9 duplicated-state reconcilers — they are deliberate self-healing designs;
  the roadmap deletes *unused* planes, not working mirrors.

## Sequencing notes

- Phases 0–1 are independent of everything and each other; start immediately.
- Phase 2.1 (governed-plane decision) gates 2.3/2.4 partially; the provenance-scaffolding and
  one-shot-script deletions (2.2, most of 2.3) need no decision.
- Phase 3.1 (squash) should land before 3.2 (fixture migration) and unblocks 3.3.
- Phase 4.5/4.6 build on Phase 3.5's conventions.
- Every deletion PR: confirm zero product readers (grep + tests green), one cluster per PR,
  full suite before push (never piped through head/tail).
