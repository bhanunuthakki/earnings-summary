# Backend streamlining: implementation checkpoints

Status: **latest bounded parser and dead-adapter checkpoint accepted by independent Sol proxy: 96/100**.
The original structural gate still fails. Final combined scoped checks pass.
Started 2026-10-22; revised 2026-10-23 Pacific. Base: `c978523d`.
Branch: `codex/research-procedure-cleanup`. R1 corrections are at `3065638a`,
R2/R3 at `3980b0ad`, R5 issuer protection at `24e2373a`, R6 at `b7f4b998`,
and R7 at `f1056db4`. The roadmap is at `1a265164`. Separate retention cleanup
is accepted at **95/100** and committed at `274074fb`. These are local commits.
The full roadmap and release gate remain open. The owner selected independent
GPT-6.1 Sol proxy reviews; no further Astra or Opus dependency applies.
The next two backend cuts are integrated at `2bd7aae5` and `86617f00` after a
fresh **95/100** acceptance: quarterly reads 95, spreadsheet cleanup 94.

The private application retains deep pre/post earnings briefs, research/Q&A
and portfolio decisions. The cleanup targets four sources of friction:

- Ingestion: one FMP batch owner and one managed presentation intake path for the
  qualified document family replace competing orchestration.
- Data reads: ordered outcomes and verification reuse remove repeated work while
  retaining source, admission, clock and failure checks.
- UI preparation: Say/Do rendering consumes prepared evidence instead of repeating
  its parsing, context and verdict reads.
- Setup and unused features: shared migration fixtures replace copied SQL schemas;
  the owner-approved standalone sandbox and monthly memo schedule are committed
  locally after 256 passing tests and independent acceptance.

No analytical-depth reduction is approved. Other feature removals need an owner
decision. Whole-branch source remains larger; the exact cost and open gates below
are part of the checkpoint, not an accepted permanent architecture baseline.

## Cuts and remaining boundaries

| Unit | Implemented change and effect | Remaining boundary |
|---|---|---|
| R1 | Two FMP batch procedures become one selection, ordering, dispatch and execution owner. Both real callers use it. Parsing failures no longer count as valid zero-result documents. Remove the duplicate 139-line quarterly test schema. Reject a pending caller transaction before extraction. Report missing segment revenue as a failed prerequisite while retaining counts for independently committed periods. | Per-document commits remain. Supplied-record `refresh_cache` retains its caller-owned transaction. |
| R2 | Calendar-year PDF presentations use managed registration from automatic discovery and explicit URLs. Qualified inputs bypass sidecar/categorizer registration. Remove the broad categorizer subprocess sweep. Move preparation and projection orchestration into the existing pipeline owner; HTTP stays in execution. | Other families, non-calendar presentations and local uploads retain supported routes. Registration, projection readiness, historical consistency and narrative completion remain distinct. |
| R3 | Delete `process_ir_documents_state.py`. Move its three callers to the real CLI with explicit state/database arguments. Remove module/path and `sys.argv` rebinding. | Legacy completion flags and cache existence remain until product-completion migration qualifies. Real governed-chain proof covers the explicit database, state root and context restoration; statistical anchor paths retain a preexisting limitation. |
| R5 | The existing parser carries selected values with source/config identity, cell/header coordinates, labels, scale and dispositions. The numeric dictionary becomes a projection. | Reject identical workbook bytes already registered to another issuer before writes. Existing KPI writer remains; native publication and durable per-cell rejected capture are not qualified. |
| R6 | Both financial consumers use ordered canonical outcomes. Remove scalar bundle rereads and the private batch-reader cache class. Reuse recursive verification within one read snapshot and complete verifier key. | No cross-operation cache or new store. The operation fails before a 4,097th distinct verification key. Reviewed-currency admission is stricter than the old discovery predicate; legacy mapping parity is proved separately. |
| R7 | The existing Say/Do section prepares supported rows, selection, context and verdicts. Remove those dependencies and the duplicate verdict read from rendering. | Current/historical Markdown import remains. Other workspace panels still read the database. |

