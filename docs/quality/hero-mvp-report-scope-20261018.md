# Complete report evidence qualification — October 18, 2026

> Status: historical record. Where it conflicts with [the live integration plan](hero-mvp-live-integration-plan-20261020.md) (2026-10-20), the plan governs.

The owner approved complete required-report evidence as the first acceptance
boundary for NU Q2 2026 historical pre/post reports and the integrated MELI brief,
DCF and recalculated workbook. Full filing-package/native-XBRL processing remains
a separate deferred completeness target. This does not reduce factual accuracy
or permit a missing critical report topic. Production writes and deployment are
outside this implementation phase.

## Why the MELI dependency list contained 213 files

Retained SEC package-index and filing-manifest metadata identify these additional
files for the two already captured primary financial filings:

| Category | Q2 2026 | FY 2025 | Purpose |
|---|---:|---:|---|
| SEC rendered table pages (`R*.htm`) | 73 | 122 | Individual statements and notes |
| XBRL instances | 1 | 1 | Tagged facts and contexts |
| XBRL schemas and linkbases | 5 | 5 | Definitions, labels, dimensions, presentation and calculations |
| Filing indexes and metadata | 2 | 2 | Table/tag navigation |
| XBRL ZIP archives | 1 | 1 | Packaged XBRL files, overlapping individual downloads |
| Total | 82 | 131 | 213 files; 30,303,549 declared bytes |

This was a full-package capture proposal, not a demonstrated minimum for the
investment brief. The bulk extension was not approved. No additional source files
were collected. The approved scope first uses the existing primary filings and
retained issuer evidence. A missing critical source remains a blocker; collecting
it needs the existing source/period/effort authority.

## Primary filing first

Use the original HTML/Inline XBRL filing as the main parsing source. Retain its
exact bytes once. Build local section, table, fact and citation indexes from it.
The retained two MELI primary filings already yielded 28 candidate financial
roles without the 195 SEC-rendered table pages. These are source candidates,
not admitted financial facts or proof that the entire report is covered.

The SEC also provides a complete-submission text container. Its submitted
documents can be indexed and separated locally. The filing manifest identifies
separate exhibits; a primary HTML file need not contain every material exhibit.
Capture a genuinely needed separate exhibit or taxonomy dependency once, and
reuse it. Cache taxonomy versions. Do not require every generated `R*.htm` page
or both individual XBRL files and their overlapping ZIP archive. Rendered tables
are optional diagnostic views, not additional independent evidence.

Robustness requires context, definitions, reconciliations, immutable versions,
source locators and visible gaps. All brief and model consumers must use the
same admitted observations. A file count is not a financial completeness test.

## Implemented boundaries

- The optional closed `complete_report_evidence/v1` request creates a distinct v3
  Analysis Evidence Scope. Existing unprofiled v1/v2 serialized identities remain.
  Every selected primary, amendment and original source document must be captured,
  including original 6-K subjects and covers after subject classification.
  The profile has no implicit nonmaterial-exhibit exemption. An exhibit retains
  its original unknown period; it does not inherit a fabricated primary period.
  Deferred package-child gaps remain in the same inventory and coverage records.
- Scoped processing requires its native text and applicable non-XBRL duties.
  Its receipt explicitly names the profile and deferred native-XBRL lane. Default
  processing continues to require XBRL. Deferral creates no successful or
  not-applicable XBRL disposition and does not change existing native obligations.
- The existing MELI source publishers accept an exact scope receipt and can load
  its exact verified documents before child capture for this profile. The public
  loader checks the inventory, accession, native selection and exact versions;
  it has no unbound primary-only flag. Source extraction,
  byte replay, definition, publication and semantic admission owners remain.
- Financial scope closure verifies native resolution, every required candidate
  publication and all its observations, including dissent and unrelated members.
  Every reported leaf and recursive derived operand must belong to the selected
  exact document versions and subjects. This proof is mandatory for historical
  reconstruction and the new bounded profile, including custom-verifier paths.
- The MELI memo has a fixed ten-topic critical evidence population, separate from
  the model's existing exact input recipe. Each topic maps to reviewed and fully
  verified reader blocks with a distinct grounded witness per topic. Scenario
  and sensitivity topics also require verified model values and a same-ticker
  native commitment. Missing or tampered declarations block acceptance.
  This mapping is an analyst coverage assertion; a numerical checksum cannot
  certify the substantive adequacy of a business argument.
- The existing read-only memo verifier can qualify bounded report evidence and
  retain its full coverage mapping and hash. It records joint MVP status as
  `not_verified` while workbook parity remains unavailable. This does not turn
  evidence qualification into successful CLI delivery or integrated acceptance.
  It remains `decision_grade=False`; it does not claim whole-issuer completeness.
  Historical NU stays degraded until its native pre/post critical-evidence
  recipe exists. Primary-company evidence is required for MELI topic witnesses;
  another issuer's supporting context cannot supply the sole grounding.

## Limits and remaining acceptance work

This code does not itself deliver accepted financial reports. Actual source
intake, context/definition/role admission, canonical snapshots, complete narrative
and scenario review, and the editable recalculated workbook still need their
native end-to-end evidence. No test fixture or advisory score substitutes for it.
The report's material-exhibit review and qualification on a representative shared
fact plane remain unproved. Deferred child coverage alone cannot establish that
every material source is represented. A filtered fact store must not be used to
hide older or outside-scope candidates from canonical resolution.

