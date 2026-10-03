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

Application read-performance changes preserve the existing Operations registry,
Jobs, Sources, Data, Actions, and README approval boundaries. Independent panel
loading, bounded reads, cache-busy responses, and retry controls change delivery
and recovery of read-only observations. They do not add an operator action,
scheduler, writer, or service-health claim. A failed README status read disables
Apply until the current status is verified. Route and read-lifecycle tests cover
these preserved boundaries.

Company Desk loading and same-company Retry preserve the prior company identity
and evidence until the response identity validates. The additive selected-observation
lookup index changes query access only. It retains resolution revisions, current-head
selection, and the existing snapshot/readiness migration and rollback procedures.
No operator action, scheduler, writer, or source authority is added.

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

### Streamlit analytics deep-dive sandbox

`explore-sandbox/analytics_deep_dive.py` is a local, optional-dependency analytics surface: an
interactive composition of deterministic catalog → ViewSpec reads over an operator-named
synthetic clone or restored snapshot. It adds no scheduled job, managed service, operator
control, database write, or production access — the app opens every connection read-only,
refuses a checkout-local `data/portfolio.db`, and every number comes from the shared
provenance-aware resolver (`viewspec.engine`), never a sandbox-local query path. Theme and
control parity with the cockpit is generated and drift-checked
(`scripts/gen_streamlit_theme.py`; `tests/test_explore_sandbox_kit.py`), so no new visual
contract exists to govern. The canonical Operations registry and its visible Jobs, Sources,
Data, and Actions contracts therefore remain unchanged; the sandbox is not an operator
workflow and never a production authority.

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

The manual SEC filing-inventory collector reads `EDGAR_USER_AGENT` from the process
environment first, then reads only that key from the configured project env file on
each invocation. An absent contact uses the existing public default. The collector
does not import other private settings into its process environment. SEC 401/403
hard stops return a stable `blocked`/`sec_inventory_hard_stop` JSON result with
`retryable: false` and exit 2; the existing identity hold remains distinct. This
status does not by itself identify the cause. A selected contact with header
controls, non-Latin1 characters, or a nonblank length outside 8–512 characters is
rejected before transport with `blocked`/`sec_contact_configuration_invalid`,
`retryable: false` and exit 2; neither contact value nor a value-bearing cause is
emitted. This does not retry acquisition, assert source coverage, or add a
scheduled action.

The manual inventory collector timestamps each successful SEC response after its
bytes arrive. A source observation records that response time separately from its
later evidence-recording time. Package `state.v2.json` retains each successful
response hash and original clock immediately, even when the next component fails.
It also pins the acquisition config and collector version; an incompatible or
malformed replay returns `blocked`/`sec_inventory_checkpoint_invalid`,
`retryable: false` and exit 2 before source network or publication. Preflight
verifies every referenced v2 component; missing or hash-conflicting bytes and
future retrieval clocks are malformed replay. Resuming a compatible component
does not claim a new HTTP retrieval. Older `state.json` checkpoints retain their
bytes and identity, but have no retrieval clocks. Their entries are refetched
within `--package-limit`
before they can support new source observations; file modification times are never
promoted to retrieval evidence. A failed component remains deferred while any
successful sibling stays available for exact-byte resume. The operator still
uses the same manual CLI and output fields; this changes no
dashboard action, schedule, provider, database schema, or approval state.

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

### Valuation evidence preflight and artifact database handoff

`execution/valuation_preflight.py` is an internal read-only diagnostic over the
existing persisted valuation and canonical fact readers. It takes an explicit or
configured database authority and reports typed blocked/unavailable reasons; it
performs no acquisition, model promotion, job enablement or provider retry.
It is deliberately excluded from interactive operator controls: it has no
mutation capability or independent runtime-health claim. Allocation/advisor gates
consume the same evidence projection. The canonical Operations registry and Jobs,
Sources, Data and Actions projections remain unchanged. Dispatcher freshness and
dirty-artifact child database handoff and strict queue availability checks tighten
existing execution contracts without adding a task, schedule or write lane.
Focused preflight, dispatcher and artifact regression tests establish this
no-surface-change disposition.

