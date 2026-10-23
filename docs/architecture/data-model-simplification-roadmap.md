# Data-model simplification roadmap

Status: **HELD — not approved for implementation.** The latest independent
Opus 5.5 verdict is REVISE, 83/100, with no blocking findings but required changes.
The owner's gate requires above 90/100 and no blockers. No structural implementation
from this roadmap has started. The calendar MVP is separately accepted at 91/100.
This document is a reviewable draft, not an execution contract. Its outstanding
requirements are recorded at the end; reconcile them before another review.

## Outcome and scope

Remove duplicate financial-read procedures and use existing shared objects.
Preserve analytical depth, evidence, admission, history, and explicit gaps.
Keep `SourceFactRepository` as publication owner, `FactReadModel` as source reader,
and `CanonicalFactResolutionEngine` as selection owner. Add no store, cache service,
generic reader framework, or new fact-shaped model.

The first deliverable removes repeated reads/checks and simplifies fixture ownership.
It does not remove persisted tables. The bounded compatibility audit below selects
the next durable-model reduction. Feature selection follows this structural work.
Sol implements; Astra judges consequential design; root integrates the evidence.
Current work is assessment only. Implementation is local code and
synthetic tests: no live access, collection, model calls, or deployment.

## First implementation: retain selected evidence instead of reading it twice

**Discovery:** in `sources.discovery_financials.read_financial_history`, replace
per-row `as_known(cell_id, cutoff)` with `read_as_known((cell_id,), cutoff)[0]`.
Retain its resolution and bundle; remove the second provenance_bundle call.
Use one path for reported and derived observations. No per-kind conditional and
no verification-failure fallback are needed; the parity evidence below supports
the shared owner procedure.
Remove the unused discovery FactReadModel construction and import. Its incidental
SQLite-function registration is not a discovery-output contract. The engine creates
its own reader after _stored_as_known and before later per-row consumer work.
Require the populated fresh-connection pair to start without any FactPlaneV2
construction or its four plane-only functions, and demonstrate the first candidate
reaching _stored_as_known successfully before reader initialization. If that
prerequisite fails, hold this removal and name the missing function dependency;
do not accept a caught-error/unavailable result as parity. Pair outputs for empty,
unresolved and populated populations, and run subsequent supported reader calls to
verify they initialize their own requirements. Do not keep a dead object for caution. `as_known` already makes that exact call and discards the bundle.
Keep candidate SQL, row order, duplicates, metric-ID lookup, exclusions, subsequent
lookups, and exception boundaries unchanged. No cross-row batch, deduplication,
changed DISTINCT, or new ordering policy.

Keep both future-period checks, dimension/security-scope skips, missing-evidence
and unresolved dispositions, reviewed roles, combined/carve-out and degraded legacy
scopes, growth formulas, and reference serialization. An earlier uncaught TypeError
must not be masked by processing a later row. Do not broaden the caught tuple.

**Report:** in `sources.report_financials._admit_cell`, make the same single-cell
substitution when `resolution_observed_through is None`, using the same owner
procedure for reported and derived bundles. Keep selection inside the
existing per-cell handler. For every explicit O, including O=K, retain resolution
at O and the separate source read at K. Preserve the subsequent derivation check
only for explicit O. With O absent, read_as_known already runs the identical
observation_lineage_current(id,K) check in the same snapshot and clears failed
selections. Remove that repeated check; pair result/reason/verification counts on
the derived owner fixture and preserve existing consumer checks. Preserve
roles, currency, units, cadence, reasons, and table-wide ambiguity checks.
Create one `CanonicalFinancialSeriesReader` at its current first construction point
and reuse it for currency verification. Move no query earlier: the first
construction remains unconditional at its existing point. The report derivation
check runs only for explicit O. Remove its duplicate at O absent. Count any constructor I/O in measurements.
Never share it across cells or requests.

In discovery, and only in the report O-is-None substitution branch, first apply the existing checks for missing resolution, non-resolved status or
`selected_observation_id is None`. These keep their current unresolved disposition.
Only after those checks, narrow a bundle-None result with RuntimeError for safe
access. Add no selected-ID mismatch guard: the owner indexes bundles by observation
ID and retrieves by that selected ID, so a second comparison proves nothing.
There is no fallback fetch. Add no new guard or reinterpretation to the explicit-O
report branch; it retains the current provenance_bundle-at-K behavior.
This is defensive handling of an impossible owner-contract result: the required
bundle reader raises before returning an incomplete selection. Do not catch an
arbitrary KeyError or reinterpret it as missing evidence.

