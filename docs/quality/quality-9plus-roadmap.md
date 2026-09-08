# Evidence-backed roadmap from Muse 6/10 to 9+/10

Commit scoped: `09d35d1a2785ff7e6a218031eb43952781be3a93`

Status: proposed implementation program. No implementation is authorized by this document alone.

## Executive correction

The prior roadmap was a preliminary hygiene wave, not a credible route from 5.5/6 to 9+. It landed useful low-risk fixes, but it did not materially change the dominant structural facts: very large high-churn modules, import cycles, duplicated authorities, data-dependent query amplification, slow test infrastructure, and an unclassified operator/route surface.

A credible pre-Train-0 gross range is **138-206 small, independently revertible PRs over 44-72 weeks** for a solo maintainer. Train 0 must replace this with a bottom-up estimate after it freezes the type-debt inventory, reachable surface, direct-builder conversion set, large-module count target, and SCC cut set. Some type fixes will land inside later module PRs, so Train 0 may reduce the gross total only by identifying exact overlap. The number is never a quota. Work stops when deterministic gates are met; cosmetic changes do not earn score. “Small” means one independently provable and revertible intent, not an arbitrary line-count ceiling.

## Current baseline: verified facts and provisional inventories

* Fresh Muse grade: elegance 6, maintainability 5, runtime efficiency 6, cleanup readiness 6, overall 6.
* Production code (`src/` + `execution/`): 1,291 Python modules and 554,615 LOC.
* Module distribution: 119 files >=1,000 LOC, 26 >=2,000 LOC, 7 >=3,000 LOC; p95 is 1,411 LOC.
* Largest executable modules:
  * `src/provenance/integrity_audit.py`: 6,337 LOC, 77 functions, 59 SQL execution sites.
  * `execution/comments_server.py`: 5,824 LOC, 184 functions, 124 routes in the root file, 89 imports.
  * `src/pipeline/portfolio_panel.py`: 3,602 LOC, 92 functions.
  * `src/pipeline/work_os_shell.py`: 3,308 LOC, primarily one large runtime asset.
  * `src/provenance/issuer_registry_bootstrap.py`: 3,159 LOC.
  * `src/provenance/gc_recovery.py`: 3,125 LOC.
  * `src/ui/conformance_scan.py`: 3,098 LOC.
* Provisional static import topology: 4,725 internal edges and 16 strongly connected components spanning 77 modules; largest SCCs contain 24 and 16 modules. Train 0 must check in/reproduce the generator before this can score.
* Provisional exact AST-body inventory: 140 duplicate groups covering 397 functions. Examples include 11 logging configurators, 12 file hashes, 6 savepoint helpers, 4 Form 10-K locators, duplicated FPI fetch, DCF, ticker-resolution, and population lifecycle families. Train 0 must check in/reproduce the generator before this can score.
* Canonicalization fragmentation: 46 public/private `canonical_json` definitions, 11 `_db_time` definitions, and 9 file-hash definitions. These are candidates for parity-classification, not blind consolidation.
* Full local suite receipt from the prior merged run: 14,043 passed / 62 skipped in 1,046.92 seconds (17m26s). The enforceable changed-file/changed-line Pyright, Ruff, and format ratchets were green; that does **not** mean the active tree is clean. Current direct checks found 2 whole-tree Ruff errors and 61 files needing Ruff formatting; 50 of those 61 are immutable archived migrations and are governed separately rather than reformatted. Strict-Pyright counts vary materially by environment (the repository documents roughly 3,070; a Sol dependency-resolving run found 3,271; the current local venv reported 27,924 over 2,380 files), so Train 0 must reproduce the CI environment and freeze the exact diagnostic inventory before work.
* Static-quality denominator is currently incomplete: configured Pyright roots omit 312 tracked Python files. The tree also contains 253 `# type: ignore` and 335 `# pyright: ignore` directives across 207 files, so a raw zero-error result alone would be gameable.
* Test infrastructure: 1,092 test files; 172 files still contain direct `command.upgrade`; 146 already use `migrated_db`; 550 contain hand-written DDL. A direct migration replay costs 18-56 seconds versus about 13.5 ms to copy a cached template.
* Visual/source census hotspot: one CSS-surface test takes 44.83 seconds; a six-test architecture/UI group takes 76.47 seconds. Profiling one emitter scan observed 6,454 AST parses, 32.45M AST walks, and 517.7M calls.
* Integrity audit has six verified data-proportional query families. The required target is cardinality-independent statement count, not merely a smaller constant.
* Request pooling is partial: 48 top-level renderers accept only `db_path`; pipeline code has 76 static `connect_sqlite` sites across 42 files.
* Operational surface: 375 executable `__main__` entrypoints, 181 Flask endpoints, 46 scheduled tasks, 47 wrappers, 2 managed services, and 29 reconstruction entrypoints.
* A provisional scan found 85 executable scripts totaling 14,291 LOC without a visible path from canonical/runbook directives, cron/root wrappers, reconstruction declarations, or explicit execution references. This is an investigation queue, **not a deletion list**, and must be regenerated by the Train 0 reachability oracle.

Known audit corrections:

* `execution/build_redesigned_dcf.py` exists, is live, and is 2,369 LOC.
* `src/synthesis/theme_synth.py`, `filings.boilerplate_classify`, `filings.cross_sectional_detrend`, `ask.turn_cache`, and `etf_sources.vanguard` are live through edges the original scanner missed.
* `execution/refetch_aggregator.py` does not exist; the relevant file is `execution/refetch_aggregator_transcripts.py`.
* The repository's actual ratchet function currently returns 172 direct-builder files and has a cap of 172; any alternative 171 count is a different heuristic and does not replace the checked test.

## What 9+ means

The score is fixed before implementation. A pass requires **at least 90/100, every hard gate, two consecutive complete CI runs, a fresh Muse score of at least 9, and an independent Sol verdict that the evidence supports at least 9**. Either judge receives the evidence and rubric without a requested verdict. A P0/P1 finding or deterministic failure is `HOLD`, never averaged away.

The deterministic scorer is additive, with the following immutable all-or-nothing point blocks. Each block earns its full points only when its stated final gate passes and zero otherwise. Missing evidence yields `HOLD` rather than zero. There is no partial or discretionary credit. Total points are divided by 10 and reported to one decimal without upward rounding. Judges cannot change the deterministic score; they can only PASS, REVISE, BLOCK, or HOLD the 9+ claim.

| Dimension | Points | Full-credit block |
| -- | -- | -- |
| Elegance: cycles | 8 | No SCC >4 modules and at most 3 SCCs total. |
| Elegance: composition roots | 6 | Fan-out <=25; no composition-root exception; `comments_server` root <=600 LOC; `portfolio_panel` facade <=200 LOC. |
| Elegance: module shape | 6 | <=35 executable modules >1,000 LOC; at most 3 >=3,000 LOC declarative/generated-asset exceptions; no other module >=3,000 LOC. |
| Elegance: cohesive typed facades | 5 | Facades only re-export/register/compose, perform no DB/network/business work, expose fully annotated public functions, and pass the frozen responsibility/cohesion checks. |
| Maintainability: static quality | 8 | Ruff, Ruff format, strict Pyright, and suppression directives are all zero over the frozen active runtime/test/tooling set, with only the capped declarative/generated exceptions. Immutable historical migrations are a separately enumerated hash/syntax/upgrade/downgrade/reconstruction set and are not reformatted. |
| Maintainability: duplication | 6 | =80% reduction in exact duplicate groups, functions, and duplicated LOC; near-miss groups/LOC do not increase. |
| Maintainability: authorities | 5 | One documented typed authority for each canonicalization equivalence class, population lifecycle, FPI acquisition, specialized DCF persistence, and CLI lifecycle. |
| Maintainability: sustainable tests | 3 | Frozen direct-builder exception inventory only, isolated cached fixtures, no repeated full-tree parse per test, and no weakened coverage. |
| Maintainability: enforced ratchets | 3 | Architecture, duplication, typing, lint, format, reachability, and exception ratchets are blocking in `make check` and CI. |
| Efficiency: integrity audit | 10 | Cardinality-independent statement count and representative median runtime >=70% faster under the benchmark contract. |
| Efficiency: request path | 6 | Normal GET uses <=1 application read connection; frozen route cohort improves >=30% median with no >10% paired regression. |
| Efficiency: test/CI | 6 | Frozen full-suite target is met; slowest CI shard median <6m; visual census <=10s; required fast signal <3m. |
| Efficiency: DCF disposition | 3 | If the approved-snapshot baseline exceeds 60 seconds for 20 names or 5% of morning-pipeline wall time, throughput improves >=2x with parent-only writes; otherwise the baseline must satisfy both thresholds and remain within 10% without new concurrency. |
| Cleanup: lifecycle inventory | 8 | 100% lifecycle disposition for CLIs, endpoints, tasks, wrappers, services, reconstruction entries, routes, and registries. |
| Cleanup: reachability oracle | 6 | Zero unresolved production dynamic/subprocess/public-export edges; touched-surface closure enforced before refactor. |
| Cleanup: deletion proof | 5 | Every deletion has static, operational, liveness/receipt, recovery, and reconstruction proof; zero completed one-offs remain active. |
| Cleanup: schema ownership | 3 | Every table/view has a reader, writer, historical-evidence, or recovery owner. |
| Cleanup: reconstructability | 3 | Reconstruction, scheduler/wrapper, directive/folder, route, and public-boundary gates pass at the final hash. |

Passing 90/100 still requires every hard gate below; score cannot compensate for a blocker.