### FMP owner admission

The shared FMP adapter and shared HTTP FMP lane read the nonsecret policy named by
`EARNINGS_SUMMARY_FMP_POLICY_FILE` before admission and each network attempt.
The path must be absolute and outside the deployed code root. The bounded JSON
schema is exactly `{"schema_version":1,"enabled":false}` (or `true`). An unset
setting preserves existing admission; a configured missing, invalid or unreadable
file fails closed. Local denials are typed nonretryable `provider_disabled_by_owner`
or `provider_admission_invalid`, never an authentication failure or fabricated HTTP
attempt. Redirect targets use the same check, including redirects from other hosts.
There is no automatic provider probe to reverse an owner opt-out.

Activation belongs to the existing external environment authority resolved by
`runtime.secrets.project_env_file`, not a checkout config or a new policy database.
The managed job runner loads that environment before copying it into each child;
the dashboard loads it through `configure_runtime_db` at startup. An already-running
service needs its approved restart to acquire a newly added pointer. The loader
preserves existing process variables (`override=False`), so activation must verify
that the effective pointer agrees with the reviewed external file; setting a Machine
or NSSM variable alone does not prove child propagation. Once loaded, policy-file
content changes apply to subsequent admission checks without restarting. Replace
that file atomically; incomplete updates deliberately block. This code change does
not activate the flag, restart an owner, or change schedules or credentials.

Automatic news collection does not turn an owner/config denial into per-ticker paid
web discovery, even when the fallback scope includes that ticker. Explicit manual
`--source websearch` and independently configured additive feeds/scoring retain their
existing policy. Calendar `--force` overrides the subscription-tier gate only. Its
wrapper still runs expected-earnings fallback, and retains a nonzero first-step exit
rather than reporting fallback success as overall success. Retained FMP caches are
not deleted or relabeled as freshly acquired issuer facts.

Disposition: the CLI's typed denial and existing job failure surface are primary;
there is no new dashboard action or provider-health badge. Existing source readiness,
source receipts and Operations registry remain authoritative; this flag proves no
source completeness. Dedicated FMP fetchers use `FMP_CLIENT`; the macro FMP seam
is already disabled. Raw third-party transports outside this shared adapter are not
a host-wide network firewall and must use this same admission boundary if an FMP
lane is added. Hermetic transport, news and calendar fixtures cover denial and hot
changes; the BAT execution fixture additionally requires Windows qualification.

### Native MELI reported-table source publication

`backfill_fulltext_evidence.py --source-lane evidence_native --document-version-id <id>`
is an exact-document operation. It neither reads nor advances the global batch checkpoint;
other captured documents remain untouched. Legacy `--document-id` remains a separate lane.

`publish_meli_reported_tables.py` is a bounded manual source-publication CLI, with dry-run as
the default and `--apply` as its explicit append boundary. It requires an explicit database,
current sealed SEC inventory/accession, primary native document version, qualified native
HTML extraction run, and allowed local content roots. It never acquires data or calls a
provider. Its current recipe supports a June 30 10-Q whose table explicitly reports the
six-month duration (a missing SEC capture start date is derived from that header, and a
conflicting start date fails) and exactly
three current-period rows: H1 NIMAL, issuer-reconciled available cash and investments,
and total debt including operating leases. Recipe v2 retains the raw percent and
USD-millions values, units, source tokens, table headers and definition commitments.
Its versioned representation conversion publishes ratio (percent ×0.01) and USD
(USD-millions ×1,000,000), with raw/normalized values and the exact multiplier in
each immutable source locator and a distinct source-definition/cell identity.
Dry-run dispositions expose both representations. No analyst cash allocation,
lease adjustment, canonical admission, or financial estimate is performed.
Prior recipe observations remain immutable. A changed/missing/ambiguous header,
definition, row, or scope is rejected.

