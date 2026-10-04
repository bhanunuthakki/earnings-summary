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

`execution/run_weekly_cleanup.py` and `execution/db_gc.py` are the executable
allowlists and exact cutoff implementations. Their already-registered weekly jobs are
the only cleanup writers; this lifecycle does not authorize a new operation, schedule,
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
