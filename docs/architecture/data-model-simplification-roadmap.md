# Research platform: backend and ingestion retirement roadmap

The [execution plan](backend-retirement-execution-plan.md) owns current scope,
status, holds, verification and release actions. This roadmap retains the
architecture endpoint and terminal populations. Superseded checkpoint narratives
remain in Git history; they do not establish current acceptance.

## Product and architecture endpoint

Serve pre/post earnings briefs, deep research/Q&A, and portfolio decisions through
one evidence flow. Retain the depth demonstrated by NU PRE/POST and MELI, including
historical context, thesis implications, assumptions, scenarios, sensitivities and
workbooks. Reuse owner profile, holdings and thresholds without mixing assumptions
with company-reported facts. Expansion uses evidence already held; preserve source
collection authority and require approval for new collection outside approved scope.

**Source bytes → contextual extraction → explicit admission → selected facts and
calculations → prepared analysis → views and exports.**

The eight logical DAG stages remain. Each stage has one execution owner and the
receipts needed for its claim. Acquired, extracted, admitted and published prove
different properties. Replace duplicate success authorities; keep those meanings.
Source adapters handle source-specific interpretation. They do not each own a
second registration, completion, fact-selection or publication procedure.

Success requires old active procedures to disappear in the same bounded change as
their replacement. A shared helper alone does not complete a retirement unit.
Existing owners can gain a coherent operation; a new generic workflow engine,
attempt store, cache service or universal fact model is unnecessary.

### Data-model reduction and ownership

Reduce competing representations and decisions before deleting historical tables.
These are existing concepts and owners, not proposed new entity tables:

| Concept | Retained authority | Representation/procedure to remove |
|---|---|---|
| Registered original and acquisition inventory | Managed IR publication and existing document/evidence records | Loose sidecar/categorizer registration decisions for each migrated input family. Discovery and downloads remain adapters. |
| Extracted source content and observations | Existing fulltext/extractor and financial provenance owners | Re-extraction when exact source/parser identity already proves usable content; automatic numeric extraction from generated summaries during refresh and comment rebuilds; keep explicit candidate extraction. |
| Generated research product | `llm_artifact_store`, with attempts in `pipeline.run_accounting` | File existence and JSON `processed` as competing completion authorities after the R3 contract qualifies. Text files become projections. |
| Selected financial fact and evidence | `CanonicalFactResolutionEngine`, `CanonicalFactRead` and provenance bundles | Consumer-specific selection/bundle rereads and copied verification logic. Keep distinct metric, period, unit, basis, scope and knowledge/observation clocks. |
| Prepared analysis and presentation | Existing typed report sections, starting with `SayDoSection` | Markdown-to-row parsing, render-time enrichment and duplicate context/filter caches for migrated sections. Export and UI consume the same prepared argument. |

Carry existing stable references through boundaries. Do not rebuild loose copies of
identity/context dictionaries or derive a financial value from presentation text.
Purpose-specific calculations and presentation rows may remain when they encode
different information; they must not become independent source or selection owners.
Holdings, thesis, personal thresholds and scenario assumptions retain their existing
owners. This roadmap adds no account, tenant or synchronization model.

## Seven simplification units

### R1 — One FMP document-batch extraction engine

**Cut:** replace `execution/extract_facts.py::_run_extraction` and
`pipeline/quarterly_refresh.py::_stage_extract_fmp_facts` orchestration with one
cohesive document-batch operation in the existing pipeline package. Delete the
second selection/dispatch/execution implementation. Both callers retain thin
CLI/stage adapters. Their maps currently duplicate **11 entries and 2 NU overrides**.

