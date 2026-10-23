# Research platform: backend and ingestion retirement roadmap

Status: **Expanded proposal; implementation held for independent review.**
Source baseline: `d19bc3e38dc5c13f27d78555ae69947bc2d9aee9`.
This version supersedes the helper-first sequence. Three fresh GPT-6.1 Sol judges
will independently assess ambition and effectiveness. Opus 5.5 must then score the
roadmap and each key finished implementation strictly above 90 with no blockers.
The owner chose subscription-only review after its reset; no paid fallback.

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

## Seven retirement units

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

First retain existing per-document commit boundaries. Roll back pending failed
work before committing failure accounting; preserve earlier completed documents.
A fatal database/schema failure stops dependent work. Qualify the exact extractor
commit paths before promising atomicity. `refresh_cache` uses supplied immutable
records and `commit=False`; preserve that caller-owned transaction path rather than
route it through an independently committing batch.

**MVP/proof:** both production routes process the same synthetic documents with the
same facts/lineage; malformed input fails truthfully; revenue precedes segments;
replay and failure recovery preserve completed work. Replace the quarterly test's
139-line hand-built schema with the existing session-migrated fixture, preserving
behavioral assertions and explicit historical tests. No schema migration is needed.

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

### R3 — One explicit-state document-product executor and completion owner

**Cut:** give processing explicit state/database arguments and remove
`process_ir_documents_state.py` global-module rebinding/`sys.argv` machinery.
Then replace JSON-index `processed` flags and cache-file existence as processing
success authorities with existing exact source/product attempt and artifact receipts.
Move consumers before retiring `mark_document_processed`, `get_unprocessed_documents`,
`mark_event_processed` and `get_unprocessed_events`.

Keep deterministic source extraction separate from narrative synthesis. Reuse
`document_evidence_pipeline` fulltext only when source and extractor identity match.
Preserve quarter/event identity, aliases, file moves, anchors and missing-summary
repair. Raw narrative and analytical summaries remain different products. Existing
index readers and transcript acquisition are mapped before changing their state.

**MVP/proof:** explicit-state processing matches existing paths/results without
mutating module globals. A source change or missing artifact produces exact pending
work; unchanged completed inputs do not repeat LLM work. Remove the adapter first,
then the obsolete processing-state procedures when their callers are gone.

### R4 — Retire generated-summary-to-number extraction

**Cut:** move numeric extraction to original documents, then remove each migrated
class from summary-input `extract_for_ticker` and `capture_for_ticker`. Once all
classes and callers migrate, retire `extract_kpis_from_summaries.py` and its two
summary-input jobs. Keep the module's live direct-PDF/shared functionality and
retain narrative summaries and historical summary-derived observations.

Extend the existing direct-source extractor one class at a time. Its automated
PDF capture currently defaults to `ir_supplement`, caps inputs at 80,000 characters
and output at 200 facts, and does not prove transcript/other-IR parity. Manual
manifest support is not equivalent to automated coverage. Keep bounded partial and
missing-original states visible. Preserve configured KPIs plus novel/one-off
management observations, wording, speaker where applicable, scale and locators.
Do not collect missing originals implicitly or promote unclassified values.

**MVP/proof:** one original document class supplies at least the retained supported
numeric population, with exact original-source anchors and explicit exclusions.
Reroute refresh/comment callers and remove that class's summary-number procedure.
Measure model calls and latency; narrative output remains required, so total call
reduction is not assumed. Missing parity blocks that class, not other retirement units.

### R5 — One publication authority for each migrated producer

**Cut:** for a qualified spreadsheet producer, replace value-only parsing and
`ingest_spreadsheet_kpis` → `persist_manifest` → independent legacy restatement /
resolution → native bridge sequencing with one source-preserving publication
operation using existing locator/value types and `SourceFactRepository`.
Required legacy outputs become controlled compatibility projections, not a second
fact-selection or completion authority. Shared legacy owners remain for other
producers and historical reconstruction until their own migration qualifies.

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
Eliminate direct redundant consumer bundle fetches and measure batch/derivation
verification counts, SELECTs and latency. Share definition checks/metadata while
removing their consumer copies; those helpers support this retirement, not replace it.

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

**MVP/proof:** one prepared section supports compact/full/evidence views with the
same values and complete argument. Rendering that section performs no database or
model work. Demonstrate the result in the browser and retain export/source parity.
The existing durable Copilot handoff stays; no duplicate chat system is proposed.

## Delivery order, subtraction and scope controls

- **Wave 1:** R1 FMP engine consolidation and R6 read batching, as separate reviewable
  changes. These are structural cuts without waiting for native table retirement.
- **Wave 2:** one R2 managed-IR family and R3 explicit-state/completion consolidation;
  qualify one R7 typed-section frontend MVP alongside them.
- **Wave 3:** migrate R4 original-source numeric extraction and R5 publication one
  qualified producer/class at a time. Do not require a whole-platform cutover.

Each change records old procedure → owner, actual callers, old definitions removed,
new abstractions added, and the remaining compatibility path with an explicit removal
condition. Do not preserve the old active engine behind a feature flag after parity.
Keep a code revert for code-only changes; persistent cutovers require bounded
migration, reconciliation and tested rollback without discarding immutable history.
CLI/process isolation remains where deadlines, native libraries or writer ownership
require it; remove duplicated business logic, not purposeful process boundaries.

Consolidate fixtures/setup with their owner change. Preserve unique financial,
corruption, clocks, admission and recovery assertions. Use existing migrated_db;
no new fixture framework or schema rebuilt per test. Run focused serial tests and
applicable static, architecture, reachability and make check-fast gates. The known
full-suite teardown failure remains an unresolved release obligation.

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

Locally committed groundwork: shared fiscal calendars; two parser helpers removed;
one summary database authority; source-aware completion and correction handling.
None of the seven retirement units above is implemented. No overall deletion
percentage or production speedup is claimed. The three-Sol panel reviews this
proposal independently; its judgments supplement source evidence and do not replace
Opus sign-off or deterministic implementation gates.