The CLI emits a closed three-member captured/rejected population. Apply appends the
scoped extraction's disposition nodes and `SourceFactRepository` publication atomically;
an identical rerun reuses the immutable publication. Exit 0 means all three source members
were captured, exit 2 means a partial/rejected population, and exit 3 means unavailable
prerequisites or invalid evidence. The seal closes only this named population: it does not
upgrade whole-document extraction coverage, claim canonical metric admission, or make a
valuation eligible. Source-definition commitments preserve management wording and exact
native evidence locators. Effective-dated ontology definition/mapping/binding, canonical
resolution, approved model-role assignment, remaining reported inputs and dated model
assumption/scenario review remain separate producer and consumer prerequisites.

Disposition: deliberate exclusion from interactive Operations controls. The existing Jobs,
Sources, Data and Actions registries remain unchanged; no scheduler, managed service or
background retry is added. CLI diagnostics and immutable scoped publication receipts own
this manual operation. `tests/test_meli_reported_tables.py` verifies native identity,
raw-byte/locator replay, semantic rejection, dry-run, append/replay and absence of fabricated
legacy documents or canonical-admission claims. Existing full-text tests retain the global
batch behavior. No browser or service change is made.

### MELI verified refresh: explicit assumptions artifact

The existing MELI refresh operation now requires a separately configured reviewed
assumptions artifact. Pass `--meli-assumptions-path <approved-state-file>` to
`execution/refresh_dcf.py --ticker MELI`, or set `DCF_MELI_ASSUMPTIONS_PATH`.
The existing explicit/configured database authority remains separate. The
subprocess receives both authorities unchanged; it never infers an artifact root
from the database parent or falls back to checkout `data/` or a runtime junction.
The exact artifact path and content hash are retained in model provenance.
For MELI, the explicit package selects the specialized recipe even in a clean
checkout. Before dispatch, the parent validates its request shape, ticker and
recipe and rejects conflicting or malformed local family hints. It passes the
captured SHA-256 as `DCF_MELI_ASSUMPTIONS_SHA256`; the child validates that hash
against the same bytes it parses, before opening the database. This routing
check does not establish fact admission or scenario acceptance. A bulk refresh
continues using each other ticker's existing routing independently of this MELI
package.

A verified artifact contains an `input_evidence` request with exact canonical
reported-fact references, a sealed research snapshot and the complete effective
forecast vector (including discount rates). Existing producers must first admit
reported observations, governed metric definitions and bindings, then seal the
resolution/research snapshots with current acquisition and extraction coverage.
The fixed MELI recipe computes TTM values from reported fiscal-year/current-YTD/
prior-YTD operands and computes non-credit fintech revenue and the corporate cash
bridge explicitly. Those calculations are not admitted as reported facts. Metric
definitions must prove the supported fiscal calendar, financial scope and unit;
unknown or mismatched definitions fail closed. No automatic role binder or
production fact bootstrap is provided by this change.

The artifact also requires a dated review bound to the exact actuals and effective
forecast vector, with explicit NIMAL, aggregate-profit, D&A and capital-spending
comparators and variance rationales. The cash bridge uses the issuer's available
cash/investments and total-debt reconciliation pools. Current and non-current
operating-lease liabilities are separately reported inputs; their sum is a model
calculation removed from total debt because native FCFF expenses rent. Finance
leases remain inside financial debt pending their capex/D&A treatment. Credit
cash, operating cash reserves and credit-funding debt allocations are explicit
reviewed assumptions, never reported surplus or non-credit balances. Allocations
must be nonnegative; cash allocations together cannot exceed the disclosed cash
pool, and credit-funding debt cannot exceed total debt less operating leases.
Their pool comparators and rationales are part of the same dated review. The net
bridge is available cash minus credit cash and operating reserve, minus financial
debt, plus allocated credit-funding debt. This retains owner/analyst forecasts as
assumptions rather than treating source freshness as approval. The numerical
engine and original scenario formulas remain unchanged. Model output is replayed
against its inputs before persistence and by readiness consumers. The owning
upsert retains MELI's timezone-aware calculation timestamp with microseconds;
readiness retains strict ordering against the earlier input verification clock,
without tolerating future inputs to accommodate SQL timestamp truncation. Missing or
changed artifacts, incomplete coverage, unadmitted inputs, absent review or a
replay mismatch block verified promotion. A verified base model still reports
`scenario_acceptance_unverified` and is ineligible for allocation; it does not
certify bear/bull assumptions or probabilities. Draft calculations retain their
separate labeled, non-promoting `.tmp/` path.

