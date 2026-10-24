# DCF and report synchronization

Status: proposed implementation contract. Research checked on 2026-10-24 against
repository commit `901b7bdf7488ff5b2843bd03c5901b35e72349c4`. This document does not
establish implementation, publication approval, deployment, or financial readiness.

## User outcome

Save the DCF through the existing model writer. Return the saved model revision
from the backend. A report that uses another revision offers **Refresh report**.
Refresh prepares a separate candidate. **Approve & publish** makes the exact
reviewed candidate current. Saving a model does not publish a report.

The same report action component must work in the standalone report and the
Work OS reader. Report controls must remain outside the immutable financial body
where practical. Opening a historical report must retain its original identity
and financial values while showing current backend synchronization state.

## Existing owners to retain

| Concern | Existing owner | Required extension |
| --- | --- | --- |
| Model save | `execution/comments_server_dcf_routes.py`, `execution/refresh_dcf.py`, `src/dcf/persist.py` | Return the exact committed revision, including no-op saves and cleanup warnings. |
| File and database consistency | `src/dcf/artifact_promotion.py` | Keep its writer lock, compensating file promotion, and recovery rules. |
| Browser requests | `src/ui/controls.py` (`uiFetch`) | Reuse bounded reads and cancellation. Keep consequential writes under their mutation owner. |
| Report jobs and progress | `src/dispatch_registry.py`, `execution/comments_server_actions_routes.py` | Reuse single-flight jobs and their event streams. Do not add a second task runner. |
| Immutable reports | `src/report/artifacts.py` | Bind each new report to its model and evidence revisions. Retain draft isolation. |
| Financial verification | `src/research/decision_brief.py`, `src/research/memo_model_evidence.py` | Reuse exact-body and evidence checks. A successful render is not verification. |
| Reader composition | `src/pipeline/work_os_runtime.js`, `src/report/renderers/workspace_html.py` | Mount one shared report action component with explicit teardown. |
| Source interaction | `src/ui/source_chip.py` | Extend the shared accessible interaction with analytical drill-through and source access. |

## Library and protocol assessment

Official documentation checked on 2026-10-24. Documentation availability is not a
compatibility or performance test in this repository.

| Candidate | Relevant capability | Decision for this change |
| --- | --- | --- |
| HTTP validators | `ETag`, `If-None-Match`, and `If-Match` provide conditional reads and stale-write preconditions. | Use at the API boundary. Enforce mutation preconditions inside the owning lock or transaction. A check before the lock is insufficient. |
| TanStack Query v5 / query-core | Shared server-state queries, keyed invalidation, and refetch behavior. A core package exists separately from UI adapters. | Consider if shared JSON subscriptions require a client cache. Do not create a custom query-cache framework. The current browser transport already owns deadlines, cancellation, and diagnostics. Any adoption must preserve them. |
| HTMX 2 | Request synchronization, response-triggered events, HTML fragment updates, and an SSE extension. | A credible option for new server-rendered controls. Broad adoption now would overlap the existing reader and request lifecycle. Do not rewrite the DCF editor to obtain one refresh control. |
| Lit 3 | Reusable custom elements with defined connection, disconnection, and update lifecycle. | Consider if report controls develop substantial reusable local interaction state. A small controller using existing controls is sufficient for the first component. |
| Celery | Flask documents background execution through a task queue. | Retain the existing subprocess registry and SSE path. This single-user feature does not show a need for a second queue service. |

The first implementation should add no dependency solely for these controls.
Reuse the installed stack and HTTP validators. Reassess query-core before adding
shared browser caching beyond normal HTTP caching. Reassess Lit before building
a general component lifecycle framework.

Primary sources:

- [HTTP conditional requests — MDN](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Conditional_requests)
- [Query invalidation — TanStack Query](https://tanstack.com/query/latest/docs/framework/react/guides/query-invalidation)
- [query-core source package — TanStack](https://github.com/TanStack/query/tree/main/packages/query-core)
- [Request synchronization — HTMX](https://htmx.org/attributes/hx-sync/)
- [Response events — HTMX](https://htmx.org/headers/hx-trigger/)
- [SSE extension — HTMX](https://htmx.org/extensions/sse/)
- [Component lifecycle — Lit](https://lit.dev/docs/components/lifecycle/)
- [Background tasks — Flask](https://flask.palletsprojects.com/en/stable/patterns/celery/)

## Modular behavior

Use cohesive components at existing ownership boundaries, rather than a new
framework or a file for every action:

1. The model writer owns **Save model**. Its response identifies the committed
   model run and input/engine/evidence hashes. A repeated identical save reuses
   the existing committed revision where the persistence owner permits it.
2. The backend derives report status from that revision and the report's stored
   binding. Do not add an independent persisted dirty flag. Missing bindings are
   **unknown**, not current. A browser event requests revalidation; it is not a
   durable registration receipt.
3. One report action component owns **Refresh report**, job progress, candidate
   access, and **Approve & publish**. It reads backend state when mounted and
   when the page regains focus. Save completion revalidates affected report
   state. Cross-tab notification, if needed, carries identifiers only.
4. The existing generator prepares a draft without replacing dated current files
   or changing the published report. Refreshing model-dependent sections must
   not implicitly collect sources, enable deeper research, or bypass budgets.
5. Publication binds approval to the candidate's exact body and manifest hashes,
   model revision, evidence receipt, and expected current report. Recheck these
   under the publication writer. Reject obsolete approval. Preserve the prior
   report on failure. The existing `draft_preview` exclusion is useful, but is
   not itself an approval-to-publication transition.
6. The shared source interaction exposes **Analyze** and **Source** through
   hover, keyboard focus, and a persistent click interaction. Preserve existing
   locator and calculation lineage. Do not infer source URLs or change a
   historical number to match live state.

## Grounded cache behavior

Cache reuse requires matching dependencies: relevant observation versions,
document and transcript hashes, metric definitions, model inputs, analysis recipe,
and verification contract. Each owner declares only the inputs it consumes.
Use existing provenance and snapshot mechanisms; do not copy the database or
maintain another financial fact store to create a cache key.

Age is useful for collection freshness. It is insufficient to establish that
analysis still matches its inputs. The current bear-case reader checks file age
and `_research_contract`; it does not bind the entry to the exact model/evidence
population. Extend that producer and reader together before treating its cache
as revision-aware.

Reuse verification only for the same complete verification inputs and contract.
A changed model or evidence set requires the affected checks again. Missing proof
must remain visible. A refreshed report cannot inherit analyst acceptance from
an older report with a different body.

Deeper research remains a separate explicit selection of sections and periods.
It uses the same candidate and publication path once complete. Missing source
coverage requires the existing bounded collection agreement. An ordinary report
refresh must not silently become a deeper-research job.

## Acceptance evidence required before delivery

- Save survives page reload and reports its actual backend revision.
- Identical saves and duplicate refresh requests do not create duplicate work.
- Old save or preview responses cannot overwrite newer local edits.
- The standalone report and Work OS reader show the same backend status.
- Unknown bindings, unavailable backend, stale evidence, job failure, and recovery
  warnings have distinct visible states.
- A model or evidence change during review invalidates the old publication
  precondition. Double approval is idempotent for the exact accepted candidate.
- Draft creation and failed publication leave the current report unchanged.
- Cache tests prove reuse for identical dependencies and invalidation for each
  changed dependency class. Warm-cache speed alone does not prove validity.
- Browser evidence covers keyboard use, source interactions, reopening the reader,
  and its supported widths. Run the design-sync and report golden comparison gates.
- Stop test servers and release task resources. No live data collection,
  production write, or publication is required to test this contract locally.

## Execution scope and authority

The owner authorized a separate implementation session on 2026-10-24 to execute,
test, and deploy this feature, subject to independent scope and implementation
reviews scoring strictly above 85/100, with no unresolved blockers. A scope change
requires an updated plan and another scope review before dependent implementation.
This authorization covers the application feature and its required migrations.
It does not approve publication of any NU, MELI, or other financial report candidate,
new financial observations, new source collection, trades, or changes to source policy.

Deliver the model/report workflow, dependency-aware reuse for the affected cached
analysis, and the shared evidence interaction. Reuse existing transcript and say/do
workflows for explicit deeper-section requests. Do not add research schedules,
replace the model engine, or refactor unrelated cache producers. Support the existing
NU/MELI model families through their existing model-specific owners. Do not parse a
bank or platform SOTP workbook as a generic redesigned workbook. No new valuation
formula or model family is authorized. An unsupported field or adapter must have a
specific unavailable state; claiming that a generic editor validates NU/MELI is a blocker.

## Required implementation seams

### Pin generation inputs

Introduce a typed report generation context at the existing builder boundary. It
contains the exact committed DCF row identity, model family, engine/input/evidence
hashes, source/context manifest hash, cutoff, recipe versions, and expected current
report identity. Resolve it before generation. Pass it through the existing section
builders, renderer supporting-panel reads, and readiness checks. Replace affected
implicit latest-model lookups, including `report/sections/snapshot.py`, with reads
by the pinned identity. All readers use the same explicit database authority and
snapshot. An affected reader that cannot honor the context must return unavailable
or fail generation, rather than fall back to latest inputs.

Reuse immutable source documents and observation versions. For mutable analysis
files, capture only the required bytes through their owning producer and bind them
by content hash. Do not copy a whole state tree or database for this feature. A
read transaction supplies consistent database inputs. Do not hold the database
writer lock during generation or model calls. Keep the captured revision in the
candidate manifest. The generated financial body includes a nonvisual context
digest so unchanged displayed values cannot cause artifact identity collisions
between different generation contexts. Report controls do not enter that digest.

An interleaved save must yield a candidate consistently bound to the captured
revision, with an obsolete-publication precondition, or an explicit generation
conflict. It must never yield sections from different model revisions.

### Isolate draft output

Extend the existing `build_artifacts` interface with an explicit candidate mode.
It writes draft intermediates to a task-owned candidate staging directory, then
uses the existing immutable artifact persistence path with `draft_preview`.
It must not overwrite dated workspace/Markdown/section files, update current report
selection, or register a retained artifact. Existing scheduled build behavior is
outside this manual-workflow change. Resolve any collision with an already retained
identical artifact as an explicit reuse result; do not rewrite its status or manifest.

### Separate content verification from publication eligibility

Refactor the existing decision-brief checker into shared content/evidence checks
and a publication-eligibility check. Add a candidate-specific entrypoint that runs
the same content/evidence checks against a draft and returns a typed review receipt
bound to its exact body, context, model, sources, and verification contract. Preserve
public rejection of unapproved drafts and its regression. Public report readiness
and every replay caller must share one publication-eligibility resolver. It accepts
legacy retained artifacts or an immutable draft with a validated exact approval in
the canonical index. It rejects missing, mismatched, corrupt, or inapplicable
approval. Do not keep an unconditional `draft_preview` rejection in a downstream
checker after adopting index-based approval. Candidate-specific verification never
creates publication eligibility or bypasses the shared content checks.
Do not relabel a draft as retained to bypass the guard at `decision_brief.py:1239`.
Do not weaken claim reconstruction, source completeness, semantic admission, or
analyst-acceptance requirements. Missing required review evidence blocks publication
and appears as specific reasons in the report action component. A verified bounded
report remains bounded; user approval does not convert it to whole-issuer decision grade.

### Publish without changing immutable artifact status

Keep the candidate manifest and body immutable, including `draft_preview`. Extend
the existing compact report artifact index with typed publication approval records.
The index remains the sole current-report selection authority. Each record binds
the owner action identity, approved candidate/body/standalone/context hashes,
verification receipt hash, model/evidence revisions, expected previous current
artifact, and approval time. Existing manifests remain readable without a record.

Update every index writer, reconciler, and latest-report consumer together. Drafts
remain excluded unless a valid exact approval record makes that artifact eligible.
Legacy retained artifacts keep their existing selection behavior. Neither a newer
draft timestamp nor a generic proposal approval makes a draft current. A later
publication supersedes current selection without deleting earlier artifacts or
approval history. A missing or corrupt approval must fail closed; a reconciliation
must not invent approval from the presence of draft files.

Replay verifies the artifact's pinned historical model and evidence population,
not the newest model. Current model/evidence revisions are publication preconditions,
not a reason to rewrite historical report facts. A point-in-time baseline replay
also checks that its publication/approval existed by the requested knowledge cutoff;
a later approval cannot make a draft retrospectively published. Keep source cutoff,
verification time, and publication time separate. Add public-reader and NU baseline
replay tests for retained, unapproved, approved, mismatched-approval, and late-approval
cases, with exact body/model bindings.

Use one cross-process writer lock for the shared index, including builds and
reconciliation. A thread-only lock or atomic replace without a read-modify-write
lock is insufficient. Define one lock acquisition order with model/source owners
before implementation. The short publication critical section holds the required
model/evidence owner locks and a database snapshot/write fence while it rechecks
current revision tokens and exact approval preconditions. It then appends approval
and changes current selection in the same existing atomic index replacement.
No generation or network call occurs inside this section. A dependent mutable
source without a usable owner fence is a publication blocker until that seam is fixed.

The prior index is current until replacement succeeds. A crash after replacement
but before the response is recovered by reading the matching approval action identity.
Repeating that exact action returns the original result. A different candidate or
different expected-current identity is a conflict, never an overwrite. This avoids
a database/file dual commit or a second mutable current-report pointer.

### Bound retries and restart recovery

Derive refresh identity from the requested generation context, section selection,
and recipe, not an attempt ID or ticker alone. Retain a small durable request record
with that identity and its candidate/result references under the existing artifact
lifecycle. The subprocess registry supplies execution and progress; it is not a
durable completion receipt. A duplicate completed request reuses its exact validated
candidate. A concurrent duplicate joins the active result. After restart, reconcile
the durable record, actual process ownership, and immutable files before resuming.
Unknown process ownership remains held. Do not launch a second writer or silently
restart a completed stage. Clean only proven disposable staging after recovery.

## Review and release evidence

Use the same fixed review rubric at both boundaries: outcome/scope 20, modularity/reuse
20, consistency/concurrency/recovery 25, evidence/cache/approval integrity 20, and
acceptance/release evidence 15. Require a total strictly above 85 and no unresolved
blockers. Preserve review identity, artifact or commit SHA, findings, disposition,
and proof. A review score does not replace deterministic checks. Judge availability
and purpose qualification must be reported separately; an advisory score is not a
qualified governance PASS. Do not lower the rubric or substitute reviewers to obtain
a preferred score. Re-review a repaired artifact against its new hash.

Before implementation acceptance, run the targeted regression tests plus the complete
applicable repository gate. Use serial targeted pytest, `make check-fast` during
iteration, and `make check` before push (or the documented full CI path). Include
design-sync, report golden comparison, and before/after browser evidence. Test a
save between two section reads, a save during candidate review, concurrent index
writers, duplicate refresh and approval, process restart, and crashes immediately
before and after atomic publication. Test source/recipe/definition changes and
rejection of stale cached financial narrative. Check NU/MELI adapter identity and
the standalone/shared-reader path. No failed deterministic check can be averaged away.

Deployment follows `directives/agent_host_operations.md` and the configured machine
authority. Resolve the actual canonical Windows code/state/runtime/database routes
before remote operations. Use an isolated managed worktree, preserve unrelated edits,
and build one reviewed release commit. Merge through the normal repository path.
The deployed code and clean source checkout must match freshly fetched `origin/main`
and the reviewed release identity. Re-review material code changes after acceptance.

Before installation, preserve the predecessor code manifest and service configuration,
coordinate the affected service/scheduler writers, and retain existing holds. Run
required native Windows file/junction/lock and migration tests with explicit synthetic
state on the actual release bytes. A migration, if required, uses the established
snapshot and restore-readiness gate; no direct production Alembic bypass is allowed.
Do not import financial observations or publish a report as a deployment smoke test.

After installation, verify runtime/source byte identity, configured database routing,
loopback service ownership, native health, the current private HTTPS origin, and a
read-only browser path from the Mac. Use isolated native test state for consequential
save/refresh/publish smoke tests. If installation or verification fails, stop changed
work and restore the retained predecessor code and matching service/watchdog pins.
Preserve legitimate appended data. Do not blindly restore a live database or downgrade
its schema. Keep the reviewable commit and concise evidence; terminate test services
and remove task-owned temporary files and managed worktrees when no longer needed.