Centralize document ordering, extractor selection, outcomes and failure accounting.
Preserve income-before-segment dependencies, NU IFRS behavior, source locators,
restatements and legitimate zero-result documents. Fix the demonstrated policy
mismatch: quarterly `_safe_extract` converts known errors to zero, and its caller
then reports OK. A parsing failure must be an explicit failed document.
Dependent completion must also be explicit. `compute.segments.passes_reconciliation`
currently accepts missing or zero revenue; failed income can therefore produce
unreconciled segment writes reported OK, not just OK-zero. The existing extractor
must expose the affected issuer/period and prerequisite-unavailable disposition;
batch accounting records blocked/degraded work instead of successful reconciliation.
Do not parse source JSON again in the batch or infer dependencies from row counts.
Preserve valid held revenue, independent periods, empty valid records and dedup zeros.
If malformed input has no reliable period, report that uncertainty instead of
inventing a period or blocking every document for the issuer. Do not label as-reported
extractors dependent without an actual source dependency. Qualify this correction
before closing the current R1 truthfulness claim.

First retain existing per-document commit boundaries. Roll back pending failed
work before committing failure accounting; preserve earlier completed documents.
A fatal database/schema failure stops dependent work. Qualify the exact extractor
commit paths before promising atomicity. `refresh_cache` uses supplied immutable
records and `commit=False`; preserve that caller-owned transaction path rather than
route it through an independently committing batch.
Require a clean connection before the batch can commit, roll back or report; reject
caller-pending work without changing it. Resolve the three `refresh_cache` extractors
from the same registry only while preserving supplied records, `commit=False` and
its caller-owned transaction. Delete its redundant selection branches when that
contract is qualified. Income-first CLI ordering intentionally corrects the old
alphabetical order, which placed as-reported JSON before income statements.

**MVP/proof:** both production routes process the same synthetic documents with the
same facts/lineage; malformed input fails truthfully; revenue precedes segments;
replay and failure recovery preserve completed work. Replace the quarterly test's
139-line hand-built schema with the existing session-migrated fixture, preserving
behavioral assertions and explicit historical tests. No schema migration is needed.
Include malformed income followed by valid segment records, mixed periods with
usable revenue, and legitimate empty/dedup results through the actual extractors.

### R2 — One managed issuer-document registration path

**Cut:** retire loose `fetch_ir_documents.process_ticker` → URL sidecar → categorizer
registration and `fetch_ir_from_urls.register_urls` registration decisions. Route
both discovered and explicit URLs through managed prepare/validate/publish.
Keep transport/discovery and manual input capabilities. Remove obsolete CLI
surfaces only after their callers move; a distinct useful CLI can remain an adapter.

Use `prepare_issuer_document_sources` and `pipeline.managed_ir_sources` as owners.
Resolve unknown document type/period through bounded triage before publication;
do not guess. Preserve automatic `requested=False`, fiscal-calendar forwarding,
index/metadata side effects, denied collection and staged-residue recovery. Loose
local-upload categorization stays until its separate input contract is supported.

**MVP/proof:** one IR input family supports discovery and explicit URL input through
one registration owner. Compare already-present inputs, changed bytes, partial
failure, classification failure and denied collection. Delete the superseded
registration/success branches, not merely add managed publication alongside them.
Start with PDF `ir_presentation` inputs, using the existing synthetic MELI managed-
publication fixtures. Exercise discovered and explicit URLs without network access.
Other document families stay on their current supported route until qualified.

**Terminal population:** inventory the actual supported combinations of document
family × media type × fiscal calendar × input route. Families include presentation,
press release, supplement, investor update, transcript, event and IR-channel SEC
documents where current callers support them; media and calendar cells are taken
from those callers, not assumed to form a fully supported Cartesian product.
Discovered URLs, explicit URLs and local uploads are separate input contracts.
The first qualified cell is calendar-FYE PDF presentation via the two URL routes.
Non-calendar presentations and all unqualified cells retain a named existing route.
Each cell records its callers, source/calendar interpretation, side effects and
deletion proof. Full R2 closes only when all supported cells use the one registration
owner and the loose registration/URL-sidecar decisions are deleted. Useful input
CLIs may remain as adapters. Do not silently remove an unsupported cell or count
an unchanged legacy route as retired.

