# Operations & Governance Surface Impact

## Directive contract

- **Target sources:** canonical Scheduler manifest and wrappers, managed-service registry, LLM and eval registries, issuer/source policy, typed runtime receipts, bounded database observations, schema compatibility, and separately governed operator capabilities.
- **Output schema:** an explicit disposition of `primary surface`, `linked governed view`, or `deliberate exclusion`, plus the affected typed registry/snapshot/view models and evidence-backed tests.
- **Refresh cadence:** declared configuration is projected at application start; request snapshots use the panel cache contract; runtime state comes only from typed receipts or bounded read-only observations with their own recorded time and freshness policy.
- **Logical Idempotency Key (`Idempotency key`):** canonical owner identity plus the required surface disposition. Repeating a projection for the same owner/disposition must not create a second logical surface item.
- **Content Identity:** digest of the typed registry, receipt, or snapshot payload used by the projection.
- **Observation Version:** registry/snapshot version, evidence-recorded time, and observed-at time.
- **Attempt Identity:** unique application-start projection or request snapshot invocation and its receipt.
- **Rate-limit budget:** zero network calls, subprocess probes, service-control calls, or unbounded filesystem/database reads in the Operations render path. Live producers write bounded receipts outside the request path.
- **Failure-mode policy:** render Missing, Stale, Invalid, or Unavailable with evidence source and time. Never turn absent or malformed evidence into a healthy claim, and never block unrelated product work merely because an internal implementation detail is not an operator workflow.

## Outcome

The Operations & Governance workspace remains a truthful operator-facing map as functionality is added, removed, renamed, or changes ownership. It is not an inventory of every module or CLI. It shows supported operations, their declared ownership, current evidence, freshness, failure state, and guarded actions at the level needed to understand or operate the product safely.

## Trigger matrix

Run this review when a change affects any of the following:

| Change | Required review |
|---|---|
| Scheduler task, wrapper, cadence, enabled state, job identity, write lane, or service ownership | Confirm the dynamic Jobs projection, receipt identity, and freshness remain truthful. |
| Supported manual or managed-service operation | Declare ownership, run/failure state, and whether an operator action is supported. |
| Source/provider pull, telemetry, rate limit, retry, backlog, or circuit | Confirm recent health, completeness, failure, and evidence-time visibility. |
| LLM purpose, model route, budget, eval, cost, latency, fallback, or failure telemetry | Confirm the linked LLM governance view remains complete and attributable. |
| Queue, lock, service, backup, restore, WAL, incident, or notification behavior | Add current evidence or state that the observation is unsupported/unavailable. A backup run may end `skipped_unchanged` (`StageStatus.SKIPPED` plus the marker in the accounting row) when the consistent snapshot's sha256 matches the last successfully uploaded backup and that upload receipt and snapshot are still present: it is a healthy no-op, not a failure and not a fresh upload. Operator evidence must keep run recency and upload recency distinct — recoverability evidence remains the last uploaded snapshot, never the most recent run row. |
| Migration that changes operational telemetry, receipts, retention, provenance, or recovery | Confirm expected/actual schema and recovery meaning. Ordinary business-schema migrations need only a no-surface-change reason. |
| Approval, retry, one-off run, enable/disable, apply, or other operator mutation | Apply the Operator-action boundary below. |
| Removal or rename of any supported item above | Apply the Removal contract below. |

Pure implementation refactors, test-only changes, prose-only changes, and internal CLIs that are not supported operator workflows may use `no surface change`, but the reason must name the preserved contract. Do not scan every Flask route or `execution/*.py` file and treat it as a product capability.

## Projection and display contract