This tightens the existing internal refresh contract; it adds no Operations
button, schedule, source-acquisition permission, provider invocation or production
activation. The focused MELI recipe tests exercise real publication, ontology,
binding and resolution authorities; source-package completeness is an explicitly
isolated boundary in those fixtures, with separate inventory checks. Routing,
provenance, model-replay and readiness tests cover the changed contract. Passing
those tests is not a claim that the production source population is complete.


### MELI derived-scenario terminal gate

The MELI builder rejects every derived scenario before workbook save or scenario
snapshot emission unless terminal loan growth equals terminal earnings growth and
the Gordon retention charge equals the equity required to fund that growth.
The base-input recipe and scenario builder share the numerical owner's validator.
Invalid/nonfinite terminal inputs fail; growth and ROE are not automatically
clamped into apparently valid assumptions. Historical generic and thesis delta
sets may therefore block a build even when the base input receipt verifies.
No forecast, scenario probability, or owner thesis is changed by this gate.

This is mathematical validation, not scenario approval. The existing
`scenario_acceptance_unverified` allocation blocker remains. Explicit approved
scenario/prior authority in the reviewed assumptions package is not yet supported;
repository-relative scenario fallback is not promoted to an approved authority.
Disposition: no new operator action, job, service, retry or UI surface. The existing
MELI refresh action now fails closed for inconsistent derived scenarios, preserving
an existing workbook. `tests/test_meli_scenario_consistency.py` verifies the failure
cases and a mathematically coherent synthetic case; emission/parity tests retain
coherent isolated fixtures.

### Explicit lexical-only research snapshot population

`execution/populate_research_snapshots.py --projection-mode lexical_only` is an
explicit, provider-free retrieval projection choice. The default `semantic` mode
still requires an exact vector projection and embedding promotion; missing evidence
never selects lexical-only automatically. This choice concerns retrieval only:
issuer/source acquisition, processing completeness, financial semantic admission,
canonical fact resolution, exact corpus membership, and lexical sealing remain
required under the existing authorities. It does not complete missing facts,
refresh a forecast, or approve scenarios.

Preview with the approved explicit `--db`, `--cutoff-at`, `--recorded-at` and optional
`--issuer-id`. For apply, repeat that scope and `--projection-mode`, pass the returned
`--input-commitment-sha256` and `--plan-commitment-sha256`, and add `--apply` through
the existing managed SQLite bootstrap. Both commitments bind the selected mode;
a mode switch refuses the old pins before writes. Lexical-only requests omit vector
and promotion coordinates and have distinct identities; retained semantic identities
are unchanged. There is exactly one terminal per issuer, K/O, and Analysis Evidence
Scope identity when present. An unscoped request retains its existing coordinate:
`research_snapshot_terminal_scope_conflict` blocks a second terminal, including a
mode switch. This operation does not supersede an existing terminal or alter that
scope. Replay verifies the existing seal through the public source verifier;
terminal verification infers the persisted mode, reassembles that exact request, and
performs the same lineage checks. No schema, store, service, schedule, provider, or
new dashboard action is introduced. A preview is an assembly plan, not a successful
sealed-source verification receipt.

### Analysis Evidence Scope planning and use