A report cell exception remains `canonical_evidence_invalid`; other cells survive.
Outer query/loop failures still make the whole table unavailable. Whole-table
batching is excluded. DCF already batches; leave its read procedure unchanged.

## Second implementation: replace the repeated predicates

In `provenance.metric_ontology`, add two pure operations using existing types:

- `numeric_definition_binding_matches(definition, admitted_definition, binding,
  canonical_metric_cell_id, *, expected_period_kind, expected_unit_family) -> bool`.
  Missing objects return false. Preserve active lifecycle, bound status, cell
  identity, numeric kind, expected period/unit family, and comparison of the seven
  existing fields: lifecycle, definition_text, value_kind, period_kind, unit_family,
  accounting_basis, and scope_constraints.
- `definition_matches_source_context(definition, source_cell) -> bool`.
  Missing definition returns false. Preserve accounting basis, reporting entity,
  and consolidation scope; use existing `.get()` behavior for absent scope keys.

Use existing definition/binding types and `FactCellV2` from `fact_plane_v2`.
That module does not import the ontology or fact read model; verify this with the
architecture gate, rather than add a cycle workaround.
The operations perform no I/O, catches, admission, or new validation. Replace the
three copies in discovery, report, and `sources.dcf_statements._read_cell`.
Report keeps lazy short-circuiting: currency runs only if the first predicate
passes; the second predicate runs only if currency passes. Do not precompute all
three booleans. Pin skipped calls and SQLite failures with the spy tests. Discovery
and DCF use first → second. Discovery retains its early missing/inactive-definition
exit before binding queries. Role checks and consumer error handling stay local.

| Representation or rule | Decision |
|---|---|
| GrowthFactReference | Keep calculation/reference serialization; reuse selected inputs internally. |
| FinancialTableCell | Keep report coordinates, reasons, and evidence projection. |
| DcfStatementCell | Keep model-input and primary-source policy projection. |
| Quarter duration and growth formulas | Keep 70 <= (end - start).days <= 105: inclusive bounds, no +1. |
| Series/semiannual duration | Keep 175 <= (end - start).days + 1 <= 200; do not merge distinct cadence contracts. |
| Report derivation-currentness check | Remove its exact duplicate at O absent, with paired proof. Retain the distinct K-based check for every explicit O. |

## Proof tied to existing fixtures

Sol owns focused additions in `tests/test_financial_reader_reuse.py`. Add and run
baseline-compatible cases before production edits. Use existing builders; no new
variant factory or multi-ticker database is required.

**Discovery population:** one isolated migrated WIX database from
`seed_growth_graph(..., include_screen_financials=True)`: nine revenue rows and
four each of gross profit, operating income, and free cash flow (21 rows).
Use as_of=2026-09-18 (the existing STAMP date), after all 21 periods. Record the
number of cells reaching the substituted path; do not assume every candidate did.
Keep the builder unchanged. Use identical persisted population and source files
for baseline/candidate reads. No five-ticker claim or invented nine-period coverage.

**Report population:** existing `test_report_canonical_financials.seed_table` and
its database fixture, with SYNTH revenue and operating income for four quarters
(eight duration rows, USD, cutoff STAMP=2026-07-27T12:00:00Z). Use quarter ends
2025-06-30, 2025-09-30, 2025-12-31 and 2026-03-31 with starts 2025-04-01, 2025-07-01, 2025-10-01 and 2026-01-01 respectively,
and labels Q2/Q3/Q4/Q1. These eight caller-supplied rows are new explicit input to
the existing seed_table builder, not a claim that a current test has that exact
population. Use revenue=100000000 and operating_income=20000000 in each quarter;
each tuple has unit USD.
Record reached cells. Keep that builder unchanged. Actual
K/O fixtures remain in `tests/test_report_financial_resolution_clock.py`.

**Derived owner proof already run:** at 630a74c5, a runtime-only wrapper called
`test_batch_preserves_valid_publications_and_revalidates_each_call`, then compared
the actual observation-independent bundle from scalar provenance_bundle and
required_provenance_bundles at 2026-07-27T12:00:00Z. Full object and JSON equality
passed; derivation-seal-independent was present. Relations matched but were empty.
The exact existing node passed (1 test, 3.38s), without source/test edits or a new
seed factory. Source inspection confirms batch uses the same provenance_bundle
and relations methods; its subclass overrides only publication verification.
This proves that derived fixture, not nonempty derived relations or end-to-end
report parity. Carry the assertions into the existing owner test at implementation.
Still require the planned public-output comparison and consumer failure cases.
The inspected report/K-O fixtures do not supply a derived-report or reviewed-role
comparison population. Record derived-report parity as owner-level bundle equality
plus existing consumer checks, and reviewed-role timing as unmeasured. Do not
claim a derived-report end-to-end fixture or reviewed-role benchmark exists.