1. **Project from owners.** Extend canonical owners and adapters first. `src/operations/registry.py` compiles Scheduler tasks/wrappers, services, LLM/eval definitions, source policy, queue states, and the expected Alembic head. Do not copy current task names, purpose names, providers, routes, or schema heads into this directive or the renderer.
2. **Observe without side effects.** `src/operations/snapshot.py` may use only the caller-owned read-only connection and bounded typed receipts/files. Configuration, registration, historical execution, current runtime state, and freshness are separate facts.
3. **Disposition every domain.** Each material `OperationsRegistry` and `OperationsSnapshot` field has exactly one disposition in `src/pipeline/operations_panel.py`: a visible primary tab, a linked governed view, or a deliberate exclusion with a reason. Adding or removing a model field must fail the surface-completeness test until reviewed.
4. **Render truthful states.** A visible observation includes status/value, observed time, evidence-recorded time when available, safe evidence label, and complete empty/loading/missing/stale/invalid behavior. An empty successful read says that no records were found; it does not say healthy.
5. **Keep attention complete.** Any visible bad, invalid, stale, stopped, failed, blocked, or otherwise action-requiring governance state contributes to the headline attention model unless a tested rationale explicitly makes it informational.
6. **Keep linked views governed.** A linked diagnostic may satisfy the disposition only when it is reachable from the Operations workspace, uses the governed loader, and preserves the same truthfulness and sanitization boundaries.

## Removal contract

Removing or renaming functionality requires all of the following in the same coherent change:

- remove or update the canonical owner and typed projection;
- remove obsolete cards, rows, controls, links, copy, filters, and attention rules;
- keep retained historical evidence distinguishable from an active capability;
- test that the retired identity is absent and that no orphan route or action remains;
- preserve a truthful unavailable/deprecated state only when historical interpretation still requires it.

## Operator-action boundary

Observability does not authorize mutation. A control appears only when the underlying capability has its own authorization, validation, idempotency/concurrency contract, bounded execution, durable receipt, loading/error/retry state, and applicable confirmation. Destructive, enabling/disabling, production-write, or broad-run controls require their separate security and activation review. If any prerequisite is absent, render read-only status or an explicit unsupported state instead of a button.

## Required evidence

At minimum, record one Pull Request disposition and run the relevant set. The Pull Request block is a reviewer/agent checklist; CI does not parse checkbox selection. The deterministic guards are the owner-equality and surface-disposition tests below.

- `tests/test_operations_registry.py` for exact projection from canonical owners;
- `tests/test_operations_snapshot.py` for bounded read-only evidence, freshness, and fail-closed states;
- `tests/test_operations_panel.py` for surface dispositions, attention, sanitization, actions, accessibility, and responsive structure;
- `tests/test_comments_server_operations.py` and `tests/test_work_os_shell.py` for route/cache/loader behavior;
- the full `tests/test_ui_controls.py` for any rendered frontend change;
- browser acceptance for a visible or interactive change: primary navigation, each affected state, 375px and desktop layouts, keyboard/focus behavior, network completion, and a clean console.

A tested `no surface change` disposition must state which canonical owner and visible contract remain unchanged. Static configuration, a green subprocess, or a successful historical row is not current-health evidence.

### Current no-surface-change dispositions

`execution/sync_list_type_from_holdings.py --apply --onboard-untracked` is the
primary operator surface for approving reviewed tracker holdings into the
research roster. It reuses `db.track_company` for SEC validation, issuer
registration, and onboarding, and requires the existing explicit `--apply`
mutation boundary. The scheduled morning job does not pass the opt-in flag.
There is no Operations-workspace control: the capability has no standalone
durable approval receipt or interactive confirmation state, so adding a button
would violate the Operator-action boundary. The existing Jobs projection and
portfolio-health views remain unchanged.

`issuer_fact_manifest.v2` extends the existing internal, explicit offline-produce/apply batch CLI.
It adds no scheduled job, managed service, runtime-health claim, or operator control. The canonical
Operations registry and its visible Jobs, Sources, Data, and Actions contracts therefore remain
unchanged; manifest validation, transactionality, and durable coverage receipts stay owned by the
ingestion boundary rather than the Operations workspace.

### KPI definition-revision shadow census