`execution/plan_analysis_evidence_scope.py --db <approved-database> --request <request.json>
--scope-receipt <receipt.json>` is a manual, read-only selection operation. Run it
through the managed SQLite bootstrap. The request is an `AnalysisScopeRequest` with
issuer, purpose, inventory key, sorted required period ends, knowledge cutoff,
observation cutoff, and optional extra accession/reason pairs. Latest-period
inclusion is the default. The command requires a current, complete authoritative
SEC inventory. It writes one immutable output receipt with no replacement of its
request, database, or an existing conflicting artifact. It does not fetch reports
or write database rows. Its result reports `selection_only_not_model_ready` and
research-document, package-dependency, and outside-scope counts.

The receipt retains every expected document in the selected inventory. Primary
filings are research documents. All other selected package members are required
capture dependencies. Outside documents retain explicit reasons and visible
coverage gaps. Selected periodic packages include known amendments. Missing or
ambiguous required periods and unavailable current-report amendment linkage fail;
selection never invents reporting dates or relationships.

Use the receipt through the existing manual interfaces:

- `execution/populate_document_processing.py --analysis-scope <receipt.json>` accepts
  it for a dry-run. Apply uses the scope embedded in the admitted request through
  `--admission-receipt`; passing a separate `--analysis-scope` to apply is rejected.
- `execution/build_grounded_search_corpus.py --analysis-scope <receipt.json>` uses
  it instead of `--inventory` or `--coverage-inventory-key`. All selected package
  members must be captured. Only primary research documents enter the corpus.
  The scoped corpus key must equal the receipt's `scope_id`
  (`analysis-scope:<sha256>`).
- `execution/populate_research_snapshots.py --analysis-scope <receipt.json>` uses
  it for planning and committed apply through the existing input/plan commitment
  checks. Processing, corpus, and research primary document sets must match exactly.

These operations retain their explicit database, clock, locking, immutable receipt,
and apply requirements. New operations require the current inventory. Internal
verification of a stored artifact can reconstruct the inventory visible at its
original scope observation cutoff. No CLI permits historical verification to
authorize a stale write. Research terminal identity includes the scope identity;
distinct purposes do not replace each other's artifacts. Scoped artifacts do not
count as full-population readiness.

This is an internal manual CLI capability. The existing `OperationsRegistry`,
`OperationsSnapshot`, and Operations workspace controls remain unchanged. There is
no new dashboard button, background job, provider activation, schema, financial
admission, valuation update, or production-state change. Selection and successful
capture alone do not establish model readiness. The focused analysis-scope tests
own selection, retained reconstruction, missing dependency, processing-lane, and
corpus consistency checks.

### Bounded accession capture

For 10-K, 10-K/A, 10-Q and 10-Q/A, the SEC submissions `reportDate` is the
source-declared reporting-period end. The inventory parser accepts it only as a
real `YYYY-MM-DD` calendar date; the expected primary filing and financial-report
package children carry that date at UTC midnight as `period_end`. SEC submissions
does not establish `period_start`, which remains unknown. A blank `reportDate`
remains unknown and cannot qualify a consumer requiring a dated filing. Filing
date and acceptance time never substitute for the reporting period. Other forms,
including 20-F and 40-F, are outside this narrowly qualified mapping.

The expected-document payload, including `period_end`, is committed in the
immutable inventory snapshot. A corrected mapping requires a new inventory
revision and a new capture checkpoint scope; retained SEC responses keep their
original acquisition identity and collector version. Already captured identical
filing bytes with conflicting period metadata fail native replay and require a
separately governed immutable correction, not an in-place date update. This
mapping adds no automatic capture, operator control or valuation admission.

`execution/capture_expected_sec_documents.py --accession-number <SEC-accession>`
selects exact dashed accession numbers within the supplied current, completely
sealed authoritative SEC inventory keys. Repeat the flag for at most 250 unique
accessions. Omission retains the existing inventory-wide ordering and batch limit.
Unknown accessions or accessions crossing multiple supplied inventory/issuer
identities are errors before network, checkpoint writes or apply receipts.
Already captured accessions remain valid selectors and can yield zero new work.

