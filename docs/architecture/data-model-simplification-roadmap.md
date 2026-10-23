# Data-model simplification roadmap

Status: **Proposed; implementation remains held.** Opus 5.5 last reviewed the prior
roadmap at 83/100. This replacement has not been graded. The owner requires above
90/100 and no blockers before implementation, then independent review of each key
finished change. No application change from this roadmap has started.

## Goal and architecture decision

Make financial research simpler by removing repeated representations and procedures.
Preserve analytical depth, original evidence, admission, history, and explicit gaps.
Use the existing owners: `SourceFactRepository` publishes source facts,
`FactReadModel` reads their provenance, and `CanonicalFactResolutionEngine` selects
facts and returns `CanonicalFactRead` containing the selection and its bundle.
Consumers retain their distinct presentation and eligibility policies. Add no new
store, cache service, generic framework, or fact-shaped model. Discovery serves
growth, peers, valuation and DCF readiness; reports serve financial/evaluation views.

The next change removes duplicate evidence work in discovery and reports.
It is a useful shared-reader cut, not a claim that the durable data model is already
simple. Later stages remove repeated semantic checks and select a competing write
procedure for consolidation. Historical tables need not disappear for runtime
models and procedures to become simpler. Feature cuts follow this structural work.
Sol implements; Astra judges architecture; root integrates evidence and delivery.
## Next change: use the evidence already selected

In `src/sources/discovery_financials.py::read_financial_history`, replace each
`as_known(cell_id, K)` with `read_as_known((cell_id,), K)[0]`. Use its resolution
and bundle. Remove the second `provenance_bundle` call and the now-unused
`FactReadModel` construction/import. Use the same path for reported and derived
observations. `as_known` already makes this exact call and discards its bundle.

In `src/sources/report_financials.py::_admit_cell`, do the same only when
`resolution_observed_through is None`. Remove the repeated derivation-currentness
check in that branch: the selection owner has already called the identical
`observation_lineage_current(id, K)` and cleared stale selections in that snapshot.
For every explicit observed cutoff O, including O=K, retain the existing resolution
at O, source read at K, and K-based derivation check. Those clocks are distinct.
Reuse one `CanonicalFinancialSeriesReader` per cell for the remaining checks,
created at the current first construction point. Do not share it across cells.

Keep the existing missing/non-resolved/null-selected-ID guards before using a
bundle. After those guards, a missing bundle raises `RuntimeError` for type safety;
the owner normally raises before it can return this incomplete selection. Do not
add a selected-ID comparison, fallback fetch, or new exception interpretation.
Keep candidate SQL, row order, duplicate rows, metric-ID lookup, future-period
checks, role/scope exclusions, ontology lookups, currency checks, and serialization.
Keep existing exception tuples and lookup order. A report cell failure must still
leave other cells usable; outer failures retain the table-wide unavailable result.
No multi-cell batching, changed DISTINCT/order, or eager consumer checks belong here.
DCF already uses the batch owner; its read procedure stays unchanged in this change.

**Why this is ready:** the owner returns the same provenance type through the same
bundle/relations methods. Its batch subclass changes only publication verification.
Existing derived-bundle proof shows object and JSON equality, with empty relations.
The [initialization proof](../quality/reader-initialization-evidence.md) uses an
existing sealed-derived fixture on a fresh connection: stored resolution succeeds
while the four FactPlaneV2-only SQL functions are absent; `read_as_known` then
initializes its own reader and returns derived value 3201 without durable writes.
The engine's early verification closure uses canonical-table reads and Python
hash/JSON helpers, not those functions. DCF already calls it without a prior reader.
There is no remaining architecture blocker to this cut; the independent gate and
implementation checks remain required.

## Proportionate implementation evidence

Use existing builders and owner tests. Do not add a permanent baseline loader,
variant factory, or large failure-injection test module for this mechanical change.
Sol owns one task-only paired comparison of exact pre-edit and candidate source,
with recorded source hashes, cutoffs, and the same committed synthetic population.
Compare serialized public outputs, reference order, reasons, and unknown states.

- Discovery: existing `seed_growth_graph(include_screen_financials=True)`, one WIX
  database, as_of 2026-09-18. It has nine revenue rows and four each of gross profit,
  operating income, and free cash flow. Record which cells reach the changed path.
- Report: existing `seed_table` builder with its supported reported population,
  plus existing `test_report_financial_resolution_clock.py` cases for explicit O.
- Derived: reuse `test_ytd_subtraction_publishes_sealed_quarter_and_keeps_original`.
  Its report probe reaches derived evidence but returns `ambiguous_table_coordinate`.
  Pair that actual outcome and reached-path counts; do not claim an available
  derived report. Keep the existing owner-level bundle equality proof as well.

Trace SELECTs and wrap the real publication verifier using the existing batch-reader
spy pattern. Require one fewer outer bundle fetch per reached substituted cell,
with its attributable query/verification reduction; count constructor work too.
Explicit-O results must match, with no added query/verification work. A later call
must verify again. Do not claim performance for branches the fixtures do not reach.
If cheap, record balanced before/after timings excluding setup as a non-regression
check; fresh connections are not cold OS caches. Make no latency-speedup claim.

Extend existing owner tests with the smallest assertion that prevents duplicate
reads from returning. Preserve real corruption, stale lineage, per-cell failure,
K/O, and snapshot tests. Add a focused regression only for an uncovered changed
boundary; do not populate the suite with impossible-contract variants. Run the
existing discovery/growth/readiness dependents, report/cadence/clock tests, source
repair and batch-reader tests serially, plus applicable `make check-fast`, static,
architecture and reachability checks. Record the exact nodes and results at delivery.
The known full-suite teardown failure remains an unresolved release obligation.
Synthetic reader evidence does not establish end-to-end NU/MELI, allocation, Ask,
advisor, or memo parity. Their depth and source contracts remain protected.