`execution/audit_kpi_revision_shadow_census.py` is a supported manual, read-only rehearsal. The
operator supplies an explicit standalone SQLite snapshot, its snapshot manifest, and timezone-aware
effective, knowledge, and evaluation cutoffs. The command derives the complete active portfolio
population from the database, emits one hash-bound `kpi-revision-shadow-census/v1` receipt, and exits
nonzero because the receipt never authorizes activation. A missing, incompatible, changed, or
sidecar-active snapshot remains `unverified`; even a matching supported manifest records only artifact
identity and the producer's assertions. It does not establish production evidence authority.
The CLI binds the connection's main path to that manifest and file hash, requires no pre-existing
transaction, rechecks identity after the owned read transaction, then closes the read-only connection
and rechecks once more before emitting output. Base KPI rows missing current resolution authority stay
in the population with an exact blocking disposition rather than disappearing through the resolved
view. Unavailable roster or population schema remains distinct from an observed empty population.
An unparseable fact period is retained as an exact blocking disposition rather than omitted or inferred.

A manifest-matched, quiesced snapshot uses the central immutable reader role so the
verification read cannot create WAL sidecars. The existing sidecar and identity
checks remain mandatory before and after reading. `execution/audit_kpi_revision_reader.py`
compares the existing sourced time-series loader with the revision-aware projection
for one explicit definition and cutoff. It retains source locators, observation and
resolution revisions, duplicate selections and semantic breaks. Its receipt remains
HOLD and does not change the default reader or activate a cutover.

This capability has no schedule, service, retry control, database write, or Operations-workspace
button. Its current supported state is `hold`: deterministic resolver readiness is visible in the
receipt, while reader activation requires separately approved portfolio evidence and an explicit owner
decision. The CLI is the primary operator surface for this rehearsal; no current-health projection is
claimed from an old receipt.

### Captured IR event ingestion

The captured-feed CLI is an internal, explicit offline-produce/apply operation. It adds
no registered Scheduler task, managed service, network fetcher or operator action.
The Operations registry therefore has no new item to project. Existing Scheduler
ownership and lock receipts remain authoritative if a job is later registered.
Forward calendar coverage is deliberately surfaced beside its retained events using
`ir_event_runs`, source observation time, and the active tracked roster; it is not a
claim about provider health. Missing, stale and failed receipts cannot render a
verified empty calendar. `tests/test_ir_events_ingestion.py` and
`tests/test_ir_events_cli_unavailable.py` cover these boundaries and disabled apply.

### Legacy evidence backfill and managed IR byte admission

`execution/backfill_evidence_ledger.py` remains an explicit, bounded manual operation;
its CLI is the primary surface, with no scheduled job or Operations-workspace control.
Apply holds the exact target database-adjacent writer lock and a checkpoint lock
rooted at the artifact state location, shared across code checkouts. A validated
ancestor lock is reused only when it owns this exact target database. Exit 2 reports quarantined
items in the current batch, and exit 75 reports lock contention; exit 0 alone never
proves population completeness. Inspect `has_more` and the retained checkpoint.
Checkpoints bind the resolved database path, retain quarantined document identities,
and process unseen documents before bounded rotating retries. A legacy unbound
checkpoint requires a new task ID; do not alter its cursor to imply successful repair.
Do not loop unchanged failing batches merely because `has_more` is true. Restore or
recapture missing/mismatched source bytes under their own authority before retrying.
The path binding is not proof against an in-place database replacement; retain the
canonical state authority and its recovery/version checks.

Managed IR publication keeps its existing operation and visible receipt contract.
It now anchors retained bytes in the evidence ledger within the publication transaction
and verifies that foundation on replay. This does not establish extraction completeness,
semantic admission, or a current-health claim. Historical publication receipts without
that foundation require explicit reconciliation; changed verifier identity can require
re-preparing staged artifacts. **No surface change:** the canonical Operations registry
and visible Jobs, Sources, Data, and Actions contracts remain unchanged. Regression
coverage lives in `test_evidence_backfill.py`, `test_evidence_backfill_cli_guard.py`, and
`test_managed_ir_sources.py`, including quarantine recovery, contention, byte drift,
replay integrity, and transaction rollback.

### Recovery queue policy migration