R4 is not implemented. Its retirement needs a held presentation original and
matching retained brief. Bounded development-cache searches did not establish
that pair; this is not proof of canonical-host absence. No new collection or
generation was started to fill the gap.

## Measured data-read MVP

The task harness loads a coherent original owner/consumer closure from Git and
compares it with the candidate on the same committed synthetic database and
source files. It compares complete JSON, including provenance and degraded states.
It counts underlying recursive verifier bodies. Each side uses six balanced timing
samples, excluding setup and instrumentation. Database state does not change.

| Synthetic operation | SELECTs before → after | Full verification before → after | Complete output |
|---|---:|---:|---|
| WIX discovery, 21 references | 1,660 → 617 (−62.8%) | 105 → 1 | Equal |
| Report, 8 available cells | 649 → 248 (−61.8%) | 40 → 1 | Equal |
| Derived report with ambiguous coordinate | 545 → 172 (−68.4%) | 39 → 2 distinct publications | Equal |
| DCF, 56 complete actual inputs | 1,576 → 1,576 | 1 → 1 | Equal |

Direct consumer bundle rereads are zero. Every full verifier input key is unique
within its operation. All four measured median latencies meet the baseline plus
max(10%, 5 ms) limit. The current paired receipt records DCF at about
51.54 → 51.94 ms; this is parity, not a speedup. The final harness rerun includes
six balanced latency samples per side, counts and complete-output comparison.
Its four cases pass in 10.51 seconds with a direct script invocation. See
`.tmp/reader-batch-comparison-run-round4-direct.log` and `.tmp/reader-batch-comparison-round4.json`.
An earlier nested-pytest invocation exited 3 after an outer collection error;
it is retained as failed invocation evidence, not the checking result.
These cases are separate from regression-suite results. No production or cold-cache
claim is made.

The spreadsheet cost check uses 1,000 data rows, 20 quarter columns and 20 configured
rows. Numeric output is equal. Median parse time is about 76.0 → 78.3 ms across
six balanced samples. A separate traced-Python-heap sample is 1.43 → 1.37 MB;
this is not a general memory or process-RSS claim. Both paths retain read-only,
value-only iteration. The typed path adds source bytes and configured cell records.
It does not claim original XML tokens, formatting, or workbook completeness.

## Prepared-section MVP

The synthetic workspace HTML before and after is byte-identical:
`40ab05f6d7fc78b92d728ba26a6a084a750efc4a48812cad5010d2399b1833bf`.
Repeated Say/Do rendering performs no parser, filter/model, context/cache or verdict
DB work. Protected workspace goldens pass in comparison mode; no regeneration.
Inspect `.tmp/saydo-mvp/after.html`, labelled as a synthetic fixture.

Browser interaction is unverified. The browser tool rejected the local file URL.
No alternate browser route or test server was used to bypass that block.

## Independent review and substantive corrections

The first exact Claude Opus 5.5 subscription review scored the whole candidate
63/100, REVISE. R1 scored 91 and R3-adapter 92; the other scoped units needed
revision. The full first review, its source binding and proof are retained in the
[checkpoint receipt](backend-streamlining-implementation-20261022.json). R1 was
committed separately only after its source/test hashes matched that review.

The second exact subscription review scored the revised candidate **66/100, REVISE**.
It accepted R5-handoff (92) and R7-SayDo (93). Other units required correction or
source evidence. That review remains in the receipt. Its 986 changed-file tests and 153 transitive tests describe that earlier
candidate. They do not prove the third revision. That earlier revision was not accepted; a later frozen checkpoint is accepted below.

The first revision addressed these findings:

- Recover committed-but-unfinalized publication before fresh staging validation.
  Bind retained receipt/intent to the durable seal and exact canonical evidence.
  Resume finalization without download, classification or duplicate insertion.
- Inspect coherent historical registration read-only, once per operation. The
  final revision below separates recorded status from strict recovery and new
  admission; it supersedes the earlier byte-reverification description.
