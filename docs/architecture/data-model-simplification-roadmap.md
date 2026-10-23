# Research platform: backend and ingestion retirement roadmap

Status: **Revised after three independent Sol reviews; Opus acceptance pending.**
Source baseline: `d19bc3e38dc5c13f27d78555ae69947bc2d9aee9`.
This version supersedes the helper-first sequence. Three fresh GPT-6.1 Sol judges
reviewed the previous version; all requested revision. Their original scores and
findings remain in the [panel receipt](../quality/backend-streamlining-panel-20261022.json).
This revision has not been independently regraded. Opus 5.5 must then score the
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

**MVP/proof:** first remove global mutation with exact path/anchor parity. The later
completion cut tests changed source/anchor/prompt/model, same-quarter documents,
missing/corrupt output, unavailable DB, output-commit interruption, projection
failure/repair and unchanged replay. Raw `ir_narrative` envelopes are a useful
source-population/projection pattern, not proof of generated-summary completion.
Only then remove the obsolete processing-state procedures and their callers.

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
Require an available derived-value result alongside unavailable/corrupt cases. Reuse
the existing positive derived-series case and real publication/binding builders for
the report case. The current ambiguous-coordinate report fixture remains a negative
case; never relabel it as usable or fabricate admission to fill this coverage gap.
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

## Delivery order, subtraction and scope controls

- **Wave 1:** R1 FMP engine consolidation and R6 read batching, as separate reviewable
  changes. These are structural cuts without waiting for native table retirement.
- **Wave 2:** one R2 managed-IR family and R3 explicit-state/completion consolidation;
  qualify one R7 typed-section frontend MVP alongside them. Make the R4 original-
  source coverage feasibility decision here; do not defer that major lane cut.
- **Wave 3:** migrate R4 original-source numeric extraction and R5 publication one
  qualified producer/class at a time. Do not require a whole-platform cutover.

Each change records old procedure → owner, actual callers, old definitions removed,
new abstractions added, and the remaining compatibility path with an explicit removal
condition. Maintain one cumulative acquisition/processing/publication/read owner map
for migrated source classes. Do not preserve the old active engine behind a feature
flag after parity.
Keep a code revert for code-only changes; persistent cutovers require bounded
migration, reconciliation and tested rollback without discarding immutable history.
CLI/process isolation remains where deadlines, native libraries or writer ownership
require it; remove duplicated business logic, not purposeful process boundaries.

## Bounded acceptance scorecard

| Change | Completion condition |
|---|---|
| R1 ownership | Two active batch engines become one: one dispatch/override registry, selection/order policy and executor. Delete the other active implementations and migrate actual callers. Exercise a valid record followed by malformed input; audit all selected extractor/receipt commit closures before claiming rollback. |
| R6 reported populations | On the exact WIX-21 and SYNTH-8 populations in `.tmp/reader-reuse-comparison.json`, direct consumer rereads become zero and full-publication verification becomes once per distinct complete verifier-input key inside the operation. Keys include publication, semantic clock inputs and snapshot; K/O partitions remain distinct. Require at least 20% fewer SELECTs on each reported population. |
| R6 derivations/failures | Count all recursive verification, not only outer reader calls. State each required key and any remaining repeated verification explicitly. Qualify an available derived report value plus stale, missing, corrupt and explicit-O cases before closing the unit. A required extra verification is an explicit reviewed exception, not a hidden exclusion from the count. |
| Latency/setup | Use balanced paired operation timing, excluding instrumentation/setup, with at least five samples per side. Median operation latency must not regress by more than max(10%, 5 ms); one balanced repeat may resolve noise. Record cold/warm focused-test setup separately, including migrated-template cost. These small synthetic samples establish no production speedup. |
| Net complexity | Record removed/added owning procedures, public operations, independent write/completion decisions, stores and compatibility paths. At least one old active engine/lane must be gone; moving copies behind a facade fails. No extra independent state/authority is accepted. Review any module/LOC ratchet change explicitly; do not loosen a scanner to pass. |

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
percentage or production speedup is claimed. The three-Sol panel's original scores
were ambition 91/92/91 and effectiveness 87/88/84, all REVISE. This revision addresses
their required plan changes; that is a root disposition, not a new panel verdict.
Their judgments do not replace Opus sign-off or deterministic implementation gates.