Migration `0040_fmp_watchlist_recovery` admits automatic evaluation and watchlist
work in the existing FMP queue. It preserves stable work IDs, retained rows, indexes,
attempts, and events; provider budgets, circuit controls, and source-policy checks
remain operative. Claimed explicit requests require a nonblank request identity.
Schema upgrade belongs to the canonical database writer under the existing backup
and deployment procedure. Downgrade fails without deleting rows that require the
newer role/request constraints. No scheduler activation or new UI action is implied.

### Progressive document evidence continuation

`execution/process_document_evidence.py` provides a read-only plan by default and
an explicit bounded `--apply` mode for stored active portfolio, evaluation, and
watchlist documents. It captures matching retained bytes, runs supported deterministic
full-text extraction through existing owners, and refreshes only already-linked
source inventories. It performs no source crawl, LLM call, financial semantic
admission, or completeness initialization. Newest stored document IDs receive
priority; that ordering is not proof of the latest issuer disclosure.

The existing morning pipeline includes this deterministic stage, with an explicit
database and product-state root, bounded batches, and existing runtime accounting.
No additional scheduler task or LLM window is created. `--resume` retains an atomic
database/scope-bound receipt and cursor under the product-state Operations runtime
directory. A failed item remains in the pending set; completed input/extractor
versions replay without another extraction. Structural scope mismatch fails closed.
The exact target database-adjacent writer lock and a product-state-root receipt
lock coordinate across code checkouts; inherited ownership is reused only for
the same verified target.

The JSON result is the primary detailed operator surface: it distinguishes capture,
extraction, already-covered, quarantine, unsupported, failed, and not-attempted
outcomes, plus missing source inventory. Exit 2 indicates degraded/unfinished proof;
75 indicates contention. The existing morning stage status exposes degradation,
while the receipt explains individual documents. Do not interpret a stored batch
receipt as current proof after inputs, extractor versions, or inventory scope change.
No new Operations-workspace action or health badge is introduced.

The existing dashboard refresh and IR-refresh actions execute the deployed code root
with the application-managed Windows interpreter while retaining the configured
product-state root and write ownership. A missing managed interpreter returns an
explicit unavailable response without starting a job; global Python is not a fallback.

Historical accession-key documents use the dedicated append-only SEC binding/fact-match
repair owners, not a hash overwrite. Those owners' isolated-target restrictions remain
in force. Restoring unavailable original bytes, bootstrapping issuer authority and
source inventories, semantic admission, and production cutover require their own
evidence; this continuation cannot imply those steps passed.

### Transcript commitment-scan evidence repair

`execution/audit_commitment_scan_evidence.py` is the supported manual, read-only inventory for
selected active-portfolio transcript evidence. It requires an explicit database and project root,
uses a query-only connection, and emits stable reason codes for missing artifacts, hash mismatches,
missing or invalid acquisition binding, typed scan coverage, and complete scans. Its receipt is an
observation only; it never creates acquisition lineage or authorizes a repair.

`execution/extract_commitments_from_transcript.py --auto --reaudit-invalid-evidence --ticker TICKER`
is the explicit bounded repair surface for legacy-unobserved or invalid scan evidence that still has
an exact authorized acquisition binding. Ordinary automatic extraction continues to exclude those
states. The repair uses the governed `saydo_commitment_extract` route and appends immutable segment
observations and output receipts; it does not overwrite historical receipts, admit unreceipted bytes,
or clear missing/hash-mismatched artifacts. A single `--transcript-id` remains the narrower explicit
target.

Neither command adds a scheduled job, managed service, current-health claim, or Operations-workspace
button. Their primary operator surface is the CLI because the re-audit requires a deliberate bounded
scope and the audit can be expensive over large retained artifacts. Any future UI control requires a
separate activation review under the Operator-action boundary.
### WIX history correction

