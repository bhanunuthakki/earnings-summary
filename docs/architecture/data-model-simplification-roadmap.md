# Research platform: backend and ingestion retirement roadmap

Status: **Implementation in progress; bounded checkpoints accepted independently.**
The owner authorized execution before the pending roadmap review on 2026-10-22.
See the [implementation checkpoint](../quality/backend-streamlining-implementation-20261022.md)
for implemented scope, measured results, review state and unfinished work.
The proposal below preserves the direction; it is not a completion claim.

The source inventory was taken at `d19bc3e3`; the revised proposal was frozen at
`c978523d`. This proposal superseded the helper-first sequence. Three fresh GPT-6.1 Sol judges
reviewed the previous version; all requested revision. Their original scores and
findings remain in the [panel receipt](../quality/backend-streamlining-panel-20261022.json).
The first revised review scored ambition 94/effectiveness 90, REVISE: its R4 slice
did not overlap the summary-number lane. The corrected R4 then passed the fresh
Sol review at ambition 94/effectiveness 92.
The earlier owner gate required Opus 5.5 membership review, with no paid fallback.
On 2026-10-23 the owner removed the Astra requirement and allowed independent
GPT-6.1 Sol judges as the review proxy. The current checkpoint uses that proxy;
the earlier roadmap and key implementation target was strictly above 90.
On October 24 the owner changed the advisory acceptance target. The latest rule is **strictly above 85 at PRD and implementation review**,
with no unresolved material blocker. Financial integrity and deterministic checks
remain required. Earlier scores and failed checks retain their original meaning.
Prioritize removal of whole duplicate procedures, repeated data work and unused
features. Check existing owners and installed components first; evaluate a new
library only where it can remove substantial custom code without losing the core
evidence contract. Defer marginal helper/formatting work. Use targeted checks
during implementation and batch complete gates at major integration/delivery
checkpoints, preserving the repository's pre-push requirements.
A proxy verdict is not an Opus verdict or a formal release qualification.
The later Opus roadmap review returned REVISE, including a missing legacy-summary
adoption contract and incomplete terminal retirement populations. Its retained
receipt is `.tmp/roadmap-execution-signoff-review.json`. The amendments below
address those plan findings; they are not a new review verdict. Source observations
in that receipt describe its supplied snapshot, not necessarily the current code.

## October 24 execution disposition

The financial-read MVP is committed locally at `1bbc671a3`. It shares operation-local materialization across the two financial views. Independent Sol scored the PRD 89 and the corrected implementation 90. All 325 owning tests pass and 224 full old-source comparisons match. In the bounded two-cell case, annual SELECTs fall from 145 to 77; projection work falls from four to two. This is a repeated-work reduction, not a general latency claim.

The current all-family R2 migration is deferred. Its net cut is not qualified, and working source formats, fiscal dates and recovery must stay. R7 structured generation scored 79, REVISE. A narrower common-store selection-cache proposal then scored 78, REVISE: its expiry, identity, dirty completion and normal-build persistence contracts do not preserve current behavior. Do not expand storage or job frameworks just to force these cuts. The R2/R7 endpoint remains open; their partial checkpoints are not terminal retirement.

The owner retained ETF role research on explicit demand. Automatic onboarding generation and the bulk synthesis option are removed in local commit `120a2dee4`. Independent Sol scored this scoped PRD 93 and implementation 94; 47 owning tests and changed static checks pass. This cut removes 42 net physical production lines. Saved research, deterministic evidence and the manual procedure remain. Create/Refresh controls, explicit-request cache removal and complete-output financial verification remain unfinished. The full on-demand PRD scored 82, REVISE. Sol accepted explicit plan corrections at 93, conditional on real capture witnesses and the named transaction, manual-request and complete-output checks. The held-snapshot capability review does not close those conditions: generic FMP indexing runs before ETF acquisition, and legacy timestamps do not prove current capture. Do not treat the automatic-call cut as full delivery.