Qualification must include automatic `requested=False` denied by policy through
the real managed route, plus explicit requested input and actual fiscal-calendar
forwarding. The older review's hardcoded-True finding is a regression case to
prove, not a claim that the current source still hardcodes it. Publication/recorded
registration outcomes, not downloaded counts, govern `ir_fetch_status.record_attempt`,
anchor refresh (`_run_narrative`) and `_set_brief_dirty`. Keep their execution in
the existing adapter owners, with failed effects resumable and `process=False`
preserved. A coherent registration no-op is not fresh source admission or full
publication re-verification. Acquisition gaps must not prevent already-eligible
processing; corrupt proof cannot be treated as a successful repair. Local uploads
retain their explicit categorizer/intake routes; removing a fetch sweep proves no
scheduled replacement.
The request mode must be bound in the sealed staging request and its receipt,
alongside calendar context. Prepare, validate and publish must authorize using
that same bound mode; none may substitute requested=True on automatic input.
Test denial through all three boundaries and reject a changed mode or receipt.
Current-source inspection finds `IssuerDocumentStagingRequest.owner_requested` in
`issuer_document_staging_request.v1` and `.v2`; v2 adds a historical window, not
different collection authority. The enclosing staging receipt seals the serialized
request; `_policy` uses `requested=request.owner_requested`, and prepare receives
the same mode. Legacy omitted True fields preserve old hashes; automatic False is
serialized explicitly. Supply these current source excerpts and the three-boundary
denial proof with review packets; the older c978523d excerpts do not prove this
change. Preserve the contract for every migrated route. Unsupported fiscal/event
or media cells require explicit managed-model qualification, not calendar relabeling.

### R3 — Explicit-state execution, then qualified product completion

**Cut:** give processing explicit state/database arguments and remove
`process_ir_documents_state.py` global-module rebinding/`sys.argv` machinery.
This adapter removal preserves current behavior and can proceed independently.
Then qualify an exact source/product completion operation before replacing JSON
`processed` flags or cache-file existence as success authorities.
Move consumers before retiring `mark_document_processed`, `get_unprocessed_documents`,
`mark_event_processed` and `get_unprocessed_events`.

Keep deterministic source extraction separate from narrative synthesis. Reuse
`document_evidence_pipeline` fulltext only when source and extractor identity match.
Preserve quarter/event identity, aliases, file moves, anchors and missing-summary
repair. Raw narrative and analytical summaries remain different products. Existing
index readers and transcript acquisition are mapped before changing their state.

The completion contract is to be built and qualified within existing owners:

- `run_accounting` owns attempts; `llm_artifact_store` owns saved generated output.
  Its current best-effort `upsert()` can return `(None, False)` and commit on its
  own connection. Add a narrow fail-closed, connection-owned operation reusing the
  existing writer; do not duplicate persistence or create another store.
- Bind logical identity to issuer, exact document/event, fiscal coordinate and
  product purpose. Hash consumed source bytes, parser/version/limits, prompt/style,
  effective model configuration, exact anchor/context and dependent artifact IDs.
  These dependencies must enter `cache_inputs`; descriptive model/source/parent
  metadata is not automatically hashed. Qualify exact-key storage/lookup for two
  documents in one quarter before cutover. A necessary key/schema change needs its
  own bounded review, rather than an opaque-ID encoding in a purpose field.
- Save one checksummed payload containing body, input identity, generation identity
  and source/context receipt references. Verify its read-back. The current output
  hash chooses Markdown or JSON; do not leave one unchecked alongside the other.
- Reserve the attempt, verify reusable saved output, then generate outside a write
  transaction. Recheck inputs/ownership; persist output plus the SYNTHESIZE receipt
  together. Atomically project compatibility text from that committed artifact.
  Record required publication and finish the run only after projection succeeds.
- A crash after output commit resumes from the verified body. Repair missing/corrupt
  projections with zero generation calls. Persistence failure stays failed/pending;
  an OK attempt or text file cannot override missing/corrupt output. Retain the
  original generation identity during repair. A legacy quarter filename must name
  a deterministically selected artifact, not prove another document complete.

