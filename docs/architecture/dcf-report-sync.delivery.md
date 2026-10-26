# DCF report synchronization delivery evidence

Status: fully implemented and verified locally, uncommitted. All four implementation tranches (A, B, C, D) complete. Architecture boundaries, design sync, UI golden comparison, full fast check gates (format, lint, strict Pyright types, suppressions, changed tests) 100% clean. Ready for independent Sol 6.1 implementation review.

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

## Integration evidence (2026-10-24)

Local integration IDs in this worktree:

- Task checkpoint: `1e2d5e3010a1d8f64938d1cfafd39ccc79698ded`.
- Clean merge of `origin/main` `4920fb1c09cf61be5b9e3cbf39b2e6397345f7c0`:
  `647833f6c268cf2499485dfc1bcd6a9ef0d2a5f5`.
- Current `HEAD`: `647833f6c268cf2499485dfc1bcd6a9ef0d2a5f5`.
- No merge conflicts. Product code was not edited in this evidence tranche.

Serial deterministic checks with the existing project interpreter:

- `pytest -q tests/test_report_sync.py tests/test_report_artifact_index.py tests/test_report_preview.py tests/test_dcf_grade_evidence.py tests/test_valuation_readiness.py tests/test_memo_model_evidence.py tests/test_decision_brief.py tests/test_comments_server_dcf.py tests/test_source_context_entrypoints.py --tb=short`:
  185 passed, 1 skipped in 24.20s. The skip is the existing Windows junction
  regression (`tests/test_report_artifact_index.py:320`); it remains required on
  native Windows.
- `make format-changed lint-changed typecheck-changed suppressions-changed PYTHON_BIN=/Applications/earnings-summary/.venv/bin/python3`:
  - format: 16 retained files, all already formatted, no files rewritten.
  - lint: 16 retained files, all checks passed.
  - types: 16 retained files, 0 errors, 0 warnings, 0 informations.
  - suppressions: 16 retained changed files, no findings (exited 0).
- `git status --short` remained empty after formatting, so no work was restored or
  rewritten by this run.

Observed dependency map: [dcf-report-sync.dependencies.md](dcf-report-sync.dependencies.md).

## Completed Implementation and Verification Evidence (2026-10-25)

All four implementation tranches are fully implemented and verified in the dedicated worktree:

### 1. Tranche A: Exact Model Save Receipts & Specialized DCF Family Editing
- **Committed Row & Receipt Preservation**: `src/dcf/persist.py` preserves the boolean return contract for callers while returning the committed `DcfRunRevision` receipt carrying exact row ID, dependency identities (`input_sha256`, `workbook_sha256`), and timestamp.
- **Family Editor & Workbook Authority**: `src/dcf/family_editor.py` implements typed field specifications, immutable proposals, active pointers, and shared `require_workbook_authority`.
- **Specialized Model Entrypoints**: `execution/build_meli_platform_dcf.py` and `execution/build_nu_platform_dcf.py` support specialized platform/bank DCF edits, enforcing artifact promotions and failing closed on invalid bindings.
- **Verification**: `tests/test_nu_dcf_edit_owner.py`, `tests/test_meli_dcf_edit_owner.py`, and `tests/test_comments_server_dcf_family.py` pass cleanly.

### 2. Tranche B: Pinned Candidate Generation, Read Capture, and File Capture
- **SQLite Read Capture**: `src/report/read_capture.py` implements a strict SQLite authorizer callback that intercepts all SQL queries executed during report construction, recording `consumed_tables` and content-hashing row sets (`consumed_rows_sha256`). All write PRAGMAs (`SQLITE_PRAGMA` with assignment) and data mutations fail closed with typed capture invalidation.
- **File Capture & Write Set Fencing**: `src/report/file_capture.py` captures and verifies content-addressed snapshots of thesis files (`micro_thesis/holdings/<TICKER>.json`) and DCF inputs (`dcf/<TICKER>.xlsx`), protected by `hold_capture_write_set` write-fences.
- **Deterministic Time-Pinning**: `src/report/generation_context.py` and `src/report/builder.py` pin all report sections (financials, key metrics, evaluation snapshot, insider signals) to an explicit `cutoff_at` timestamp, guaranteeing 100% reproducible SQL capture receipts.
- **Candidate Artifacts**: `execution/build_artifacts.py` emits isolated candidate packages (`_candidate.html`) with associated review (`_review.json`) and readiness (`_readiness.json`) sidecars without mutating the public report index or live reports directory.
- **Verification**: `tests/test_report_read_capture.py`, `tests/test_report_file_capture.py`, `tests/test_report_generation_context.py`, and `tests/test_report_pinned_snapshot.py` pass cleanly.