**Consumer failure tests:** local pytest monkeypatches in the new test module wrap
`CanonicalFactResolutionEngine.read_as_known`, delegate normally, and alter only a
named cell. Return `CanonicalFactRead(cell_id, None, None)` for an unavailable
selection or raise RuntimeError for a caught evidence failure. For error order,
patch the consumer's post-selection ontology lookup to raise TypeError on the
first row and the reader to raise RuntimeError on a later cell; assert that the
first error escapes and the later cell is not read. For reports, a cell-specific
reader failure must leave a different valid cell usable. These are injected
control-flow cases, not fabricated admissions or proof of malformed stored graphs.
Existing owner tests cover real corruption and publication checks. Pin both
consumers for a status='resolved' receipt with selected_observation_id=None and
for a (None, None) result: discovery retains an unresolved entry; report retains
canonical_resolution_unavailable. The latter also covers consumer handling of
stale-lineage absence. A selected-ID-present result with an absent bundle separately tests the type
narrowing RuntimeError; it is not a baseline-compatible case. Add no mismatch arm. Preserve existing tests for real stale/derived evidence;
returning no selection tests consumer handling, not the derivation engine itself.

Predicate tests alter one field at a time in existing typed examples. Cover all
listed comparisons, missing objects/keys, and each consumer. Spy on lookups and
inject currency rejection/SQLite failure with a later source-context mismatch.
Check call order and reasons, including discovery's whole-read role-query failure.
Do not replace original consumer assertions with pure-function tests.

**Baseline capture:** before source edits, save exact discovery/report modules
from commit `630a74c5` only after verifying both working files hash-equal that
commit. If either differs, capture the actual pre-edit working files instead and
record their identities. For later stages capture their own pre-edit accepted HEAD,
not the first stage's source. Store these as task-owned `.tmp/reader-reuse-baseline/` source files and
record SHA-256. Load them with `importlib.util.spec_from_file_location` under distinct
module names registered in sys.modules. This preserves their original globals and
class identities without copying or translating functions. Load candidate modules
normally. Seed each synthetic migrated SQLite database once, commit it, and keep
its source files unchanged. Both implementations read that same database population
using separate connections; do not reseed, renormalize, or create parallel live
state. Compare serialized public model outputs, not Python class identity. Record
fixture/input hashes and the cutoff with counts/timings. Keep source and data
unchanged throughout paired measurements; remove disposable baseline source/data
through the task's existing temporary-test lifecycle after recording results.

**Measurement:** trace SELECT statements separately from transaction controls.
Wrap the real publication verifier using the pattern in
`test_fact_read_model_batch::test_batch_reuses_full_verification_and_matches_single_reads`.
For each population, record whether reviewed-role (`financial_statement_reviews`)
and derived-observation branches actually ran. Do not extend count claims to
unexercised branches. The default WIX population exercises legacy-namespace,
reported facts; reviewed-role and derived-branch performance is unmeasured unless
an existing fixture is actually run and recorded. On each unchanged real population, require identical public outputs. Exactly one
outer provenance-bundle fetch disappears per reached substituted cell, along with
that fetch's SELECT set and full-publication verification calls measured in the
baseline. Do not assume a bundle performs only one verification: admission can
verify several members. Attribute each removed read/call to the eliminated fetch;
O-absent derived cells also lose the identical repeated-lineage SELECTs. Require
the measured reduction, not merely an incidental total-count decrease.
Every subsequent call must verify again. Predicate extraction adds no SQL.
Explicit-O report source rereads remain. On existing K/O fixtures require identical
public outputs and SELECT/verification counts no greater than baseline. Count
constructor I/O; deleting its second instance can reduce setup calls.

Measure only these readers: five paired baseline/candidate runs with fresh
connections and five on open connections, excluding setup. Record host, Python,
and SQLite versions. Fresh connection does not mean cold OS cache. Median latency
must not regress beyond max(10% of baseline, 5 ms). Allow one balanced repeat for
noise; combine samples and hold if still over tolerance. Results support synthetic
reader-level claims only, not portfolio or whole-screen performance. The gain is
a deterministic reduction in SELECTs and publication verifications per eliminated
fetch. Timing is a non-regression guard only; five samples establish no latency
speedup and must not be reported as one.