Hard gates:

 1. Full `make check`, reconstruction, scheduler/wrapper, directive/folder, public-boundary, and applicable design/golden gates pass.
 2. Ruff, Ruff format, strict Pyright, and `# type: ignore` / `# pyright: ignore` suppression counts are zero over the frozen active runtime/test/tooling set, except at most three declarative/generated-asset files whose exact diagnostics are frozen, owned, justified, and expiring. Immutable historical migration files live in a disjoint frozen set governed exclusively by hash, syntax, upgrade/downgrade, and reconstruction gates; they are not reformatted for this program. No active file may disappear through a directory exclusion; no new broad exclusion, weakened rule, or weakened test counts as progress.
 3. Historical hashes, receipt semantics, fact provenance, restatement chains, sample ordering, failure classes, idempotency, and no-clobber behavior remain identical unless a separately authorized behavior change exists.
 4. Benchmarks use a fixed runner class and fixtures with at least seven paired repeats after one unscored warmup, reporting median, median absolute deviation, paired bootstrap 95% confidence interval, SQL statements, rows, elapsed time, and peak RSS for cold/warm behavior separately. Train 0 increases repetitions until the predeclared interval-stability rule passes when baseline MAD is high. The confidence interval for improvement must exclude zero and no paired companion benchmark or individual frozen route may regress >10%. Production-shaped performance exits require an approved Windows snapshot; Mac-only results are labeled non-production-shaped and cannot satisfy the integrity or request-path performance exits.
 5. Mac work uses disposable migrated databases. Production-shaped claims use an approved read-only Windows snapshot or Windows evidence; no checkout-default database is created or inspected.
 6. No deletion occurs from zero imports, tests-only reachability, or no telemetry alone.
 7. Any fetch/discovery or network-boundary consolidation must retain deny-by-default SSRF tests, allowlist tests, robots/budget/truncation goldens, timeout/cancellation behavior, and proof that zero request DB connections are held during network work.
 8. The owner must accept the final evidence package after both independent judges pass. Judge scores do not authorize production writes, migrations, scheduler changes, or deletion.
 9. The frozen architecture and duplication ratchet runs inside `make check` and blocks any regression in SCCs, fan-out, module shape, facade size, duplicate/near-miss inventory, or exception caps.
10. Before any refactor or deletion merges, the Train 0 oracle must prove closure for every touched import, registry, subprocess target, route, wrapper, directive, reconstruction entry, and public export. Unknown touched edges are `HOLD`.

Companion benchmarks are fixed here:

* Integrity primary: wall time and SQL count. Companions: findings/receipt parity, rows, RSS, lock duration, and database mutation count.
* Route primary: paired cold latency for the fixed 20-route cohort. Companions: warm latency, query count, connection count, response hash, and external-call hold time.
* Test/CI primary: full-suite and slowest-shard wall time. Companions: collected/passed/skipped counts, worker RSS, fixture isolation, randomized-order result, and architecture-test detection corpus.
* DCF primary: 20-name throughput when consequential. Companions: peak RSS, workbook/formula hashes, receipt/idempotency parity, and parent-only database writes.

Zero score is awarded for instrumentation alone, exception-ledger entries, renames, file moves, file/LOC deletion without ownership or topology improvement, test removal, or splitting a large file while retaining the same giant implementation behind a facade.

Declarative/generated-asset exceptions are capped at three and require an owner, evidence, removal issue, and expiry <=90 days from creation. Renewal requires fresh owner approval and a new evidence record. Zero expired exceptions may remain at grading time; active exceptions count against the cap and appear in the final evidence package.

## Program and PR trains

### Train 0 — Measurement, reachability, and safety contract (10-12 PRs)

 1. Freeze the scoring implementation at the scoped commit: tool versions and hashes, executable-module file set, physical/nonblank/noncomment LOC definitions, fixed counts above 1,000/2,000/3,000 LOC, SCC and fan-out rules, public-facade rules, responsibility/cohesion checks, and benchmark fixture identities. Final total executable noncomment LOC may not exceed baseline except for separately authorized behavior/migration work that is excluded from the cleanup score. Adding small files cannot score unless the frozen module-count, SCC, fan-out, facade, and responsibility checks also improve. An exception is valid only with owner, evidence, removal issue, expiry <=90 days, and a CI ratchet; exceptions cannot raise the hard caps.
 2. Freeze duplicate detection as normalized Python AST bodies with at least 20 AST nodes and 15 physical body lines. Publish baseline exact and near-miss groups, participating functions, and duplicated LOC. Names/comments may normalize; literals, operations, exception flow, SQL, and call targets do not. Renames or comment-only edits that move an exact clone to near-miss remain counted as not reduced.
 3. Pin compatibility evidence: Flask URL/method/endpoint map, integrity serialized summaries/order, public import surfaces, DCF formula/cell receipts, population dry-run/apply receipts, and key report/dashboard goldens. Hash the rubric and evidence checklist so judges receive the pre-registered version.
 4. Add performance receipts: integrity scaling fixtures, actual Alembic invocation and elapsed-time accounting, per-route SQLite connection/query accounting, DCF stage timing, source-analysis RSS/cache-hit rate, and CI setup/test duration. Publish a causal cold/warm breakdown of migration time, test-body time, collection/AST time, fixture I/O, SQLite build time, and shard imbalance. Freeze the source-analysis per-worker RSS baseline and a route cohort consisting of the 20 slowest cold endpoints at baseline, with endpoint IDs, auth state, fixture hash, cold/warm definition, and connection/query/timing method.
 5. Publish the disjoint test taxonomy and Windows snapshot evidence SOP. Before conversions, freeze an initial direct-builder target <=60 plus the exact final exception inventory and count. The snapshot SOP records approving owner, capture date, immutable hash, schema revision, maximum age, read-only mount proof, provenance, and destruction/retention rule. Mac-only measurements remain labeled non-production-shaped.
 6. Implement the operational graph parser and version manifest with regression fixtures for every known scanner miss. It traverses ordinary/relative/local imports, `from pkg import module`, package re-exports, `importlib`, string registries, `getattr`, subprocess/runpy targets, Flask routes, rendered JS, Work OS/panel registries, cron/task manifests, wrappers, services, Make/CI, reconstruction, current canonical/runbook directives, and explicitly registered manual operations.
 7. Run the graph across the full current surface and resolve every parse failure or unknown production edge. Publish raw graph nodes/edges and tool hashes. Unknown edges `HOLD` only the affected train while being resolved, but Train 0 cannot exit with any unknown production edge.
 8. Assign a baseline lifecycle disposition to all 375 executable entrypoints, 181 endpoints, 46 tasks, 47 wrappers, 2 services, 29 reconstruction entrypoints, routes, and dynamic registries. Every disposition requires evidence: `manual-supported` names a canonical/runbook owner and invocation contract; `internal-delegate` has a verified incoming edge; `one-shot-completed` has sealed completion evidence; tombstones name a current consumer and expiry; dormant items name owner, activation condition, and review date. Nothing is unclassified.
 9. Reproduce the CI environment and publish raw whole-tree Pyright, Ruff, Ruff-format, and suppression-directive diagnostics, including every current exclusion and its reason. Freeze three disjoint file sets: typed active runtime/test/tooling files that must reach zero; immutable historical migration files governed by hash/syntax/upgrade/downgrade/reconstruction; and at most three exact generated/declarative exceptions. Produce retained-code type-debt ownership clusters used by Trains 2 and 7; Train 0 `retire` candidates are not typed unless retirement is later declined.