- Preserve valid IR, `MANUAL_DROP` and `USER_INTAKE` foreign aliases, including
  nullable URLs, processed flags and timestamps. Hold whole-file corruption
  byte-for-byte. Repair only an unambiguous invalid/missing entry in a valid index.
- Report legacy per-file failures and post-commit lock contention truthfully.
  Restore the runtime issuer registry after categorization. Keep usable partial
  work but fail the overall CLI/discovery attempt when required work failed.
- Wrap stored primitive decode failures at their owning boundaries. Keep corrupt
  cells local; retain independent good references. Database and programmer
  failures still propagate. Reviewed-role admission failures remain typed.
- Verify the three production strict-reader callers omit the observation clock O.
  Real materialization tests cover omitted O, O=K and O>K with source admission at K.
- Compare the real spreadsheet writer's numeric and typed paths on migrated
  fixtures. Basis, period, status, dimensions and publication lane match; only
  exact raw-label enrichment differs when the source label differs. Unsupported
  cell types are explicit skips, not a whole-workbook parse failure.
- Restore Say/Do cache reuse in ordinary forced builds and valid-subset handling
  for non-strict mixed responses. Strict dirty refresh still rejects invalid IDs
  without overwriting the cache. Two forced builds make one filter call.

Source checks also resolve two unverified review concerns without new code:
production producers build fresh ReportSpecs; historical routes serve stored HTML,
not deserialized old ReportSpecs. The prior P3 reader already mapped missing verdict
tables to unavailable and successful empty reads to empty. That behavior remains.

The owner later selected independent GPT-6.1 Sol review as the proxy. Its first
implementation review scored 86, REVISE. It accepted five scoped units at 92–95,
but found that outer financial readers could roll back a replacement transaction.
Those accepted units are committed locally. The fourth revision addresses that
remaining blocker. A fresh independent Sol proxy review accepted the frozen
checkpoint at **92 overall**: R1 96, R2 91, R3-adapter 95, R5-handoff 93,
R6 94 and R7-SayDo 95. There were no blockers or required changes. The review
inspected the supplied source and test evidence; it did not rerun the runtime.
The receipt preserves all earlier failed reviews.

The final frozen revision adds these corrections and proofs:

- R1 rejects an active caller transaction before extractor or accounting work.
  Segment extraction distinguishes absent revenue from a reported zero. A missing
  prerequisite fails the document with its period coordinates. Reconciled periods
  can remain committed; the failure receipt and both callers include only their
  actual inserted counts. A retry can report zero new rows while still failed.
  Valid empty documents remain successful zero-result extractions. The unchanged
  run/SEC/later-writer commit boundaries do not provide whole-pipeline transaction
  isolation. See `.tmp/streamlining-final-unchanged-dependency-closure.md`.
- R2 carries a classification bound to exact bytes, size, issuer, calendar and URL
  into the legacy route. That route does not classify those same bytes again.
  Transport skips remain separate from registration failure. Previously registered
  pending work can run after a new publication fails, while the overall attempt
  still reports failure. Recovery checks all attempts for the same document: a
  coherent older attempt cannot hide a later unfinished finalization. Historical
  checks read small completion metadata, current rows and retained-file status.
  They do not hash PDFs, compare current code or renew admission; the outcome is
  `registration_not_reverified`. Repair uses strict original-binding recovery
  and can fail after code-root drift. Legacy staging copies remain because the
  legacy path callback has no deletion authority.
- R3 exercises the real summary generator, model-pin resolver, budget alert and
  usage ledger with explicit synthetic databases. The explicit state gets cache
  and index writes. A conflicting decoy database and global paths remain unchanged;
  context restores after success and budget denial. Only provider transport/setup
  is stubbed. This does not qualify the preexisting statistical anchor branch that
  derives a checkout database path. See the hash-bound R3 source appendix.