The owner's next priority is larger pruning, starting from the frontend interface and actions, then their connected backend paths, then redundant test and setup chunks. Trace each candidate end to end before changing it. Retire a whole duplicate path when an existing retained path preserves its user task; do not count relocated code, different presentations or necessary lineage as duplication. The first larger unit now removes prototype regions already discarded by the live shell. It removes 1,678 net lines and 102,817 bytes across the template, assembly and owning checks. Independent Sol scored the PRD 92 and implementation 93. Three complete HTML comparisons match except six approved spaces on an empty line. All eight destinations match in synthetic desktop/narrow browser checks; all 18 design route canaries pass. The 109 shell/design tests and final 271 design/registry/conformance tests pass. Changed static checks pass. The frontend checkpoint is committed locally at `f58cb536d`. The next unit removes the approved legacy company feed/page and their unused projection fields. It removes six data classes and three assembly procedures, with 440 net production lines removed. Independent Sol scored its corrected PRD 92 and implementation 96. The retained Holding helpers and shared styles are unchanged; the feed returns 410 with a Company Desk link. Owning tests, static checks and evidence bindings are recorded in the implementation checkpoint. No production data or service change is part of either cut. These are local checkpoints; the combined roadmap and release gates remain open. Require independent Sol scores above 85 for each concrete PRD and implementation. Existing approved retirements stand. Keep core analysis and history; ask only for a genuine feature-use or policy choice. Execution remains with the inexpensive owner-selected OpenRouter model.

The next test-suite cut replaces the fragile real-tree list of test-only dynamic edges with a short production-closure check and focused synthetic cases. Independent Sol revised the initial 72-point proposal to a 94-point bounded plan and scored the implementation 94. The cut removes 151 net test lines. All eight existing core assertions and 59 other definitions remain unchanged. Three raw-edge payload cases and two orphan/reached classification cases retain the unique coverage. No scanner or production allowance changes. The owning acceptance receipt records the final module and source-binding checks. The census checkpoint is committed locally at `8b0fb7bce`. The following setup tranche removes 60 redundant import-path calls from 57 files, for 243 net lines. All 607 module definitions and retained import order stay unchanged. The 24 skipped candidates need changes outside this bounded scope. Fresh-process collection probes show identical test IDs, module origins and namespace paths for every changed file; the selected population has 498 tests. Independent Sol scored the PRD 92 and implementation 94. The owning acceptance receipt records terminal suite and source-binding checks. No setup framework, dependency or application behavior is added.

The next setup deletion removes three unused database bootstrap helpers and their three copied table-definition blocks. Existing builders already use the shared `migrated_db` fixture. The final cut removes 199 net test lines and 6,951 bytes, including obsolete prose. All surviving executable module ASTs, imports, builders, decorators and assertions remain unchanged. Independent Sol scored the PRD 95 and implementation 93. The three debt-bearing candidates stay deferred. Final tests and source bindings are recorded in the owning acceptance receipt. This is maintenance cleanup, with no runtime speed claim.

A further sandbox follow-through deletes the orphaned 943-line `viewspec.latency_benchmark` module and its two architecture exceptions. The component supplied a dormant rerun/pickle latency harness and a synthetic database builder; no active CLI, scheduler, reconstruction entry or application caller remains. The complete cut removes 955 net lines and 34,214 bytes. All other test ASTs and debt entries are unchanged. Independent Sol scored the PRD 94 and implementation 96; all 107 selected architecture/ViewSpec tests and changed static checks pass. This retires that unique dormant measurement workload. Retained benchmarks measure different workloads and do not replace its performance evidence. No application speed claim or live operation is part of this cut. Final source and dependency evidence is recorded in the owning acceptance receipt.

A bounded ETF ingestion change now removes the adapter's competing stable/legacy URL ladder and duplicate tier setting. It delegates to the existing FMP client with a symbol query parameter. Numeric alias selection preserves zero and rejects bool, nonfinite and overflowing values. Sol scored the PRD 93 and implementation 95; 66 selected ETF/transport tests pass, while the new tests detect 12 relevant failures on the old source. All existing test definitions and the AUM conversion function remain unchanged. The application adds 10 physical lines and the regression module adds 116; this is a reduction in competing request paths, not a net source-line cut. Existing AUM unit/currency assumptions and ETF role grounding stay open. No provider calls, new collection or live operations were performed.

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

### R4 — Explicit research procedures with one evidence interface

**2026-10-24 owner decision:** retain summary-to-KPI generation as a backend
procedure triggered on command. The earlier full retirement proposal is superseded.
Its estimated 1,450–1,600-line cut is withdrawn, not credited as achieved savings.
This preserves configured extraction and novel discovery, including explicit reruns
of saved summaries. The backend CLI, source modes and provenance guards remain.