10. Check in or attach the raw generators/receipts for SCCs, fan-out, duplicates, module counts, test timings, migration invocations, and the provisional 85-script queue. Classify every baseline statement as verified, corrected, or rejected.
11. Compute the minimum large-module reductions for the <=35 count cap, the SCC cut set, the exact direct-builder conversion inventory, type-debt clusters, and bottom-up PR/calendar estimates. Freeze the score script, interval-stability/repetition rule, and train ownership matrix; verify the companion mapping above is implemented without changing it.

The disjoint test taxonomy is assigned by this precedence decision table: direct-downgrade; archived-graph; seeded-upgrade; direct-historical; custom-bootstrap; performance-volume; hand-DDL-unit-schema; cached-current-head. The first matching class wins, and the inventory records the evidence for that match.

Exit: all baseline facts reproduce or are corrected; complete production reachability has zero unknown edges; every operational surface has a baseline lifecycle disposition; raw static-quality diagnostics and scoring code exist; rubric/checklist hash is recorded; benchmark causal accounting demonstrates whether later targets are reachable; route cohort, RSS baseline, final direct-builder exception cap, large-module count target, SCC cut set, and bottom-up program estimate are frozen. Reaching the fixed 8m30 target from 1,046.92 seconds requires at least 536.92 seconds of safe savings. If the causal breakdown cannot support that without weakening coverage, the 9+ program is `HOLD` and must be explicitly amended and re-judged before Train 1; the target is never silently relaxed.

Entry gates after Train 0:

* Trains 3-6: every touched retained Python file is strict-Pyright clean and suppression-free before its functional/refactor PR merges.
* Train 3 integrity optimization and Train 5 request optimization: the approved Windows snapshot, fixed fixture/cohort, interval-stability/repetition rule, and companion mapping must exist before the first optimization PR merges. Mac evidence may guide implementation but earns no performance score.
* Train 5: the exact number of files that must cross below 1,000 LOC, SCC cut set, and bottom-up PR budget are frozen. If the budget exceeds the program range, the program is amended and re-judged before Train 5 starts.

### Train 1 — Test and CI feedback loop (18-29 PRs)

1. Classify all 172 direct migration-builder files into the disjoint Train 0 taxonomy. Unclassified builders fail CI.
2. Prove cached-template isolation and direct-vs-template schema parity.
3. Convert pure current-head product cohorts in 5-10-file PRs; retain genuine historical migration tests. Reaching the <=60 intermediate cap from 172 requires at least 12 conversion PRs at this cohort size; the exact final count and additional PRs come from Train 0's frozen inventory.
4. Classify all 550 hand-DDL test files. Create domain-specific fixture testkits for GC, DCF, population CLIs, triggers, LLM ledgers, and schema parity only for semantically equivalent cohorts. Preserve intentionally incomplete-schema tests.
5. Add one immutable, session-memory `SourceAnalysis` per worker; parse each source file once and share the inventory across architecture tests. No persistent cache initially.
6. Preserve one full 22k-file corpus case and one full 3.3k-row rehearsal; move ordinary assertions to stratified smaller fixtures without removing boundary, malformed, duplicate, restated, or hostile cases.
7. Deduplicate the verified SQLite build into one canonical, hash-verified CI helper; rebalance shards only after new durations stabilize. Add an earlier required fast-signal job whose median completion time is <3 minutes without replacing or excluding anything from the complete gate.

Exit:

* Actual migration replays reduced >=70%; no direct full-chain upgrades in ordinary product tests.
* Cached database materialization p95 meets the Train 0 frozen threshold, initially `max(50 ms, 4x the CI-runner immutable-copy baseline)`, including schema-parity hash verification, and every test gets an independent writable copy.
* CSS census <=10s; the measured six-test group <=25s.
* Full local suite <=8m30s; longest CI test shard median <6m; required fast signal median <3m. Infeasibility is `HOLD` and requires explicit amendment plus re-judgment under the Train 0 contract.
* No tests removed, weakened, or made order-dependent.
* Per-worker source-analysis RSS rises no more than 250 MB or 15%, whichever is smaller; every source is parsed at most once per worker and cache hit-rate is reported. An OOM or order-pollution failure rolls back to scoped per-module caching.

### Train 2 — Static-quality foundation and retained-code ratchet (15-25 PRs)

1. Clear Ruff violations and the Ruff-format backlog only in the frozen active set through mechanical, no-behavior PRs. Findings in the immutable migration set are inventoried for provenance but never reformatted.
2. Freeze strict-Pyright diagnostics and suppression directives by subsystem and diagnostic class under the CI-matched environment. Separate true typing defects, missing third-party stubs, dynamic-registration false positives, generated/declarative assets, immutable migration history, and Train 0 `retire` candidates.
3. Fix leaf types and schemas first, then shared data models and high-leverage service/domain modules. A retained module touched by a later train must be strict-Pyright clean and suppression-free before that later refactor merges. Do not invest in a Train 0 `retire` node unless retirement is declined.
4. Replace dynamic/untyped context bags with narrow typed protocols or models only where the existing runtime contract can be pinned. Do not use `# type: ignore`, broad `Any`, weakened rules, or new exclusions.
5. Remove existing exclusions as their owning retained modules are decomposed. Only the frozen immutable migration set and up to three declarative/generated-asset files may remain outside the active typed set.
6. Change both CI and `make check` from no-new diagnostics to exact descending diagnostic and suppression ceilings per subsystem; lower the ceiling in every debt PR. Train 7 makes zero the final ceiling after pruning removes approved retirements.

Exit:

* Ruff and Ruff format pass over the complete frozen active set; immutable historical migrations pass their separate gates without rewriting.
* CI and `make check` enforce exact descending whole-active-set diagnostic and suppression ceilings.
* The typed-active, immutable-migration, and capped-exception sets remain disjoint and complete for all tracked Python files.
* Runtime behavior, serialized schemas, public imports, and tests remain unchanged.

### Train 3 — Integrity audit: scale first, then structure (8-10 PRs)

Performance PRs, each independently attributable:

1. XBRL child reads become ordered streams keyed by extraction run.
2. Source-inventory and search-manifest seals become ordered grouped streams.
3. Cell dimensions and extraction seals become merge-joined streams.
4. Search-projection validation uses fixed membership/row/eligibility streams. Indexes are not part of the behavior-preserving batching PR. A proven index requires a separate explicitly authorized migration with query-plan evidence, snapshot/restore proof, and its own physical database delta.

Structural PRs after output and performance parity:

5. Introduce a typed gate registry.
6. Extract filing/XBRL and evidence gates.
7. Extract Fact V2/cutover gates.
8. Extract search/projection gates.
9. Leave a small import-compatible orchestrator/facade.

Exit:

* No SQL execution occurs inside a data-sized audit loop.
* For each pre-registered family, `Q(n)` is the traced SQL execution count and `Q(1000)-Q(1) <= K`, where `K <= 5` is fixed before implementation. `R(n)` is total rows returned; Train 0 freezes a justified linear bound `R(n) <= a*n+b` per family so one unbounded mega-query cannot game the statement count. The harness also reports elapsed time and RSS for cold and warm runs.
* A CI query-log assertion fails on SQL executed inside a data-sized loop.
* Batching-only PRs preserve logical equivalence: identical findings, counts, samples, ordering, severity, serialized summaries, and receipt hashes excluding timestamps. Query plan, row count, elapsed time, and RSS are reported. Any physical migration/index change is a separate explicitly authorized migration with snapshot/restore proof and an approved physical-delta contract.
* Representative audit runtime >=70% faster; peak RSS <=10% worse.
* The frozen SCC, fan-out, duplicate, facade-size, and module-size metrics improve; merely moving 6,000 lines behind another giant file does not pass.

### Train 4 — Shared authorities and duplicate families (10-13 PRs)

1. Classify canonical JSON/hash/time variants by exact semantics; pin parity vectors including `Z` versus offsets, separators, Unicode, and microseconds. Consolidate only identical classes and never rewrite historical hashes.
2. Centralize streaming file hashing, logging configuration, ticker resolution, savepoint semantics, and Form 10-K location where equivalence is proven.
3. Make `sec_fpi_ingest` the canonical seam; retain `sec_6k_fetch` as a thin compatibility adapter. Preserve UA, density, auth, admission, and error classes.
4. Make generic history discovery a thin wrapper over inventory discovery only if SSRF, robots, budget, ordering, and truncation parity are exact.
5. Expand `population_cli_harness` to own lock, admission, revision/file identity, checkpointing, atomic receipt publication, and rollback; migrate the six CLIs in paired PRs while domain schemas stay explicit.
6. Extract transcript parse/stage/persist and thesis history/rule helpers with idempotency and period-stamping parity.
7. Consolidate shared SOTP persistence/scenario plumbing while retaining explicit archetype engines and DCF goldens.

Exit:

* Exact groups, participating functions, and duplicated LOC each fall >=80% from the frozen baseline, while near-miss clone groups and LOC do not increase. Any increase fails CI; renames/comment-only changes cannot claim reduction.
* Each shared layer owns lifecycle mechanics, not domain meaning; no untyped callback/config bag replaces readable duplication.
* Differential tests prove values, units, source locators, dispositions, checkpoints, retries, and failures are unchanged.

### Train 5 — Request efficiency and composition-root decomposition (55-85 PRs)

1. Finish optional-connection threading in allocation, portfolio, and peeks helpers.
2. Convert composite portfolio/performance/provenance renderers, then remaining read-only panels and stores.
3. Add route-level connection/query gates. External network calls complete before acquiring the request read connection.
4. Extract `comments_server` middleware/runtime, then move routes in cohesive blueprint PRs: Ask/research, portfolio/Work OS, discovery/capture, operations/actions, and views/positioning. Preserve registration and middleware order.
5. Split `portfolio_panel` into performance, synthesis, risk, and live-position modules behind a <=200-line facade.
6. Use Train 0's inventory of all 119 >=1,000-line production files and frozen minimum reduction count. Each target has one reviewed disposition: reduce by deletion/deduplication, split across genuine cohesive boundaries, retain below 3,000 LOC while still meeting the final <=35-file count, or treat as one of at most three time-bounded declarative/generated-asset exceptions. The 55-85 PR range budgets roughly one cohesive large-module/cycle-cut intent per PR; Train 0 must revise and re-judge the program before implementation if the bottom-up cut set is larger.
7. Prioritize issuer bootstrap, GC recovery, conformance scan, Work OS asset assembly, `build_redesigned_dcf` import safety/workbook sections, `peeks`, `ledger_panel`, `llm.cli`, and high-churn population modules. A split earns credit only when SCC, fan-out, cohesion, or authority metrics improve.
8. Remove import SCCs in bounded families: LLM facade/runtime state, eval package roots, Ask/advisor schemas, small DB/alerts/allocation cycles, then provenance/search/pipeline cycles. At 9+, no SCC may exceed four modules and no more than three small, ratcheted SCCs may remain.

Exit:

* Normal GET <=1 application read connection; Operations may retain its separately justified connection.
* No connection held across tracker/network latency.
* On the frozen cohort of the 20 slowest cold routes at baseline, paired per-route results show median cold-time improvement >=30%, and no cohort route regresses >10% in time, query count, or connection count. Cold and warm results are reported separately.
* Flask URL map, endpoint names, teardown/CORS/SSE behavior, monkeypatch seams, and response goldens remain stable.
* SCC, fan-out, <=35 large-module count, exception-cap, and module-shape gates pass, and compatibility facades are stateless.

### Train 6 — Proof-led pruning and operational classification (10-15 PRs)

 1. Re-run the Train 0 reachability oracle and reconcile it against the new tree. Parse errors or unknown production dynamic edges are `HOLD` for the affected pruning cluster.
 2. Assign exactly one lifecycle disposition to every executable and endpoint: scheduled, service, UI-reachable, manual-supported, internal-delegate, dormant-until, one-shot-completed, compatibility-tombstone, or retire. Dormant items require an owner, review reason, activation condition, and <=90-day review date. CI flags expiry; it never auto-deletes. The owner must renew or choose another disposition.
 3. Reuse panel activation counts, managed-job receipts, and the operations journal for bounded no-use evidence; do not collect payloads, parameters, tickers, or user content.
 4. Collapse structurally denied audio/aggregator implementations to minimal compatibility entrypoints that validate input and emit the canonical denial receipt. Remove a dependency only after runtime/import closure proves no other consumer.
 5. Consolidate the duplicate offline-build boundary after parity and one compatibility window.
 6. Retire ticker/cohort-specific one-offs only after the Windows snapshot SOP proves the exact logical result or a sealed receipt, latest-write evidence is captured, the generic capability is retained where useful, and no current runbook/schedule/reconstruction/UI action depends on it.
 7. Clear the 85-script queue by semantic cluster. Every item is registered, folded into a generic CLI, deleted with proof, or made time-bounded dormant. There is no permanent `maybe useful` bucket.
 8. Resolve tests-only product implementations through explicit owner disposition. Confirmed live false positives are excluded.
 9. Prune routes only after static doorway, JS, manual-contract/tombstone, and bounded usage evidence. Remove handler, renderer, JS action, CSS, copy, tests, and Operations disposition atomically.
10. Treat schema/table deletion as a separate, explicitly authorized migration program requiring Windows population/latest-write evidence, approved snapshot, and restore proof. It is not bundled with code cleanup.
11. Build a versioned table/view inventory recording name, schema revision, readers, writers, and historical-evidence or recovery owner. Unowned tables/views block 9+ grading even though deletion remains a separate authorized migration program.

Steps 4 and 9 change observable behavior and therefore require a separately authorized behavior-change record under Hard Gate 3 in addition to the lifecycle/owner disposition.

Exit:

* 100% classified operational surface; zero unresolved dynamic/subprocess edges.
* Zero production nodes reachable only from tests/history unless explicitly time-bounded dormant.
* Zero completed one-offs in the active execution namespace.
* Every table/view has a reader, writer, historical-evidence, or recovery owner.
* Each deletion has a proof bundle, recovery path, and clean full gates.
* Expected secondary outcome: 6,000-12,000 production LOC, 3,000-6,000 obsolete test LOC, and 20-50 active-namespace files removed. These are not pass conditions; correct retention with an owner is a successful disposition.

### Train 7 — Final static-quality zero pass (10-15 PRs)

1. Recompute the typed active set after Train 6 deletions and lifecycle decisions. Any retirement that was declined returns to a retained-code ownership cluster.
2. Clear the remaining strict-Pyright diagnostics and every active `# type: ignore` / `# pyright: ignore` directive by subsystem, lowering both ceilings in every PR.
3. Remove all obsolete Pyright directory/file exclusions. Verify the union of typed active, immutable historical migrations, and capped generated/declarative exceptions equals the complete tracked Python file set.
4. Run the immutable migration hash/syntax/upgrade/downgrade/reconstruction gates separately; do not rewrite history merely to make it type-check.
5. Switch CI and `make check` to final zero-error, zero-suppression enforcement for the typed active set.

Exit:

* Ruff and Ruff format pass over the complete frozen active set; immutable historical migrations pass their separate gates without rewriting.
* Strict Pyright and suppression-directive counts are zero across the complete typed active set.
* No active file is hidden by a directory exclusion; no more than three exact generated/declarative exceptions remain; immutable migration history is fully enumerated and passes its separate gates.
* Full tests, public imports, schemas, receipts, and runtime behavior remain unchanged.

### Train 8 — Final closure and grading (2 closure PRs plus 2 evidence cycles)

1. Run two consecutive complete CI cycles plus reconstruction, cron/wrapper, route, golden, DCF, integrity, and benchmark gates on the identical commit hash. The frozen scoring script must compute at least 90/100. Confirm one Alembic head and enforce a single-writer queue for migration/generated-task work.
2. Run a fresh whole-repo Muse audit at that exact hash with the frozen rubric/checklist hash, raw evidence, and blind-spot list. Independently brief Sol with the repo at that hash, the same frozen rubric/checklist, raw evidence, and blind-spot list, but no preferred verdict. Each judge independently reproduces the metrics it can execute and marks any unavailable proof as `HOLD` rather than accepting curated summaries.
3. Any disagreement, P0/P1, score below 9, or missing live evidence returns the program to the owning train. No averaging or “close enough.” The owner reviews and explicitly accepts the final evidence package after both judges pass.

## Score bridge and stop points

These are forecasts, not earned grades:

| Milestone | Plausible overall band | Required proof before proceeding |
| -- | -- | -- |
| Current | 6.0 | Fresh audit at scoped commit. |
| Trains 0-1 | 6.5-7.0 | Sustainable feedback loop and objective scorecard; instrumentation itself earns no score. |
| Train 2 | 6.9-7.4 | Static-quality ceilings descend and every subsequently touched retained module is clean. |
| Train 3 | 7.5-8.0 | Major measured integrity-runtime win and smaller audit authority. |
| Train 4 | 8.0-8.4 | Duplicate families and authorities consolidated with parity. |
| Train 5 | 8.4-8.8 | Composition roots, cycles, module counts, and request efficiency meet gates. |
| Train 6 | 8.7-9.0 | Complete reachability/lifecycle dispositions and proof-led pruning. |
| Trains 7-8 | 9.0-9.3 | Final static-quality zero plus independent grading at one exact hash. |

If a train fails to move its named metric after two bounded iterations, stop it and re-scope. A 9 claim is not made from forecast bands.

## Parallelism and ownership

After Train 0, safe parallel lanes are:

* Test-harness/CI files.
* Integrity audit (one exclusive writer).
* Shared-authority families with non-overlapping modules.
* Reachability inventory and evidence collection (read-only until classification lands).

Never run overlapping writers on `integrity_audit.py`, `comments_server.py`, a migration head, generated task files, production database state, scheduler state, or the same browser session. Migration heads and generated scheduler artifacts use an explicit single-writer queue with `alembic heads` and generated-diff verification. Rebase each train onto current `main`, keep PRs to one revertible intent, run targeted gates per PR, and run the complete gate at each train boundary.

Ownership roles are assigned before Train 0 exits:

* Program integrator: owns the frozen rubric, dependency order, merge train, score computation, and final evidence assembly.
* Train code owner: one exclusive owner per overlapping file family; owns implementation, targeted proof, rollback, and handoff.
* Quality owner: the repository owner; approves behavior changes, exception creation/renewal, Windows evidence use, and final acceptance.
* Windows evidence operator: an explicitly authorized operator who captures read-only snapshot/live evidence under the SOP; this role does not imply mutation authority.
* Independent judges: Muse and Sol; read and reproduce evidence only, make no code/production changes, and cannot approve exceptions or side effects.

Every PR and evidence receipt names the program integrator, train code owner, applicable quality-owner approval, and rollback owner. “Owning train” means the assigned train code owner plus program integrator; it is not an anonymous queue.

## Explicitly out of scope