`execution/repair_wix_history.py --db <explicit-target> prepare` produces a private,
immutable review plan from complete current tracker holdings and transaction pages,
the exact owner decision, and retained before-images. It never writes the database.
The plan freezes any existing linked owner checkpoint and expires after one hour or
at the UTC date boundary. `apply --plan <file> --approved-sha256 <fingerprint>` uses
the exact database-adjacent writer lock, revalidates sources and target identity,
and atomically corrects the lifecycle while superseding the invalid generated note.
Migration 0044 retains a duplicate lifecycle identity through an immutable supersession
link; canonical active readers exclude it, while direct historical lookup retains it.
Original note text and all affected before-images remain in correction provenance.
The replacement observation is advisor-authored and explicitly owner-review-pending.
No AVDV proceeds attribution, investment grade, or owner lesson is inferred from trades.

This is a manual recovery command, not a scheduler or dashboard action. Production
use requires the exact reviewed target/plan and the canonical host's verified backup
and deployment procedure. Failed compare-and-swap, stale evidence, ambiguous new
purchases, invalid account coverage, or transaction failure leave records unchanged.
The CLI emits a typed failure category without source payloads. Keep its private
plan outside public artifacts; prepare again after changed evidence or migration.

### Factor and rate calculation repair

The existing business-factor refresh resolves an explicit/configured database,
uses materialized holdings rather than the tracked roster, and shares the target
writer lock. Factor admission binds current thesis, business mix and taxonomy
hashes; holdings timestamps and the existing 48-hour cache policy remain operative.
`execution/compute_macro_sensitivities.py` writes immutable, input-bound estimates
under migration 0041. Legacy unversioned rate rows remain retained but are excluded
from current rate conclusions; old risk snapshots do not establish an admitted beta.
Rate shocks in basis points are converted to percentage-point changes before using
a return-per-percentage-point coefficient. Canonical-host migration, recomputation
and fresh reader verification remain operational steps; local fixtures do not prove
that live legacy values have been replaced. No new scheduled window is added.


### Collection evidence read surface

Settings distinguishes authorization from observed SEC coverage. Portfolio, evaluation,
and watchlist retain automatic source authorization. A sealed SEC inventory defines the
listed native-filing population; immutable document/source identities, exact-byte location
receipts and successful extraction locators prove individual captures. CompanyFacts is one
aggregate snapshot. Missing, unavailable, unsealed and partial evidence remain explicit.
A covered historical population has **unknown current freshness**: capture age is displayed,
with no invented universal SEC expiry or inference from an Operations heartbeat.

FMP timing/backlog detail derives from the recovery ledger. Migration
`0045_fmp_recovery_receipts` adds an immutable final run receipt, separate from events and
attempts, with expected work, attempted/reused proof, unresolved work and typed outcome.
The existing 24-hour FMP freshness policy governs receipt age. An interrupted run has no
terminal receipt until finalization; reusing a finalized run ID for new execution is rejected.

Operations headline attention includes these same derived FMP/SEC evidence gaps and links
to Settings. Covered historical populations with unknown freshness are informational gaps,
never a green current-coverage claim. No new collection action, scheduler, refresh control,
network permission or production activation is introduced by this read surface.

### Qualitative common-drawdown inspection

`execution/common_drawdown.py` reads an explicit existing database and emits
current or paired scenario evidence. It cannot apply trades or modify holdings.
Factor admission, constituent-weight coverage, source dates and unknown ETF
publication currency remain visible; missing after-state coverage cannot be
presented as an improvement. The WIX/AVDV target band remains an unverified
scenario input. Synthetic replay lives in `tests/test_qualitative_stress.py`.

### News collection failure state

The existing Yahoo journalism adapter remains the default free general-news leg;
paid web discovery remains opt-in. A Yahoo transport or response-container failure
now yields partial completion (exit 2), while valid rows from other feeds remain
persisted. Any persistence failure yields exit 1. A completed attempt does not
certify source completeness. Rejected provider diagnostics use the canonical
structured redactor before storage; article identity retains nonsecret URL query
parameters. No new source or scheduled action was added.

### Captured foreign-source normalization

`execution/normalize_foreign_filings.py` now requires an explicit database,
frozen exact-document input manifest and new immutable receipt path. Dry-run is
read-only. Apply holds the existing target/package locks and publishes only
selected captured-source facts through the shared source repository. HOLD means
prerequisites failed; PARTIAL includes any committed source progress but does not
claim canonical semantic binding or decision-grade consumer readiness. Native
packages still require authoritative sealed inventory and the qualified offline
processor. This command performs no source acquisition, scheduler activation or
whole-corpus canonical population.

