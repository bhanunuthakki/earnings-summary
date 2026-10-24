# DCF report synchronization delivery evidence

Status: partial local implementation, uncommitted. Candidate generation, publication,
shared controls, complete validation, implementation review, merge, deployment, and
live verification remain open.

## Authority and reviewed inputs

- Plan commit: `43cbb81f8ea8c80a31d04a27ce25271c39ae72cb`.
- [Plan](dcf-report-sync.md) SHA256:
  `ff5745f4c6ed026e7782feb3c2532d82b8ffdf90bbe319897cbc13f5e5fad6f3`.
- Reviewed application baseline and freshly fetched `origin/main` on 2026-10-24:
  `901b7bdf7488ff5b2843bd03c5901b35e72349c4`. No application drift was found.
- [Scope review history](dcf-report-sync.scope-review.md): 80 BLOCK, 89 BLOCK,
  then 93/100 with no scope blockers. Each review remains bound to its plan hash.
- The source chat forwarded the human owner's answer on 2026-10-24:
  “Use the task-specific independent review (Recommended).” The source chat is
  `01a165df-ea65-7a32-88bc-da393bd9a0f3`. This task-specific exception applies to
  both scope and implementation boundaries. It accepts separately briefed frontier
  review above 85/100 with no unresolved blockers. It does not establish formal
  Judge calibration or qualification. Deterministic checks remain mandatory.
- Deployment of the feature is authorized after those gates. Financial report
  publication, live model edits, new observations, and source collection remain
  outside deployment-test authority.

## First inspection

The existing owners match the reviewed baseline. These are implementation seams,
not new scope or proof that the planned behavior exists.

| Behavior | Owner and required change | Evidence to add |
| --- | --- | --- |
| Exact model save receipt | `src/dcf/persist.py`, `execution/refresh_dcf.py`, `execution/comments_server_dcf_routes.py`; preserve the Boolean persistence API for existing callers while returning the actual committed row and dependency identities through Save. Do not resolve an unfenced later current row as the saved revision. | New version, identical save, cleanup failure after commit, concurrent save, reload. |
| NU/MELI editing | Existing bank and platform SOTP model-specific owners; audit their actual input and persistence paths before extending the generic route. | Real family inputs and formulas; explicit unsupported-field results. |
| Pinned generation | `src/report/builder.py`, snapshot section, supporting panels and provenance readers; reuse `report_read_snapshot`. | Save between section reads; no implicit latest lookup or mixed context. |
| Candidate isolation | `execution/build_artifacts.py`; current `_build_one` writes dated workspace, Markdown and section files and runs artifact synchronization before persistence. Candidate mode must separate those effects. | Current files/index selection stay unchanged on candidate success and failure; no implicit collection. |
| Shared index writer | `src/report/artifacts.py`; its persist, reconcile, migration, rollback and fiscal-period backfill paths all read/modify/write the shared index. `_write_index` alone cannot protect an earlier read. | Real process contention, distinct ticker preservation, crash recovery. |
| Eligibility and publication | Same artifact owner plus `src/research/decision_brief.py`; preserve immutable draft manifests and replace unconditional public draft rejection with the shared exact-approval resolver. | Unapproved/approved/mismatched/corrupt/late approval; exact action retry; crash before/after replacement. |
| Historical evidence | `src/research/memo_model_evidence.py`, DCF evidence/readiness owners and NU baseline replay; current model verification reads latest readiness. | Pinned historical model and evidence replay after later saves; point-in-time approval. |
| Latest report consumers | Brief Library, company desk and `/reports/<ticker>`; company desk currently takes the newest indexed item directly, while the route also consults dated workspace files. | Each current-report surface uses the same eligibility and selection authority. |
| Cache dependencies | `src/report/sections/bear_case.py` producer and reader; currently age plus research contract. Include the exact consumed inputs through existing provenance. | Identical reuse; observation, document/transcript, definition, model, recipe and contract invalidation. |
| Shared report actions | `src/ui/controls.py`, Work OS runtime and workspace wrapper; reuse `uiFetch`, registry/SSE and explicit teardown. | Both wrappers; duplicate requests, restart, loading, unavailable, failure, recovery, focus and stale responses. |
| Evidence interactions | `src/ui/source_chip.py` and registered surface masters. | Analyze and Source on hover, focus and persistent click; preserved locator/calculation lineage. |
| Explicit deeper sections | Existing transcript and say/do research workflows. | Explicit section/period selection; no implicit source collection or new schedule. |

The implementation must establish one lock order before changing publication.
Acquire model artifact owners first, then required mutable source owners, then the
database writer gate and a writer-excluding SQLite transaction, and finally the
shared report index lock. Do not acquire those upstream owners from an index
critical section. A read snapshot alone is not a writer fence. Existing DCF Save
is outside the daily-chain gate, so publication must use SQLite exclusion as well
as the applicable owner locks. A mutable dependency without a usable owner fence
blocks publication. Generation and network calls stay outside this short section.
Resolve actual source-owner ordering during the dependency audit; do not invent a
general cache or locking framework to conceal an unsupported dependency.

## Baseline checks

On the unchanged application baseline, run serially with the existing project
interpreter and the repository's synthetic test isolation:

```text
PYTHONDONTWRITEBYTECODE=1 /Applications/earnings-summary/.venv/bin/python3 -m pytest -q tests/test_report_artifact_index.py tests/test_report_preview.py
14 passed, 1 skipped in 2.46s
```

The skipped test is the Windows junction regression. It remains required on the
native release bytes. This result is baseline evidence only. It does not verify
the planned feature, browser behavior or native Windows behavior.

## Local implementation checks