**Legacy cutover is adoption, not regeneration.** Inventory the existing generated
summary projections before changing completion selectors. Preserve each body's
exact bytes and hash in the existing artifact owner, with original path and any
recoverable document/period references. Record unavailable prompt, model, anchor
and input identities explicitly as unknown; do not invent an exact input key from
the filename or current context. Where logical identity cannot be established,
retain the source as unresolved history rather than attach it to a guessed document.
Any narrow schema change needed to express this state must qualify before cutover.
No separate adoption store is needed.
Do not insert adopted history through the current tuple-based `upsert`/`LIMIT 1`
lookup before exact document/product identity and history-versus-cache eligibility
are qualified. A history row must neither supersede nor shadow current output or
another same-quarter document. Test both insertion orders with current and adopted
artifacts. Until that lookup contract qualifies, prior files remain readable in
place. Do not manufacture input hashes or encode identities in purpose strings to
fit the old key.

An adopted artifact is readable history, never an exact-key cache hit or proof that
the current product is complete. Adoption and projection repair make **zero model
calls** and preserve the original bytes. They must not enqueue an automatic mass
regeneration. On source, anchor, thesis, prompt or model change, mark the relevant
product stale/dirty while keeping its prior artifact readable and its old generation
context intact. Regeneration proceeds only through the existing dirty/drain owner
within an already approved scope and budget; adoption grants no new model spend.
If no such authority covers the work, present the bounded owner choice. A stale or
legacy historical view cannot be presented as current verified analysis.

**Terminal population:** all quarter and event narrative products currently selected
through processing flags or summary-file existence, including existing legacy
projections and same-quarter multiple documents. Migrate the processing selectors,
repair/regeneration callers, report/history consumers and CLI adapters to the one
artifact/attempt contract. Then remove the four processing-state procedures named
above and their completion checks. Index aliases may remain for lookup; they no
longer decide generated-product completion. Transcript acquisition receipts remain
separate acquisition authority, not collateral deletions.

**MVP/proof:** first remove global mutation with exact path/anchor parity. The later
completion cut tests changed source/anchor/prompt/model, same-quarter documents,
missing/corrupt output, unavailable DB, output-commit interruption, projection
failure/repair and unchanged replay. Raw `ir_narrative` envelopes are a useful
source-population/projection pattern, not proof of generated-summary completion.
Only then remove the obsolete processing-state procedures and their callers.
Add legacy adoption, unknown generation inputs and stale-but-readable history to
that matrix. Count model calls: adoption, unchanged replay and projection-only
repair each require zero; an authorized cold/dirty generation makes no more calls
than its qualified predecessor for the same product, unless separately approved.

### R4 — Explicit research with original evidence

October 26 scope supersedes the earlier summary-lane preservation decision.
Retire the summary-KPI CLI, its refresh step, its exclusive candidate engine,
and separate generated release/deck/investor-update summaries and cards.
Retain earnings transcript summaries, SayDo, original source access and explicit
KPI manifests/capture from original evidence. No legacy summary adoption or
filename-derived identity is required. Historical derived financial observations
keep their actual evidence until their dependencies are resolved.

Research lenses are on demand. Targeted questions use existing backend skills
and grounded chat. Invalid or empty selections must not start collection. No
new workflow engine, evidence store, transport or automatic coaching writer.
Original transcript numeric automation remains unavailable until exact version
and source-segment mapping is proved. Typed manual manifests must validate the
original source identity, excerpt and locator before persistence.

### R5 — Retire active legacy financial and KPI write/bridge lanes

**2026-10-23 execution amendment:** the spreadsheet adapter already uses the
shared KPI writer. There is no separate spreadsheet-specific financial writer
to delete. The qualified cleanup keeps that owner, resolves configured definitions
once and rolls back a failed period before committing failure accounting. The
typed extraction-output reader is a synthetic prototype, not a production one-parse
consumer or financial admission. Native promotion still needs source-reviewed
meaning and historical consumer/recovery proof absent from the current config.
The larger terminal proposal below remains conditional; it earns no cleanup credit
from this adapter change. See the current checkpoint for exact source, evidence,
independent scores and the restored original structural gate.