Before source publication, apply establishes the selected documents' recorded-subject
bindings from the existing canonical issuer registry. Unknown or conflicting identities
stop the run. This prerequisite is a separately committed, idempotent stage; its actual
progress remains in the receipt if a later source publication fails. It grants no metric
semantic admission.

### Source measurements and foreign-source comparison

The shared HTTP clients append actual attempt measurements with their source-call rows
under migration `0047_source_regime_measurements`. Missing regime attribution, costs,
record counts and operator time remain unknown. Dashboard logical-call totals exclude
the additional physical-attempt rows, avoiding double counting. Persistence failures
remain visible through the existing HTTP event.

`execution/attribute_source_cost.py` verifies an explicit retained canary selection and
reports measured and missing evidence. `execution/backfill_foreign_oracle.py` compares
explicit sealed source observations with independent counterparts through the shared
fact reader and, when supplied, a verified ontology snapshot. Empty inputs, missing
counterparts, self-comparisons and unknown completeness cannot pass. Both are manual
diagnostics; they introduce no schedule, acquisition, dashboard action or activation.


### Actual SEC execution receipts

Migration `0046_sec_execution_receipts` records actual apply requests, running attempts,
and immutable terminal results from the native capture and inventory synchronization
writers. A retry is a new attempt of the same scope-bound logical request; replaying an
identical terminal result preserves its original timestamp. An interrupted attempt remains
completion unconfirmed. A completed batch never proves current issuer coverage or process
liveness. Dry runs and pure SEC plan/admission functions remain database-write-free; no
historical scheduling state is backfilled.

Settings uses the existing canonical transient-fetch assessment reasons to distinguish
actual deferred native work from other failures. Execution details bind selected documents
and snapshots; mismatches with the current population are explicitly historical. Missing
or invalid terminal evidence contributes to Operations attention. Exact captures and
source inventory remain the coverage authorities, with no universal SEC freshness TTL.

### Sealed canonical growth rendering

The existing internal `execution/render_three_regimes.py` action accepts
`--input-manifest`, `--manifest-sha256`, and `--output-dir` for an explicit closed
snapshot and hash-bound source inventory. It reconstructs the migrated canonical
growth projection for the fixed cohort under all three source regimes and writes
immutable, provenance-bearing artifacts. Excluded canonical winners remain
unavailable; this action does not choose an alternate source. Full acceptance stays
`HOLD` with a nonzero exit while report/DCF/valuation migration, acquisition
completeness, regime-specific resolution, and OS isolation evidence are missing.
This extends the internal offline action only: no Operations control, schedule,
production write, or managed activation is added.

### Grading database authority

`execution/grade_bear_cases.py` now accepts `--db` for an explicit existing
fixture or approved retained database; omission resolves the configured database
through `db_paths.require_db_path`. A checkout-default or missing database fails
before materialization or grading. The same path is passed to prediction stores,
corpus reads and calibration, and scoped through internal LLM bookkeeping with
restoration on failure. No scheduler, Operations control or live grading is
activated by this change. The targeted authority tests exercise explicit fixture
selection, refusal of checkout state, and context restoration; they do not certify
the remaining legacy financial corpus or qualitative grading accuracy.

`execution/grade_decisions.py` likewise requires the configured existing database,
with `--db-path` / `--db` for an explicit override. It preserves the price-only
verdict rules while admitting only same-instrument, quote-currency-bound,
split-and-dividend-adjusted observations within the shared market-price age
limit. Missing or contradictory price evidence leaves the decision pending and
does not emit calibration. Successful outcomes retain a versioned input manifest
in their notes; source capture freshness remains explicitly unverified. This
changes the existing manual grader only, without activating a run or schedule.

### README generation contract evidence