- R5 rejects a globally unique workbook SHA already registered to another issuer,
  before document or fact writes. Same-issuer retry suppression remains. Both the
  numeric and typed paths cover the guard. The earlier accepted handoff is retained;
  the guard is now committed after scoped review.
- R6 retains the seven-field definition comparison and proves complete JSON parity
  for a real legacy mapping without `source_currency`. Reviewed financial mappings
  still require matching committed currency; that tightens the old discovery
  predicate and follows the existing currency contract. Stored period/reference
  corruption stays local to inspectable evidence but fails calculated output closed,
  including corrupt history outside the growth window. The operation cache has
  4,096 keys, no eviction, and immutable failure metadata instead of retained
  exception objects. See `.tmp/r6-round3-source-closure.md`.


The fourth revision removes ten duplicate snapshot owners across financial history,
report tables, consumer series, DCF inputs, thesis metrics, soft rules, cockpit
snapshots/materialization and condition reads/baselines. They reuse the existing
`publication_verification_scope`; no new guard or cache is added. The materializer
still rejects a caller transaction and writes its cache only after a valid complete
read. Replacement transactions and pending writes survive rejected snapshots;
owned normal reads close, caller transactions remain, and cancellation restores
ownership and row factories. Raw proof records 4 original and 16 expanded
fail-before cases, then 47 expanded focused passes with 15 cancellation cases.
The final raw changed-file gate covers all current tests.

One test expectation changes intentionally: the public verifier now lets invalid
O<K arguments raise `ValueError`, while typed stored-ledger corruption remains a
quarantine error. The old wrapper converted generic `ValueError` into malformed
ledger errors. Earlier-K and cross-connection checks remain, and a valid read after
the invalid call proves no cache poison. The nine-suite run had 401 passes and one
old-policy expectation failure; the correction then passed its 91-test group and
final targeted check. These are not a fabricated single 402-test pass. Exact
source and proof bindings are in `.tmp/r6-round4-source-closure.md` and
`.tmp/streamlining-data-source-closure-round4.md`.

The final `make check-fast` passes **1,147 tests across 37 changed test files in
175.59 seconds**. Format and lint pass across 87 retained Python files. Pyright
reports zero errors, warnings or information messages for those files. The gate
also passes architecture-boundary and configured-ratchet checks. Its raw output is
`.tmp/streamlining-check-fast-round4.log`. A configured ratchet pass does not accept
the proposed structural baseline described below.

The whole-tree static check reports the exact current counts: **1,826 Pyright
diagnostics and 1,811 suppressions across 2,876 retained files**. See
`.tmp/streamlining-static-ratchet-round4.log`. The old 2,012/1,835 values were ceilings,
not a paired before measurement. These counts do not prove that the change removed
186 diagnostics or 24 suppressions. Existing whole-tree debt remains.

Design sync, reconstruction inventory, directive references and folder checks pass.
Their raw outputs are `.tmp/streamlining-design-round3.log`,
`.tmp/streamlining-reconstruction-round3.log`, `.tmp/streamlining-directives-round3.log`
and `.tmp/streamlining-folders-round3.log`.

Focused proof is separate and overlaps the changed-file gate:

| Proof | Result and exact reference |
|---|---|
| R1 | Transaction entry: 54 passed in 5.76s; `.tmp/streamlining-r1-transaction-round3.log`. Later worker tool output reports 92 passed in 8.16s and the added five-case zero/missing matrix in 4.33s; no raw logs were saved for those runs. Final changed-file gate includes these tests. |
| R2 | Worker tool output reports 175 passed in 56.79s; no raw pytest log was saved. Tests in `tests/test_ir_presentation_intake.py` also run in the final raw changed-file gate. |
| R3 real chain | One proof passed in 3.83s; combined state/repair/ledger suites: 21 passed in 3.93s. `.tmp/streamlining-r3-real-chain-source-round3.md` binds the source and labeled tool-output transcriptions. |
| R5 issuer guard | Worker tool output reported two fail-before cases in 3.47s, then 17 passed in 4.37s. No raw log was retained. `test_identical_workbook_bytes_cannot_reuse_another_issuer_document` is also in the final raw changed-file gate. |
| R6 affected readers | 265 passed in 67.01s; `.tmp/r6-round3-regression.log`. Eight focused cases and the legacy full-JSON pair are in `.tmp/r6-round3-after-and-legacy.log`; the pair receipt is `.tmp/r6-round3-legacy-pair.json`. |
| R7 prepared section | Retained 212-test result includes protected golden comparison. The accepted R7 source is unchanged. Browser interaction remains blocked. |