**Cut:** remove summary KPI extraction from routine refresh defaults and both
automatic comment-rebuild branches. Keep definition seeding, thesis evaluation,
report generation and narrative products. An explicit extraction request uses the
existing command dispatcher and job owner. No replacement workflow engine, evidence
store, execution catalog or implicit collection path is needed.

Omitted refresh steps retain their documented default behavior, except summary KPI
extraction. An explicit null, empty, blank, non-list or unknown HTTP selection fails before
job dispatch.
A selection that excludes every step performs no work; it must never fall back to
source collection. Direct CLI invocation and a valid explicit extraction selection
remain callable. Preserve explicit source/state/database arguments and failures.

**Interface MVP:** use Explore for open-ended grounded questions and its existing
Analytics workbench. Add a compact map to saved PRE/POST and research reports and
the available manual procedure. Keep the question central. Use existing navigation,
registered controls, the job lifecycle and the existing evidence resolver. Label
summary-derived output as candidates requiring primary review. Job acceptance is
not completion, financial admission, source completeness or a decision-grade result.
The existing refresh extraction step supports configured earnings extraction only;
IR and novel modes remain available through the retained backend CLI unless their
interface adapters are separately implemented and tested. Do not imply broader UI
coverage. Missing evidence stays explicit; new collection still needs its approved
scope. No typed question silently launches extraction or acquisition.

**Preservation references:** use corrected October 23 NU PRE/POST and expanded
MELI packets. Their manifest/body/context identities are pinned by the held
reconciliation. This selects engineering outputs to preserve; it does not establish
financial acceptance, decision-grade coverage, automated extraction recall or
missing original-source lineage. Historical raw-summary replay gaps remain separate.

**MVP/proof:** automatic refresh and comment paths make zero summary-extraction
calls; explicit commands retain configured/novel source behavior and quarantine.
Prove invalid selections start no job or collection, retained child order/failures,
saved candidate/review/correction/history parity and unchanged selected reference
bytes. Observe the interface before/after at supported widths, including empty,
loading, accepted-job, conflict and error states. Ask, Analytics and saved report
navigation must reuse their existing owners. Measure actual source cuts after the
patch; retained capability receives no deletion credit.

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

**Cut:** move panel loading and enrichment out of `workspace_html`/`workspace_data`
into existing section/build owners. Replace Say/Do and news data → Markdown → data
round trips with structured section output. Remove superseded render-time parsers,
filter/cache machinery and independent context assembly for migrated sections.

Preserve specialized pre/post research, full narrative, source links and exports.
Any LLM filtering/selection remains a qualified analytical operation with source,
owner-context, cost and verification ownership; do not silently delete judgment.
Historical artifacts retain reconstruction support. Extend existing report section
models; avoid a parallel presentation fact database.

**First section:** `SayDoSection`, retaining `SayDoCard` narrative and reusing or
extending its existing structured rows. Do not assume `SayDoHistoricalMetric` alone
covers every row currently selected from narrative. Prepare the complete supported
population and owner-context selection before removing its Markdown parser/filter.
**MVP/proof:** that section supports compact/full/evidence/export views with the
same values, strategic selection, complete argument and source links. Rendering
performs zero database/model work. Preserve invalidation on thesis/context/source
changes and repair/recovery; qualify prompt/schema changes through existing LLM
ops and financial-grounding gates. Demonstrate the result in the browser.
The existing durable Copilot handoff stays; no duplicate chat system is proposed.

**Terminal population:** Say/Do; Recent Developments/news tiles; and the current
workspace P3 load population: macro sensitivities, strategic targets, customer
concentration, leases, decision history, peer comparison, open notes, position
review counts, standing rules and comparable-set context. Move each section's
loading/analysis to its existing build owner, retaining availability and provenance.
Also include graded-sell base rate, the you-said strip and comment boot data.
The terminal inventory is **all database, file, model and cache work reachable from
`render_report_body`**, not only this named starting list. Trace callees and assign
each read to its existing preparation owner. An interactive comment/session boot
read may belong to delivery composition rather than analysis; record its exact
purpose and move it outside the pure render call with prepared input. That exception
cannot hide analytical loading or claim that the end-to-end delivery performs no IO.
Full R7 closes when these renderers perform no database, file, model or cache work
and new Say/Do/news generation supplies structured data directly. Delete the
render-time loaders/parsers/context assembly and redundant selection caches after
their callers move. Preserve purpose-specific analytical calculations and formatting.

