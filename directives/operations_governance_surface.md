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

This capability has no schedule, service, retry control, database write, or Operations-workspace
button. Its current supported state is `hold`: deterministic resolver readiness is visible in the
receipt, while reader activation requires separately approved portfolio evidence and an explicit owner
decision. The CLI is the primary operator surface for this rehearsal; no current-health projection is
claimed from an old receipt.

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