These groups must not be summed. A fresh third-revision transitive consumer run
passes 153 tests in 28.95 seconds; see `.tmp/streamlining-transitive-tests-round3.log`.
This is a bounded consumer check, not a full-suite pass.

## Explicit structural transition cost

This consolidates procedures but increases total source size. Against the actual
structural baseline, noncomment source lines change **562,282 → 563,645 (+1,363,
+0.2424%)**. Modules over 2,000 lines change **24 → 25**. Fetch changes from 856 to
787 noncomment lines. The existing managed publisher changes from 1,725 to 2,459
and crosses 2,000. Executable-module count, dependency cycles and maximum fan-out
stay unchanged. Duplication passes its original baseline. Scanners, rules and the
duplication baseline are unchanged.

The exact comparison with the actual baseline is **FAIL** for those two metrics:
`.tmp/streamlining-architecture-comparison-round4.json`. Candidate measurement is
`.tmp/streamlining-architecture-candidate-round4.json`. The working-tree baseline
refresh is a proposal, not an accepted exception. A passing configured ratchet
must not hide this comparison.

The proposed exception covers only this frozen checkpoint's growth for exact
source handoff, recovery and the existing publisher owner. If accepted, review it
at the next producer-retirement checkpoint or by **2026-10-30 Pacific**, whichever
comes first, with no further growth allowance. The date creates no runtime expiry,
service, automation or data deletion. Do not split helpers to change a size bucket.
Independent review accepted the bounded transition. Owner disposition of the
proposed structural baseline remains open. Original-baseline comparison still fails.

## Further setup cut

R8 builds one operation-local set of hold paths for each existing finalization,
recovery and reviewed-registration operation. Candidate and ancestor membership
replace a scan of every hold for every candidate. Full hold reasons, ordering,
path validation and link/Git safety remain. No new persisted cache or framework.
Production source changes by +3 physical lines; this is repeated-work reduction.
The independent Sol proxy scored it **95 overall and for R8-setup**, ACCEPT.

Focused checks: **52 passed, one Windows-only skip in 1.77 seconds**. Existing
test/fixture ASTs remain. The new complexity cases fail on the exact prior source.
A synthetic path-only benchmark classifies 40,000 paths against 7,000 holds in
0.740 seconds, plus 0.019 seconds to build the set. Thirty paired paths match
exactly across three balanced rounds. The full prior 40,000-path operation was
not timed. No full-suite or end-to-end speedup is claimed. Exact proof is retained
under `.tmp/retention-proof/`, with original hashes in `relocation.json`.

The separate metrics IO fixture cut removes a duplicate 97-line SQL schema.
It uses the existing cached real migration fixture and explicitly marks historical
valuation cases. All 100 original assertions remain, with six added. The final
source passes **33 tests in 19.17 seconds** and the fresh Sol proxy scores it
**94/100**, ACCEPT with no blockers. It is integrated locally at `cb7cb140`. This removes 17 net
physical test lines. It does not establish current-head valuation publication or
a suite speedup. Exact source and evidence bindings are under `.tmp/fixture-proof/`.

The next setup cut removes the repeated FMP-derived KPI schema and segment
table builders. All 55 original assertions remain, with three helper checks added.
It passes **17 tests in 3.84 seconds** and the fresh Sol proxy scores it **94/100**,
ACCEPT. Local commit `127470e6` removes **87 net physical test lines**. Evidence
is under `.tmp/fmp-fixture-proof/`. Neither setup cut claims a runtime speedup.

