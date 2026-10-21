# Operations & Governance Surface Impact

The existing daily database backup and weekly filesystem cleanup jobs own file
retention. Backups keep the latest verified completed member per source and
family. Snapshot, archive and required upload failures preserve the last good
backup and unfinished attempt evidence. Retirement starts after every required
upload succeeds. Operational KPI backups can enter the weekly retention engine
through exact snapshot, restore-readiness and sealed successful-apply evidence.
The cleaner discovers this evidence on each pass, keeps the latest good copy per
source and purpose, and holds failed or incomplete operations. Compatible later
success can resolve a hold. Other legacy backups require explicit hash-bound
catalog registration; the automatic filename/month-based legacy sweep is retired.
Weekly cleanup
preserves the newest job log, failed checkpoints, recovery scopes and registered
pins. Its filesystem phase uses the filesystem-maintenance lane and resolves the
configured product state root. See `weekly_cleanup.md` and
`data_pipeline_dag.md` for the executable boundaries and retention windows.
Weekly cleanup also emits read-only coverage counts for the state and runtime
temporary roots, named test runs, operations evidence and backup recovery roots.
Unknown or unreadable material reports incomplete coverage. Pytest session
receipts supply automatic successful-run registration. This extends the existing
job receipt; it adds no Operations page, database mutation, network call, or job.
Operational backup discovery adds typed hold reports to that same cleanup receipt.
Unknown or invalid producer evidence prevents complete coverage. This is a
deliberate extension of existing maintenance evidence, with no new UI surface.
Legacy temp trees without completion evidence have no automatic expiry date.
Windows coverage now includes unnamed legacy trees under `C:\tmp` as read-only
inventory. The cleanup does not infer ownership or deletion rights from that count.

## Directive contract

- **Target sources:** canonical Scheduler manifest and wrappers, managed-service registry, LLM and eval registries, issuer/source policy, typed runtime receipts, bounded database observations, schema compatibility, and separately governed operator capabilities.
- **Output schema:** an explicit disposition of `primary surface`, `linked governed view`, or `deliberate exclusion`, plus the affected typed registry/snapshot/view models and evidence-backed tests.
- **Refresh cadence:** declared configuration is projected at application start; request snapshots use the panel cache contract; runtime state comes only from typed receipts or bounded read-only observations with their own recorded time and freshness policy.
- **Logical Idempotency Key (`Idempotency key`):** canonical owner identity plus the required surface disposition. Repeating a projection for the same owner/disposition must not create a second logical surface item.
- **Content Identity:** digest of the typed registry, receipt, or snapshot payload used by the projection.
- **Observation Version:** registry/snapshot version, evidence-recorded time, and observed-at time.
- **Attempt Identity:** unique application-start projection or request snapshot invocation and its receipt.
- **Rate-limit budget:** zero network calls, subprocess probes, service-control calls, or unbounded filesystem/database reads in the Operations render path. Live producers write bounded receipts outside the request path.
- **Failure-mode policy:** render Missing, Stale, Invalid, or Unavailable with evidence source and time. Never turn absent or malformed evidence into a healthy claim, and never block unrelated product work merely because an internal implementation detail is not an operator workflow.

## Outcome

### Peripheral scheduled-task freeze (2026-10-20)
Disposition: **no surface change** (enabled-state change to existing tasks; no task, wrapper, cadence, write lane or service ownership changed). Simplification Phase 0 sets `Settings/Enabled` to `false` in the tracked XML for `coach_pings`, `decision_nudge`, `grade_calibration`, `monthly_calibration_scorecard`, `ledger_synthesis`, `red_team`, `tenet_accountability`, `thesis_collision`, `weekly_score_stances`, `weekly_packet` and `submit_saydo_batch`. These jobs are outside the briefs, reports, portfolio decisions, Q&A and data-plane jobs. The existing Jobs projection derives `required_disabled` from each XML, so it stays truthful: until the canonical host re-registers the tasks, a host state of `Ready` shows as a policy violation, not as healthy. Registration on the host is a separate deployment step that this change does not perform. Their artifacts become stale rather than deleted; re-enabling restores them. Evidence: `execution/generate_cron_artifacts.py --check` (45 tasks validated) and the existing `required_disabled` cases in `tests/test_operations_snapshot.py` and `tests/test_operations_panel.py`.

### Lazy `operations` package exports (2026-10-20)
Disposition: **no surface change** (import-time refactor). `src/operations/__init__.py` resolves its five public exports on first access instead of importing the registry, evals and domain code eagerly. The public export set in `docs/quality/compatibility-baseline.json` is unchanged. Importing `operations.journal` from a job wrapper now loads 176 modules in 0.07 s instead of 618 modules in 0.99 s. Evidence: the `tests/test_operations_*.py` and `tests/test_job_runtime*.py` suites.

### Read-only research tool server (2026-10-20)
Disposition: **no surface change** (internal CLI for an orchestrator agent; not an operator workflow). `execution/serve_research_tools.py` serves `src/ask/tool_server.py` over MCP stdio for a client session that starts it. It requires an explicit database through `require_db_path`, opens it only with the `READ_ONLY` role, never calls the LLM pack router or any model, and writes nothing. No scheduler task, managed service, route, panel, alert or operator action is added. Evidence: `tests/test_ask_tool_server.py` (byte-identical database after a tool call, ticker path-traversal rejection, ledger numbering and status labels, claim binding, JSON-RPC handshake and a stdio subprocess round trip).