The first Say/Do checkpoint only prepares rows and selection before rendering; new
generation still emits Markdown and the existing cache remains. It is a partial
cut. Historical Markdown import remains a bounded compatibility adapter with no
model call; it cannot qualify the new-generation round-trip retirement. Qualify
structured generation and exact source/context/prompt/model cache identity through
the existing section/artifact owners before deleting that import path for new work.
Do not replace the cache merely during the first move or charge for regeneration
to make historical artifacts fit a new schema.

## Delivery order, subtraction and scope controls

Gross baseline body sizes bound the candidate cuts below. These are AST physical
line spans at c978523d (comments/docstrings included), not scanner noncomment LOC,
net savings or promises to delete retained functionality. Moved bodies earn no net
reduction credit; later wave accounting records actual deletion minus additions.

| Unit | Named baseline procedures / gross body span |
|---|---|
| R1 | `_run_extraction`, `_safe_extract`, `_stage_extract_fmp_facts`: 3 bodies, 104 lines; duplicated 11-entry dispatch and 2 overrides counted separately as ownership, not extra measured lines. |
| R2 | `_run_categorize` and `process_ticker`: 2 mixed adapter/orchestration bodies, 112 lines. Retain needed transport/input adapters; only obsolete registration decisions count as cuts. |
| R3 | State adapter: 91-line file; four index processing procedures: 51 body lines. Replacement completion code offsets these gross spans. |
| R4 | Automatic summary-KPI refresh membership and two comment-rebuild spawn blocks. Retain `extract_for_ticker`, `capture_for_ticker` and their CLI under the October 24 owner decision; their bodies no longer count as planned deletions. |
| R5 | `ingest_spreadsheet_kpis`: 79-line first-producer span. The separately measured terminal inventory identifies 10 disjoint financial/KPI write/bridge bodies totaling 1,645 baseline body lines, including 878 shared financial/KPI lines. Shared bodies earn deletion credit only after all writer and historical-reader obligations clear. |
| R6 | `_BatchFactReadModel`: 38-line class, plus the named consumer loops/cache whose separate deletable spans need cutover accounting. |
| R7 | Say/Do parser and filter: 2 bodies, 109 lines. Moving them to preparation earns zero net credit; structured generation and remaining render-path retirement must remove the round trip. |