The first terminal financial-series cut is locally committed at `8027659a`. Its first
Sol proxy scored **88, REVISE** and found a missing duplicate-observation disposition
check. The corrected version scored **93, ACCEPT**, with no blockers. It passes
**375 affected and strict-owner tests in 103.92 seconds**. Full result, citation,
exception and provenance-handoff comparison passes **51 paired cases in 19.96
seconds**. That checkpoint still makes two ordered reads per consumer. The next
qualified cut below addresses quarterly composition. The separate MELI instrumentation correction
keeps exact one/two full-verification thresholds and catches a cache-bypass fault;
it measures work rather than cache lookups. Its corrected five-case file passes.

The proposed spreadsheet capture extension uses the existing extraction-output
artifact and blob owners, with no schema migration. Its first review is **89,
REVISE**. Exact parser/extractor configuration hashes and the node-array binding
have been revised; the second frozen review remains **89, REVISE**. An executable contract proof now
passes **one case in 6.26 seconds**, including fourteen exact tagged-value byte
oracles, rollback/reopen/retry clocks, committed replay and two actual orphan
artifact deletions through the existing cleanup owner. The fresh Sol capability
review scores this bounded prototype **94, ACCEPT**, with no blockers. A strict
reader and actual parser-population proof pass **one case in 4.09 seconds**:
41 parser records, 29 evidence nodes and 13 negative cases. A separate fresh Sol
review scores that bounded reader prototype **94, ACCEPT**. Its workbook reparse
is a proof oracle; no production one-parse design or efficiency credit is accepted.
No application code changed. This earns no writer-retirement
credit and does not enable live collection or writes.

The earlier combined source at `8027659a` measures **563,630 noncomment lines**,
versus **562,282** originally: **+1,348**. Modules over 2,000 lines remain 25 versus
24. The new series cut removes 20 physical source lines and duplicated read/cache
procedures; it does not make the whole branch smaller. Original architecture
comparison still fails; duplication passes its unchanged baseline. Exact current
measurement for that checkpoint is `.tmp/streamlining-architecture-comparison-checkpoint5.json`.
The earlier 563,645 measurement above describes the first frozen checkpoint.

Current source at `86617f00` measures **563,583 noncomment lines**: **+1,301**
against the original 562,282. The active original baseline is restored; the
unapproved larger proposal is retained only as an ignored historical artifact.
The original structural gate still fails; duplication passes its unchanged baseline.
Current measurement: `.tmp/streamlining-architecture-comparison-checkpoint7.json`.

The complete combined `make check` finished with **21,410 passed, five failed,
101 skipped and two passed subtests** in 1,537.30 seconds. Supervised teardown
completed with no remaining observed task-owned processes. Source and HEAD stayed
unchanged. The failures concern the disclosed reachability HOLD, a verification
counter, helper dispositions, a private path in this checkpoint JSON and the
legacy-reader inventory. The counter/helper corrections passed 89 tests and a
fresh Sol review at 97. The private-path fix passed the public-tree verifier.
The integrated changed-file gate passed 907 tests, one Windows-only skip and
format/lint/strict typing for 78 files. Its configured structural check used the
explicitly unapproved proposal; it does not establish original-baseline acceptance.
Legacy/reachability reconciliation remains separately qualified below. The original
full suite is not reclassified as passing and has not been repeated.

Two owner-approved feature cuts are integrated locally at `613fa773`.
The standalone sandbox cut deletes eight files and its exclusive setup/theme
pipeline: 1,430 net physical lines, including generated CSS, with 560 deleted Python
lines. Shared Explore, analytics and dependency locks remain. Three retained
synthetic renderer outputs match exactly. The monthly memo cut removes one declared
automatic task and its XML/BAT. Manual CLI/dashboard generation and saved history
remain. All other 44 declarations match; the calibration XML changes its description
only. Static checks and **256 tests in 96.11 seconds** pass, including synthetic
browser behavior. Fresh Sol accepts **95 overall**, sandbox 95 and memo cadence 96,
with no blockers. The separate worktree was archived after all commits and 58 exact
proof files (3.39 MB) were preserved in this worktree. The main integration check
matches 22 paths exactly and retains both operations-governance sections.
No installed Windows task was changed; declaration removal alone does not stop it.