Use the same task id and selector for dry-run/apply/resume. New checkpoint scope
hashes bind inventory keys, current snapshot IDs and the accession selection;
a changed scope requires a new task id. Existing unfiltered checkpoints retain
exact-byte replay, but cannot be repurposed as filtered checkpoints. Raw response
storage and hash validation are unchanged. Apply receipts bind the actual selected
expected-document IDs, not the entire inventory. Validated selections pin a
checkpoint even when no documents remain; selected-mode logical request identity
also includes the scope hash so empty accession requests cannot collapse together.

Results name `selection_scope` and `accession_numbers`. `has_more` describes
pending capturable documents within that scope beyond this batch;
`pending_outside_selection` separately counts currently pending capturable
expectations outside the selected accessions in the supplied inventories. These
are selection progress fields, not whole-inventory or extraction completeness.
Authority-omitted locators retain their existing coverage dispositions; this
selector does not manufacture locators, source coverage, or canonical admission.

### SEC inventory duty scope and retained accession dispositions

`sync_sec_filing_inventory.py` retains the entire parsed SEC submissions population,
including administrative, ownership and registration filings. Existing
`governed-reporting-package-scope@4` selects only periodic/current-report roots and
their package attachments for expected-document duty binding. A governed root with
no primary locator remains `authority_unavailable`; it is not filtered away.
Unclassified forms remain visible and add a failed required scope-validation
component, so their inventory cannot acquire a complete seal.

Apply preserves a hash-bound `sec_inventory_duty_scope.v1` JSON manifest as an
existing evidence blob and `sec_inventory_scope_derived` source observation,
linked by a required `other` inventory component. It contains every accession and
its typed policy disposition, canonical and SEC issuer identities, parser issues,
required component names, and the actual parent source observation IDs/hashes.
This is a software-derived routing receipt, not a SEC HTTP response, document
version, reported financial fact, or extraction/admission receipt. Reconstructing
it compares the complete parser population and ledger-bound parent hashes.
Dry-run and apply both count the required derived component and any failed scope
validation component; only apply returns its persisted observation ID and digest.

Manifest content excludes wall-clock generation time. Exact parent observations,
policy and parsed inputs reuse the original derived observation and its first
local capture clocks; those clocks never replace the referenced SEC acquisition
clocks. A new SEC capture remains a new input even if bytes match. The existing
immutable snapshot/revision contract is unchanged: reusing a revision for a
changed capture fails closed and requires an explicit next revision. Previously
captured raw evidence and failed execution receipts are retained after a routing
failure; no successful snapshot or revision is inferred from those raw captures.
No source duty taxonomy, schema, provider, schedule or financial readiness rule
changes. Mixed-inventory tests use the actual migrated ledger and duty bindings;
network responses alone are synthetic fixtures.

### Investing workflow maintenance and brief inputs

The existing explicit post-earnings HTTP request also accepts paired fiscal
selectors and a paired saved retrieval-trace ID/aware knowledge cutoff. Its
server-configured state root and database remain the only read authorities.
The request verifies saved scope, seals and raw bytes before reuse or synthesis.
It retains exact source spans and partial coverage in the existing artifact JSON.
This extends the existing request contract; it adds no Operations control, job,
provider purpose, acquisition path, schema or schedule. Current-schema synthetic
pipeline, HTTP, cache and raw-byte failure tests establish local behavior only.

Disposition: **no Operations surface change**. The internal post-earnings CLI
adds an optional paired fiscal-period selector with complete-scope validation.
Its scheduled default remains latest reported active portfolio quarters. The
pre-earnings writer retains its rendered prompt inputs and explicit source and
fiscal-identity gaps in the existing artifact JSON. Grounding remains partial.
Neither change adds an operator control, job identity, writer, schema, provider
purpose, or production schedule.

The existing Mac Monthly prompt architecture refresh automation also checks the
project-owned investing skill for changed source hashes and broken references.
Its bounded review does not run the application, acquire financial data, change
owner policy, or mutate canonical Windows state. The deterministic checker has
no network or database access. Readout, brief and maintenance regression tests
establish local behavior; they do not establish deployment or a future job run.
