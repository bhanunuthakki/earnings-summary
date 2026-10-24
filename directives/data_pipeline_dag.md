# Data Pipeline DAG

**Status**: Layer 1 baseline. Defines the 8 stages every per-task directive composes from. Immutable without explicit user authorization.

**Why this exists**: The pipeline as it stood was ad-hoc script-calls-script with `.tmp/` JSON as the only inter-stage contract. That doesn't scale to the FMP × SEC × IR × audio × manual matrix. Every per-task directive (`fetch_transcripts`, `fetch_ir_documents`, `quarterly_refresh`, etc.) is now expressed as a slice of these 8 stages with explicit per-stage status, contracts, and resumption.

## Progressive acquisition and proof policy (2026-09-19)

The owner-approved scope is automatic full source acquisition for portfolio,
evaluation, and watchlist companies. `src/pipeline/source_policy.py` owns executable
authorization. List priority may order work; it does not reduce evidence standards.
Corporate SEC, IR, and transcript lanes require a resolved active equity/ADR identity;
ETFs need their applicable fund sources, and unknown instrument identity needs repair.
The five-reported-quarter bound for IR/text transcripts, source authorization,
provider entitlement limits, and audio/webcast exclusion remain in force. This does
not expand expensive narrative, DCF, or LLM schedules. FMP recovery uses the same
roles at both service and database boundaries, ordering portfolio, evaluation,
watchlist, then permitted index screening. Automatic work records `requested=false`;
an explicit request records its actual invocation identity. A scheduler run ID
must not be fabricated into owner authorization to satisfy an obsolete role rule.

Apply scrutiny at the boundary that can establish the claimed property:

| Boundary | Required check | What may proceed |
|---|---|---|
| Discovery | Stored issuer/instrument/role, approved source, bounded period and request budget | Record candidate and explicit unavailable/denied outcomes; never claim archive completeness |
| Capture | Exact raw bytes, SHA-256/size, immutable location, source URL/time, document/observation version | Retain and register bytes atomically before interpretation; no semantic approval required |
| Deterministic extraction | Current extractor identity + exact input version; locators, output identity, supported format and failure disposition | Parse each available document independently; preserve raw management wording and novel observations |
| Financial admission | Issuer, period, unit/currency, scope/basis, definition revision, evidence locator and conflict/comparability disposition | Admit evidenced facts through the shared resolver; unresolved candidates remain explicit |
| Completeness and publication | Authoritative expected population, acquisition and extraction receipts for the stated scope, reader/reconstruction parity | Claim decision-grade only for the scope proved; expose all missing prerequisites |

A missing archive inventory must not prevent safe capture or deterministic parsing of
an available document. It does prevent an archive-complete or decision-grade claim.
A complete single document does not establish complete issuer coverage. Confidence
scores, a successful subprocess, a file count, and narrative caches are not proof seals.

Drain stored processing debt independently of whether discovery downloaded anything.
Select by immutable input version and extractor identity; retry scoped failures rather
than replaying successful LLM work. `execution/process_document_evidence.py` owns the
bounded deterministic capture/extraction plan and apply interface. It does not crawl,
run LLMs, admit financial meaning, or invent missing source inventories. Its explicit
cursor lets an operator advance past quarantine and retry those document IDs separately.
Existing source-inventory and document-processing owners retain completeness authority.

Structural/identity/schema failures require repair, not unchanged automatic retries.
Transient failures may retry within the owning source's budget; authentication denial
stops that provider. Independent items may proceed with a degraded batch result;
dependent stages cannot consume a failed prerequisite as success. Retry checkpoints
are bound to the intended database and retain failed item identities.

## Analysis-specific evidence selection (2026-10-02)