A source review corrects an R5 assumption: the active spreadsheet adapter already
uses shared `persist_manifest` for KPI writes. There is no second spreadsheet-specific
financial writer to delete. Keep that owner and the typed capture boundary. Native
financial promotion needs source-reviewed meaning absent from current config and is
a separate feature, not cleanup credit.

The next backend checkpoint removes **46 physical source lines** and two helper
procedures. Shared quarterly preparation reduces source enumeration, canonical
populations and bundle materialization **2 → 1**. Annual canonical admission stays
separate; only its source enumeration is shared. Full publication-verification
work is unchanged. **81 paired cases**, each with two continuity modes, and
**326 regressions** pass. No production latency claim is made.

Spreadsheet config resolution reuses one ordinal result population: tested capture
name resolutions fall **6 → 3**. All configured currencies still validate, including
absent and overwritten rows. A real migrated constraint fault showed failure
accounting committing a pending partial period. Rollback now precedes that commit;
prior complete periods remain and retry inserts only the remaining facts. **24 tests
pass**. Existing document/run helper commits remain; caller-atomic behavior is not
claimed. Fresh independent Sol accepts 95 overall, quarterly reads 95 and ingest 94.

### Historical KPI guard disposition

Root retains the exact pre-0059 `_insert_kpi_fact` duplicate guard introduced at
`c96bab532`. It runs only after no insert, an exact-source miss and proof of the old
logical index. It prevents an incoming document's context from attaching to the
retained older fact. This approves no new SQL or analytical reader. Retire the guard
when pre-0059 recovery support ends. The separate deleted pre-model skip justifies
summary inventory **3 → 2**; the existing historical guard justifies persistence
**5 → 6**. Fresh independent Sol accepts this unit at **98/100**. Exact equality and all other counts
remain. The three existing architecture/persistence/completion suites pass **88
tests**; production code is unchanged.

### Current static and contract checks

At `86617f00`, the static gate passes for 79 changed files. Format, lint and strict
type checks are clean. The retained population is 2,874 files; diagnostic and
suppression totals do not increase. The reachability test corrections have separate
whole-file static checks. The raw checkpoint-8 supervisor record has an inherited
architecture note; its command ran only static targets. That note does not describe
the active baseline. The original structural baseline is restored and fails.

Directive authority, folder structure, reconstruction (11 of 11), design sync and
public-tree validators pass. These checks do not prove live behavior or complete
investment-analysis acceptance.

### Further pruning assessment

The ten-file ingestion closure grows by 927 noncomment lines against `c978523d`.
The four-file Say/Do closure grows by 110. Preparation moved into shared owners,
but total source growth remains. A bounded read-only assessment identifies two
small cuts worth 15 lines; they are proposals and do not solve the structural gate.
A material consolidation remains under investigation. Do not split files or raise
ceilings to count those changes as a reduction.

The NU/MELI review artifacts remain a separate evidence baseline. Read-only review
of the retained MELI result confirms a verified bounded report receipt with
`decision_grade: false`, `joint_mvp_status: not_verified` and independent analyst
acceptance pending. The receipt explicitly retains the native workbook parity
requirement. These reports support preserving analytical depth; they do not qualify
R4 raw-presentation retirement or whole-application semantic acceptance. No artifact,
model, policy or live state was changed by this reconciliation.

### Shared KPI parser and unused SQL adapter

The next bounded checkpoint is accepted by independent Sol at **96/100**:
shared parser **94**, unused adapter removal **98**, no blockers. The two changes
remove **232 physical source lines**, with no new tracked tests.