**Fixture cleanup is separate:** first inventory every symbol imported from
`tests.test_canonical_growth_screen`. If any symbol other than STAMP or
seed_growth_graph is imported, stop and report it rather than widen the move.
Then move only STAMP and unchanged seed_growth_graph
body/signature/import dependencies to `tests/fixtures/growth_financials.py`.
Use `from tests.fixtures.growth_financials import ...`, following the existing
`tests.fixtures.dcf_statements` pattern. Tests is a package; fixtures already works
as its namespace subpackage. Change no pytest import mode or pythonpath; verify
collection under current settings. Update the original growth test and these seven
files: test_discovery_financial_inputs.py, test_canonical_peer_comp.py,
test_valuation_canonical_ltm.py, test_valuation_basis_provenance.py,
test_valuation_readiness.py, test_evaluation_snapshot_canonical.py, and
test_sealed_growth_regime_render.py. Target one builder and no imports from the old
test module. Require identical pytest collection node IDs/counts before and after,
and zero imports from test_canonical_growth_screen outside that original module.
Accept plain-assert diagnostics for the moved builder, consistent with existing
fixture modules; preserve every assert and its pass/fail semantics. Add no assertion
rewrite hook. Keep report/DCF builders distinct. No setup-speed gain is promised.

Run those eight modules plus test_financial_reader_reuse.py and
test_fact_read_model_batch.py serially for discovery/fixture work. Predicate and
report stages also run test_dcf_canonical_statements.py,
test_report_canonical_financials.py, test_report_financial_resolution_clock.py,
test_report_financial_semiannual.py, and test_canonical_financial_peek.py.
These checks cover their named readers, calculations, readiness, evaluation, and
render paths. They do not establish end-to-end allocation, advisor, memo, NU/MELI,
or Ask parity. Those consumers remain unmodified; claim more coverage only when
actual checks run. Require applicable static/architecture/reachability checks and
make check-fast. The known full-suite teardown failure remains a separate release
obligation, never a passed gate.

## Source-backed writer finding and prerequisite proposal

The [bounded spreadsheet audit](../quality/spreadsheet-publication-parity-audit.md)
compares an existing CO/NII workbook-history fixture with native publication
fixtures. Four existing tests passed. No writer is yet all-PASS. The workbook
handoff drops coordinates, original numeric representation and scale detail into
scaled floats; ingest reconstructs decimals and uses a legacy locator exception.
Original source bytes remain retained. Native identity/period/scope/admission,
replay, supersession and partial-commit recovery also lack same-input parity.

Before proposing native writer replacement, design one source-preserving handoff
using existing `SpreadsheetCellRef`, `FactLocator` and value types. Carry source
identity, sheet/cell, original representation, explicit scale and parse disposition.
Preserve current attempt-fingerprint/replay inputs and the legacy value produced
by Decimal(str(float*scale)). Pair fingerprint and numeric-value equality on the
same synthetic workbook. Project legacy fingerprint inputs from the one new
handoff; do not keep a second parser or persisted representation.
Replace the float-only intermediate and repeated conversion; do not leave them
alongside a new sidecar. Preserve legacy outputs and unknown/rejected states.
Distinguish an original workbook token from text reconstructed from a converted
float. Do not infer duration, reporting identity, basis, scope or admission.

Sol prepares that bounded design; Astra judges it; Opus must score it above 90
before implementation. Prove parser-to-writer parity on the same synthetic workbook,
including skipped cells. This is source preservation enabling a later cut, not a
new eligibility guard or a completed native-writer retirement. Re-run only affected
matrix rows afterward; the other semantic and recovery blockers remain explicit.
The eventual removal target is this producer's competing legacy manifest /
restatement / resolution procedure, preserving shared owners for other callers.

## Bounded durable-model inventory and next decision

| Dependency; current owner/callers | Target and evidence needed |
|---|---|
| Legacy financial namespace: population_source_facts._source_fact_from_row; discovery/report/series/DCF selectors | Preserve immutable IDs. Remove namespace-specific selection only when existing ontology/role mappings enumerate identical cells, concepts, cutoffs, and gaps. |
| Legacy document joins: DCF `_read_cell`/primary_source_eligible and report `_admit_cell` | DCF reads ticker/source_type/source_quality_tier for eligibility. Report projects legacy_document_id/source_url/source_kind via evidence_document_versions. Inventory both reader families separately. Compare existing evidence-owner projections; ExactEvidenceReference lacks DCF's authority fields, so removal is unproven. A URL is insufficient. |
| Spreadsheet write: refresh_ir_kpis → ingest_spreadsheet_kpis → persist_manifest → _insert_kpi_fact → restatement detector | Audit one spreadsheet publication against SourceFactRepository. Preserve configured/captured metrics, evidence, fiscal/unit/currency semantics, validation, replay, and supersession. Summary callers share persist_manifest; cover them before changing that writer. |
| financial_facts/kpi_facts and old observation/resolution bridge → populate_source_fact_plane | Target one source-publication authority with necessary compatibility projections. Prove population, history, recovery, and reader parity before retiring competing writes. |
| KPI registry, issuer-definition revisions, semantic contexts, canonical ontology | RETAIN distinct configuration, issuer meaning, admission, and comparison records. Remove repeated procedures, not different meanings. |