The strict fact reader currently uses a single cutoff K for both knowledge and
recording. Scoped closure therefore requires all fact and operand admissions and
seals by K. Evidence recorded between K and later observation cutoff O holds
conservatively. The change does not add a second fact plane or resolution policy.

MELI scenario displays still need the existing finite memo model-evidence owner
extended beyond ONON. Unqualified arbitrary model paths remain unsupported.
Historical reports always retain a bounded label and `decision_grade=False`,
including an unprofiled v2 reconstruction. Its identity remains reconstructable;
this explicitly changes its financial readiness behavior from the prior hold.

## Architecture disposition

The original gate rejected the added code. The reviewed budget increases by
658 measured noncomment lines and one executable module. The new module owns
the versioned report evidence and readiness contract; native assessment remains
in the existing brief owner. A preliminary import cycle and large-module
increase were removed. The two MELI CLI adapters now share request/DB-role
mechanics. No duplication, dependency-cycle, fan-out or large-module cap rises.

SEC acceptance timestamps are filing-event evidence. The SEC states that public
dissemination can follow later; these timestamps must not be presented as exact
public-availability witnesses. V3 reasons name `sec_accepted_at`. Legacy v2 reason
bytes remain reconstructable. Intraday public-availability acceptance needs its
own evidence; document-source closure alone does not supply it.

## Review and validation status

The latest Mac CLI review used `claude-opus-5-5` through the authenticated
subscription transport with no tools. It scored the then-current enabler patch
66/100 and held acceptance. Later local corrections address original 6-K
capture, unprofiled version pinning, missing NU readiness recipes, primary-company
topic grounding and contradictory decision-grade metadata. The corrected
combined regression run passes 259 tests. These corrections have no new Judge
score and do not establish report acceptance.
The final `make check-fast BASE=a78f157f` passes architecture, format, lint,
strict types, suppressions and 150 changed tests. Full CI and end-to-end
financial qualification remain pending. Completed synthetic fixture databases
were retired; red/green logs and review receipts remain under the task root.

The remaining design blocker is the boundary between an issuer-wide canonical
resolution and an analysis-specific source set. Older/outside candidates must
remain explicit in the canonical plane. Requiring every observation in every
candidate publication to originate from the selected report documents predicts
false holds. A new scope must not silently hide dissent or create a filtered fact
store. Resolve and prove this boundary on a representative shared plane before
using this patch for report acceptance. Material exhibit research promotion,
NU recipes, MELI scenario/sensitivity semantics and native workbook parity also
remain incomplete.

The review's nullable-publication question has a local answer:
`canonical_fact_resolution._enumerate` marks missing-publication candidates
ineligible with `missing_sealed_source_publication`; stale derived operands also
become ineligible. They remain in the sealed candidate population. This is not
authority to treat them as admitted report evidence.

Source publications retain exact document, extractor and recipe lineage. They
describe a source fact population, not whole-filing acquisition completeness.
The analysis profile does not redefine those financial facts; analysis and
processing receipts separately own the selected source set and deferred duties.

This work remains local and uncommitted on `codex/hero-report-scope-20261018`.
No new source acquisition, installation, production write or deployment occurred.

Execution update, 2026-10-19: the new, unmerged report profile includes original
SEC exhibits from each selected accession in its research document population.
These exhibits require exact capture and qualified text processing. Generated
financial views and supporting dependencies remain visible and deferred. This
lets the MELI brief and model cite the underwriting agreement in the same corpus.
Earlier experimental v3 receipts must be rebuilt. They were not qualified or
released. Strict unprofiled selection and explicitly pinned historical selection
keep their prior rules.

The MELI financing bridge uses a separate v5 input recipe. It preserves the 28
reported baseline inputs and adds reported principal and the underwriter purchase
fraction. The contract date and closing date remain separate. The model calculates
proceeds before other expenses. An explicit analyst expense reserve is required.
This bridge does not claim a complete current cash balance.

## Owner clarification: depth follows the decision — October 20, 2026

The owner now emphasizes an 80/20 starting point: directionally sound financial
reasoning, cited core figures, explicit assumptions and uncertainty, followed by
more depth when the decision warrants it. The shared research method owns this
behavior across companies and report types. Its existing adapter loads the source
and includes its hash in prompt/cache identity; no new stock-specific workflow,
schema or database is needed.

For the current MVP, correct tax-versus-operating interpretation, period/basis
comparability, unsupported price comparisons and decisive valuation assumptions.
Retain nonmaterial completeness and optional execution-environment gaps as visible
limits. An unresolved potentially decisive issue conditions the affected conclusion.
It does not invalidate unrelated verified findings. The existing scoped report,
model and bundled-engine workbook proofs remain evidence at their stated scope;
legacy `joint_mvp_status=not_verified` and `decision_grade=False` are not rewritten.
They do not establish that supported bounded research is unusable. The proposed
additional MELI-only native joint-gate module is deferred.

This clarification does not permit incorrect reported facts, unsupported derived
numbers, unmarked assumptions, hidden basis changes or falsely definitive advice.
Existing capital-allocation and decision-grade requirements remain unchanged.

An intrinsic-value analyst memo can omit the market quote when both the price and
its timestamp are absent. The shared readiness reader retains `market_status=missing`.
It still replays financial inputs, source evidence, assumptions and scenarios.
Allocation readiness continues to require a current quote. A partially supplied,
invalid, future-dated or stale quote still blocks either use. This permits a cited
valuation range without claiming current upside or an allocation recommendation.