The curated and broad KPI paths now use one row-to-value parser and validated
manifest constructor. Their two existing construction entrypoints remain. Three
typed construction arguments carry the existing origin, unit and exact-name
contracts. This adds a small interface cost; it removes the second decoding loop.
The cut removes 47 physical lines, including documentation, and 29 executable
lines. **1,700 before/current comparisons** match full manifests, explicit-field
membership, errors and ordered locator/log calls. **136 existing tests pass**.
No prompt, route, source selection, writer or admission rule changes.

The unused `report.metrics_view` adapter loses its only remaining caller: its own
implementation parity test. Its last production caller had already moved to the
canonical reader. Delete 185 source lines, 154 lines of dedicated tests and only
its exact legacy-inventory entry. Current and historical financial-view tests,
canonical admission, report evaluation and source-provenance tests remain.
**77 retained-consumer tests pass**. No runtime speedup is claimed for dead code.

The combined source now measures **563,380 noncomment lines**, versus the original
562,282: **+1,098**. That is 203 fewer than the preceding checkpoint. Large modules
remain 25 versus 24. The original structural gate still fails; duplication passes
its unchanged baseline. These internal changes add no operator action or stored
authority. Final format, lint and strict typing pass for **81 changed files**.
The complete static ceiling checks 2,872 retained files without increased diagnostic
or suppression totals. Final dependency/helper qualification passes **97 tests**,
with five optional benchmark cases skipped. All 202 disposition sites and 86 internal
target files remain byte-identical; only exact global provenance changes. The normal
unchanged scanner reports COMPLETE/PASS, with zero unknown or unresolved production
edges and zero diagnostics. Supervisors report no remaining owned test/static processes.
No full-suite rerun is claimed. See `.tmp/parser-retirement-final/final-checks.json`.

A further bounded dead-code search found no additional justified backend-only
removal. A calibration badge is a ratified feature without a proved replacement;
the KPI catalog has real scratch-seeder callers; DCF authoring and MELI/SEC helpers
have explicit retention boundaries. Further feature cuts need owner choices.

## Gates that remain open

The complete release gate remains **HOLD**. A prior full-suite run reached 100%
test progress but was interrupted in the existing temporary-retention teardown path. Its logs
remain in `.tmp/cleanup-validation.log` and `.tmp/cleanup-teardown-sample.txt`.
It is not a passed full suite. Full NU/MELI/Q&A/portfolio semantic acceptance and
canonical-host verification are not claimed. R5 native writer retirement remains
HOLD; R3 product completion and R4 original-source numeric retirement remain open.

The final operational scan reports **COMPLETE/PASS**, with zero unknown or unresolved
production edges and zero diagnostics. All 188 previously unknown production edges
match reviewed sites. Reconciliation preserves 176 byte-identical source/target
attestations and rechecks 27 focused attestations. It corrects eight line moves,
three adapter targets and one removed shellout. The sandbox retirement removes one
deduplicated test-only edge; an unused import removal shifts three fixture lines.
The scanner, source lists and all other assertions remain. **85 tests pass in
73.70 seconds**. Independent Sol accepts reachability at **97/100**, the historical
reader guard at **98**, and the combined scoped checkpoint at **97**, with no blockers.
The frozen graph is `.tmp/reachability-reconciliation/raw-qualified.json`.
Both intermediate failed runs remain. This is not a whole-repository dead-code proof
or a full-suite rerun.

A separate R4 read-only canonical metadata inventory found held PDFs and retained
rendered presentation counterparts for NU/MELI. Registered raw Markdown was missing
at inspected paths. This improves feasibility evidence but does not qualify old raw
replay or automated numerical coverage. All remote processes ended. No collection,
schema migration, trade, push, merge, deployment or production mutation occurred. Integration with concurrent original-checkout changes is separate.
No test server was launched. Historical consistency is not current requalification
or decision-grade fact admission.

Task-owned source closures, synthetic outputs and review packets remain for exact
checkpoint reproduction. Release them through the existing temporary-artifact owner
after the final receipt is durable and no recovery question remains. Failed-test
proof is retained under that owner's policy. No production database was copied.