The [terminal cut inventory](../quality/backend-ingestion-retirement-inventory.md#measured-terminal-cut-population) uses
body-only AST intervals, excluding signatures, so its 1,645-line R5 subtotal is
not added to the table's first-producer span. Its earlier 888-line R4 retirement
population is historical and superseded by the October 24 owner decision. Retained
summary jobs, helpers and CLI earn no deletion credit. Gross spans are not net promises.

- **Wave 1:** R1 FMP engine consolidation and R6 read batching, as separate reviewable
  changes. These are structural cuts without waiting for native table retirement.
- **Wave 2:** one R2 managed-IR family and R3 explicit-state/completion consolidation;
  qualify one R7 typed-section frontend MVP alongside them. Make R4 extraction
  explicit-only and expose its scope alongside existing reports, Ask and Analytics.
- **Wave 3:** consolidate R5 publication one qualified producer/class at a time.
  Retain original-source extraction and optional summary candidate discovery under
  distinct admission rules. Do not require a whole-platform cutover.

After Wave 1, show a cumulative before/after owner map and open feature-pruning
assessment in parallel with later qualified cuts. Record each candidate's core-task
use, actual route/scheduler/import dependencies, retained data and replacement.
Bring concrete feature-removal choices to the owner; static non-reachability alone
does not prove operational non-use. After an approved cut, remove its UI, route,
backend and redundant tests together. Preserve unique invariant coverage.

Seed that assessment with these existing operator surfaces, not analytical content:

| Candidate | Evidence needed for an owner choice | Replacement or retained capability |
|---|---|---|
| Separate `fetch_ir_documents.py` and `fetch_ir_from_urls.py` CLI surfaces alongside `manage_issuer_document_sources.py` | Actual scheduled, script and owner invocations; argument/FYE/request-mode parity; denial and recovery behavior | One managed command with discovered/explicit-input adapters. Keep a thin old command if it has a distinct supported use; delete duplicate orchestration regardless. |
| `process_ir_documents --regenerate-missing` as an independent completion/repair mode | Real operator use and callers; legacy-adoption and missing-projection recovery results | Existing artifact/attempt repair through the same processing owner, with zero regeneration for a committed body. Retain the user operation if needed, remove its file-existence success engine. |
| Automatic summary-number refresh/comment actions | Owner approved on-command behavior October 24; prove zero automatic calls and explicit command parity | Keep the backend CLI, configured/novel extraction, saved candidates and review. Remove automatic coupling; expose truthful scope through Explore. |

These are assessment candidates, not approved feature deletions. If none removes
real user friction or redundant ownership, retain the interfaces and still execute
the named backend lane cuts. No analytical panel or depth is sacrificed to meet a
source-size target. Physical financial-history deletion remains under §11 and its
separate approval; it is not the mechanism for this roadmap's reduction.

Each change records old procedure → owner, actual callers, old definitions removed,
new abstractions added, and the remaining compatibility path with an explicit removal
condition. Maintain one cumulative acquisition/processing/publication/read owner map
for migrated source classes. Do not preserve the old active engine behind a feature
flag after parity.
For each wave, record before/after counts of owning procedures and dual authorities,
including every addition as well as deletion, plus exact and near-miss duplicate
scanner totals. Reuse the existing scanners with unchanged scope, rules and identity;
do not make a changed measurement definition look like a reduction.
Keep a code revert for code-only changes; persistent cutovers require bounded
migration, reconciliation and tested rollback without discarding immutable history.
CLI/process isolation remains where deadlines, native libraries or writer ownership
require it; remove duplicated business logic, not purposeful process boundaries.

First slices are milestones, not terminal acceptance. The per-unit populations above
must have explicit remaining entries until their old procedures are deleted. Do
not close the roadmap after its easy adapters/helpers move while automatic R4
coupling or competing completion/publication owners remain. The optional summary
candidate procedure is intentionally retained. Missing source pairs still limit
financial/native qualification; they do not authorize retiring that capability.
Where the lossless R5 capture gate blocks a deletion, bring the exact decision to
the owner and continue another qualified population. Do not add an alternate
engine to report progress.

The proposed structural exception is reviewed at the next producer-retirement
checkpoint or **2026-10-30 Pacific**, whichever comes first. Its checkpoint receipt
records the candidate +1,047 source lines and one additional >2,000-line module;
these are historical candidate measurements, not permission to absorb later growth.
At review, measure the current tree, report actual procedure deletions and remaining
dual authorities, and record one owner disposition: close the exception with
noncomment source LOC **≤ 562,282** and modules over 2,000 noncomment LOC **≤ 24**;
otherwise explicitly re-approve the exact remaining delta with a named
next deletion and review date; or choose a bounded code rollback/re-scope plan.
No silent extension or automatic baseline refresh is allowed. An overdue review
remains an open acceptance obligation. The date does not revoke existing code-work
authorization, trigger automatic rollback/data deletion, start a scheduler or expand
model/collection authority. Preserve unique history and recovery in any chosen plan.
The exception's ≤ thresholds restore the original size limits; terminal < thresholds
require an actual net cut after all named lane retirements. That distinction is deliberate.

## Bounded acceptance scorecard

| Change | Completion condition |
|---|---|
| R1 ownership | Two active batch engines become one: one dispatch/override registry, selection/order policy and executor. Delete the other active implementations and migrate callers. Test malformed income then valid segments: affected prerequisite-unavailable coordinates are blocked/degraded, never OK; independent periods and legitimate zeros stay usable. Audit extractor/receipt commit closures and preserve caller-pending work. |
| R2 registration | For each supported family/media/calendar/input cell, move `process_ticker`, `register_urls` and applicable local-input callers to one managed registration owner; delete that cell's sidecar/categorizer success branch. Test denied automatic requested=False, real FYE, existing/changed bytes, partial commit, transport gaps, projection/effect recovery, foreign aliases and corrupt-index HOLD. Full closure requires the complete supported population, not the first calendar PDF cell. |
| R3 adapter/completion | First delete `process_ir_documents_state.py` and move discovery/onboarding/refresh callers with explicit DB/state parity. Separately migrate quarter/event processing selectors and repair/history consumers; then delete processed-flag/file-existence completion decisions. Adopt legacy bytes as unknown-provenance history with zero model calls and no exact-key reuse. Prove same-quarter identity, stale readability, atomic output/attempt persistence and zero-call replay/repair before completion cutover. |
| R4 explicit procedures | Routine refresh/comment paths make zero summary-KPI calls. Valid explicit CLI/step requests preserve configured/novel extraction and quarantine; empty/unknown selections start no default collection. Keep saved history and narrative producers. Explore maps existing reports, manual procedures, Analytics and grounded Ask without a new execution or evidence owner. Verify real interaction, failure states and actual source cuts. |
| R5 financial/KPI publication | Qualify every named financial and KPI producer through existing native publication/admission/resolution, with source/context, unknown/rejected capture, historical selection, correction, replay and transaction recovery parity. Full active-lane closure requires zero inventoried legacy product writes and zero product-time bridge population. Delete producer-specific legacy sequencing; delete the named shared bridge/resolution bodies only after their historical consumers qualify. Retained historical compatibility earns no deletion credit; §11 physical retirement remains separate. |
| R7 prepared analysis | Close the full IO inventory reachable from `render_report_body`, including Say/Do, news, P3, graded-sell, you-said and comment boot data. Move calls to preparation or explicitly bounded delivery composition; delete render-time IO/context/filter procedures. A full-workspace renderer test must forbid DB/file/model/cache access, not just test one section. Prove full argument, selection, availability and citations across supported views; qualify structured new generation before claiming round-trip retirement. |
| R6 reported populations | On the exact WIX-21 and SYNTH-8 populations in `.tmp/reader-reuse-comparison.json`, direct consumer rereads become zero and full-publication verification becomes once per distinct complete verifier-input key inside the operation. Keys include publication, semantic clock inputs and snapshot; K/O partitions remain distinct. Require at least 20% fewer SELECTs on each reported population. |
| R6 derivations/failures | Count all recursive verification, not only outer reader calls. State each required key and any remaining repeated verification explicitly. Qualify an available derived report value plus stale, missing, corrupt and explicit-O cases before closing the unit. A required extra verification is an explicit reviewed exception, not a hidden exclusion from the count. |
| R6 terminal ownership | Reconcile every actual scalar/batch/bundle/verifier caller against the named R6 inventory. Remove `_BatchFactReadModel`, duplicate snapshot/cache ownership, discovery/report scalar rereads and the named series selection/bundle cache loops; prove DCF/strict-adapter parity through the same owner. Keep R6 terminal status open until all inventoried callers qualify; WIX/SYNTH checkpoint performance alone cannot close it. |
| Latency/setup | Use balanced paired operation timing, excluding instrumentation/setup, with at least five samples per side. Median operation latency must not regress by more than max(10%, 5 ms); one balanced repeat may resolve noise. Record cold/warm focused-test setup separately, including migrated-template cost. These small synthetic samples establish no production speedup. |
| Net complexity | Record removed/added owning procedures, public operations, independent write/completion decisions, stores and compatibility paths. At least one old active engine/lane must be gone; moving copies behind a facade fails. No extra independent state/authority is accepted. Review any module/LOC ratchet change explicitly; do not loosen a scanner to pass. |
| Terminal R1–R7 net reduction | Close every retained terminal population and its named old procedures, excluding the owner-retained R4 candidate engine, then measure noncomment source LOC **< 562,282** and near-miss duplicated LOC **< 9,753** against the retained original baselines. Report exact-duplicate totals and per-wave owner/dual-authority counts too. These are necessary net-cut conditions, not a claim of substantial reduction by themselves: the specified whole-lane retirements and retained analytical-depth gates must also pass. No arbitrary percentage, scanner loosening or physical-history deletion substitutes for them. |

The numeric SELECT threshold is an implementation acceptance target, not an achieved
result. If it proves incompatible with required integrity work, show the measured
tradeoff and revise the acceptance decision explicitly. Do not weaken checks or
count a helper-only change as the completed batch retirement.

## Retained core-task acceptance

Use these bounded cases with the relevant cuts and before feature pruning. They
test one shared evidence flow; they are not a new test framework. For every affected
path compare issuer, period/unit/basis, selected observation, cutoff, source links,
missing/degraded states and retained analytical content. Fixtures remain hermetic.

| Task/reference | Required retained result |
|---|---|
| NU PRE: `report_NU_2026-10-20_6c613e17e0dfc741faa5` | Pre-event expectations, watch items, thesis conditions and supporting history use the exact pre-event scope/cutoffs; later evidence cannot leak in. |
| NU POST: `report_NU_2026-10-20_9998561c61f3404e62c9` | Actual changes versus the prior setup, surprises and thesis implications retain exact source/context links and separate physical/knowledge clocks. |
| MELI deep research: `report_MELI_2026-10-20_169e43887147a28df9e8` | Retain full thesis/counter-case, assumptions, scenarios and sensitivities. Recalculate the workbook and compare relevant formulas/results with the cited inputs. Compact presentation preserves access to this depth. |
| Research/Q&A from a selected report | Carry its immutable report identity, issuer, period, facts and citations into the answer context. Deeper work uses the held corpus; missing evidence produces an explicit gap/collection plan. No implicit collection or unrelated latest-report substitution. |
| Portfolio/threshold decision | Use existing approved snapshot or synthetic holdings/threshold fixtures. Shared facts/calculations must produce the same supported allocation/hold/trim conditions, preserve assumptions and evidence, and expose unavailable inputs. No trade or live state action. |

The retained NU/MELI artifacts are depth/comparison references, not accepted truth
oracles. Their bytes were reconciled, but native and semantic acceptance remains
unproved. Correctness comes from verified sources, formulas and applicable review.
Record unexecuted checks as pending. A section-only render or identical artifact
bytes cannot establish the broader product endpoint.

Consolidate fixtures/setup with their owner change. Preserve unique financial,
corruption, clocks, admission and recovery assertions. Use existing migrated_db;
no new fixture framework or schema rebuilt per test. Run focused serial tests and
applicable static, architecture, reachability and make check-fast gates. The earlier
full-suite teardown failure was repaired and verified at a prior frozen checkpoint.
The later 22,282-test pass binds `17cfdd5f`, not the current cleanup source. The
current architecture ratchet still fails at 26 modules above 2,000 noncomment lines
against its limit of 25. `pipeline/managed_ir_sources.py` is the new crossing
relative to that baseline; moving code between files would not prove simplification.
Root must complete the applicable integrated gate before release. Focused passing
checks permit bounded implementation evidence, not an exception to that gate.

For a blocked unit, name the exact missing capability and move to another qualified
unit. After one bounded feasibility decision, bring the unresolved choice to the
owner rather than repeat audits or add guards. Feature pruning can proceed from
actual usage and core-task coverage after initial structural cuts; it does not wait
for every producer migration or physical table deletion.

No live state changes, collection, application model calls, deployment or physical
retirement are authorized by this proposal. Legacy fact-plane physical retirement
retains the §11 observation windows and separate final owner approval. Existing
source artifacts, unknown/novel observations, histories and financial meanings stay.

## Evidence and current state

[Procedure inventory](../quality/backend-ingestion-retirement-inventory.md) records
source callers and bounded retirement candidates. Existing test references are not
new test passes. [Reader evidence](../quality/reader-initialization-evidence.md) and
[spreadsheet audit](../quality/spreadsheet-publication-parity-audit.md) retain their
precise proof limits. NU/MELI artifact byte reconciliation is not native/semantic
acceptance. Report further parity only after it is actually executed.

Groundwork before this execution included shared fiscal calendars, removal of two
parser helpers, one summary database authority, and source-aware completion and
correction handling. The first implementation checkpoint covers R1 consolidation
and bounded R2/R3/R5/R6/R7 slices; R1 dependency truth and R6 terminal ownership remain
open until the specified proofs and cuts qualify. Its receipt owns validation and review
status. R4 and the unqualified parts remain open. No overall deletion percentage
or production speedup is claimed. The three-Sol panel's original scores
were ambition 91/92/91 and effectiveness 87/88/84, all REVISE. This revision addresses
their required plan changes; that is a root disposition, not a new panel verdict.
Those historical judgments do not establish the current proxy acceptance gate or
replace deterministic implementation checks.