**Cut:** for a qualified spreadsheet producer, replace value-only parsing and
`ingest_spreadsheet_kpis` → `persist_manifest` → independent legacy restatement /
resolution → native bridge sequencing with one source-preserving publication
operation using existing locator/value types and `SourceFactRepository`.
Required legacy outputs become controlled compatibility projections, not a second
fact-selection or completion authority. Shared legacy owners remain for other
producers and historical reconstruction until their own migration qualifies.

**Proposed terminal scope expansion:** migrate all active legacy financial and KPI
producer writes to `SourceFactRepository.publish`, with existing semantic admission
and `CanonicalFactResolutionEngine` selection. A KPI-only endpoint cannot retire
the shared legacy-observation population bridge. This larger endpoint removes the
intermediate write → capture → re-read → native-publication lane; it does not create
a universal record type or combine acquisition, publication and admission receipts.
This is a phased roadmap proposal, not a claim that producers are qualified or
authorization for a schema, data or live cutover. The current spreadsheet handoff
and issuer-collision correction remain preparatory work.

Keep one handoff carrying source identity, winning sheet/cell locator, raw available
representation, scale, period and disposition. Canonical-name/period overwrites
must carry the winning value's provenance. Preserve current numeric and replay
identity until an intended change is separately qualified. Do not reconstruct an
unavailable XML lexical token from an openpyxl float.

**Unresolved design gate:** native complete-context cells do not establish storage
parity for unknown/unclassified/rejected captures. Name their existing lossless
capture/disposition owner and prove read/recovery parity before routing new data.
If existing storage is insufficient, present that exact capability decision; do not
invent financial context, weaken admission or add a second generic raw store.

**Proposed existing-owner capture contract:** use a versioned typed capture in
`EvidenceLedger`, with an `ExtractionRun` and locator-backed `EvidenceNode`
children. The root contains metadata and ordered child IDs/hashes. Missing
selections have explicit metadata and no invented cell address. Preserve each
parser value with a tagged representation and retain unavailable dispositions.
This records configured selections; it does not prove workbook completeness.

Derive capture identity only from issuer/document identity, source bytes, full
ordered configuration, parser version and selection window. Store the output
digest separately. Exact replay verifies the complete graph and returns its
original IDs and clocks. Changed output under the same input identity conflicts.
Schema sufficiency and the capture reader remain unproved.

Keep separate commit units: acquired source bytes/document; complete configured
capture plus parse receipt; then one eligible period publication plus its receipt.
A failed period cannot erase earlier committed periods. Record failure separately
after rollback. Empty or rejected-only capture publishes no admitted facts and
reports a skipped publication. Qualify these units through the actual producer,
including exact replay, corruption and injected failures, before cutover.

**MVP/proof:** same-workbook parser-to-publication equality, unknown/rejected/novel
retention, exact replay/conflict and supersession results, and tested mid-period
failure/recovery. Specify the new transaction boundary explicitly; current per-period
commits differ from native graph atomicity. Then remove that producer's competing
publication sequence. Current audit does not yet qualify this switch.
Begin qualification with the existing synthetic CO/NII two-workbook supersession
case in `test_ir_spreadsheet_ingest.py`, extended through the actual parser and
including unknown/rejected rows. This is a test slice, not production issuer
qualification. Select a real supported producer only after its exact source,
definition and capture contracts pass; do not infer NU readiness from this fixture.
The first producer contract must include `_ensure_spreadsheet_document`: its
baseline matches SHA-256 without issuer identity and commits the document before
fact publication. Identical workbook bytes must not attach one issuer's facts to
another issuer's document. Qualify exact issuer/document binding and a visible
conflict for foreign matches. Specify whether a committed document with incomplete
facts remains acquired/pending, how replay resumes it, and which later writes are
atomic. Test failure after the document commit and mid-publication without deleting
earlier history. Native producer retirement cannot inherit an accidental hash-only
identity or claim atomicity across an already committed document.

**Terminal producer population:**