* New product features, prompt/citation programs, LLM routing changes, authentication, tenancy, deployment, or UI redesign.
* Rewriting provenance, financial semantics, DCF archetypes, or investment logic.
* Production mutations, scheduler changes, Tailscale changes, database migrations, or live trigger firing without separate authorization.
* Cosmetic renames, four-line deletions, test renames, or broad utility consolidation that lacks measurable risk/cost reduction.
* Touching or committing the user-owned untracked `uv.lock`.

## Principal rollback/hold conditions

* Historical digest or receipt mismatch.
* Changed fact provenance, admission, restatement, unit, currency, fiscal period, or source locator.
* Different integrity findings/order or worse lock/snapshot behavior.
* Golden/UI/API/DCF output drift without a separately approved behavior change.
* Shared mutable test state, randomized-order failure, or missing adversarial coverage.
* New import cycle, unmodeled dynamic edge, route loss, operator breakage, or reconstruction gap.
* Runtime/query regression above 10% on a fixed benchmark.
* Any deletion that requires inference from a Mac checkout database or absent telemetry.

## Owner-approved execution amendment — 2026-09-07

The owner approved the reviewed execution map and directed aggressive use of Codex 5.6 subagents. This amendment replaces coarse whole-issue sequencing with the explicit gates below. Frozen score thresholds, integrity/compatibility boundaries, and final owner evidence acceptance remain unchanged. Completed BHA-118/141/142/146/147 stay closed.

1. Preserve the urgent sequence BHA-143 → BHA-144 → BHA-145. R3 collects the exact BHA-147 subject `6b9f77610f0cb5c6b425204eea510c6bde0f2db2`; R3 and R5 are evidence-only. Missing roadmap, raw unpaired timing and unavailable admissions remain honest HOLD. Static receipts must be freshly collected as bha-120.v3 with raw membership.
2. BHA-104 is authorized for a bounded preflight while BHA-122 remains HOLD: the minimum paired immutable experiment/receipt and offline isolation machinery, followed by optional capture-poller tap test isolation with dedicated behavior coverage retained. A source-analysis experiment is conditional on measurements. Preflight code merges wait until the original R5 evidence bundle is merged/validated. This is not general Train1 authorization, a relaxed 510s target, or automatic feasibility acceptance. BHA-145 then owns a separately identified follow-up freeze bundle for the new executable subject.
3. BHA-144 owns the typed freeze validator, durable hash-bound Linear roadmap/claim mapping, exact candidate/owner/lane/dependency/scope/evidence/covered_by relationships, and admission ownership map. It indexes evidence without becoming another score policy. Calendar remains HOLD without measured lane throughput; historical 156 PR/57 weeks and conversion/LOC counts are not quotas.
4. Once BHA-122 acceptance passes, BHA-104 test infrastructure, BHA-105 static foundations, and BHA-109 function/schema inventories may proceed in independent lanes. BHA-106 integrity, BHA-107 typed authorities and BHA-108 structure/request work can overlap only with disjoint file ownership, fresh touched-surface closure, clean suppression-free touched retained files, and their benchmark/snapshot prerequisites. Deletion/deduplication precedes decomposition of the same code. BHA-110 final zero follows all retained source changes and pruning; BHA-111 closes last.
5. Missing semantic admission predicates are explicit remaining work: BHA-104 test/fixture/benchmark/database contracts; BHA-105 whole-gate enforcement and compatibility contracts; BHA-106 integrity efficiency; BHA-107 duplicate/authority/network and DCF disposition; BHA-108 structural/request proof; BHA-109 lifecycle/deletion/schema/reconstruction proof; BHA-110 static-zero and complete final gate aggregation. BHA-144 defines real owner-acceptance binding; BHA-111 records the eventual decision. No admission PASS comes from a source tuple or successful collection alone. BHA-111 is evidence/review rather than deferred implementation.
6. Root Astra owns integration and Linear transitions. Up to three Codex 5.6 workers use isolated worktrees with exclusive source, test, registry and output ownership. Separate independently briefed reviewers check final PR diffs; resolve every P0–P2 or HOLD. Use targeted serial tests in iteration, bounded full-suite workers, required pre-push/full CI gates and exact-commit reviews. Codex workers replace the default paid coding/per-PR review route for this execution. No new paid calls are necessary for routine work; retain the authorized $2/wave OpenRouter ceiling if such a call becomes necessary within the task.
7. Route all six BHA-147 P3 observations into existing issues: static-v3 recollection to143/145; boolean-locator diagnostics and parity-prefix lifecycle to104; early manifest-path rejection and consistent alias enumeration to144; speculative directory-fd TOCTOU work remains a deferred144 threat-model note. No duplicate issues or standalone cosmetic PRs.
8. Main advanced after planning via unrelated PR1490. For historical evidence, preserve three identities: S is exact executable subject, B is its docs-only evidence commit, M is the ordinary merge integrating B into current main. Validate and score B after it is a retained ancestor of origin/main; never validate M as that bundle if S..M contains unrelated code. Start successor implementation from exact M, record B as evidence predecessor, and retain both. This uses the existing validator ancestry contract without rewriting main, rebasing receipts, or weakening proof.

Concrete owner decisions remain for a named approved Windows evidence source/operator, any measured infeasibility or material budget amendment, meaningful feature removal, exception creation/renewal, and final evidence acceptance. Production mutations, schema deletion and scheduler changes require separate authority. Planning approval does not pre-approve those effects.