An [Analysis Evidence Scope](../DEFINITIONS.md#analysis-evidence-scope) selects the
evidence required for one analysis. It does not change source acquisition policy or
the eight pipeline stages. `src/provenance/analysis_scope.py` owns selection and
verification. The request declares issuer, purpose, required reporting-period ends,
knowledge cutoff, observation cutoff, and any additional accession packages with
reasons. The receipt binds the current complete authoritative SEC inventory and
its exact expected-document population. Latest reporting-period inclusion is the
default; missing or ambiguous required periods fail explicitly.

The same immutable receipt is an input to processing, corpus construction, and
research assembly. Processing and research retain it. The corpus key and selection
configuration digest bind it without storing the full receipt in the manifest.
Selected periodic packages include known amendments. Unknown
amendment periods block when the amendment could concern a declared base filing.
An amendment filed before every declared base filing remains explicitly outside.
Related current reports require explicit selection; unavailable original/amendment
linkage remains a failure, not an inferred relationship.

Capture must cover every selected package member. Primary filings become research
documents. Generated report pages and other selected children remain required
capture dependencies; they do not duplicate the primary research text. Applicable
processing lanes, including XBRL extraction, remain mandatory. Processing, corpus,
and research must agree on the exact primary document set.

### Complete report evidence profile (owner-approved 2026-10-18)

An explicit `AnalysisScopeRequest.readiness_profile=complete_report_evidence/v1`
selects the versioned v3 report boundary. This is an additive acceptance profile.
The default full-processing policy above remains unchanged. The report profile
requires every selected primary, including known amendments, and its complete
qualified text. Original SEC exhibits keep their capture requirement. This
profile has no implicit nonmaterial-exhibit exemption. Other package-child
capture coverage remains visible in the same scope; it is deferred, not complete.
Applicable native filing-XBRL processing is a
separate deferred duty. The processing receipt names that duty and its bounded
profile. No XBRL obligation becomes succeeded or not applicable through deferral.

This profile changes processing prerequisites, not financial truth. Source bytes,
identity, public and physical clocks, extraction, definitions, admission, conflicts,
comparability, canonical resolution and reader reconstruction remain mandatory.
Processing, corpus and financial research bind the same exact scope. A report
cannot obtain acceptance merely from all model slots or all rendered claims.
Its versioned required evidence population must also cover the complete report.
Missing critical evidence blocks acceptance. Full-output claim verification,
integrated model/scenario proof and recalculated workbook parity remain required.
A qualified result identifies its bounded report scope and cannot inherit a
whole-filing or whole-issuer decision-grade label.

Analysis Evidence Scope v7 can bind an explicit `report-pdf-table-scope/v1`
under the same report profile. This implements the approved report-first boundary;
it does not add a package-wide table requirement. The review commits every page,
the exact retained native artifact and raw PDF, and a source population for the
existing ten MELI report requirements before generation. Each deferred page has a
content-backed rationale and either exact replacement sources or an explicit
outside-report disposition. Required quarantined pages still block acceptance.

The distinct `report-pdf-table-certificate/v1` verifies required native child pages
and the complete parent artifact/member seal. It retains all page dispositions,
complete qualified text, the quarantined parent and deferred-page receipt. Its
obligation and certificate identities bind the reviewed page scope. It cannot
satisfy an unscoped/full-PDF obligation. A report cannot use a deferred page through
fulltext, financial references, calculation operands, derived financial leaves,
supporting contexts or model inputs. Replacement sources require native snapshot
membership and actual report use. Report review binds the independently declared
source population to the existing analytical requirements. Review rationales are
analyst judgments; a schema-valid page plan does not supply materiality approval.

Direct IR supplement continuation has a separate, narrower reviewed-page rule.
An OCR-flagged page can be omitted from native model input only after an exact
append-only `no_financial_content` review binds its document, source bytes,
assessment, page text and reviewed render. The detector still reports OCR
required. Unknown pages and withdrawn reviews block capture. The default
processing certificates and financial evidence requirements remain strict.

An explicit `extract_kpis_from_ir.py --revalidate-only <manifest>` can process a
complete historical V16 native-section set with the current V18 validator. The
manifest pins every old artifact, governed call, source byte hash, document
version, and failed attempt. It cannot call a provider or repair old raw fields.
One new attempt commits processed-section artifacts, candidate evidence, and its
completion receipt in one transaction; an exact replay verifies both populations.
Original V16 calls and the failed attempt
remain unchanged. Candidate retention uses the existing evidence ledger with
V16 as the producer and V18 as the processor, but it cannot publish financial
facts or claim visual, semantic, or archive completeness. Missing or changed
historical commitments make revalidation unavailable.
Only exact registered `ir_supplement` and `ir_press_release` issuer PDFs can enter
this historical action; normal automatic capture still selects supplements only.
Prospective direct PDF captures use V20 or V21 with a separate, source-witnessed
reporting entity boundary. The old `consolidation_scope` raw field remains intact.
An unknown boundary remains unknown and no unreviewed row gains admission.

Unrelated archive gaps remain visible. A complete selected analysis does not make
the archive complete or establish financial-fact admission or model readiness.
The scoped artifact has its own identity. Full-population verification does not
count it as full-population completion. Terminal research coordinates are issuer,
knowledge cutoff, observation cutoff, and Analysis Evidence Scope identity when
present. The existing unscoped coordinate and retrieval-mode conflict rule remain.

New processing, corpus, and research operations require the current inventory.
Verification of a stored artifact can explicitly reconstruct the inventory visible
at the receipt's original observation cutoff. It cannot fall back past an incomplete
newer inventory already visible at that cutoff. Historical verification is a read
operation; it does not authorize a new write with a stale inventory.

`execution/plan_analysis_evidence_scope.py` produces a selection-only receipt through
an explicit read-only database connection. The [Operations contract](operations_governance_surface.md#analysis-evidence-scope-planning-and-use)
owns its manual interface. This change adds no scheduled work, provider activation,
financial publication, or production-state change.
The morning pipeline selects its database and artifact root independently.
`execution/run_morning_pipeline.py --repo-root` names the artifact root; it
defaults to the installed project root. `--db-path` names the database authority.
Lifecycle reconciliation, document evidence and factor proxies receive the
selected artifact root. Database placement does not select an artifact root.
Checkpoint scope and invocation inputs bind both paths. A changed root cannot
reuse a checkpoint from another artifact set. This contract does not start a
pipeline or change scheduler activation.

## Stage sequence

```
INGEST → TRANSCRIBE → PARSE → VALIDATE → PERSIST → COMPUTE → SYNTHESIZE → PUBLISH
```

Each stage:
- Has typed inputs and outputs (Pydantic models in `src/models/`).
- Writes one row per `(run_id, ticker, period_end, stage)` to `stage_transitions`; `run_id` is the Attempt Identity.
- Is logically idempotent: rerunning the same Logical Idempotency Key at the same Observation Version is a no-op unless `--force`.
- Is resumable: a failed Attempt Identity restarts from its first `(ticker, period_end, stage)` where `status != ok`.

## Per-stage contract

| Stage | Input | Output | Terminal status (subset) |
|---|---|---|---|
| INGEST | `(ticker, source_type, doc_type, period_end)` | `documents` row + raw file in `data/raw/{source}/{ticker}/` | `OK`, `FAILED` |
| TRANSCRIBE | `documents.id` of audio | `documents` row of transcript + `transcript_segments` rows | `OK`, `SKIPPED`, `FAILED` |
| PARSE | `documents.id` | Pydantic-validated payload in `.tmp/parsed/{run_id}/{document_id}.json` | `OK`, `FAILED` |
| VALIDATE | parsed payload | `validation_issues` rows (severity `warn` or `halt`) | `OK`, `NEEDS_REVIEW`, `FAILED` |
| PERSIST | validated payload | rows in `financial_facts` / `segment_facts` / `kpi_facts` | `OK`, `FAILED` |
| COMPUTE | facts | derived metrics (ROE, ROA, ROIC, FCF, segment incremental margin, DCF) into `metric_facts` and `dcf_runs` | `OK`, `SKIPPED`, `FAILED` |
| SYNTHESIZE | facts + transcripts + thesis | LLM summary, Say-Do, thesis-state delta | `OK`, `FAILED` |
| PUBLISH | summaries + facts | master PDF, thesis tracker update, frontend feed | `OK`, `FAILED` |

The full enum lives in `src.models.runs.StageStatus`.

## Identity and repeat safety

- **Logical Idempotency Key:** `(directive_name, ticker_scope, period_end, stage)`;
  this is the stable business effect used to prevent duplicate work.
- **Attempt Identity:** `run_id = {directive_name}_{ticker_scope}_{period_end}_{started_at_iso}`;
  this changes on every execution and owns logs, costs, and checkpoints.
- **Observation Version:** the source-side version plus Content Identity described in
  `directives/data_provenance.md`; changed source content may legitimately rerun the same
  Logical Idempotency Key and append a new version.

Resumption queries one Attempt Identity and proceeds from its first non-`ok` stage.
Cross-attempt skip logic compares the Logical Idempotency Key and required Observation Version,
never the timestamp-bearing `run_id`.

## Intermediate and telemetry lifecycle

Pipeline checkpoints and `.tmp/` files are disposable only after they are no longer
needed for exact resumption. Active checkpoint trees, malformed or unrecognized
`state.json` files, locks, database/recovery material, and unverified temporary audio
fail closed and are never cleanup candidates.

The current retention defaults are:

- 30 days for completed checkpoint trees and general disposable `.tmp/` artifacts;
- seven days for rebuildable caches, including news and Python tool caches; and
- 90 days for bounded pipeline telemetry (`stage_transitions`, `source_calls`, and
  `ingestion_runs`).

Explicitly registered, verified, completed test files expire after seven days.
New owned test sessions use `src/operations/temp_run_retention.py`. A per-run
`.earnings-temp-run.json` receipt starts active. Successful closure records exact
disposable paths and their hashes after test teardown. The seven-day window starts
at completion, not creation. Pytest stores its default fixtures in that owned run
root on Windows and seals it through its session-finish hook. Other hosts retain
pytest's existing fixture cleanup; tests can opt in through
`EARNINGS_SUMMARY_MANAGED_TEST_TEMPS=1`. Failed or interrupted sessions
remain protected for their seven-day review window. A run's peak storage lease covers only its live producer: the
seal releases it after a pass or a failure, and the next admission on the same
host reclaims a lease whose holder process no longer exists. Retained failed
bytes still count against the free-space headroom. A reviewed resolution can
bind to a passed rerun whose only holds are retained links or multiply linked
files. Explicit user `--basetemp` paths are not claimed by this hook.

At session start, the pytest controller prunes earlier session roots in the
system temp directory (`prune_abandoned_test_runs`). It considers only roots the
harness names (`earnings-summary-pytest-<controller|gwN>-*`). It never touches
the shared `earnings-summary-pytest-secrets*` folders, links, roots with an
unreadable manifest, or any root whose lease holder may still run. Completed
roots and roots without a manifest are removed one day after completion or last
change. Failed roots, and active roots whose holder is gone, are removed after
the seven-day review window. Owner decision, 2026-10-22: no host runs a cleaner
for these local roots, so a failed pytest session's local evidence expires after
seven days instead of waiting indefinitely for a reviewed resolution.
The existing weekly cleaner discovers these receipts in both configured state
and runtime `.tmp` roots, plus narrowly named `earnings-summary-*` runs under
the Windows/user temporary roots. Unknown paths are inventoried, not registered
by filename, age, or a passing command. Lifecycle receipts remain after retirement.
Registration in `data/operations/artifact-retention.json` names each file, its
allowed scope, SHA-256, family, completion status and recovery pins. A supervised
operator may use a zero-day cutoff for the owner's immediate cleanup request.
Unclassified files, active or failed attempts, changed bytes, links, database
sidecars and pinned recovery material stay protected. Retirement writes a durable
plan and result receipt before removing registered bytes. Original source
documents, financial observation history and `portfolio_gc_archive.db` remain
outside this cleanup.

Backup files retain the latest verified, completed member per source and family.
Daily database backup defaults are one portfolio snapshot and one cumulative
archive snapshot. Snapshot and archive verification and both required uploads
must finish before retention starts. Failed or unfinished attempts and explicit
recovery pins keep their bytes and the last good backup. The weekly cleaner also
discovers operational SQLite backups in state `data/backups`. A valid snapshot
manifest, a sealed restore-readiness receipt, and a matching successful apply
receipt must bind the exact bytes, canonical source and completed operation.
Snapshot creation and a passed dry run do not establish operation completion.
KPI repair and disposition purposes retain separate backup families per source.
Failed attempts remain held until a later successful attempt proves recovery
with the same backup, logical operation, manifest, review and executor bindings.
Each cleanup run reads the current evidence again. It rechecks all evidence
digests before retirement and before relying on a surviving backup. Operator
catalog pins and holds take priority. Other legacy files require explicit catalog
registration; calendar age or a filename is insufficient.

Legacy temporary trees without ownership and completion evidence have no expiry
date. The coverage receipt must keep their sizes and holds visible. A supervised
review must classify their exact disposable files before registering them; active
work, source clones and unresolved recovery copies must not become disposable
because they are old. Registered completed test files clear on the first weekly
pass after their seven-day completion window. Ordinary eligible files clear on
the first pass after their 30-day window. These windows are not deadlines for
unclassified files, source evidence or recovery holds.

`execution/run_weekly_cleanup.py` and `execution/db_gc.py` are the executable
allowlists and exact cutoff implementations. Their already-registered weekly jobs are
the only cleanup writers, apart from the pytest session-start pruning of its own
earlier session roots above; this lifecycle does not authorize a new operation, schedule,
or generic directory sweep. A retention change belongs here first, followed by its
executable constant and focused tests.

## Failure-mode policy

| Class | Example | Action |
|---|---|---|
| Transient | 5xx, network timeout, 429 | Retry with exponential backoff (jittered, max 3 attempts). On final failure, mark stage `FAILED` and halt the run. |
| Schema/contract | Missing field, enum value not in `DocType` | Mark stage `FAILED` with `validation_issues` rule `SCHEMA_DRIFT`. Do not retry. Surface to the user — the directive needs updating. |
| Auth | 401, 403 | Halt immediately. Do not retry. |
| Validation halt | Severity `halt` issue (range, currency, period) | Stage `FAILED`. Do not advance to PERSIST. |

## Resumption

After failure, `python execution/daily_fetch_and_brief.py --ticker <T>` (or re-run the relevant per-ticker CLI):
1. Reads `stage_transitions` for the run.
2. Identifies the first `(ticker, period_end, stage)` with `status != ok`.
3. Re-runs from there.
4. Never silently restarts from stage 0.

## Routing at INGEST

Shared source policy authorizes acquisition using stored active identity, role,
instrument, and artifact kind. Source availability, filing regime, and provider
entitlements then determine applicable lanes. `kpi_definitions.primary_source`
selects metric extraction authority; it must not exclude other authorized issuer
documents from capture. An ETF is not a failed corporate filer. Unknown identity
is unresolved, never silently treated as an equity.

**Manual IR uploads are an additional, orthogonal source.** Explicitly supplied
documents enter the same byte/lineage contract through `categorize_ir_uploads.py`.
They do not authorize an unbounded crawl. A missing IR document remains a visible
coverage gap; preliminary analysis may use other available evidence with that
limitation, but may not claim complete source coverage.

## Refresh cadence

Per `directives/data_provenance.md` §6 (per-source overrides) and the project memory:

- Initial acquisition: source-policy-bounded history per name; wider historical work
  requires its own scope and provider budget.
- Quarterly refresh: triggered by either (a) a new period appearing in `FMP_FINANCIAL_REPORTS_DATES`, or (b) elapsed wall-clock quarter, whichever fires first.
- Stage 3 IR-override fetches run on the same trigger as the FMP refresh.

## What this rules out

- No transformation logic in Layer 2 (orchestration). Layer 2 sequences and reads stdout/stderr; it does not parse, validate, or compute.
- No long-running monolithic scripts that cross stages. Each stage is its own executable in `execution/`.
- No partial success reported as completeness. A `HALT` blocks its dependent write set;
  explicitly isolated items may continue with non-success accounting for failed items.
- No skipping VALIDATE. PERSIST never reads from PARSE directly.