## Following stages: separate changes, not ready to execute

**1. Share semantic checks.** Replace the three copies in discovery, report, and
`src/sources/dcf_statements.py::_read_cell` with two pure operations owned by
`provenance.metric_ontology`: `numeric_definition_binding_matches` over existing
definition/admitted-definition/binding/cell-ID and expected period/unit family;
`definition_matches_source_context` over definition and existing `FactCellV2`.
Preserve lifecycle, bound/cell identity, numeric/period/unit checks, and equality of
lifecycle, definition_text, value_kind, period_kind, unit_family, accounting_basis,
and scope_constraints; then accounting basis, entity, and consolidation scope.
No I/O or catches belong in either function. Report must retain lazy order:
first predicate → DB-backed currency check → source-context predicate. Discovery
retains its early missing/inactive-definition exit before binding queries.
Require field-by-field tests and consumer failure/order tests. DCF parity relies
on `test_dcf_canonical_statements.py` unless a paired DCF comparison is actually run.
Keep `GrowthFactReference`, `FinancialTableCell`, and `DcfStatementCell`: their
calculation, report-coordinate, and model-input roles differ.

Retain the short quarter-span expressions for now: both use
`70 <= (end - start).days <= 105`; semiannual uses the distinct `days + 1` rule.
They encode local cadence acceptance, not competing fact authority. Do not create
a calendar abstraction to remove two expressions. Retain DCF's per-cell ontology
construction in this stage: moving its setup changes query scheduling, whereas the
pure checks require no such move. Neither decision blocks the first cut.

**2. Simplify fixture ownership.** Move only the unchanged `STAMP` and
`seed_growth_graph` from the growth test into the existing `tests/fixtures` pattern.
Inventory imports first; update all consumers together. Preserve asserts, collected
node IDs, and fixture population. Require zero imports from the old test module.
No pytest configuration, assertion hook, new factory, or setup-speed claim is needed.

**3. Select the next write-procedure cut from a bounded compatibility matrix.**
Reuse the [spreadsheet audit](../quality/spreadsheet-publication-parity-audit.md).
Finish only these named rows; each must identify current readers/writers, existing
replacement owner, removable procedure, and PASS/FAIL/RETAIN with source/fixture proof.

| Current representation/procedure | Target or necessary distinction |
|---|---|
| `population_source_facts._source_fact_from_row` legacy financial namespace; discovery/report/series/DCF selectors | Existing ontology/role selection only if it preserves population and immutable IDs. |
| DCF legacy `documents` eligibility join and report document metadata join | Existing evidence owner; keep authority fields separate from URL/display fields. `ExactEvidenceReference` alone is insufficient for DCF eligibility. |
| `refresh_ir_kpis` → `ingest_spreadsheet_kpis` → `persist_manifest` → restatement/resolution writers | Existing `prepare_reviewed_kpi_native_projection` → `populate_source_fact_plane` → `SourceFactRepository`, only after same-input semantic and recovery parity. |
| `financial_facts`/`kpi_facts` observation/resolution bridge | One publication procedure with necessary compatibility projections; preserve history. |
| KPI configuration, issuer definitions, semantic admission, canonical ontology | RETAIN distinct meanings; remove repeated procedures, not their information. |

Compare identity, value, period, unit/currency, scope, locator, definition/admission,
cutoff, supersession, replay, partial failure, transactions, and recovery, including
rejected/unclassified observations. Choose only a procedure with every required
behavior PASS; prioritize core-consumer reach, then least scope and simplest rollback.
Remove its competing implementation when replacing it. Do not force a failed row.
An existing owner may gain a narrow shared operation; the helper need not already
exist. Report/series metadata query reuse is one modest candidate in the existing
provenance-read owner. Qualify null/error/conversion parity and unchanged query
counts; it does not replace DCF authority or reduce durable representations.

No spreadsheet writer currently qualifies. The float-only handoff loses coordinates,
scale and original representation; native semantics and recovery also differ.
If no matrix row qualifies, prepare one bounded handoff feasibility proposal using
existing `SpreadsheetCellRef`/`FactLocator`, replacing the float-only intermediate.
It must preserve values/replay and the winning cell's provenance when canonical-name
merging overwrites a period. Measure coordinate/number-format parsing cost against
current `iter_rows(values_only=True)`; `openpyxl(data_only=True)` does not expose the
original XML lexical token. Do not fabricate it or infer missing semantic admission.
This proposal is not implementation authorization. After the matrix and this one
proposal, stop if no procedure qualifies. Return exact missing capabilities and
feature-usage evidence to the owner for the next priority decision; do not add guards
or repeat open-ended audits. Feature cuts otherwise wait for one matrix-selected
procedure consolidation, not physical table deletion.

## Boundaries and completion

All proposed implementation is local code with synthetic fixtures. No production
writes, collection, schema/data migration, or physical retirement is authorized.
Section 11 of `directives/data_provenance.md` retains its two-quarter/one-annual
observation windows, recovery evidence, and separate final retirement approval.
Code-only rollback is a revert. Each stage needs Astra review, independent Opus 5.5
above 90 with no blockers, and applicable deterministic checks. Report actual
removed work and limits; a score cannot override failed tests or an incomplete gate.