- Financial: FMP income statement, balance sheet, cash flow and as-reported adapters,
  including the immutable-record `refresh_cache` path; S1 financial extraction;
  SEC CompanyFacts (`sec_xbrl`); and FPI financial ingestion/foreign normalization.
  Their source-specific parsers stay; their `insert_financial_facts` /
  `insert_with_restatement_detection` write sequence must stop being a product owner.
- KPI: spreadsheet refresh, original-PDF/manual manifests, generic XBRL capture,
  issuer-fact manifests, competitive category-share/transcript mentions, reviewed
  recapture (`kpi_source_review`), FMP-derived KPIs, metrics-engine derived writes
  and FPI KPI writes. Optional summary-derived producers remain under R4 and
  retain quarantine pending primary review; they do not become admitted writers
  through command execution. Retire `persist_manifest`, `_insert_kpi_fact`,
  `insert_kpi_with_restatement_detection` and `prepare_reviewed_kpi_native_projection`
  as active publication paths after their last real producer moves.

Reconcile this list against all actual writers before each cut. Similarly named
commitment persistence is a different product, not included by function name.
Use one bounded producer/coordinate cluster at a time: qualify source/context and
the exact publication graph, switch its real callers, remove its legacy write/bridge
branch, then qualify native reader behavior for the same population. Preserve
reported versus derived observations, issuer/document identity, unit/scale/currency,
period/duration, accounting basis, consolidation scope/dimensions, source locators,
definition revisions and both clocks. Compare complete selected values, conflicts,
overrides, same-document corrections, restatements and supersession at historical
cutoffs; a financial name/value match is insufficient. Include exact replay,
partial failure, recovery, accounting and rollback under each caller's transaction
contract. `refresh_cache` keeps its supplied-record, caller-owned transaction.

Unknown/unclassified/rejected source content needs a qualified existing durable
capture/disposition owner before its producer can move. Do not manufacture native
context or promote legacy `other`/`unknown` coordinates to gain coverage. A blocked
producer remains visibly unmigrated; it does not justify another raw store or a
second native publisher. A qualified financial cluster may proceed independently
of the spreadsheet capture gate.

**Shared-lane removal:** once both financial and KPI product writers have migrated,
remove active calls to `populate_source_fact_plane` from publication/normalization
orchestration and retire the competing logical resolution path. The named shared
population is `_population_plan`, `_source_rows`, `_source_fact_from_row` and
`resolve_fact_logical_key`. Full body deletion also requires every retained
backfill, historical selector and cutover caller to be qualified elsewhere; live
readers of `canonical_fact_relation` in report/Ask, timeseries, thesis, metrics and
DCF must have canonical-reader parity. R6 batching alone does not prove that parity.

Keep unique historical observations, source bytes and migration/checkpoint evidence.
Historical reconstruction may still use the existing legacy readers/bridge while
their obligations remain; it must not become a fallback writer for new products.
Do not count those retained shared bodies as deleted. Full R5 active-lane closure
requires zero inventoried product writes through the legacy financial/KPI sequences,
zero product-time bridge population and one native publication/selection authority.
Removal of legacy views, modules, migrations or tables remains separately gated by
§11's observation windows, final owner approval, migration and rollback proof.
Neither a producer rehearsal nor this scope proposal starts that window.

### R6 — One bounded multi-cell financial read per consumer operation

**Cut:** replace per-row selection/bundle orchestration with existing batch reads.
Discovery can use `read_as_known(tuple_ids, K)` with ordered row fanout. Reports need
a narrow ordered-outcome operation in the same resolution owner, reusing existing
read/failure types where their semantics fit. The required-bundle API currently
raises on one corrupt source and cannot preserve report cell isolation as-is.
Keep required reads as an adapter over the same owner rather than a second engine.

Treat missing selection, cell-specific evidence corruption, operation-wide
DB/schema/snapshot failure and programmer errors distinctly. Qualify this deliberate
failure policy; do not claim exact incidental error-order parity. Batch only matching
cutoffs. Every explicit observed clock O retains selection at O and source/admission
at K, including O=K. Keep growth, report-coordinate and DCF eligibility policies local.