The spreadsheet audit above already completes that producer's bounded comparison
against an existing native fixture plus explicit missing inputs. The remaining
matrix covers the other named rows. Completion requires one named qualifying
procedure candidate or one precise missing capability for each row, with its
existing owner and fixture/source evidence. No row may end as an open-ended
"investigate more" item. Reuse the spreadsheet evidence without
reseeding it or asserting a same-input native publication exists. Name each current field's
writer/readers, proposed existing replacement, and removable procedure. Compare
identities, values, periods, units/currency, scope, locator, definitions, admission,
cutoffs, supersession, replay, partial failure, transactions, and recovery. Mark
PASS, FAIL, or RETAIN with a fixture reference or precise missing authority.
Include rejected and unclassified observations; admitted KPIs alone are insufficient.
Finish the audit at the matrix. A missing equivalent blocks that candidate;
do not invent evidence, discard fields, or create a replacement framework.

The next implementation is one matrix-selected procedure consolidation, not a
guessed schema migration. Only candidates with every required field and behavior
PASS through fixtures qualify. Prefer the candidate serving the most core
consumers, then the smallest scope, clearest write ownership and simplest rollback.
Astra reviews the choice. Replace the competing procedure with its existing owner
and remove the superseded implementation. Preserve persisted formats, identities,
history and recovery. Require Opus above 90 and applicable checks at completion.
If none qualifies, name the exact missing capability and continue bounded
structural work; do not use feature deletion as a substitute.

Runtime models, write procedures, compatibility adapters and repeated derivations
can become simpler while historical tables remain. Table deletion is not the
measure of completion for these local procedure consolidations.

No schema/data migration or physical retirement is authorized here. Section 11 of
directives/data_provenance.md still requires two quarterly cycles, one annual cycle,
recovery evidence, and separate final owner approval for physical retirement.
The read/predicate/fixture work is code-only; rollback is a code revert.

Sequence after the reader/predicate/fixture stages: complete the remaining matrix
rows (namespace selectors, document joins, and old-fact bridge). Reuse the completed
spreadsheet row and its existing-native-fixture comparison; do not invent a native
CO/NII publication from absent semantic inputs. Then prepare the bounded spreadsheet
handoff design. Inventory every parse_spreadsheet caller and public re-export before
replacing its return type, updating all affected callers as one change. Current
direct production use is refresh_ir_kpis; search again at that design checkpoint.
openpyxl data_only supplies Python values and accessible number_format metadata,
not an original XML lexical token. Preserve that distinction and mark an unavailable
token explicitly; do not recreate it with str(float). Exact workbook bytes remain
retained. Implement the handoff only after its own reviews, recheck affected matrix
rows, and select a qualifying procedure consolidation under the rule above.

Feature usage review can start after the matrix. Feature cuts wait until shared-read
work and at least one matrix-selected procedure consolidation pass their checks
and reviews. Cuts also require their own consumer/replacement evidence. They need not
wait for physical-retirement observations; the matrix does not authorize deleting
durable state. Active callers are not proof of unused functionality.

## Delivery

Each major implementation needs Astra advisory review, independent Opus 5.5 score
above 90 with no blockers, and passing applicable deterministic checks. Report
actual removed procedures, measured reader results, retained boundaries, and limits.
A score cannot override a failed test or an incomplete release gate.

## Outstanding independent-review requirements

The latest reviewed proposal is preserved with its source and prompt identities
in the cleanup review record. No approval is claimed for this status update.

- Prove the stored-resolution read does not need the four plane-only SQLite
  functions before the engine initializes its reader. Include the DCF precedent.
- Pair DCF output against its own baseline, or explicitly limit that parity claim
  to its existing owner tests.
- Remove the claim that the derived bundle-owner fixture establishes report
  outputs/counts. Use the source identity argument and state its precise limits.
- Give the program an owner-decision exit if the matrix and handoff enable no cut.
- Specify provenance when canonical-name merging overwrites a period, and measure
  the parsing cost of retained coordinates and number format.
- Decide explicitly whether to share or retain the repeated quarter-span check and
  per-cell DCF ontology initialization. Astra must judge consequential changes.

The next work is proof and bounded design. No weaker score, feature deletion,
invented evidence or unreviewed migration substitutes for this gate.