The first local changes add exact DCF persistence receipts without changing the
Boolean `upsert` API. Save returns the receipt captured by the persistence owner,
including a committed save with a cleanup warning. New tests cover identical
persistence, superseding versions and a second save immediately after commit.

All report-index mutators now hold the existing cross-process JobLock across the
complete read/modify/write. The lock follows the resolved output directory, so
separate code checkouts cannot own the same output independently. Read-only
migration and backfill previews do not acquire a write owner. Tests cover process
exclusion, contention from another execution context and simultaneous ticker
writers preserving all six artifacts. Approval records are not implemented yet.

The DCF evidence and readiness readers accept an exact run ID. They keep the
default current-row behavior for existing callers. A pinned missing row or a row
for another issuer returns missing without selecting current inputs. Memo model
verification now uses its committed historical run. Publication preconditions and
historical approval clocks still require implementation.

Serial checks on these partial changes:

- First artifact/index/preview checks: 16 passed, one native Windows junction test
  skipped. The later changed-test run also includes the new concurrent ticker test.
- Changed tests for persistence, redesigned Save and index ownership: 116 passed.
- Evidence, valuation readiness, memo model and DCF route tests: 114 passed.
  The first run exposed an old test substitute that did not accept the new optional
  run ID. Its signature now accepts that field and asserts it is absent in the
  existing unpinned corruption matrix. The corruption checks remain unchanged.
- Changed-file format, Ruff, strict Pyright and suppression checks pass. No inline
  suppression was added.
- `make check-fast` remains failed at the frozen architecture-size ratchet. The
  first measurement found 91 added noncomment lines against the unchanged baseline.
  Further changes followed that measurement. A measured baseline refresh must be
  explicit in the implementation review; this failure is not an acceptance PASS.

No visible controls were changed. Before/after browser evidence and all final
release gates remain required. No application LLM call or source collection ran.

## Capacity and remaining gates

The account snapshot at 02:38 Pacific on 2026-10-24 reported ordinary usage
unavailable, the five-hour window at 100%, and reset at 06:41:41 Pacific. No worker
or metered fallback was launched. Respect the protected 03:00–05:00 Pacific window
and recheck capacity before dispatch. The owner's review exception resolved the
review-policy hold; it did not authorize a quota or protected-window bypass.

Before delivery, complete the plan's regression matrix, `make check-fast`, full
applicable checks, design sync, golden comparison and visual diff review,
before/after browser evidence, separate implementation review, normal push/merge,
and canonical Windows deployment and read-only live verification. Resolve the
host route fresh through machine operations. Keep rollback bytes, runtime
configuration, existing operational holds and durable state under their owners.

The attached implementation worktree remains needed for this unfinished task.
No database, state tree or environment was copied. No test server, listener,
worker or monitor was started. Retire test scratch through its existing owner
and archive this managed worktree only when its retained work is no longer needed.

## Current corrections (2026-10-24)

The following corrections are appended. All earlier evidence remains preserved as
historical evidence.

- The initial "fresh main/no drift" finding at application baseline
  `901b7bdf7488ff5b2843bd03c5901b35e72349c4` is historical and superseded by fresh
  main `4920fb1c09cf61be5b9e3cbf39b2e6397345f7c0`.
- Approval-record TYPES and shared eligibility foundations now exist, but there
  is no complete append/publication transaction. The earlier statement that
  approval records are not implemented is stale.
- 52 report-preview/memo/decision-brief tests passed after the eligibility
  refactor. This is not a full feature gate. Do not sum overlapping test runs;
  the separate counts above cover overlapping code and are not an aggregate.
- Independent Sol PRD review: PASS 92/100 with no unresolved scope blockers, bound
  to the exact plan hash and fresh main. Scores: 19/20 outcome, 19/20 reuse, 23/25
  consistency, 18/20 financial/cache/approval, 13/15 acceptance.
- The human authorized a task-specific advisory exception. It establishes no
  formal calibration or qualification claim. The Sol PASS is a PRD review, not
  implementation acceptance. Partial implementation remains BLOCK for acceptance.
- Open blockers include actual NU/MELI adapters, pinned candidate generation,
  unified current-report selectors, fenced atomic publication, durable
  dedup/recovery, complete validation and UI. No new product scope is needed.
- Latest human routing instruction supersedes the earlier GLM preference: use the
  exact `deepseek/deepseek-v4.1-flash-1017` via OpenRouter on the cheapest reliable
  eligible endpoint. Root only orchestrates; execution is delegated. Sol judges
  PRD/plans and implementation strictly above 85 with no blockers before CI and
  merge. This is dev-agent tooling only; do not change application model/provider
  routing or add a product dependency. Use the existing OpenCode 1.19.4 native
  client. Floor: 99% reported uptime over 30 minutes; refresh before every
  dispatch; pin eligible exact provider slugs; no fallback outside the eligible
  set.
- Current verified quotes in USD per million tokens: `open-inference/fp4` $0.02
  input, $1 output, $0.015 cached input; `sail-research/fp4/us` $0.101 input,
  $0.15 output, $0.006735 cached input. Both are FP4 and reported 100% 30-minute
  uptime. OpenInference is the read-heavy document candidate; Sail is the
  lower-cost code-output candidate. Choose by expected total request cost with
  measured input/output/cache/retry usage. Quantized endpoint quality remains
  unverified until bounded local work and deterministic/Sol review; never claim
  broad qualification or an SLA. `DeepInfra/fp8/flex` reported 86.67% uptime and
  is ineligible. Source:
  https://openrouter.ai/api/v1/models/deepseek/deepseek-v4.1-flash-1017/endpoints
- This task uses OpenInference as a bounded mechanical trial. Future code
  tranches can use Sail if the cost estimate supports it.
- No live model changes, source collection or financial report publication as
  smoke tests.