Verification reuse is bounded by snapshot and cutoff, never global or persistent.
Independent requests verify again. Baseline: WIX 21 facts used 1,660 SELECTs and
105 full-publication verifier calls for **one distinct publication**; reported
report 8 cells used 649 SELECTs and 40 calls for one publication. These are measured
costs, not predicted gains. Recursive derivation reads must be counted too.

**MVP/proof:** full values/order/reasons/citations match qualified behavior; a corrupt
report cell leaves valid cells usable; snapshot and explicit-clock tests pass.
Require an available derived-value result alongside unavailable/corrupt cases. Reuse
the existing positive derived-series case and real publication/binding builders for
the report case. The current ambiguous-coordinate report fixture remains a negative
case; never relabel it as usable or fabricate admission to fill this coverage gap.
Eliminate direct redundant consumer bundle fetches and measure batch/derivation
verification counts, SELECTs and latency. Share definition checks/metadata while
removing their consumer copies; those helpers support this retirement, not replace it.
**Terminal population:** reconcile actual callers of resolver scalar/batch reads,
`FactReadModel.provenance_bundle(s)` and full-publication verification at cutover.
The current caller list includes `discovery_financials.read_financial_history`,
`report_financials.read_financial_table`, `CanonicalFinancialSeriesReader`,
`dcf_statements.read_dcf_statements`, `dcf.input_evidence.verify_model_inputs` and
`dcf.meli_input_preview.preview_meli_inputs`, plus the resolver's own strict/scalar
adapters. Delete `_BatchFactReadModel` and its duplicate snapshot/cache machinery;
remove scalar selection-plus-bundle loops from the discovery/report operations and
from series `_covered_cumulative_ids`, `_project_coordinate` and `_read_uncached`.
Retire the series `_provenance_bundles` cache when its callers consume shared ordered
reads. Already-batched DCF callers require parity, not a manufactured deletion.
Retain eligibility, coordinate projection and calculations. The first checkpoint is
partial R6; terminal closure remains open until the inventory and these cuts qualify.

### R7 — Prepared analytical sections; renderers display results

Move loading and enrichment out of the body renderer into existing build and
section owners. The complete retained population must perform zero database,
file, model or cache work from `render_report_body`. Prepare comments and access
capabilities through their existing delivery owner outside that pure call.
Retain their separate freshness/access rules and honest partial/unavailable states.

The owner retired generated SayDo pairwise narratives, row selection and their
Markdown/filter caches. Retained SayDo uses source-linked commitments,
guidance/actual comparisons and recorded verdicts. It has no replacement
narrative generator or historical narrative-adoption interface.

The separate Recent developments write-up and four-quarter Q&A rollup are pending
owner choices. Keep their current behavior until answered. Do not start new news
or rollup generation, schema work or model qualification while that choice is
pending. If retained, qualify the changed generation path through existing LLM and
financial-grounding owners; if retired, remove its exclusive code and eligible
history. Raw news/source links, individual transcript summaries and source-linked
SayDo remain either way.

Prepare retained macro sensitivities, strategic targets, customer concentration,
leases, decision history, peers, open notes, current process rules and comparable
context through existing section owners. Retired personal-coaching counts,
guard/base-rate strips and automatic adoption are removed from this population.
The terminal inventory is every retained read reachable from the body renderer,
including comment boot data. Do not substitute a named subset for that closure.

Prove complete retained reader/export parity, current source/context invalidation,
repair/recovery, no model calls on completed-product replay and pure rendering.
Qualify changed owner-retained narratives with source-grounded evaluations, not
renderer doubles alone. No parallel presentation fact store, automatic deleted
output regeneration or general preservation of retired cache history is approved.
Protect only exact retained decision, process, report or financial-source lineage.
The existing durable Copilot handoff remains the chat owner.

### October 26 SayDo scope clarification

The owner keeps source-linked SayDo commitments, guidance-versus-actual comparisons and recorded verdicts. The separate generated pairwise narrative and row-selection filter are retired, including their exclusive cache history, runners and schedule declarations. Retained commitment and comparison ledgers remain. This source change does not stop the live Windows task; canonical retirement remains a release step.