The existing README update action retains a compact attestation for each generator
and judge call, binding its registered purpose, prompt and schema versions, source
hashes and observed provider attempt. The facade is limited to the existing
`meta_eval` scope; prompt drift fails closed and missing usage, cost or effective
prompt verification remains unknown. Setup, schema and budget failures cannot
enter provider fallback. This changes evidence retained by the existing action;
it adds no Operations control, schedule, provider activation or live execution.
Other LLM consumers remain outside this migrated tranche.

`execution/grade_predictions.py` uses the same explicit/configured database
boundary. It grades only uniquely identified, admitted revision-aware KPI facts
known at the batch cutoff; ambiguous names, unsupported concept bindings and
unit mismatches remain pending. Retained notes bind the source and target inputs.
A short conditional publication checks the complete loaded prediction, preserving
newer owner edits and existing outcomes. Future-made predictions are excluded;
extraction calibration retains its existing malformed-input formula and excludes
failed outcome writes. This extends evidence and controls for the existing manual
action only; no grading run, service or schedule is activated.

### Canonical report and valuation readers

The existing report financial tables read admitted canonical observations at one
knowledge cutoff, retain their source manifest, and show unavailable cells when
fiscal identity, units or comparability are unproved. Current P/E and P/FCF use
comparable annual windows of reported facts plus separately captured market
capitalization. Annual consensus estimates and historical provider ratios retain
distinct bases; unlike bases cannot establish a valuation comparison band.
These read surfaces add no operator action, schedule, acquisition permission or
live cutover. Source-population completeness and activation remain separate gates;
positive synthetic reader tests do not certify decision-grade coverage.

### Canonical DCF statement inputs

`execution/build_redesigned_dcf.py` now reads `src/sources/dcf_statements.py`,
which resolves exact reported statement observations through the existing canonical
fact and provenance readers at one cutoff. Missing, semantically changed, partial,
or non-primary inputs fail closed rather than falling back to provider cache data.
This changes the inputs to the existing explicit DCF build only. It adds no
operator action, scheduler entry, managed service, source acquisition permission,
or production activation; the Operations registry and visible workspace contracts
remain unchanged. `tests/test_dcf_canonical_statements.py` and
`tests/test_redesigned_dcf_smoke.py` exercise the admitted and rejected boundaries.

### Canonical evaluation snapshot

The existing evaluation report snapshot now projects admitted financial cells and
captured market context at one explicit cutoff. It retains exact cell lineage for
annual, TTM, margin, and CAGR displays; unavailable, stale, malformed, mixed-
currency, or semantically incompatible inputs remain unavailable. This is a
read-only report projection. It adds no operator action, scheduler, managed
service, source acquisition permission, or production activation; the Operations
registry and its visible workspace contracts remain unchanged.

### Canonical soft-rule financial reader

Soft-rule evaluation now reads reported financial quarters through
`src/sources/canonical_financial_series.py` at one explicit cutoff. It retains admitted
observation and definition lineage; unavailable, stale, semantically changed,
mixed-currency, or incomplete series remain unresolved. This only changes the
existing local evaluator's read path. It adds no operator action, scheduler,
managed service, source acquisition permission, or production activation; the
Operations registry and visible workspace contracts remain unchanged.

### Canonical cockpit fundamentals

The existing cockpit fundamentals cache now reads admitted reported financial
series at one explicit cutoff and retains source manifests for its displayed
revenue growth and free-cash-flow margin. Rejected, malformed, incomplete, or
incompatible series remain unavailable; the existing refresh command and cache
contract are retained. This adds no operator action, scheduler, managed
service, source acquisition permission, or production activation; the
Operations registry and visible workspace contracts remain unchanged.

### Canonical revenue year-over-year computation

The existing metrics engine can now persist an admitted canonical revenue
year-over-year derivation with its output observation and sealed input lineage.
It remains part of the existing local computation path: missing, ambiguous,
incomparable, incomplete, or cutoff-ineligible quarterly inputs produce an
unavailable computation rather than a derived value. This adds no operator
action, scheduler, managed service, source acquisition permission, or
production activation; the Operations registry and visible workspace contracts
remain unchanged.