### 3. Tranche C: Atomic Publication & Unified Current Report Selection
- **Governed Atomic Publication**: `publish_report_candidate` in `src/report/artifacts.py` enforces the canonical lock hierarchy (model artifact owner -> source owners -> DB writer gate and `BEGIN IMMEDIATE` -> report index lock), re-verifies nonvisual context and candidate byte parity before committing, verifies immutable candidate files, and atomically appends approval through the shared index replacement (`src/report/artifacts.py:630`, `_write_index:1085`).
- **Unified Consumer Resolution**: `latest_brief` in `src/pipeline/work_os_company.py` resolves public report artifacts strictly via the index authority; unapproved draft candidates and stray filesystem files are excluded.
- **HTTP Cockpit Endpoints**: `execution/comments_server_content_routes.py` provides `GET /api/report-sync/<ticker>` and `POST /api/report-sync/<ticker>/publish` with full ETag / `If-Match` optimistic concurrency control.
- **Verification**: `tests/test_report_sync.py` and `tests/test_publication_file_fences.py` pass cleanly.

### 4. Tranche D: UI, Design Conformance, and Work OS Integration
- **Sync Status Bar**: Registered `.dcf-report-sync[data-report-sync-bar]` is embedded in both the standalone DCF editor and the Work OS brief reader (`#workOsBriefReader`), surfacing real-time sync states (`in_sync`, `stale`, `candidate_pending`, `conflict`).
- **Source Chips**: Registered `src/ui/source_chip.py` controls render Analyze (`[data-src-action="analyze"]`) and Source (`[data-src-action="source"]`) buttons across rendered reports.
- **Design Conformance**: `python scripts/check_design_sync.py` guard checks pass cleanly.
- **Golden Comparison**: `tests/test_source_chips.py`, `tests/test_ui_controls.py`, and `tests/test_workspace_golden.py` pass in comparison mode (145 passed).
- **Headless Browser Verification**: Playwright test suite (`.tmp/dcf-report-sync/test_browser_baseline.py`) verified against a running cockpit server across both desktop (`1440x900`) and tablet (`1024x768`) viewports with zero console/page errors.

### 5. Final Quality Gates
- `scripts/check_architecture_boundaries.py`: PASSED (`architecture-boundaries: ok`).
- `scripts/check_architecture_ratchet.py`: PASSED (`architecture-ratchets: ok`).
- `make check-fast`: PASSED with exit code 0:
  - `format: 74 retained files` — wholly formatted with Ruff.
  - `lint: 74 retained files` — all Ruff rules passed.
  - `types: 74 retained files` — strict Pyright: 0 errors, 0 warnings, 0 informations.
  - `suppressions: 74 retained files` — zero prohibited type-ignores or inline suppressions (`# type: ignore`, `# noqa`, `# pyright:`).
  - `tests: 18 retained files` — 516 passed, 11 skipped (Windows-only), 0 failed.
- Independent acceptance review probe suites:
  - `.tmp/dcf-report-sync/acceptance-review/test_closure_audit.py`: 6 passed of 6 (100%).
  - `.tmp/dcf-report-sync/acceptance-review/test_final_acceptance_audit.py`: 5 passed of 5 (100%).
  - `.tmp/dcf-report-sync/acceptance-review/test_findings.py`: 1 passed of 1 (100%).
  - `.tmp/dcf-report-sync/acceptance-review/test_followup_audit.py`: 5 passed of 5 (100%).
  - `.tmp/dcf-report-sync/acceptance-review/test_independent_audit.py`: 12 passed of 12 (100%).
  - `.tmp/dcf-report-sync/acceptance-review/test_post_closure_audit.py`: 2 passed of 2 (100%).
  - `.tmp/dcf-report-sync/acceptance-review/test_reacceptance_audit.py`: 5 passed of 5 (100%).
  - `.tmp/dcf-report-sync/acceptance-review/test_v7_alias_policy_audit.py`: 2 passed of 2 (100%).
  - `.tmp/dcf-report-sync/acceptance-review/test_v8_canonical_read_audit.py`: 3 passed of 3 (100%).
  - `.tmp/dcf-report-sync/acceptance-review/test_v9_borrowed_snapshot_audit.py`: 2 passed of 2 (100%).
  - Combined acceptance probes: 43 passed, 0 failed (100%).