### Folded-module reference sweep and review-identity digest cleanup (2026-10-13)
Disposition: **no surface change** (prose-only reference corrections plus one digest-registry entry removal; not an operator workflow). The stale prose references left by the 27f0e240 module folds now name their retained homes: the canonical next-dollar directive's module list names `book_risk.py` (not the folded `covariance.py`), the QA walkthrough's `REVIEW_FULL_VERDICT` row cites `src/ask/engine.py:161` where the flag is read, and the seven source docstrings/comments (`src/compute/key_metrics.py`, `execution/extract_key_metrics.py`, `src/redteam/__init__.py`, `src/redteam/lenses.py`, `src/discovery/store.py`, `execution/comments_server_shared.py`, `src/ask/engine.py`) point at `src/pipeline/explore_panel.py`, `src/redteam/engine.py`, and `ask.engine`/`run_chat_command` instead of the folded modules. `REVIEW_CODE_IDENTITY_DEPENDENCY_CLASSES` no longer digests the phantom topology-observer path (that module folded into `src/operations/host_runtime.py`, which remains a listed dependency); `execution/extract_key_metrics.py` keeps byte-identical CLI behavior (line-neutral docstring-plus-import-block diff; `--help`, defaults, and the bandit/canonical-helpers line-pinned receipts unchanged) while its legacy per-file `sys.path` bootstrap and `# noqa: E402` directives were replaced by the shared `execution/_lib.py` bootstrap seam, and its stale `execution_sys_path_mutations` allowlist entry was removed together (allowlist 308 → 307). No operator action, alert, panel, route, scheduler task, managed service, flag, or default changed: the Ask command routing (`COMMAND_PREFIXES`, `run_chat_command`), the `REVIEW_FULL_VERDICT` default-on kill switch, and the operations review bundle's identity/digest contract are unchanged, and every listed review-identity dependency still binds the digest. Evidence: serial `tests/test_operations_review_bundle.py` green (its registry iteration proves each remaining dependency — `src/operations/host_runtime.py` included — binds `review_code_identity`), `execution/extract_key_metrics.py --help` direct-invocation parity after the bootstrap change, `make instruction-check`, `scripts/check_architecture_boundaries.py`, and the standing changed-file gates (format, lint, strict pyright, suppressions) green, and the folded-destination reference sweep over the VAL-CROSS-004 scope returning zero live operational references (remaining hits are history-classified directives per `directives/directive_manifest.json` and committed receipt artifacts).
### Sealed citation source-version viewer route (2026-10-08)
Disposition: **no surface change** (read-only product viewer route; not an operator workflow). The cockpit now serves `GET /source/version/<document_version_id>` (with the sealed lane's `?node=<evidence_node_id>` locator and the existing `?fragment=1` peek shape) through the new `pipeline.source_viewers.render_document_version_page`. It resolves one immutable evidence-ledger coordinate — `evidence_document_versions` + `evidence_source_observations` + `evidence_nodes` + `search_chunks` + the canonical fact projection entries bound to the cited node — and renders the issuer, fiscal period, source-document identity (form/document type, accession, source URL, blob sha256), the cited node locator, the observation version (document version id/sequence, observation id, observed/retrieved/recorded times, supersede link), and every reported fact (metric, value, unit, currency, period, locator). An unknown document version, an unknown node, or a node belonging to a different document version returns an honest 404; nothing is fabricated. It reads only append-only evidence tables on one READ_ONLY connection, adds no operator action, alert, panel, scheduler task, managed service, mutable state, or network call, and changes no existing route. The sealed plane's emitted href shape (`src/ask/sealed_retrieval.py` `_narrative_item` / `_fact_item`) is unchanged. Evidence: `tests/test_source_version_routes.py` (fixture corpus resolves to 200 with every displayed field reconciling to the immutable fixture observation; unknown version / unknown node / cross-version node 404; href-template coupling to the sealed lane) and the reconciliation artifacts under `.tmp/search-validation/source-version-val-search-00{2,3}-*.json`; `services.yaml:commands.source-version-validate` reproduces them.

### `execution/_lib.py` adoption for a bounded read-only command subset (2026-10-10)
Disposition: **CLI database-resolution hardening, no new surface**. Six read-only
`execution/` diagnostics now resolve their database through the shared
`execution/_lib.py` seam (`command_parser`, `add_database_argument`,
`require_db_path`) instead of each re-deriving a `sys.path` bootstrap, an
`argparse` shell, and a checkout-relative `data/portfolio.db` default:
`show_source_calls.py`, `report_workload_inventory.py`,
`provenance_coverage_report.py`, `filing_sections_report.py`,
`audit_transcript_evidence.py`, and `expire_stale_research.py` (its dry-run and
`--resurrect-expired` read paths). Their flags, defaults, and `--help` output are
byte-identical; `--db`/`--db-path` resolution is now explicit flag →
`EARNINGS_SUMMARY_DB_PATH`/configured → the canonical `require_db_path` policy, so
running with no flag and no configured database exits 2 with
`refusing to run: The checkout-default portfolio database is prohibited` and never
opens or creates `data/portfolio.db`. `report_workload_inventory.py`'s
`--repo-root` no longer selects the checkout database, and `expire_stale_research.py`
no longer falls back to it. The `config/architecture_boundaries.json`
`execution_sys_path_mutations` allowlist shrank by the six adopted files (314 → 308)
and `scripts/check_architecture_boundaries.py` exits 0. No alert, action, panel,
route, scheduler task, or managed service changed; the weekly
`cron/run_weekly_cleanup.bat` `expire_stale_research.py --apply` invocation is
unchanged and resolves the machine-visible `EARNINGS_SUMMARY_DB_PATH` as before.
Evidence: `.tmp/valid/exec-lib/` (per-command `--help` and functional before/after
transcripts byte-identical against `.tmp/portfolio_bench.db`; the six
checkout-default refusal transcripts; env-only vs explicit-flag output equality),
`tests/test_execution_lib.py` (legacy-help reproduction, raw-formatter,
`require_db_path` refusal, and no-own-bootstrap tests), and the unchanged
`tests/test_source_calls_routes.py`, `tests/test_provenance_coverage_report.py`,
`tests/test_audit_transcript_evidence.py`, `tests/test_expire_stale_research.py`.

### Report citation anchors resolve the Observation Version chain (2026-10-12)
Disposition: **no surface change** (reader-facing citation routing in the workspace report and the read-only source viewers; not an operator workflow). Report citation anchors now resolve the full provenance chain wherever the cited document has an Observation Version: the Sources-tab document rows route their citation link to `/source/version/<document_version_id>` and render a visible per-row Observation Version column; synthesis-lens cite-mark chips route their href the same way; fact-cell source-chip popovers state the Observation Version and link the chain beside the existing in-app viewer and original-source links. A cited document WITHOUT an Observation Version keeps its legacy `/source/<doc_id>` viewer link and carries the explicit "not recorded" degraded state on every surface (Sources-tab row and anchor title, cite-mark popover meta in both the Python and JS mirrors, chip popover row) — never a silently partial chain. The legacy `/source/<doc_id>` fallback view (documents with no in-app viewer, including every already-built report's hrefs) resolves the cited document's Observation Version through the same evidence-plane reader the `/source/version/<id>` route uses and embeds the identical chain rows, or states the absence explicitly. Resolution is one batched read-only query per section build; no existing route changes shape, no route is added, and unknown versions/nodes keep their honest 404s. No operator action, alert, panel, scheduler task, managed service, mutable state, or network call changed. Evidence: `tests/test_report_citation_version_chain.py` (exact admitted-version preservation, unique legacy binding, and fail-closed behavior; Sources-tab routing + degraded state; cite-mark payload routing + popover degraded state in both mirrors; chip popover chain link vs degraded state; the financials section stamping pass; the fallback view's embedded chain, explicit degraded state, fragment parity, and the by-legacy-id loader's honest 404s), the regenerated and reviewed `tests/golden/workspace/` panes (sources table column/routing, chip popovers, scripts mirror), and `scripts/check_design_sync.py` green with the updated `pipeline/source_viewers.py` dynamic-visual digest (existing `sv-*` classes only, no new visual vocabulary).

### Sealed Ask retrieval readiness

Disposition: **no additional operator surface**. Startup emits the cheap
`ask_retrieval_readiness deferred_to_request=true fail_closed=true` event. It
does not open the financial database, derive a cohort, inspect vector storage,
or start a background worker. Actual sealed requests retain the typed registry,
cohort, vector-runtime and evidence checks. A missing or invalid prerequisite
remains a visible request failure; startup telemetry does not claim readiness.
The unused boot-assessment helper and its dedicated tests are removed.
Evidence: startup non-probing tests and actual-request fail-closed retrieval tests.

### Cross-process daily-brief writer gate (2026-10-07)
Disposition: **no surface change** (internal execution change inside an existing scheduled job). The 06:30 daily-brief pool's single-writer guarantee now holds across the driver AND its writer-capable step subprocesses, not just the driver's threads: `src/pipeline/db_writer_gate.py` serializes actual write transactions per resolved database path with a process-safe advisory gate (an atomic `os.link` claim with holder payload, stale-holder breaking, a bounded 120 s wait and per-thread re-entrancy — never the whole-job production run lock, which stays a run-scope one-per-host claim), and every chain participant (driver skip/completed-build windows, thesis-evaluator run accounting and episode alerts, commitments matching, DCF refresh and the five specialized DCF builders, artifact sync and provenance) takes that gate around its real write windows while reads and compute stay ungated. This entry's original claim covered the chain's EXPLICIT windows only; the auxiliary connections opened by best-effort writers inside the same chain were closed at the connection seam by the follow-up entry below, which also corrects this scope statement. The driver hands the resolved database path to each child entrypoint explicitly (`--db` / `--db-path`, plus `EARNINGS_SUMMARY_DB_PATH` for the in-process builder legs), so no child can derive a checkout-local `data/portfolio.db`; `refresh_dcf.apply_edits` deliberately stays on SQLite's busy timeout because it serves the cockpit's `/api/dcf/save` request path, which is not a chain child of the daily pool. Pool width, per-ticker step order, per-step 600 s timeouts, summary keys, exit codes and the scheduler task are unchanged; the pool benchmark's measured overlap survives (2.75× median, 96 σ beyond observed noise on the same stubbed fixture). No alert, action, panel, route, scheduler task, or managed service changed, and no new operator action is supported. Evidence: `tests/test_db_writer_gate.py` (gate claim/hold/break/re-entrancy contract), `tests/test_daily_fetch_brief_cross_process_writers.py` (real width-3 driver with real step subprocesses over a disposable migrated database: the writer-gate trace shows a maximum of one open writer window per database path across processes, chain order intact, no lock-timeout self-contention, evaluator run records and `dcf_runs` rows landed, the env-configured database outside `repo/data` reaches every child argv with no checkout-local database created, and two real concurrent refresh children serialize their gated windows), and the unchanged `tests/test_daily_fetch_brief_pool.py` / `tests/test_daily_fetch_brief_gates.py`.

### Daily-brief auxiliary writers serialized at the connection seam (2026-10-08)
Disposition: **no surface change** (internal execution change inside an existing scheduled job; corrects the scope statement of the 2026-10-07 entry above). The 2026-10-07 gate covered the chain's explicit write windows; it did NOT cover the WRITER-role connections that auxiliary best-effort helpers open for themselves inside the same chain — source-call telemetry (`sources.registry.log_call` / `log_calls_batch` / `log_http_measurement`), and, under `--enable-llm`, the `llm_calls` cost ledger, `llm_artifacts` upserts, `llm_budget_alerts`, and the quota-breaker row (`llm_circuit_breakers`). Those could overlap a sibling chain's window. The fix serializes them at the single common seam instead of at each call site: the driver marks the run (`pipeline.db_writer_gate.CHAIN_WRITER_DB_ENV` = the resolved database path) for its duration, every descendant inherits the mark, and `sqlite_runtime.connect_sqlite` then holds one cross-process writer gate per resolved database path for the LIFETIME of every `WRITER`-role connection a marked process opens on that database — explicit windows and auxiliary writers alike. Scope is exactly that: chain-marked processes, that one resolved path, `WRITER`-role connections, connection lifetime. Exclusions are unchanged and explicit — processes outside the chain (cockpit, crons, one-off CLIs, `refresh_dcf.apply_edits` on the `/api/dcf/save` request path) keep their previous behavior and pay one environment lookup; other database files and `READ_ONLY`/quiesced/snapshot-destination roles are untouched. Loss is never silent: a gate timeout in an auxiliary writer leaves a WARNING plus a durable counter beside the database (`telemetry_health.record_dropped_write`: `source_call_write`, `source_attempt_write`, `llm_ledger_write`, `llm_artifact_write`, `llm_budget_alert_write`, `llm_quota_breaker_write`), which is what the cron-health panel reads. Pool width, per-ticker step order, per-step timeouts, summary keys, exit codes and the scheduler task are unchanged; no alert, action, panel, route, scheduler task, or managed service changed, and no new operator action is supported. Evidence: `tests/test_daily_chain_auxiliary_writers.py` (real width-3 `--enable-llm` chain over a disposable migrated database with a local stub `claude` CLI: `source_calls`, `llm_calls`, `llm_artifacts` and `llm_budget_alerts` rows really land, and the opt-in connection-seam trace proves the maximum simultaneous `WRITER`-role open per database path is 1 across pids with every chain open holding the gate; a stubbed exhausted usage window records the breaker row and still serializes; per-writer drop-counter tests; non-chain opens stay ungated) and the unchanged `tests/test_daily_fetch_brief_cross_process_writers.py` / `tests/test_daily_fetch_brief_pool.py` / `tests/test_db_writer_gate.py`.

### Trigger-stage dismissed-signature lookup hoist (2026-10-07)
Disposition: **no surface change** (internal execution change inside an existing scheduled stage). The stage_1 trigger driver (`execution/run_triggers.py`) loads the ticker-independent 90-day dismissed-signature lookback (`_load_dismissed_signatures`) once per run instead of once per (ticker × trigger) pair that produced candidates (audit F5); the identical set is threaded into every pair's user-state snapshot (`_compose_user_state` takes it as a required argument, so every caller supplies the run-scope set explicitly). No current sensor consults the set — `find_by_signature` already covers dismissed rows — so the hoist is unobservable in run outputs, which the M3 closeout fixture-equivalence record confirms (identical alerts, queued actions and memo artifacts pre/post; the set still reaches every pair with the 90-day lookback semantics intact). No flag, summary key, structured event, alert, action, panel, route, scheduler task, or managed service changed, and no new operator action is supported. Evidence: `tests/test_run_triggers_dismissed_signature_hoist.py` (exactly one lookback query per run across six candidate pairs, delivered-set parity with the fresh-in/91-day-stale-out lookback semantics) and the unchanged `tests/test_run_triggers_e2e.py`.

### SEC CompanyFacts conditional fetch and digest short-circuit (2026-10-06)
Disposition: **no surface change** (internal execution change inside an existing scheduled job). The Saturday 01:40 `fetch_sec_xbrl` stage now sends conditional companyfacts GETs (ETag/If-Modified-Since, taken from the per-ticker fetch receipt written in the same commit as the ingest) and short-circuits unchanged payloads on the sha256 digest: an unchanged ticker performs zero downloads when the server honors the validator, skips the pydantic re-parse and the fact re-walk otherwise, writes no staleness fields, and emits an explicit `sec_companyfacts_unchanged_skip` stderr receipt plus a summary row carrying the disposition, skip reason and payload digest; the run summary gained the additive `skipped_unchanged` count. Interrupted runs resume from per-ticker completion receipts (receipt presence means the ingest committed), and a retry reprocesses only incomplete tickers; the per-fact `PRAGMA table_info` probes were hoisted to once per ingest, and the write transaction now covers only the bounded persistence batch (parse and fact-walk selection run before it opens). Stage identity, ingestion-run accounting, genuine-staleness handling, exit codes and the scheduler task are unchanged; a skip is a positive capture-boundary observation — one retrieval observation is recorded against the retained digest, never silence. No alert, action, panel, route, scheduler task, or managed service changed, and no new operator action is supported. Evidence: `tests/test_sec_companyfacts_conditional_ingest.py` (zero re-download/re-parse/re-walk on unchanged replay, changed payload reprocesses exactly that ticker, interrupted-run resume from receipts, probe hoist, bounded transaction window), the unchanged `tests/test_sec_native_capture.py`, `tests/test_sec_accession_refresh.py` and `tests/test_filing_xbrl_extraction_ledger.py`, and the M3 closeout fixture-equivalence record (identical fact plane, documents, observations and links pre/post; the receipts table is the migration-0050 addition).

### Daily brief ticker pool (2026-10-07)

Disposition: **no surface change** (internal execution change inside an existing scheduled job). `execution/daily_fetch_and_brief.py` now drains the independent tickers of the 06:30 dirty-brief queue on a bounded worker pool (new internal flag `--workers`, default 3; the cron wrapper's argv is unchanged, so it takes the default) instead of one serial chain, so a rebuild day no longer pays every ticker's 4-5 process boots end to end. The step order inside one ticker is unchanged (thesis evaluator → commitments → DCF → artifacts → history sweep) and only across-ticker work overlaps. Every driver-side database write (skip record, completed-build record, artifact-dirty pass) runs inside one writer lock per database file, so at most one writer connection per file is open at once; the step subprocesses keep their own connections and their own 600s per-step timeout, so a hung ticker fails alone. The run's stdout summary keeps its existing keys (`started_at`, `tickers`, `skipped_by_tier`) and lists tickers in candidate order; a `--workers` value below 1 fails closed with exit 2. No alert, action, panel, route, scheduler task, or managed service changed, and no new operator action is supported. The driver also adopted the shared `execution/_lib.py` bootstrap seam in the same change (its legacy per-file `sys.path` bootstrap and the stale architecture-allowlist entry were removed together). Evidence: `tests/test_daily_fetch_brief_pool.py` (recorded cross-ticker overlap with per-ticker step order preserved, bounded pool width, one writer connection per database file, independent timeout failure, and `--workers 1`/`3` summary parity) and the unchanged `tests/test_daily_fetch_brief_gates.py`.

### Morning-pipeline stage lanes (2026-10-06)

Disposition: **no surface change** (internal execution change inside an existing scheduled job). `execution/run_morning_pipeline.py` now dispatches the manifest's stages as bounded lanes (at most 3 stage subprocesses at once) instead of one serial chain. A stage starts only after every declared dependency in the run has settled. At most one stage that can write to the database runs at a time; the manifest now marks this with `StageSpec.db_writer`. The read-only and file-cache stages (preflight, fundamentals, candidate fit, factor proxies, position guard, feed) may overlap the writer. The feed also waits for the trigger sweep (`StageSpec.lane_dependencies`) so it renders the day's alerts. Stage keys, argv, per-stage timeouts, the always-attempt contract, the 18h resume checkpoint, run accounting, exit codes and the scheduler task are unchanged. The single dispatcher thread records checkpoint completions, so concurrent completions cannot lose a key. Both new manifest fields are bound into the manifest digest. No alert, action, panel, route, scheduler task or managed service changed, and no new operator action is supported. Evidence: `tests/test_morning_pipeline_lanes.py` (recorded overlap, single in-flight writer, dependency ordering, manifest timeout under lanes, checkpoint merge and resume) and `tests/test_run_morning_pipeline.py`.

### Overnight writer start stagger (2026-10-06)

Disposition: **scheduler cadence change, no new surface**. The three database-writing jobs that all started at 02:00 on Saturdays now start at different times: `fetch_sec_xbrl` at Saturday 01:40 (after the Friday P2 lens dispatch stops at 01:35), `backfill_transcripts` at daily 02:00 (unchanged), and `submit_saydo_batch` at Saturday 02:20. `cron/task_manifest.json`, the task XML files, the wrapper comments and the generated `cron/TASKS.generated.md` agree. The Jobs projection derives cadence from the manifest, so it shows the new times without a code change. The moved SayDo LLM leg is registered in `directives/llm_quota_scheduling.md`. Task ceilings are unchanged, so a long `fetch_sec_xbrl` run can still overlap the later jobs; their writes then serialize through SQLite's busy timeout as before. The live Windows Task Scheduler must be re-registered from the main checkout, and that has not been verified from this Mac. Evidence: `tests/test_cron_hardening.py::test_overnight_writer_cluster_is_staggered` and `tests/test_scheduler_manifest.py`.

### Trigger-stage scan sweep and cost-cap accounting (2026-10-06)

Disposition: **no surface change** (internal execution change inside an existing scheduled stage). The stage_1 trigger driver (`execution/run_triggers.py`) now runs the `material_news` per-ticker classification scans in a bounded-concurrency sweep (new internal flags `--scan-workers`, default 4, and `--scan-timeout-s`, default 180s; the morning-pipeline manifest's stage argv is unchanged) and admits each wave only while the shared `llm_calls` ledger total since run start is below the run's `--max-cost-usd`, so scan-time spend is cap-accounted exactly like candidate spend. The LLM governance view remains complete and attributable with an unchanged schema: each `material_news_classification` call still writes its per-call ledger row (purpose, ticker, model, cost) at call time through the same transport writer — the change is that the run's cap decision now reads that spend. The run's stdout summary gained additive internal accounting (`scan_deferrals` with a closed reason set `cost_cap`/`per_ticker_timeout`, `scan_workers`); no alert, action, panel, route, scheduler-task, or managed-service surface changed, and no new operator action is supported. The driver also adopted the shared `execution/_lib.py` bootstrap seam in the same change (its legacy per-file `sys.path` bootstrap and the stale architecture-allowlist entry were removed together); its agreed CLI-flag change is the two sweep flags above. Evidence: `tests/test_run_triggers_material_news_sweep.py` (bounded worker maximum, wave-synchronized cap admission reconciling to the ledger total, explicit per-ticker timeout/cap deferrals, fixed-fixture driver parity via the unchanged `tests/test_run_triggers_e2e.py`).
### Shared truth remedies — 2026-10-03

Disposition: **no Operations surface change**. Explore, Ask, saved financial
conditions and Financials evidence use the existing provenance-aware readers.
`GET /api/peek/canonical-financial` supports the exact table or series reference.
`GET /api/peek/financial-calculation` reconstructs growth from its original
inputs and calculation version. Both are read-only product evidence views.
They add no operator action, source acquisition or financial write. Invalid
references fail before database access. Missing admission remains unavailable.

The existing DCF save, rebuild and Sheet import operations retain their action
boundaries. One per-ticker artifact owner now covers their writes. All durable
builders use the selected configured database. A committed result with failed
backup cleanup remains saved, names the recovery requirement and blocks another
write. It must not be displayed as a rejected candidate. Sheet export transfers
captured accepted bytes; import validates an editable candidate before promotion.
This repair does not create a new action or authorize a live Sheet transfer.

Retained extractor, repair and seed commands, and source telemetry, use the
existing database resolver. Artifact roots are explicit and independent of
database placement. Portfolio cache displays and decision reads share validation,
source date and degraded state. Tracker v1 rejects incompatible position and
transaction snapshots. No transport switch or scheduled operation is activated.
The removed foreign arithmetic helper had no operator surface. Its retained CLI
still refuses unsupported financial writes. Shared evaluation-file parsing keeps
the same purpose validation and grading contracts.

Evidence: retained-tool authority tests; DCF artifact ownership and real import
rejection tests; financial consumer and growth evidence tests; portfolio cache,
tracker adoption and morning pipeline tests. The release audit records exact
test and rendered results. Those checks establish code behavior, not live health.

### Publisher authority capture repair — 2026-10-04

Disposition: **no Operations surface change**. The existing authority capture
command uses the shared portfolio database writer lane. Apply requires an
explicit configured database and rejects another target before connection or
network access. Repeated captures of identical publisher bytes retain the
original immutable blob-location clocks and record a separate source observation.
Reuse verifies the retained local bytes within the supplied actual content root.
Missing, corrupt, outside-root and reparse-path replicas fail closed without
replacement or a new observation. Existing request, receipt, coverage and source
admission contracts remain unchanged. An observed archive response still does
not prove exhaustion or acquisition completeness.

Evidence: capture renewal and replay tests; writer contention and configured
route tests; retained-byte, local-path and immutable-artifact tests. Native
Windows verification remains a separate release gate. No new action, scheduler,
service, schema or financial-data admission is introduced.

### Scheduler job receipt authority

The Operations panel and review bundle read each job receipt from its declared
writer root. `cron/run_python.bat` defaults to the deployed code checkout.
`prepare-kpi-semantic-review` is the explicit exception: its wrapper derives
the product-state root from the configured database path and passes that root
to the job runtime. The dashboard uses the same resolved database declaration
for this exception. Dashboard scratch artifacts do not replace either writer
root. Missing, stale and invalid receipts remain explicit; another root never
supplies fallback evidence. This changes receipt observation only. It adds no
scheduler operation, job execution or financial-state write.

### Cached host-owner monitoring endpoint

Disposition: **linked governed view**. `GET /api/operations/host-runtime`
restores the existing private fleet monitor's read-only transport for the
`HostRuntimeBundle` owned by `src/operations/host_runtime.py`. It reads only
the bounded cached host receipt from the configured product-state root.
Missing, stale, invalid and current evidence remain explicit. The response
uses the same configured private HTTPS origin and code identity as the
operations review bundle; request headers cannot supply either identity.
It returns a content digest, `ETag` and `Cache-Control: no-store`. It adds no
host probe, database read, recovery action, financial payload or scheduler owner.
`tests/test_comments_server_host_runtime.py` verifies those boundaries.

The Operations & Governance workspace remains a truthful operator-facing map as functionality is added, removed, renamed, or changes ownership. It is not an inventory of every module or CLI. It shows supported operations, their declared ownership, current evidence, freshness, failure state, and guarded actions at the level needed to understand or operate the product safely.

## Trigger matrix

Run this review when a change affects any of the following:

| Change | Required review |
|---|---|
| Scheduler task, wrapper, cadence, enabled state, job identity, write lane, or service ownership | Confirm the dynamic Jobs projection, receipt identity, and freshness remain truthful. |
| Supported manual or managed-service operation | Declare ownership, run/failure state, and whether an operator action is supported. |
| Source/provider pull, telemetry, rate limit, retry, backlog, or circuit | Confirm recent health, completeness, failure, and evidence-time visibility. |
| LLM purpose, model route, budget, eval, cost, latency, fallback, or failure telemetry | Confirm the linked LLM governance view remains complete and attributable. |
| Queue, lock, service, backup, restore, WAL, incident, or notification behavior | Add current evidence or state that the observation is unsupported/unavailable. A backup run may end `skipped_unchanged` (`StageStatus.SKIPPED` plus the marker in the accounting row) when the consistent snapshot's sha256 matches the last successfully uploaded backup and that upload receipt and snapshot are still present: it is a healthy no-op, not a failure and not a fresh upload. Operator evidence must keep run recency and upload recency distinct — recoverability evidence remains the last uploaded snapshot, never the most recent run row. |
| Migration that changes operational telemetry, receipts, retention, provenance, or recovery | Confirm expected/actual schema and recovery meaning. Ordinary business-schema migrations need only a no-surface-change reason. |
| Approval, retry, one-off run, enable/disable, apply, or other operator mutation | Apply the Operator-action boundary below. |
| Removal or rename of any supported item above | Apply the Removal contract below. |

Pure implementation refactors, test-only changes, prose-only changes, and internal CLIs that are not supported operator workflows may use `no surface change`, but the reason must name the preserved contract. Do not scan every Flask route or `execution/*.py` file and treat it as a product capability.

The bounded lock-filename repair has **no surface change**. Logical write-set keys,
ownership proofs, mutex rights and cleanup remain under the existing runtime owner.
Lock and transition-guard filenames of at most 255 filesystem-encoded bytes stay
exact. Longer components use the whole original filename's SHA-256 and retain
`.lock` or `.guard`. This can change a long Unicode name that a local filesystem
previously accepted. Deployment must use the existing coordinated sole-writer
boundary; mixed old and new lock-name versions do not prove mutual exclusion.
No alias lock, new operator action or deployment authorization is added.

## Projection and display contract

1. **Project from owners.** Extend canonical owners and adapters first. `src/operations/registry.py` compiles Scheduler tasks/wrappers, services, LLM/eval definitions, source policy, queue states, and the expected Alembic head. Do not copy current task names, purpose names, providers, routes, or schema heads into this directive or the renderer.
2. **Observe without side effects.** `src/operations/snapshot.py` may use only the caller-owned read-only connection and bounded typed receipts/files. Configuration, registration, historical execution, current runtime state, and freshness are separate facts.
3. **Disposition every domain.** Each material `OperationsRegistry` and `OperationsSnapshot` field has exactly one disposition in `src/pipeline/operations_panel.py`: a visible primary tab, a linked governed view, or a deliberate exclusion with a reason. Adding or removing a model field must fail the surface-completeness test until reviewed.
4. **Render truthful states.** A visible observation includes status/value, observed time, evidence-recorded time when available, safe evidence label, and complete empty/loading/missing/stale/invalid behavior. An empty successful read says that no records were found; it does not say healthy.
5. **Keep attention complete.** Any visible bad, invalid, stale, stopped, failed, blocked, or otherwise action-requiring governance state contributes to the headline attention model unless a tested rationale explicitly makes it informational.
6. **Keep linked views governed.** A linked diagnostic may satisfy the disposition only when it is reachable from the Operations workspace, uses the governed loader, and preserves the same truthfulness and sanitization boundaries.

## Removal contract

Removing or renaming functionality requires all of the following in the same coherent change:

- remove or update the canonical owner and typed projection;
- remove obsolete cards, rows, controls, links, copy, filters, and attention rules;
- keep retained historical evidence distinguishable from an active capability;
- test that the retired identity is absent and that no orphan route or action remains;
- preserve a truthful unavailable/deprecated state only when historical interpretation still requires it.

## Operator-action boundary

Observability does not authorize mutation. A control appears only when the underlying capability has its own authorization, validation, idempotency/concurrency contract, bounded execution, durable receipt, loading/error/retry state, and applicable confirmation. Destructive, enabling/disabling, production-write, or broad-run controls require their separate security and activation review. If any prerequisite is absent, render read-only status or an explicit unsupported state instead of a button.

## Required evidence

At minimum, record one Pull Request disposition and run the relevant set. The Pull Request block is a reviewer/agent checklist; CI does not parse checkbox selection. The deterministic guards are the owner-equality and surface-disposition tests below.

- `tests/test_operations_registry.py` for exact projection from canonical owners;
- `tests/test_operations_snapshot.py` for bounded read-only evidence, freshness, and fail-closed states;
- `tests/test_operations_panel.py` for surface dispositions, attention, sanitization, actions, accessibility, and responsive structure;
- `tests/test_comments_server_operations.py` and `tests/test_work_os_shell.py` for route/cache/loader behavior;
- the full `tests/test_ui_controls.py` for any rendered frontend change;
- browser acceptance for a visible or interactive change: primary navigation, each affected state, 375px and desktop layouts, keyboard/focus behavior, network completion, and a clean console.

A tested `no surface change` disposition must state which canonical owner and visible contract remain unchanged. Static configuration, a green subprocess, or a successful historical row is not current-health evidence.

### Current no-surface-change dispositions

Reviewed KPI re-extraction preserves the existing source-fact population owner,
explicit dry/apply boundary, immutable capture identity and completeness seals.
The admitted definition selects evidence from the same immutable document version
and raw bytes; population seals that selected run. Dry/apply/replay and rejection
tests cover these boundaries. No operator action, registry entry, scheduler,
service, schema or source authority changes.

Application read-performance changes preserve the existing Operations registry,
Jobs, Sources, Data, Actions, and README approval boundaries. Independent panel
loading, bounded reads, cache-busy responses, and retry controls change delivery
and recovery of read-only observations. They do not add an operator action,
scheduler, writer, or service-health claim. A failed README status read disables
Apply until the current status is verified. Route and read-lifecycle tests cover
these preserved boundaries.

Company Desk loading and same-company Retry preserve the prior company identity
and evidence until the response identity validates. The additive selected-observation
lookup index changes query access only. It retains resolution revisions, current-head
selection, and the existing snapshot/readiness migration and rollback procedures.
No operator action, scheduler, writer, or source authority is added.

`execution/sync_list_type_from_holdings.py --apply --onboard-untracked` is the
primary operator surface for approving reviewed tracker holdings into the
research roster. It reuses `db.track_company` for SEC validation, issuer
registration, and onboarding, and requires the existing explicit `--apply`
mutation boundary. The scheduled morning job does not pass the opt-in flag.
There is no Operations-workspace control: the capability has no standalone
durable approval receipt or interactive confirmation state, so adding a button
would violate the Operator-action boundary. The existing Jobs projection and
portfolio-health views remain unchanged.

`issuer_fact_manifest.v2` extends the existing internal, explicit offline-produce/apply batch CLI.
It adds no scheduled job, managed service, runtime-health claim, or operator control. The canonical
Operations registry and its visible Jobs, Sources, Data, and Actions contracts therefore remain
unchanged; manifest validation, transactionality, and durable coverage receipts stay owned by the
ingestion boundary rather than the Operations workspace.

### Streamlit analytics deep-dive sandbox

`explore-sandbox/analytics_deep_dive.py` is a local, optional-dependency analytics surface: an
interactive composition of deterministic catalog → ViewSpec reads over an operator-named
synthetic clone or restored snapshot. It adds no scheduled job, managed service, operator
control, database write, or production access — the app opens every connection read-only,
refuses a checkout-local `data/portfolio.db`, and every number comes from the shared
provenance-aware resolver (`viewspec.engine`), never a sandbox-local query path. Theme and
control parity with the cockpit is generated and drift-checked
(`scripts/gen_streamlit_theme.py`; `tests/test_explore_sandbox_kit.py`), so no new visual
contract exists to govern. The canonical Operations registry and its visible Jobs, Sources,
Data, and Actions contracts therefore remain unchanged; the sandbox is not an operator
workflow and never a production authority.

### KPI definition-revision shadow census

`execution/audit_kpi_revision_shadow_census.py` is a supported manual, read-only rehearsal. The
operator supplies an explicit standalone SQLite snapshot, its snapshot manifest, and timezone-aware
effective, knowledge, and evaluation cutoffs. The command derives the complete active portfolio
population from the database, emits one hash-bound `kpi-revision-shadow-census/v1` receipt, and exits
nonzero because the receipt never authorizes activation. A missing, incompatible, changed, or
sidecar-active snapshot remains `unverified`; even a matching supported manifest records only artifact
identity and the producer's assertions. It does not establish production evidence authority.
The CLI binds the connection's main path to that manifest and file hash, requires no pre-existing
transaction, rechecks identity after the owned read transaction, then closes the read-only connection
and rechecks once more before emitting output. Base KPI rows missing current resolution authority stay
in the population with an exact blocking disposition rather than disappearing through the resolved
view. Unavailable roster or population schema remains distinct from an observed empty population.
An unparseable fact period is retained as an exact blocking disposition rather than omitted or inferred.

A manifest-matched, quiesced snapshot uses the central immutable reader role so the
verification read cannot create WAL sidecars. The existing sidecar and identity
checks remain mandatory before and after reading. `execution/audit_kpi_revision_reader.py`
compares the existing sourced time-series loader with the revision-aware projection
for one explicit definition and cutoff. It retains source locators, observation and
resolution revisions, duplicate selections and semantic breaks. Its receipt remains
HOLD and does not change the default reader or activate a cutover.

This capability has no schedule, service, retry control, database write, or Operations-workspace
button. Its current supported state is `hold`: deterministic resolver readiness is visible in the
receipt, while reader activation requires separately approved portfolio evidence and an explicit owner
decision. The CLI is the primary operator surface for this rehearsal; no current-health projection is
claimed from an old receipt.

### Captured IR event ingestion

The captured-feed CLI is an internal, explicit offline-produce/apply operation. It adds
no registered Scheduler task, managed service, network fetcher or operator action.
The Operations registry therefore has no new item to project. Existing Scheduler
ownership and lock receipts remain authoritative if a job is later registered.
Forward calendar coverage is deliberately surfaced beside its retained events using
`ir_event_runs`, source observation time, and the active tracked roster; it is not a
claim about provider health. Missing, stale and failed receipts cannot render a
verified empty calendar. `tests/test_ir_events_ingestion.py` and
`tests/test_ir_events_cli_unavailable.py` cover these boundaries and disabled apply.

### Legacy evidence backfill and managed IR byte admission

`execution/backfill_evidence_ledger.py` remains an explicit, bounded manual operation;
its CLI is the primary surface, with no scheduled job or Operations-workspace control.
Apply holds the exact target database-adjacent writer lock and a checkpoint lock
rooted at the artifact state location, shared across code checkouts. A validated
ancestor lock is reused only when it owns this exact target database. Exit 2 reports quarantined
items in the current batch, and exit 75 reports lock contention; exit 0 alone never
proves population completeness. Inspect `has_more` and the retained checkpoint.
Checkpoints bind the resolved database path, retain quarantined document identities,
and process unseen documents before bounded rotating retries. A legacy unbound
checkpoint requires a new task ID; do not alter its cursor to imply successful repair.
Do not loop unchanged failing batches merely because `has_more` is true. Restore or
recapture missing/mismatched source bytes under their own authority before retrying.
The path binding is not proof against an in-place database replacement; retain the
canonical state authority and its recovery/version checks.

Managed IR publication keeps its existing operation and visible receipt contract.
It now anchors retained bytes in the evidence ledger within the publication transaction
and verifies that foundation on replay. This does not establish extraction completeness,
semantic admission, or a current-health claim. Historical publication receipts without
that foundation require explicit reconciliation; changed verifier identity can require
re-preparing staged artifacts. **No surface change:** the canonical Operations registry
and visible Jobs, Sources, Data, and Actions contracts remain unchanged. Regression
coverage lives in `test_evidence_backfill.py`, `test_evidence_backfill_cli_guard.py`, and
`test_managed_ir_sources.py`, including quarantine recovery, contention, byte drift,
replay integrity, and transaction rollback.

### Recovery queue policy migration

Migration `0040_fmp_watchlist_recovery` admits automatic evaluation and watchlist
work in the existing FMP queue. It preserves stable work IDs, retained rows, indexes,
attempts, and events; provider budgets, circuit controls, and source-policy checks
remain operative. Claimed explicit requests require a nonblank request identity.
Schema upgrade belongs to the canonical database writer under the existing backup
and deployment procedure. Downgrade fails without deleting rows that require the
newer role/request constraints. No scheduler activation or new UI action is implied.

### Progressive document evidence continuation

`execution/process_document_evidence.py` provides a read-only plan by default and
an explicit bounded `--apply` mode for stored active portfolio, evaluation, and
watchlist documents. It captures matching retained bytes, runs supported deterministic
full-text extraction through existing owners, and refreshes only already-linked
source inventories. It performs no source crawl, LLM call, financial semantic
admission, or completeness initialization. Newest stored document IDs receive
priority; that ordering is not proof of the latest issuer disclosure.

The existing morning pipeline includes this deterministic stage, with an explicit
database and product-state root, bounded batches, and existing runtime accounting.
No additional scheduler task or LLM window is created. `--resume` retains an atomic
database/scope-bound receipt and cursor under the product-state Operations runtime
directory. A failed item remains in the pending set; completed input/extractor
versions replay without another extraction. Structural scope mismatch fails closed.
The exact target database-adjacent writer lock and a product-state-root receipt
lock coordinate across code checkouts; inherited ownership is reused only for
the same verified target.

The JSON result is the primary detailed operator surface: it distinguishes capture,
extraction, already-covered, quarantine, unsupported, failed, and not-attempted
outcomes, plus missing source inventory. Exit 2 indicates degraded/unfinished proof;
75 indicates contention. The existing morning stage status exposes degradation,
while the receipt explains individual documents. Do not interpret a stored batch
receipt as current proof after inputs, extractor versions, or inventory scope change.
No new Operations-workspace action or health badge is introduced.

The existing dashboard refresh and IR-refresh actions execute the deployed code root
with the application-managed Windows interpreter while retaining the configured
product-state root and write ownership. A missing managed interpreter returns an
explicit unavailable response without starting a job; global Python is not a fallback.

Historical accession-key documents use the dedicated append-only SEC binding/fact-match
repair owners, not a hash overwrite. Those owners' isolated-target restrictions remain
in force. Restoring unavailable original bytes, bootstrapping issuer authority and
source inventories, semantic admission, and production cutover require their own
evidence; this continuation cannot imply those steps passed.

### Transcript commitment-scan evidence repair

`execution/audit_commitment_scan_evidence.py` is the supported manual, read-only inventory for
selected active-portfolio transcript evidence. It requires an explicit database and project root,
uses a query-only connection, and emits stable reason codes for missing artifacts, hash mismatches,
missing or invalid acquisition binding, typed scan coverage, and complete scans. Its receipt is an
observation only; it never creates acquisition lineage or authorizes a repair.

`execution/extract_commitments_from_transcript.py --auto --reaudit-invalid-evidence --ticker TICKER`
is the explicit bounded repair surface for legacy-unobserved or invalid scan evidence that still has
an exact authorized acquisition binding. Ordinary automatic extraction continues to exclude those
states. The repair uses the governed `saydo_commitment_extract` route and appends immutable segment
observations and output receipts; it does not overwrite historical receipts, admit unreceipted bytes,
or clear missing/hash-mismatched artifacts. A single `--transcript-id` remains the narrower explicit
target.

Neither command adds a scheduled job, managed service, current-health claim, or Operations-workspace
button. Their primary operator surface is the CLI because the re-audit requires a deliberate bounded
scope and the audit can be expensive over large retained artifacts. Any future UI control requires a
separate activation review under the Operator-action boundary.
### WIX history correction

`execution/repair_wix_history.py --db <explicit-target> prepare` produces a private,
immutable review plan from complete current tracker holdings and transaction pages,
the exact owner decision, and retained before-images. It never writes the database.
The plan freezes any existing linked owner checkpoint and expires after one hour or
at the UTC date boundary. `apply --plan <file> --approved-sha256 <fingerprint>` uses
the exact database-adjacent writer lock, revalidates sources and target identity,
and atomically corrects the lifecycle while superseding the invalid generated note.
Migration 0044 retains a duplicate lifecycle identity through an immutable supersession
link; canonical active readers exclude it, while direct historical lookup retains it.
Original note text and all affected before-images remain in correction provenance.
The replacement observation is advisor-authored and explicitly owner-review-pending.
No AVDV proceeds attribution, investment grade, or owner lesson is inferred from trades.

This is a manual recovery command, not a scheduler or dashboard action. Production
use requires the exact reviewed target/plan and the canonical host's verified backup
and deployment procedure. Failed compare-and-swap, stale evidence, ambiguous new
purchases, invalid account coverage, or transaction failure leave records unchanged.
The CLI emits a typed failure category without source payloads. Keep its private
plan outside public artifacts; prepare again after changed evidence or migration.

### Factor and rate calculation repair

The existing business-factor refresh resolves an explicit/configured database,
uses materialized holdings rather than the tracked roster, and shares the target
writer lock. Factor admission binds current thesis, business mix and taxonomy
hashes; holdings timestamps and the existing 48-hour cache policy remain operative.
`execution/compute_macro_sensitivities.py` writes immutable, input-bound estimates
under migration 0041. Legacy unversioned rate rows remain retained but are excluded
from current rate conclusions; old risk snapshots do not establish an admitted beta.
Rate shocks in basis points are converted to percentage-point changes before using
a return-per-percentage-point coefficient. Canonical-host migration, recomputation
and fresh reader verification remain operational steps; local fixtures do not prove
that live legacy values have been replaced. No new scheduled window is added.


### Collection evidence read surface

The manual SEC filing-inventory collector reads `EDGAR_USER_AGENT` from the process
environment first, then reads only that key from the configured project env file on
each invocation. An absent contact uses the existing public default. The collector
does not import other private settings into its process environment. SEC 401/403
hard stops return a stable `blocked`/`sec_inventory_hard_stop` JSON result with
`retryable: false` and exit 2; the existing identity hold remains distinct. This
status does not by itself identify the cause. A selected contact with header
controls, non-Latin1 characters, or a nonblank length outside 8–512 characters is
rejected before transport with `blocked`/`sec_contact_configuration_invalid`,
`retryable: false` and exit 2; neither contact value nor a value-bearing cause is
emitted. This does not retry acquisition, assert source coverage, or add a
scheduled action.

The manual inventory collector timestamps each successful SEC response after its
bytes arrive. A source observation records that response time separately from its
later evidence-recording time. Package `state.v2.json` retains each successful
response hash and original clock immediately, even when the next component fails.
It also pins the acquisition config and collector version; an incompatible or
malformed replay returns `blocked`/`sec_inventory_checkpoint_invalid`,
`retryable: false` and exit 2 before source network or publication. Preflight
verifies every referenced v2 component; missing or hash-conflicting bytes and
future retrieval clocks are malformed replay. Resuming a compatible component
does not claim a new HTTP retrieval. Older `state.json` checkpoints retain their
bytes and identity, but have no retrieval clocks. Their entries are refetched
within `--package-limit`
before they can support new source observations; file modification times are never
promoted to retrieval evidence. A failed component remains deferred while any
successful sibling stays available for exact-byte resume. The operator still
uses the same manual CLI and output fields; this changes no
dashboard action, schedule, provider, database schema, or approval state.

Settings distinguishes authorization from observed SEC coverage. Portfolio, evaluation,
and watchlist retain automatic source authorization. A sealed SEC inventory defines the
listed native-filing population; immutable document/source identities, exact-byte location
receipts and successful extraction locators prove individual captures. CompanyFacts is one
aggregate snapshot. Missing, unavailable, unsealed and partial evidence remain explicit.
A covered historical population has **unknown current freshness**: capture age is displayed,
with no invented universal SEC expiry or inference from an Operations heartbeat.

FMP timing/backlog detail derives from the recovery ledger. Migration
`0045_fmp_recovery_receipts` adds an immutable final run receipt, separate from events and
attempts, with expected work, attempted/reused proof, unresolved work and typed outcome.
The existing 24-hour FMP freshness policy governs receipt age. An interrupted run has no
terminal receipt until finalization; reusing a finalized run ID for new execution is rejected.

Operations headline attention includes these same derived FMP/SEC evidence gaps and links
to Settings. Covered historical populations with unknown freshness are informational gaps,
never a green current-coverage claim. No new collection action, scheduler, refresh control,
network permission or production activation is introduced by this read surface.

### Qualitative common-drawdown inspection

`execution/common_drawdown.py` reads an explicit existing database and emits
current or paired scenario evidence. It cannot apply trades or modify holdings.
Factor admission, constituent-weight coverage, source dates and unknown ETF
publication currency remain visible; missing after-state coverage cannot be
presented as an improvement. The WIX/AVDV target band remains an unverified
scenario input. Synthetic replay lives in `tests/test_qualitative_stress.py`.

### News collection failure state

The existing Yahoo journalism adapter remains the default free general-news leg;
paid web discovery remains opt-in. A Yahoo transport or response-container failure
now yields partial completion (exit 2), while valid rows from other feeds remain
persisted. Any persistence failure yields exit 1. A completed attempt does not
certify source completeness. Rejected provider diagnostics use the canonical
structured redactor before storage; article identity retains nonsecret URL query
parameters. No new source or scheduled action was added.

### Captured foreign-source normalization

`execution/normalize_foreign_filings.py` now requires an explicit database,
frozen exact-document input manifest and new immutable receipt path. Dry-run is
read-only. Apply holds the existing target/package locks and publishes only
selected captured-source facts through the shared source repository. HOLD means
prerequisites failed; PARTIAL includes any committed source progress but does not
claim canonical semantic binding or decision-grade consumer readiness. Native
packages still require authoritative sealed inventory and the qualified offline
processor. This command performs no source acquisition, scheduler activation or
whole-corpus canonical population.

Before source publication, apply establishes the selected documents' recorded-subject
bindings from the existing canonical issuer registry. Unknown or conflicting identities
stop the run. This prerequisite is a separately committed, idempotent stage; its actual
progress remains in the receipt if a later source publication fails. It grants no metric
semantic admission.

### Source measurements and foreign-source comparison

The shared HTTP clients append actual attempt measurements with their source-call rows
under migration `0047_source_regime_measurements`. Missing regime attribution, costs,
record counts and operator time remain unknown. Dashboard logical-call totals exclude
the additional physical-attempt rows, avoiding double counting. Persistence failures
remain visible through the existing HTTP event.

`execution/attribute_source_cost.py` verifies an explicit retained canary selection and
reports measured and missing evidence. `execution/backfill_foreign_oracle.py` compares
explicit sealed source observations with independent counterparts through the shared
fact reader and, when supplied, a verified ontology snapshot. Empty inputs, missing
counterparts, self-comparisons and unknown completeness cannot pass. Both are manual
diagnostics; they introduce no schedule, acquisition, dashboard action or activation.


### Actual SEC execution receipts

Migration `0046_sec_execution_receipts` records actual apply requests, running attempts,
and immutable terminal results from the native capture and inventory synchronization
writers. A retry is a new attempt of the same scope-bound logical request; replaying an
identical terminal result preserves its original timestamp. An interrupted attempt remains
completion unconfirmed. A completed batch never proves current issuer coverage or process
liveness. Dry runs and pure SEC plan/admission functions remain database-write-free; no
historical scheduling state is backfilled.

Settings uses the existing canonical transient-fetch assessment reasons to distinguish
actual deferred native work from other failures. Execution details bind selected documents
and snapshots; mismatches with the current population are explicitly historical. Missing
or invalid terminal evidence contributes to Operations attention. Exact captures and
source inventory remain the coverage authorities, with no universal SEC freshness TTL.

### Sealed canonical growth rendering

The existing internal `execution/render_three_regimes.py` action accepts
`--input-manifest`, `--manifest-sha256`, and `--output-dir` for an explicit closed
snapshot and hash-bound source inventory. It reconstructs the migrated canonical
growth projection for the fixed cohort under all three source regimes and writes
immutable, provenance-bearing artifacts. Excluded canonical winners remain
unavailable; this action does not choose an alternate source. Full acceptance stays
`HOLD` with a nonzero exit while report/DCF/valuation migration, acquisition
completeness, regime-specific resolution, and OS isolation evidence are missing.
This extends the internal offline action only: no Operations control, schedule,
production write, or managed activation is added.

### Grading database authority

`execution/grade_bear_cases.py` now accepts `--db` for an explicit existing
fixture or approved retained database; omission resolves the configured database
through `db_paths.require_db_path`. A checkout-default or missing database fails
before materialization or grading. The same path is passed to prediction stores,
corpus reads and calibration, and scoped through internal LLM bookkeeping with
restoration on failure. No scheduler, Operations control or live grading is
activated by this change. The targeted authority tests exercise explicit fixture
selection, refusal of checkout state, and context restoration; they do not certify
the remaining legacy financial corpus or qualitative grading accuracy.

`execution/grade_decisions.py` likewise requires the configured existing database,
with `--db-path` / `--db` for an explicit override. It preserves the price-only
verdict rules while admitting only same-instrument, quote-currency-bound,
split-and-dividend-adjusted observations within the shared market-price age
limit. Missing or contradictory price evidence leaves the decision pending and
does not emit calibration. Successful outcomes retain a versioned input manifest
in their notes; source capture freshness remains explicitly unverified. This
changes the existing manual grader only, without activating a run or schedule.

### README generation contract evidence

The existing README update action retains a compact attestation for each generator
and judge call, binding its registered purpose, prompt and schema versions, source
hashes and observed provider attempt. The facade is limited to the existing
`meta_eval` scope; prompt drift fails closed and missing usage, cost or effective
prompt verification remains unknown. Setup, schema and budget failures cannot
enter provider fallback. This changes evidence retained by the existing action;
it adds no Operations control, schedule, provider activation or live execution.
Other LLM consumers remain outside this migrated tranche.

`execution/grade_predictions.py` uses the same explicit/configured database
boundary. It grades only uniquely identified, admitted revision-aware KPI facts
known at the batch cutoff; ambiguous names, unsupported concept bindings and
unit mismatches remain pending. Retained notes bind the source and target inputs.
A short conditional publication checks the complete loaded prediction, preserving
newer owner edits and existing outcomes. Future-made predictions are excluded;
extraction calibration retains its existing malformed-input formula and excludes
failed outcome writes. This extends evidence and controls for the existing manual
action only; no grading run, service or schedule is activated.

### Canonical report and valuation readers

The existing report financial tables read admitted canonical observations at one
knowledge cutoff, retain their source manifest, and show unavailable cells when
fiscal identity, units or comparability are unproved. Current P/E and P/FCF use
comparable annual windows of reported facts plus separately captured market
capitalization. Annual consensus estimates and historical provider ratios retain
distinct bases; unlike bases cannot establish a valuation comparison band.
These read surfaces add no operator action, schedule, acquisition permission or
live cutover. Source-population completeness and activation remain separate gates;
positive synthetic reader tests do not certify decision-grade coverage.

### Canonical DCF statement inputs

`execution/build_redesigned_dcf.py` now reads `src/sources/dcf_statements.py`,
which resolves exact reported statement observations through the existing canonical
fact and provenance readers at one cutoff. Missing, semantically changed, partial,
or non-primary inputs fail closed rather than falling back to provider cache data.
This changes the inputs to the existing explicit DCF build only. It adds no
operator action, scheduler entry, managed service, source acquisition permission,
or production activation; the Operations registry and visible workspace contracts
remain unchanged. `tests/test_dcf_canonical_statements.py` and
`tests/test_redesigned_dcf_smoke.py` exercise the admitted and rejected boundaries.

### Canonical evaluation snapshot

The existing evaluation report snapshot now projects admitted financial cells and
captured market context at one explicit cutoff. It retains exact cell lineage for
annual, TTM, margin, and CAGR displays; unavailable, stale, malformed, mixed-
currency, or semantically incompatible inputs remain unavailable. This is a
read-only report projection. It adds no operator action, scheduler, managed
service, source acquisition permission, or production activation; the Operations
registry and its visible workspace contracts remain unchanged.

### Canonical soft-rule financial reader

Soft-rule evaluation now reads reported financial quarters through
`src/sources/canonical_financial_series.py` at one explicit cutoff. It retains admitted
observation and definition lineage; unavailable, stale, semantically changed,
mixed-currency, or incomplete series remain unresolved. This only changes the
existing local evaluator's read path. It adds no operator action, scheduler,
managed service, source acquisition permission, or production activation; the
Operations registry and visible workspace contracts remain unchanged.

### Canonical cockpit fundamentals

The existing cockpit fundamentals cache now reads admitted reported financial
series at one explicit cutoff and retains source manifests for its displayed
revenue growth and free-cash-flow margin. Rejected, malformed, incomplete, or
incompatible series remain unavailable; the existing refresh command and cache
contract are retained. This adds no operator action, scheduler, managed
service, source acquisition permission, or production activation; the
Operations registry and visible workspace contracts remain unchanged.

The same existing morning stage also materializes a bounded cockpit file-input
snapshot under `.cache/` for FMP profile, valuation-basis, earnings-calendar,
and holdings inputs. The cockpit continues to read the original source files on
a per-ticker cache miss or invalid entry; source authority and freshness
semantics do not change. This adds no operator action, schedule, managed
service, source acquisition permission, or production activation; the
Operations registry and visible workspace contracts remain unchanged.

### Canonical revenue year-over-year computation

The existing metrics engine can now persist an admitted canonical revenue
year-over-year derivation with its output observation and sealed input lineage.
It remains part of the existing local computation path: missing, ambiguous,
incomparable, incomplete, or cutoff-ineligible quarterly inputs produce an
unavailable computation rather than a derived value. This adds no operator
action, scheduler, managed service, source acquisition permission, or
production activation; the Operations registry and visible workspace contracts
remain unchanged.

### Valuation evidence preflight and artifact database handoff

`execution/valuation_preflight.py` is an internal read-only diagnostic over the
existing persisted valuation and canonical fact readers. It takes an explicit or
configured database authority and reports typed blocked/unavailable reasons; it
performs no acquisition, model promotion, job enablement or provider retry.
It is deliberately excluded from interactive operator controls: it has no
mutation capability or independent runtime-health claim. Allocation/advisor gates
consume the same evidence projection. The canonical Operations registry and Jobs,
Sources, Data and Actions projections remain unchanged. Dispatcher freshness and
dirty-artifact child database handoff and strict queue availability checks tighten
existing execution contracts without adding a task, schedule or write lane.
Focused preflight, dispatcher and artifact regression tests establish this
no-surface-change disposition.

### FMP owner admission

The shared FMP adapter and shared HTTP FMP lane read the nonsecret policy named by
`EARNINGS_SUMMARY_FMP_POLICY_FILE` before admission and each network attempt.
The path must be absolute and outside the deployed code root. The bounded JSON
schema is exactly `{"schema_version":1,"enabled":false}` (or `true`). An unset
setting preserves existing admission; a configured missing, invalid or unreadable
file fails closed. Local denials are typed nonretryable `provider_disabled_by_owner`
or `provider_admission_invalid`, never an authentication failure or fabricated HTTP
attempt. Redirect targets use the same check, including redirects from other hosts.
There is no automatic provider probe to reverse an owner opt-out.

Activation belongs to the existing external environment authority resolved by
`runtime.secrets.project_env_file`, not a checkout config or a new policy database.
The managed job runner loads that environment before copying it into each child;
the dashboard loads it through `configure_runtime_db` at startup. An already-running
service needs its approved restart to acquire a newly added pointer. The loader
preserves existing process variables (`override=False`), so activation must verify
that the effective pointer agrees with the reviewed external file; setting a Machine
or NSSM variable alone does not prove child propagation. Once loaded, policy-file
content changes apply to subsequent admission checks without restarting. Replace
that file atomically; incomplete updates deliberately block. This code change does
not activate the flag, restart an owner, or change schedules or credentials.

Automatic news collection does not turn an owner/config denial into per-ticker paid
web discovery, even when the fallback scope includes that ticker. Explicit manual
`--source websearch` and independently configured additive feeds/scoring retain their
existing policy. Calendar `--force` overrides the subscription-tier gate only. Its
wrapper still runs expected-earnings fallback, and retains a nonzero first-step exit
rather than reporting fallback success as overall success. Retained FMP caches are
not deleted or relabeled as freshly acquired issuer facts.

Disposition: the CLI's typed denial and existing job failure surface are primary;
there is no new dashboard action or provider-health badge. Existing source readiness,
source receipts and Operations registry remain authoritative; this flag proves no
source completeness. Dedicated FMP fetchers use `FMP_CLIENT`; the macro FMP seam
is already disabled. Raw third-party transports outside this shared adapter are not
a host-wide network firewall and must use this same admission boundary if an FMP
lane is added. Hermetic transport, news and calendar fixtures cover denial and hot
changes; the BAT execution fixture additionally requires Windows qualification.

### Native MELI reported-table source publication

`backfill_fulltext_evidence.py --source-lane evidence_native --document-version-id <id>`
is an exact-document operation. It neither reads nor advances the global batch checkpoint;
other captured documents remain untouched. Legacy `--document-id` remains a separate lane.

`publish_meli_reported_tables.py` is a bounded manual source-publication CLI, with dry-run as
the default and `--apply` as its explicit append boundary. It requires an explicit database,
current sealed SEC inventory/accession, primary native document version, qualified native
HTML extraction run, and allowed local content roots. It never acquires data or calls a
provider. Its current recipe supports a June 30 10-Q whose table explicitly reports the
six-month duration (a missing SEC capture start date is derived from that header, and a
conflicting start date fails) and exactly
three current-period rows: H1 NIMAL, issuer-reconciled available cash and investments,
and total debt including operating leases. Recipe v2 retains the raw percent and
USD-millions values, units, source tokens, table headers and definition commitments.
Its versioned representation conversion publishes ratio (percent ×0.01) and USD
(USD-millions ×1,000,000), with raw/normalized values and the exact multiplier in
each immutable source locator and a distinct source-definition/cell identity.
Dry-run dispositions expose both representations. No analyst cash allocation,
lease adjustment, canonical admission, or financial estimate is performed.
Prior recipe observations remain immutable. A changed/missing/ambiguous header,
definition, row, or scope is rejected.

The CLI emits a closed three-member captured/rejected population. Apply appends the
scoped extraction's disposition nodes and `SourceFactRepository` publication atomically;
an identical rerun reuses the immutable publication. Exit 0 means all three source members
were captured, exit 2 means a partial/rejected population, and exit 3 means unavailable
prerequisites or invalid evidence. The seal closes only this named population: it does not
upgrade whole-document extraction coverage, claim canonical metric admission, or make a
valuation eligible. Source-definition commitments preserve management wording and exact
native evidence locators. Effective-dated ontology definition/mapping/binding, canonical
resolution, approved model-role assignment, remaining reported inputs and dated model
assumption/scenario review remain separate producer and consumer prerequisites.

Disposition: deliberate exclusion from interactive Operations controls. The existing Jobs,
Sources, Data and Actions registries remain unchanged; no scheduler, managed service or
background retry is added. CLI diagnostics and immutable scoped publication receipts own
this manual operation. `tests/test_meli_reported_tables.py` verifies native identity,
raw-byte/locator replay, semantic rejection, dry-run, append/replay and absence of fabricated
legacy documents or canonical-admission claims. Existing full-text tests retain the global
batch behavior. No browser or service change is made.

### MELI verified refresh: explicit assumptions artifact

The existing MELI refresh operation now requires a separately configured reviewed
assumptions artifact. Pass `--meli-assumptions-path <approved-state-file>` to
`execution/refresh_dcf.py --ticker MELI`, or set `DCF_MELI_ASSUMPTIONS_PATH`.
The existing explicit/configured database authority remains separate. The
subprocess receives both authorities unchanged; it never infers an artifact root
from the database parent or falls back to checkout `data/` or a runtime junction.
The exact artifact path and content hash are retained in model provenance.
For MELI, the explicit package selects the specialized recipe even in a clean
checkout. Before dispatch, the parent validates its request shape, ticker and
recipe and rejects conflicting or malformed local family hints. It passes the
captured SHA-256 as `DCF_MELI_ASSUMPTIONS_SHA256`; the child validates that hash
against the same bytes it parses, before opening the database. This routing
check does not establish fact admission or scenario acceptance. A bulk refresh
continues using each other ticker's existing routing independently of this MELI
package.

A verified artifact contains an `input_evidence` request with exact canonical
reported-fact references, a sealed research snapshot and the complete effective
forecast vector (including discount rates). Existing producers must first admit
reported observations, governed metric definitions and bindings, then seal the
resolution/research snapshots with current acquisition and extraction coverage.
The fixed MELI recipe computes TTM values from reported fiscal-year/current-YTD/
prior-YTD operands and computes non-credit fintech revenue and the corporate cash
bridge explicitly. Those calculations are not admitted as reported facts. Metric
definitions must prove the supported fiscal calendar, financial scope and unit;
unknown or mismatched definitions fail closed. No automatic role binder or
production fact bootstrap is provided by this change.

The artifact also requires a dated review bound to the exact actuals and effective
forecast vector, with explicit NIMAL, aggregate-profit, D&A and capital-spending
comparators and variance rationales. The cash bridge uses the issuer's available
cash/investments and total-debt reconciliation pools. Current and non-current
operating-lease liabilities are separately reported inputs; their sum is a model
calculation removed from total debt because native FCFF expenses rent. Finance
leases remain inside financial debt pending their capex/D&A treatment. Credit
cash, operating cash reserves and credit-funding debt allocations are explicit
reviewed assumptions, never reported surplus or non-credit balances. Allocations
must be nonnegative; cash allocations together cannot exceed the disclosed cash
pool, and credit-funding debt cannot exceed total debt less operating leases.
Their pool comparators and rationales are part of the same dated review. The net
bridge is available cash minus credit cash and operating reserve, minus financial
debt, plus allocated credit-funding debt. This retains owner/analyst forecasts as
assumptions rather than treating source freshness as approval. The numerical
engine and original scenario formulas remain unchanged. Model output is replayed
against its inputs before persistence and by readiness consumers. The owning
upsert retains MELI's timezone-aware calculation timestamp with microseconds;
readiness retains strict ordering against the earlier input verification clock,
without tolerating future inputs to accommodate SQL timestamp truncation. Missing or
changed artifacts, incomplete coverage, unadmitted inputs, absent review or a
replay mismatch block verified promotion. A verified base model still reports
`scenario_acceptance_unverified` and is ineligible for allocation; it does not
certify bear/bull assumptions or probabilities. Draft calculations retain their
separate labeled, non-promoting `.tmp/` path.

This tightens the existing internal refresh contract; it adds no Operations
button, schedule, source-acquisition permission, provider invocation or production
activation. The focused MELI recipe tests exercise real publication, ontology,
binding and resolution authorities; source-package completeness is an explicitly
isolated boundary in those fixtures, with separate inventory checks. Routing,
provenance, model-replay and readiness tests cover the changed contract. Passing
those tests is not a claim that the production source population is complete.


### MELI derived-scenario terminal gate

The MELI builder rejects every derived scenario before workbook save or scenario
snapshot emission unless terminal loan growth equals terminal earnings growth and
the Gordon retention charge equals the equity required to fund that growth.
The base-input recipe and scenario builder share the numerical owner's validator.
Invalid/nonfinite terminal inputs fail; growth and ROE are not automatically
clamped into apparently valid assumptions. Historical generic and thesis delta
sets may therefore block a build even when the base input receipt verifies.
No forecast, scenario probability, or owner thesis is changed by this gate.

This is mathematical validation, not scenario approval. The existing
`scenario_acceptance_unverified` allocation blocker remains. Explicit approved
scenario/prior authority in the reviewed assumptions package is not yet supported;
repository-relative scenario fallback is not promoted to an approved authority.
Disposition: no new operator action, job, service, retry or UI surface. The existing
MELI refresh action now fails closed for inconsistent derived scenarios, preserving
an existing workbook. `tests/test_meli_scenario_consistency.py` verifies the failure
cases and a mathematically coherent synthetic case; emission/parity tests retain
coherent isolated fixtures.

### Explicit lexical-only research snapshot population

`execution/populate_research_snapshots.py --projection-mode lexical_only` is an
explicit, provider-free retrieval projection choice. The default `semantic` mode
still requires an exact vector projection and embedding promotion; missing evidence
never selects lexical-only automatically. This choice concerns retrieval only:
issuer/source acquisition, processing completeness, financial semantic admission,
canonical fact resolution, exact corpus membership, and lexical sealing remain
required under the existing authorities. It does not complete missing facts,
refresh a forecast, or approve scenarios.

Preview with the approved explicit `--db`, `--cutoff-at`, `--recorded-at` and optional
`--issuer-id`. For apply, repeat that scope and `--projection-mode`, pass the returned
`--input-commitment-sha256` and `--plan-commitment-sha256`, and add `--apply` through
the existing managed SQLite bootstrap. Both commitments bind the selected mode;
a mode switch refuses the old pins before writes. Lexical-only requests omit vector
and promotion coordinates and have distinct identities; retained semantic identities
are unchanged. There is exactly one terminal per issuer, K/O, and Analysis Evidence
Scope identity when present. An unscoped request retains its existing coordinate:
`research_snapshot_terminal_scope_conflict` blocks a second terminal, including a
mode switch. This operation does not supersede an existing terminal or alter that
scope. Replay verifies the existing seal through the public source verifier;
terminal verification infers the persisted mode, reassembles that exact request, and
performs the same lineage checks. No schema, store, service, schedule, provider, or
new dashboard action is introduced. A preview is an assembly plan, not a successful
sealed-source verification receipt.

### Analysis Evidence Scope planning and use

`execution/plan_analysis_evidence_scope.py --db <approved-database> --request <request.json>
--scope-receipt <receipt.json>` is a manual, read-only selection operation. Run it
through the managed SQLite bootstrap. The request is an `AnalysisScopeRequest` with
issuer, purpose, inventory key, sorted required period ends, knowledge cutoff,
observation cutoff, and optional extra accession/reason pairs. Latest-period
inclusion is the default. The command requires a current, complete authoritative
SEC inventory. It writes one immutable output receipt with no replacement of its
request, database, or an existing conflicting artifact. It does not fetch reports
or write database rows. Its result reports `selection_only_not_model_ready` and
research-document, package-dependency, and outside-scope counts.

The receipt retains every expected document in the selected inventory. Primary
filings are research documents. All other selected package members are required
capture dependencies. Outside documents retain explicit reasons and visible
coverage gaps. Selected periodic packages include known amendments. Missing or
ambiguous required periods and unavailable current-report amendment linkage fail;
selection never invents reporting dates or relationships.

The optional `historical_reconstruction` request selects reviewed original 6-K
financial subjects under a separate public cutoff. It requires sorted exact
authority observation/hash references and original document-version selections.
Latest-period selection and extra accessions are disabled in this mode. Physical
knowledge and observation clocks retain their actual values. Stored SEC metadata
must replay acceptance times; issuer identity must also be visible at the
observation cutoff. Missing clocks, changed originals and unstable metadata fail.
This remains a source selection operation. Financial Research Snapshots return
`historical_research_fact_source_closure_unavailable` until selected reported and
derived facts prove the same historical source boundary. No scheduler, service or
interactive Operations control changes.

Use the receipt through the existing manual interfaces:

- `execution/populate_document_processing.py --analysis-scope <receipt.json>` accepts
  it for a dry-run. Apply uses the scope embedded in the admitted request through
  `--admission-receipt`; passing a separate `--analysis-scope` to apply is rejected.
- `execution/build_grounded_search_corpus.py --analysis-scope <receipt.json>` uses
  it instead of `--inventory` or `--coverage-inventory-key`. All selected package
  members must be captured. Only primary research documents enter the corpus.
  The scoped corpus key must equal the receipt's `scope_id`
  (`analysis-scope:<sha256>`).
- `execution/populate_research_snapshots.py --analysis-scope <receipt.json>` uses
  it for planning and committed apply through the existing input/plan commitment
  checks. Processing, corpus, and research primary document sets must match exactly.

These operations retain their explicit database, clock, locking, immutable receipt,
and apply requirements. New operations require the current inventory. Internal
verification of a stored artifact can reconstruct the inventory visible at its
original scope observation cutoff. No CLI permits historical verification to
authorize a stale write. Research terminal identity includes the scope identity;
distinct purposes do not replace each other's artifacts. Scoped artifacts do not
count as full-population readiness.

This is an internal manual CLI capability. The existing `OperationsRegistry`,
`OperationsSnapshot`, and Operations workspace controls remain unchanged. There is
no new dashboard button, background job, provider activation, schema, financial
admission, valuation update, or production-state change. Selection and successful
capture alone do not establish model readiness. The focused analysis-scope tests
own selection, retained reconstruction, missing dependency, processing-lane, and
corpus consistency checks.
The same command accepts `--source-publication-reference-clock publication_created_v2`
for current research that includes earlier publications.
This mode uses the verified publication creation clock; it preserves the actual
recorded/sealed clock and all publication graph checks. The default `cutoff_v1`
preserves legacy identities and commitments. Repeat the exact clock mode in
dry-run and apply; V2 has a distinct snapshot identity and bound commitments.
Changing the clock mode does not replace a retained terminal or bypass
`research_snapshot_terminal_scope_conflict`. Retained-header verification
reconstructs the persisted clock mode. This adds an option to the existing
operation, with no new database, service, schedule or write authority.

### Bounded accession capture

For 10-K, 10-K/A, 10-Q, 10-Q/A, 20-F, 20-F/A, 40-F and 40-F/A, the SEC
submissions `reportDate` is the source-declared reporting-period end. The inventory
parser accepts it only as a real `YYYY-MM-DD` calendar date. The expected primary
filing and financial-report package children carry that date at UTC midnight as
`period_end`. SEC submissions
does not establish `period_start`, which remains unknown. A blank `reportDate`
remains unknown and cannot qualify a consumer requiring a dated filing. Filing
date and acceptance time never substitute for the reporting period. Other forms,
including 6-K and 6-K/A event report dates, remain outside this qualified mapping.

The expected-document payload, including `period_end`, is committed in the
immutable inventory snapshot. A corrected mapping requires a new inventory
revision and a new capture checkpoint scope; retained SEC responses keep their
original acquisition identity and collector version. Already captured identical
filing bytes with conflicting period metadata fail native replay and require a
separately governed immutable correction, not an in-place date update. This
collector has no executable same-byte native period-metadata correction route;
an affected capture remains blocked pending that correction. This mapping adds no
automatic capture, operator control or valuation admission.

`execution/capture_expected_sec_documents.py --accession-number <SEC-accession>`
selects exact dashed accession numbers within the supplied current, completely
sealed authoritative SEC inventory keys. Repeat the flag for at most 250 unique
accessions. Omission retains the existing inventory-wide ordering and batch limit.
Unknown accessions or accessions crossing multiple supplied inventory/issuer
identities are errors before network, checkpoint writes or apply receipts.
Already captured accessions remain valid selectors and can yield zero new work.

Use the same task id and selector for dry-run/apply/resume. New checkpoint scope
hashes bind inventory keys, current snapshot IDs and the accession selection;
a changed scope requires a new task id. Existing unfiltered checkpoints retain
exact-byte replay, but cannot be repurposed as filtered checkpoints. Raw response
storage and hash validation are unchanged. Apply receipts bind the actual selected
expected-document IDs, not the entire inventory. Validated selections pin a
checkpoint even when no documents remain; selected-mode logical request identity
also includes the scope hash so empty accession requests cannot collapse together.

Results name `selection_scope` and `accession_numbers`. `has_more` describes
pending capturable documents within that scope beyond this batch;
`pending_outside_selection` separately counts currently pending capturable
expectations outside the selected accessions in the supplied inventories. These
are selection progress fields, not whole-inventory or extraction completeness.
Authority-omitted locators retain their existing coverage dispositions; this
selector does not manufacture locators, source coverage, or canonical admission.

### SEC inventory duty scope and retained accession dispositions

`sync_sec_filing_inventory.py` retains the entire parsed SEC submissions population,
including administrative, ownership and registration filings. Existing
`governed-reporting-package-scope@6` selects periodic/current-report roots, the
exact `10-12B` and `10-12B/A` registration financial roots, and their package
attachments for expected-document duty binding. Registration forms retain their
actual form and remain separate from periodic-report dates and latest financial
anchors. Their `issuer_financial_statements` duty requires `sec_edgar` authority
and `regulator_inventory` completeness. An issuer-publisher statement duty cannot
satisfy that SEC obligation. The shared `SEC_REGISTRATION_FINANCIAL_FORMS` constant
owns the exact form set. Document-processing policy @2 and document-processing selection v2 use the
same registration financial family and required native lane. Research-snapshot
selection retains its existing `research-snapshot-terminal-at-k-observed-through-o.v1`
policy; its family and duty resolution now includes the exact registration forms. A governed root with
no primary locator remains `authority_unavailable`; it is not filtered away.
Unclassified forms remain visible and add a failed required scope-validation
component, so their inventory cannot acquire a complete seal.

The exact `SEC STAFF ACTION` form is administrative and remains inventory-only.
Its accession and source evidence remain in the derived scope manifest. The policy
version changes; the collector and package checkpoint configuration do not. A new
revision can seal that reviewed disposition while preserving an earlier partial
revision. Unknown staff forms still fail the scope check.

Apply preserves a hash-bound `sec_inventory_duty_scope.v1` JSON manifest as an
existing evidence blob and `sec_inventory_scope_derived` source observation,
linked by a required `other` inventory component. It contains every accession and
its typed policy disposition, canonical and SEC issuer identities, parser issues,
required component names, and the actual parent source observation IDs/hashes.
This is a software-derived routing receipt, not a SEC HTTP response, document
version, reported financial fact, or extraction/admission receipt. Reconstructing
it compares the complete parser population and ledger-bound parent hashes.
Dry-run and apply both count the required derived component and any failed scope
validation component; only apply returns its persisted observation ID and digest.

Manifest content excludes wall-clock generation time. Exact parent observations,
policy and parsed inputs reuse the original derived observation and its first
local capture clocks; those clocks never replace the referenced SEC acquisition
clocks. A new SEC capture remains a new input even if bytes match. The existing
immutable snapshot/revision contract is unchanged: reusing a revision for a
changed capture fails closed and requires an explicit next revision. Previously
captured raw evidence and failed execution receipts are retained after a routing
failure; no successful snapshot or revision is inferred from those raw captures.
No source duty taxonomy, schema, provider, schedule or financial readiness rule
changes. Mixed-inventory tests use the actual migrated ledger and duty bindings;
network responses alone are synthetic fixtures.

### Windows job-owner liveness

The shared `src/runtime/job_runtime.py` process probe uses a zero-time wait on a
Windows process handle. A signaled handle proves that the process has terminated,
even if another handle keeps its kernel object available. A failed or unavailable
probe retains ownership. A positive creation-identity mismatch still proves that
the recorded owner is no longer the current process. Creation-time calls use
explicit pointer-sized Windows argument and result types.

Disposition: **no surface change**. The existing `OperationsRegistry`, Jobs
projection, job identities, health-receipt format, lock tokens, write sets and
Scheduler cadence remain unchanged. This corrects the existing stale-lock and
scheduler-owner checks; it adds no retry action, service, scheduled job or
operator control. `tests/test_job_runtime.py` covers signaled, running, unknown
and identity-mismatch decisions. `tests/test_job_runtime_process_tree.py` retains
the Windows held-handle and process-tree checks. Local tests do not establish
deployment or a successful future scheduled run.

### Manual financial-fact resolution backfill

`execution/backfill_financial_fact_resolutions.py` remains a manual bounded
observation/resolution operation. Dry-run is the default. Apply uses the explicit
configured database, existing transaction boundary, and durable checkpoint.
The unused selection mirror and its convergence scan are retired. Historical
checkpoint fields remain readable. Canonical selection stays with the immutable
ledger and resolved views. No daily writer task or wrapper is added.

Operations surface disposition: **no additional scheduled writer**. A future
scheduled backfill needs its own workload, overlap, source and activation review.

### Investing workflow maintenance and brief inputs

Disposition: **no Operations surface change**. The internal post-earnings CLI
adds an optional paired fiscal-period selector with complete-scope validation.
Its scheduled default remains latest reported active portfolio quarters. The
pre-earnings writer retains its rendered prompt inputs and explicit source and
fiscal-identity gaps in the existing artifact JSON. Grounding remains partial.
Neither change adds an operator control, job identity, writer, schema, provider
purpose, or production schedule.

The existing Mac Monthly prompt architecture refresh automation also checks the
project-owned investing skill for changed source hashes and broken references.
Its bounded review does not run the application, acquire financial data, change
owner policy, or mutate canonical Windows state. The deterministic checker has
no network or database access. Readout, brief and maintenance regression tests
establish local behavior; they do not establish deployment or a future job run.

### SEC ingestion deadline and bounded provenance work

The registered SEC task, schedule, provider and write lane remain unchanged.
The managed supervisor now enforces the checked task XML's finite elapsed-time
limit for scheduled SEC children. A deadline returns 124 and records failed,
incomplete health and operation-journal evidence. It does not replay ingestion.
After child-tree exit, canonical run accounting fails only the exact attributed
SEC attempt under its retained lane and bounded database lock. Unavailable or
ambiguous attribution stays explicit; no broad attempt sweep is permitted.

Ticker-scoped immutable-source reuse and successor-first resolution queries
reduce repeated work without changing fact admission, manifests, temporal
eligibility or completeness. Migration 0050 adds only a nonunique KPI
supersedes lookup index. There is no new operator action or external effect.
Regression and native child-tree checks establish the correction; a genuine
subsequent scheduled run remains required for recurrence verification.

### Approved thesis KPI registrations and calculated review rules

Persisted thesis evaluations through the existing deterministic and quarterly
refresh actions consume
optional `kpi_registry_candidates` from the approved holdings configuration.
It checks exact governed report references and eight admitted, comparable
quarterly observations before inserting a missing user registry row. Existing
owner settings remain unchanged. New rows have no scalar threshold and are not
marked thesis breakers. The thesis evaluator owns numerical thresholds,
adjacent-quarter persistence, TTM calculations and compound review warnings.
Missing definitions, approximate source bands, incomparable populations and
unavailable discrete-quarter inputs remain unresolved. A pending candidate does
not become a zero-valued fact or a passing check. The approved holdings tiers
retain disclosure and actual-source-cadence research watches.

`metric_expression` hard rules and `metric_threshold` soft predicates use the
same admitted calculated-series reader. Stored evaluation evidence retains
source manifests and calculation definitions. Distinct company/market entities
require explicitly matching source populations. TTM monetary sums require typed
standalone-quarter durations; YTD labels cannot substitute for those inputs.
No new scheduler, listener, provider request or external message action is added.
The existing evaluation and attention lifecycle remains the delivery owner.

### SEC CompanyFacts identity and explicit state root

`execution/fetch_sec_xbrl.py` retains authorized evaluation tickers without a static
CIK entry. Ingestion resolves their stored canonical issuer and one verified SEC
authority before HTTP. Conflicts, material dissent and unusable recorded bindings
fail closed. Retained historical pins remain compatibility evidence.

`--project-root` selects the configured product-state root for immutable CompanyFacts
bytes and working caches. `--db` independently selects the database. Operators must
resolve both through approved configuration; neither path can be guessed from the
other. This adds an explicit CLI path selector to the existing native ingestion
interface. It does not add a provider, schedule, fact writer, admission rule or
financial-readiness override.

### Requested decision brief and source continuations

`execution/prepare_decision_brief.py` plans by default. Its `--apply` path coordinates
authorized onboarding, current inventory, bounded package capture, installation
preflight, source-context continuations, prepared valuation and the existing full
evaluation renderer. It holds no database write lock across child processes.
Each child must return its typed terminal receipt; exit zero alone is insufficient.
`--skip-fmp` leaves SEC enabled. Optional LLM stages require `--enable-llm`.
Acquisition-only mode retains transcript and IR collection with `--skip-llm`;
it suppresses extraction/synthesis model work rather than skipping source bytes.
Review and valuation requests must match the selected ticker and canonical issuer.
A failed dependent stage does not fabricate completeness or stop independent
report delivery. The exact returned artifact and readiness receipt remain bound.

`execution/continue_companyfacts_statements.py` accepts source-bound matched or
raw-entry reviews. Dry run verifies identity, exact bytes, locator, source entry,
actual filing context and semantic role. Apply appends immutable observations,
reviewed roles and canonical selections. It never rewrites a legacy proof.
`execution/prepare_cashflow_dcf.py` applies a prepared request only under its exact
SHA. The generic recipe has an explicit supported method/cadence; it is not an
all-sector certification. Neither continuation creates owner-thesis approval.

The prepared generic cash-flow request may include `AnalystCashflowScenarioReview`.
It binds the exact admitted base receipt, source snapshot/member commitment,
base/bear/bull effective inputs, attributed assumptions, probabilities and replayed
outputs. Readiness rechecks the retained request bytes, SHA, size and file time.
The source-data cutoff, actual request capture and actual calculation are separate
clocks; a freshly written request is not rejected merely because its mtime follows
the data cutoff. Future capture metadata, quotes and review clocks fail closed.
`load_valuation_readiness(..., purpose="analyst_memo")` consumes this evidence for
the registered generic recipe only. The default `allocation` purpose retains
`scenario_acceptance_unverified`; this review grants no owner approval or allocation
permission. MELI's existing scenario-acceptance boundary remains unchanged.

`execution/ingest_sec_filing_xbrl.py --preflight` reads one configured typed
installation descriptor. Missing, template, unapproved, source-drift and runtime
failures are separate states. Preflight cannot approve a bundle. Protocol v2
uses canonical XBRL measure units and a separately committed unit helper.

`provenance.integrity_audit.verify_filing_xbrl_run` is a read-only retained-run
verifier. Its caller must hold one read transaction and supply an exact retained
extraction-run ID. It rejects missing schema, run, input/disposition seal,
referenced artifact, incomplete populations and bounded row/text overflow before
full replay. It shares the global audit's raw commitment checks and the processor's
QName unit rule. Global audit selection remains unchanged. The receipt distinguishes
`verified_v2` unit semantics from `not_proven_legacy_v1`; it does not approve a
processor, admit financial roles, verify publication membership, establish reader
parity or grant decision-grade. There is no new CLI, schedule, service or writer.
Actual execution still requires the existing canonical route and resource owner.

`execution/verify_decision_brief.py` checks the retained body and reader, exact
claim population, sealed processing and canonical-resolution membership, admitted
financial values, calculation replay, raw-source reconstruction, current source
coverage and valuation readiness. `--inspect-body` returns the actual text
population without database access. `--persist` appends a content-addressed
readiness receipt; a successful render alone cannot receive decision-grade.
These operations add no schedule, public disclosure, trade or publication approval.
Local implementation does not establish installed/live availability.

The same verifier accepts legacy reviews and `memo_context_review.v2`. V2 names
independently verified supporting issuer snapshots and finite selections from a
committed ONON model run. Primary valuation authority, source admission and
reader parity remain required. The composed context source-population pass
shares one source-byte budget. Model and readiness rechecks retain their
existing independent per-call bounds and current-byte checks. There is
no new action, CLI, service, schedule, database migration or write authority.

### Source repair disposition (2026-10-03)

No workspace control, scheduled job, service or listener is added. Existing KPI
repair prepare/dry-run/apply entrypoints retain their exact manifest approval,
current Windows review, sole writer and verified backup boundaries. The prepared
legacy-disposition input is an explicit one-ticker allowlist capped at 25 heads.
Immutable capture and replay receipts expose head/source drift rather than claiming
success. Judge recorder inputs now name the actual model and current purpose
qualification; historical receipts remain readable and cannot grant new apply
authority. Internal financial derivation helpers publish source-bound formula and
operand evidence only through the existing provenance repository. Source capture,
semantic admission and threshold eligibility remain distinct. The migration graph
owns the expected schema head; do not hardcode it in the Operations projection.


### Reviewed foreign-filer financial packages and ONON valuation

`bootstrap_issuer_reporting_registry.py --ticker ONON` narrows the existing
tracked reporting universe. It cannot add an untracked security. The default
remains the full tracked universe. SEC fetching uses the configured contact.

`sync_sec_filing_inventory.py --package-subject-review PATH` consumes a typed
`sec_package_subject_review.v1` review. It verifies exact source bytes, an
unambiguous source selector, issuer/accession identity and the reported fiscal
period. Financial statements, supplements and earnings releases can supply a
financial period. Investor presentations and investor updates cannot. The source
remains native SEC evidence. The authoritative package must contain every reviewed
URL. Dry-run and apply return the same expected-document vector digest and review
digest. Apply retains the review bytes as a required inventory component in the
existing evidence ledger. Processing and valuation verify this component and the
exact captured source hash. A review does not prove archive or extraction coverage.

`publish_reviewed_sec_financial_tables.py --db PATH --request PATH` plans one
closed reviewed population of native 20-F/6-K cells. Apply also requires
`--expected-plan-sha256 SHA` and the database writer lock. Exact reported signs,
units, dates, definitions and source spans survive publication. Split numeric
cells and narrative numeric spans must replay from the qualified HTML extraction.
Source publication leaves canonical semantic admission and archive completeness
explicitly unperformed. Run entrypoints through `execution/sqlite_bootstrap.py`.

`review_onon_input_roles.py --db PATH --request PATH` plans a closed analyst review
of the exact reported recipe population: 45 interim operands or 23 annual
operands. Apply requires `--expected-plan-sha256 SHA`,
the database writer lock and a caller-owned transaction. The review verifies the
sealed ontology and issuer-scoped resolution, source bytes, immutable observation
and locator, binding, mapping, period, unit, basis, dimensions and reported signs.
It appends definition metadata through `MetricOntology`; it does not change source
facts or metric meaning. The review clock must follow the prior snapshot cutoff.
New ontology and research snapshots are required before model use. Exact replay
creates no additional revisions. Analyst review does not grant owner approval.

`refresh_dcf.py --ticker ONON --onon-assumptions-path PATH` selects the explicit
five-year `onon_economic_fcff` recipe. It uses the reviewed reported operands,
expensed cash rent and economic stock compensation. The bridge deducts the
reported other non-lease financial liability aggregate as a conservative allowance;
it does not assert that this aggregate is borrowing debt. Forecasts, FX and the
guidance midpoint remain attributed assumptions. The existing artifact owner and
atomic database/workbook promotion apply. No implicit current seed is permitted.
An optional exact prior input-receipt sidecar and scenario-review artifact retain
the review's immutable input and output commitments. The builder environment
uses `DCF_ONON_MODEL_INPUT_RECEIPT_PATH` and
`DCF_ONON_SCENARIO_ACCEPTANCE_PATH` for these explicit artifacts.
An out-of-range reverse DCF records an unavailable solution, its bracket and the
endpoint values. It does not discard the fair values or the other diagnostics.

Readiness reconstructs the full model and canonical bridge. It also verifies the
input-bound bear/base/bull review. An absent or changed review remains blocked.
The stored quote, source and date must agree with the reviewed market inputs.
A passing analyst review establishes research evidence readiness. It does not
establish owner approval or trade authority. Existing MELI reviews and generic
foreign-filer captures keep their prior gates. No schema, provider, schedule,
listener or Operations UI control is added.

### Exact retained IR source aliases

`execution/capture_issuer_document_inventory.py` accepts the explicit
`issuer_document_inventory_request.v2_alias` read-only route. Each selected
alias binds a legacy document ID, native version, exact blob SHA, persisted
source observation and document link, selected issuer/reporting subject,
quarter/type and aware knowledge/observation cutoffs. The owner verifies actual
local bytes and seals the original registered URL plus exact immutable lineage
in a separate v2 receipt. Missing, ambiguous, superseded or future evidence
fails closed. The existing v1 exact-URL route and staging/publishing callers
remain strict v1. This command performs no acquisition, source identity write,
archive completeness certification or financial admission.

The source alias write preparation for Booking remains a private, separately
reviewed adapter over existing SourceObservation and DocumentObservationLink
owners. It is not a new workspace action or automatic pipeline stage. Native
host, canonical route, current production authority and concrete reviewed
request/receipt gates remain external prerequisites. No scheduler or live
write capability is added by the read-only inventory route.


### Physical source verification for reviewed model consumers

The existing verified builders and persisted valuation readers now require an
explicit `SourceReadContext`. `execution/refresh_dcf.py` and
`execution/valuation_preflight.py` accept `--state-root`; the specialized child
builders receive `DCF_SOURCE_STATE_ROOT`. The SEC capture owner stores blobs
under `<state-root>/data/evidence/blobs`. A code checkout, database path, or
workbook output root grants no source-read authority. The dashboard passes its
explicit state-root context to valuation consumers and child actions.
`execution/run_advisor_memos.py`, `execution/run_socratic_questions.py`, and
`execution/dcf_sheets.py import` accept the same explicit `--state-root`.
Socratic questions and memo synthesis forward that context to their existing
advisor reader. The code/output root and source state root remain separate.
`execution/refresh_dispatch.py --state-root` retains its existing data/output
meaning. Its separate `--source-state-root` is the model byte-read authority.
The dashboard forwards that separate flag only from its explicit source state
root. Direct `refresh_dcf.py --state-root` retains the source-read meaning.

`dcf_model_inputs.v3` records fresh, bounded, stable byte verification for every
source document used by the recipe. Missing, changed, linked, oversized, or
out-of-root source files fail closed. The present-byte verification clock is
separate from the financial knowledge cutoff. A v2 receipt remains readable
but cannot qualify a verified MELI, ONON or operating-cash-flow valuation. Source-byte verification
does not replace source completeness, semantic admission, role review, model
reconstruction, or assumption and scenario acceptance.

Existing allocation and risk-reward consumers exclude unqualified MELI and
ONON valuations from their reward legs. Their other ticker routes and scenario
formulas retain their existing behavior. This is a read-only verification
change. It creates no provider acquisition, scheduler, database writer,
Operations control, or automatic receipt upgrade.

The generic cash-flow input dispatcher, preparation and transaction recheck use
the same source context. `execution/prepare_cashflow_dcf.py --state-root` selects
that authority. `execution/prepare_decision_brief.py` and
`execution/verify_decision_brief.py` accept `--source-state-root`. The three CLIs
also accept the configured `DCF_SOURCE_STATE_ROOT`. Decision-brief preparation
forwards only this explicit source root to its cash-flow child and final reader;
its report root, code root and database location grant no byte-read authority.

`analyst_cashflow_scenarios.v2` commits the complete model-input receipt except
the fresh physical-reader observation clocks in `raw_documents[].verified_at`.
The financial cutoff, source population, file digests, sizes, storage identities,
reader policy and all financial and scenario operands remain committed. Each
replay still performs a new physical read. A retained v1 scenario review cannot
reinterpret a v3 physical receipt; it requires an explicitly prepared v2 review.
This version change grants no owner approval or allocation permission.


### Governed reporting projection from sealed acquisition inventories

`execution/build_grounded_search_corpus.py --coverage-inventory-key <key>` now
requires both existing clock arguments: `--knowledge-cutoff <K>` and
`--recorded-at <O>`. Use the approved explicit `--db`, repeat the inventory key
for a union, and supply the existing corpus key, revision and selector code
version. Preview without `--apply`; apply repeats the same scope through the
existing managed SQLite bootstrap. O must not precede K. The current complete
inventory and its seal must already be observed at O. A future or incomplete
inventory refuses before corpus publication.

The corpus and document-processing population use
`provenance.reporting_document_scope` for lifecycle, coverage and retained SEC
package-subject review at K/O. Positive supporting captures stay in the immutable
acquisition inventory and source-duty bindings; they do not join the reporting
included-document set. Uncaptured or quarantined supporting duty remains a missing
or quarantined corpus membership. A discovery-complete inventory does not prove
capture completeness. Missing lifecycle or an invalid 6-K subject witness remains
visible as an unresolved, quarantined membership and prevents a complete corpus.

The existing JSONL diagnostic names every selection disposition and both clocks.
The builder reconstructs the shared projection from all supplied acquisition
snapshot links and requires exact membership equality. Its existing immutable
selection-config commitment also binds the complete decision population and K/O.
A manually reduced request cannot use those links. Corrected membership requires
a new corpus revision; no retained inventory, expectation, capture, duty, corpus
or seal is rewritten. The explicitly unsafe caller-supplied JSON mode retains
its existing separate contract. There is no new action, flag, service, schedule,
provider or schema; this is a stricter clock requirement and corrected membership
behavior for the existing sealed-inventory corpus command.

The retained-run verifier requires an exact built-in `str` run ID. It rejects string subclasses and non-string values. Typed public aliases expose the existing retained closure and eligibility owners; test fixtures reuse the existing persistence owners through their public aliases. Each alias references the original function object. These exports add no operator command or write authority. The processor measurement fixture export remains in `tests/`.

### Financial narrative grounding audit — no operational surface change

The October 2026 grounding contract and template repairs change instruction
delivery, financial context serialization and missing-data wording. They add no
operator action, route, service, schedule, provider, model pin, budget, schema or
live-state write. Synthetic prompt/template tests and workspace golden comparison
check the changed behavior. The dated [audit](history/financial_narrative_grounding_audit_2026_10.md)
records the remaining runtime gates; these instruction changes do not establish
that all financial narratives are verified before release.

### Report claim binding contract — no operational surface change

The memo verifier now binds analyst prose to its support. A figure in an analyst
claim must match a cited verbatim excerpt of a retained node, an admitted value, a
verified model value or arithmetic that the verifier replays; quoted wording must
occur in a cited excerpt. Claims may also quote the owner's thesis when the review
pins its exact text and hash. Reader blocks exclude citation markers, and an
analyst section can be marked with `data-voice="analyst"`. Earlier claim
serializations are unchanged. This adds no operator action, route, service,
schedule, provider, model pin, budget, schema or live-state write.
`tests/test_memo_analysis_binding.py` and the existing memo suites check it. The
binding is a deterministic release check; it does not certify the meaning of a
paraphrase, so semantic review remains required.

### MELI displayed-statement source populations

`execution/publish_meli_reported_statements.py` is a manual, source-only operation:

```
python execution/publish_meli_reported_statements.py --db <configured-db> \
  --inventory-key <sealed-sec-inventory-key> --accession <accession> \
  --document-version-id <captured-primary-version> --fulltext-run-id <qualified-html-run> \
  --content-root <approved-retained-blob-root> [--apply]
```

Dry-run is the default. Its separate `meli-h1-displayed-statements.v1` recipe closes
exactly 18 members: current/prior H1 Commerce, consolidated, Fintech and Credit
revenues, operating income, D&A and productive-asset expenditures; June 30 gross
loans and current/noncurrent operating leases; and current H1 diluted weighted
average shares. The separate `meli-fy-displayed-statements.v1` recipe closes seven
annual duration members from an explicitly dated 10-K: Commerce, consolidated,
Fintech and Credit revenues, operating income, D&A and productive-asset
expenditures. Exact annual headings, units, business populations and footnotes
are required. Interim and annual recipes have separate extraction identities and
completeness memberships. TTM, net cash, economic allocations, model-role
bindings and assumptions are outside this operation. The existing three-member
NIMAL/liquidity/debt publisher keeps its recipe and immutable identities.

The publisher reuses the qualified native HTML hierarchy/replay and captured
primary-filing authority. It reads displayed rows, headers, subsections and
footnotes only. It does not invoke the held filing-native XBRL processor or use
inline tags/contexts as evidence of taxonomy qualification. Source concepts use a
separate `issuer-reported-statement` namespace with complete displayed
business/product/geography scope in hash-bound definitions and locators.
XBRL dimensions remain empty; no invented axis/member or canonical equivalence is
admitted. A later reviewed mapping must establish any relationship to an XBRL
series. Current/prior H1 and FY flows are durations; gross loans and leases are instants.
Diluted shares remain a count under the explicit share-data exception, never a
million-scaled amount. Source cash-flow parentheses are retained; the explicitly
named expenditure measure uses their positive payment magnitude, with the signed
normalization multiplier and convention committed. No sum is invented.

Every published fact retains raw display tokens, source node locators, table
headers, US GAAP preparation evidence, applicable USD/unit declaration, exact
period and definition commitments. Local header/scope changes, basic-vs-diluted
ambiguity, gross-vs-net substitutions and conflicting consolidated presentations
are rejected. Same-byte replay verifies and reuses the original publication.
Apply atomically appends source observations and all 18 captured/rejected
dispositions; a partial population is visible and does not become complete.
Exit 0 means the named population is captured, 2 means partial/rejected, and 3
means invalid/unavailable prerequisites. None means whole-filing coverage,
canonical admission, valuation readiness or scenario acceptance.

No scheduler, interactive Operations control, provider call or automatic retry
is added. Explicit database/content roots remain mandatory. Reconstruction is
owned by the tracked CLI, typed publisher, shared native-evidence loader and
source-authentic isolated fixtures; the reconstruction manifest lists this CLI
and its focused test. Tests use real migrated publication authorities; they
never create checkout-local production state. The retained full-filing research
rehearsal is separate from canonical acquisition/admission proof. Later model
binding must explicitly preserve the requested shares/NIMAL duration; matching
period-end alone does not decide owner policy.

Delivery dependency: this displayed-source population does not replace the
periodic-filing XBRL disposition seal or satisfy the current full-inventory
corpus/processing contract. Model-scope qualification and restoration of the
held native XBRL lane remain unresolved prerequisites. This operation is not a
route around their applicability, corpus or completeness guards.


### Function lifecycle collector — no operational surface change

The internal `execution/inventory_function_lifecycle.py` command collects
Git-tracked source references and conservative function dispositions. It does
not execute scanned source, delete code, admit a quality score, or change a
supported operator workflow. `quality.lifecycle` remains the module-level
lifecycle owner. The Operations registry, snapshot, and visible panel retain
their current contracts. No job, service, schema, provider, live-state writer,
or operator action is added. `tests/test_function_lifecycle.py` checks source
non-execution and preservation, incomplete-scan refusal, current source identity,
and protected receipt output. The reconstruction manifest registers this
collector under `test_and_verification_suite`; no historical measurement is
registered as current evidence.

### Declared SEC analysis selection

`execution/plan_analysis_evidence_scope.py` requires an explicit database, a typed
request, and an immutable output receipt. It reads a complete authoritative issuer
inventory and selects declared reporting periods plus explicitly justified related
accessions. The receipt retains outside-scope documents, package dependencies,
publication and observation clocks, inventory identity, and a content commitment.
It refuses incomplete inventories, ambiguous periods, missing source identity,
stale or fabricated commitments, and receipt paths that replace the request or
database. The database connection is read-only. Receipt creation uses the existing
artifact writer lock and immutable no-clobber owner.

The CLI returns `selection_only_not_model_ready`. Selection does not establish
archive acquisition, extraction completeness, financial admission, or research
readiness. Existing research and narrative consumers keep their current corpus
selection. This integration adds no collection action, scheduler, dashboard
control, or production operation. Consumer adoption requires a separate current
source-clock and financial acceptance integration.

The owner-approved report profile is selected through the typed request field
`readiness_profile: complete_report_evidence/v1`. Its v3 scope has a distinct
identity; existing requests keep their original identity. Package dependency
coverage remains inspectable and native filing-XBRL processing remains deferred.
Processing receipts expose the profile and `deferred_processing_lanes`. Complete
primary text and exact reported/derived fact-source closure stay mandatory. This
does not change source collection limits, start a provider or authorize production
writes. The report evidence population and reader/model gates determine acceptance.

### MELI reported-input and explicit-vector previews — 2026-10-10

Disposition: **no Operations surface change**. The bounded CLI
`execution/preview_meli_inputs.py` requires an explicit database, source state
root, sealed snapshot identity, fiscal period end, aware cutoff and new output
artifact. Invalid source roots refuse before database access. It opens the
selected database read-only. Exact role, dimensions, period, unit, currency,
basis, issuer and binding checks use the current fact and model-input readers.
Complete selections recheck present raw bytes within the explicit source roots.
Missing or ambiguous inputs emit no usable fact bindings. The deterministic
artifact preserves financial commitments; fresh physical-read witnesses remain
in the invocation result. Exact output replay preserves bytes. A different
existing artifact refuses replacement. The command writes only its new output
artifact and task lock; it creates no financial or readiness rows.

`src/dcf/meli_scenario_preview.py` performs pure calculations from a supplied
verified input receipt and explicit numerical case vectors, probabilities and
quote. It checks the base replay commitment, fixed reported values and bridge,
common forecast horizon, exact probability population and finite outputs.
It does not choose priors or forecasts, reverify current source bytes, or persist
or accept a scenario. Results remain `calculated_unaccepted` or `incomplete`.
Both previews keep `model_ready=False`. Scenario previews also keep
`allocation_eligible=False` and leave forward return unavailable. These commands
add no scheduler, acquisition, role writer, research population consumer or
production execution authority.

Evidence: current migrated synthetic input fixtures, read-only selection,
missing/ambiguous roles, raw-byte refusal, invalid-root refusal before connection,
immutable artifact replay/conflict, and explicit scenario replay and refusal cases.
Source-inventory/research coverage is mocked in these fixtures; this evidence
does not establish native package qualification, production readiness or
financial/scenario acceptance.


### PR 1695 reconciliation — 2026-10-16

Disposition: **no new operator surface**. The combined branch preserves the current
`main` database authority, source-root forwarding, cached host evidence, financial
evidence views and scheduler receipt roots through the extracted route modules.
The migration merge retains both historical revision chains. CompanyFacts receipt
reuse also requires the bounded transitive extractor import closure, active
vendor dependencies, metric ladders and full schema identity. Shared tier ranks
and table-cell locators live with their domain models. SEC ingestion imports
neither IR discovery nor PDF parsing; unrelated adapter changes do not invalidate
its receipts. Missing legacy identities force reprocessing. Relevant source replacement
within the process requires a restart; unrelated UI changes do not invalidate SEC
receipts. Closure discovery is lazy; module import does not scan the source tree.
Unknown loaders fail closed. Report fact chips retain their exact admitted document version; an
ambiguous legacy document identity does not select the latest version.

Discovery commands, build children and card rendering use the selected database
independently of the source state root. The two discovery/card CLIs use the
shared `execution/_lib.py` bootstrap; their obsolete per-file path-mutation
allowlist entries are removed. This changes internal authority forwarding and
bootstrap mechanics only. Existing actions and approval boundaries remain.
Cockpit cached snapshots expire 30 seconds after build reservation. Slow direct
results can return to the requesting caller, but expired results enter neither
the data memo nor the response cache.

The unused `current_fact_selection` mirror writes and convergence scan are
retired. Its historical schema and checkpoint fields remain readable. Canonical
reads still use the established resolved views, so stale preparation rows cannot
change selected facts. The existing manual bounded backfill still serves observation
capture and resolution convergence. No scheduler task is added or activated,
no database is migrated, and no application is deployed by this source merge.


### Sealed native admission and MELI input-preview diagnostics

Migration0058 rejects new rows that would retroactively change a sealed ontology
or issuer-scoped canonical-resolution snapshot. Existing exact replay is retained.
The stable SQLite failure reasons are `sealed_ontology_admission_conflict` and
`sealed_resolution_admission_conflict`. These are terminal validation failures for
that input, not transient retry signals. Preserve the failed input and old seals;
plan a supported new-coordinate admission instead of changing timestamps or seals.
No backfill operation or live migration is activated by the source change.

`preview_meli_inputs.py` keeps its read-only database, explicit roots, no-clobber
artifact and exit-code contracts. Known verification failures return exit3 and
bounded unavailable JSON with the requested snapshot, period, cutoff and whether
candidate inspection was performed/completed. They emit no usable bindings or
invented slot counts. Unexpected faults and output conflicts remain command
failures. Candidate selection and the final model-input verifier share the same
canonical/source reader, but each independently verifies its own bounded pass.
The final verifier requires definition, binding and mapping identity/payload parity
with the snapshot's sealed projection. A later valid revision fails with
`input_snapshot_context_mismatch`; it needs a new snapshot. An absent or different
projected selection fails with `input_outside_canonical_snapshot`. These checks
also apply to other model recipes using the shared verifier.

Disposition: no interactive Operations control, scheduler, service, provider,
publication or model-readiness change. Existing manual CLI and migration owners
remain authoritative. Regression evidence: `tests/test_sealed_snapshot_admission.py`,
`tests/test_meli_preview_batch_reuse.py` and `tests/test_preview_meli_inputs_cli.py`.
Exact-read and model-context regressions are in
`tests/test_canonical_fact_exact_read.py`, `tests/test_meli_input_evidence.py`,
`tests/test_cashflow_input_evidence.py` and `tests/test_memo_model_roundtrip.py`.
### Scoped issuer-document obligation bindings — 2026-10-19

Disposition: **no UI or operator CLI surface change**. The native research
snapshot assembler and verifier share one membership-to-duty resolver. An exact
issuer document selected by a verified analysis scope can bind to its persisted
publisher duty without claiming that its partial publisher inventory is a SEC
inventory. The existing authority, coverage, public-witness, issuer and reporting
entity checks remain required. No source collection, processing waiver, scheduler,
service or publication authority changes. Regression evidence:
`tests/test_research_issuer_obligation_bindings.py`,
`tests/test_population_research_snapshots.py` and `tests/test_research_snapshot.py`.


### Financial report resolution materialization clock — 2026-10-19

Disposition: **no UI or operator CLI surface change**. The shared report-table
reader can retain a later physical canonical-resolution recording clock O while
keeping reported source evidence, financial-role review, definitions and bindings
at knowledge cutoff K. A memo requires O to equal its exact Research Snapshot
recording clock. NU PRE/POST baseline checks retain the exact original cell and Observation
Version while verifying each context's separate sealed resolution revision.
Legacy wire identities omit the optional field. This adapter
adds no source collection, financial admission, review approval, service or
publication authority. Native regression evidence:
`tests/test_report_financial_resolution_clock.py`,
`tests/test_report_canonical_financials.py` and `tests/test_nu_earnings_evidence.py`.


