# DCF report synchronization scope review

Date: 2026-10-24. Recommendation: **BLOCK pending plan repairs**, as an ad hoc
independent advisory review. This review has no calibrated blocking-Judge
qualification. The generic governance registry is shadow-only. This recommendation
does not authorize deployment or financial publication.

Inspected commit: `901b7bdf7488ff5b2843bd03c5901b35e72349c4`.

Reviewed artifact: `docs/architecture/dcf-report-sync.md` (uncommitted).
SHA256: `6edf514faa5cbdb7b36134fd498ff1ff5f44669f861632b851e73530f4d7b818`.

| Category | Score |
| --- | ---: |
| User outcome and bounded scope | 19 / 20 |
| Modularity and reuse | 18 / 20 |
| State consistency, concurrency, and recovery | 18 / 25 |
| Financial evidence, caching, and approval integrity | 16 / 20 |
| Executable acceptance and release plan | 9 / 15 |
| Total | **80 / 100** |

The plan captures the intended user outcome. It keeps DCF save separate from
report publication. It reuses the request, job, artifact, and source-control owners.
Its cache rule correctly requires matching dependencies rather than file age.
Its acceptance list covers several important races and failure states. However,
the critical draft verification and publication transition is still unresolved.
The exact model-to-render binding and the authorized release are also underspecified.

## Findings and minimal repairs

### 1. P1 — Define a verifiable candidate and its publication transition

Plan references: lines 27, 77–84, and 104–107.

`src/research/decision_brief.py:1239` rejects `draft_preview` before its evidence
checks. `src/report/artifacts.py:391` rejects changing the publication status of
an existing immutable artifact identity. The available preview entry point is
limited to pre/post earnings drafts (`src/research/report_preview.py:31`), not the
full workspace report. The full generator writes dated workspace, Markdown, and
JSON outputs before registering a retained artifact
(`execution/build_artifacts.py:651`, `execution/build_artifacts.py:691`).

The plan recognizes that draft exclusion is not a publication transition, but
does not choose the needed extension. An implementation cannot simply reuse
these functions unchanged. Marking a candidate retained so the verifier can run
would expose it before approval. Changing its manifest after approval would also
change a hash that the approval was meant to bind.

Minimal repair: specify the candidate build entry point and its isolated output
paths; separate reusable content verification from published-state eligibility;
define an immutable candidate manifest and a separate publication/approval record
or an equivalent explicit transition. State the authority for the current report,
the compare-and-swap operation, and the ordering of durable writes. Preserve the
exact reviewed body without regeneration. Require a crash/retry test at each
publication boundary and a test that candidate verification never exposes it in
the current-report readers. This is a scope blocker.

### 2. P1 — Bind generation reads to the revision named by the candidate

Plan references: lines 66–82 and 92–96.

The save writer can supply a precise revision under its existing owner. However,
the report builder currently takes a ticker and connection, not a selected DCF
revision (`src/report/builder.py:45`). Its valuation reader selects the newest row
at read time (`src/report/sections/snapshot.py:212`). The full generator also reads
sections and provenance separately (`execution/build_artifacts.py:632`). Recording
a model ID in the final manifest does not prove those reads used that model.

Minimal repair: define when the model/evidence population is captured, how the
selected DCF row reaches every affected section and verifier, and how mutable file
inputs are sealed or hash-checked. Reuse `report_read_snapshot`
(`src/research/report_contract.py:90`) where it fits. A model save during generation
must either produce a coherent candidate bound to the older revision and marked
obsolete, or stop and require a new request. It must never attach the new revision
to a mixed body. Add an interleaved-save generation test. This is a scope blocker.

### 3. P2 — Specify durable retry identity without adding another task runner

Plan references: lines 25, 73–76, and 117–124.

The reused registry is explicitly in memory and does not recover jobs across
server restarts (`src/dispatch_registry.py:11`). Its key is `(ticker, kind)` and it
starts fresh work after the prior job completes (`src/dispatch_registry.py:211`).
It supplies single flight, not durable request idempotency. The report index is a
shared read-modify-write JSON file (`src/report/artifacts.py:434`), so different
tickers also share one write set.

Minimal repair: define a request/candidate key from the selected dependencies,
resolve completed candidates before starting work, and state recovery after loss
of the HTTP response, process exit, and server restart. Name the shared index and
publication writer lock. Keep the existing registry for execution. Add restart,
lost-response, and two-ticker index-preservation tests.

### 4. P2 — Add the release and operational-contract steps

Plan references: lines 114–130.

The acceptance list is useful but has no implementation sequence, mapped test
owners, migration/backward-compatibility plan, or deployment/rollback evidence.
The user authorized deployment. The repository also requires an operational
surface update or a tested no-surface-change disposition (AGENTS.md, Authority
map), full applicable checks, and canonical-host release alignment
(`directives/agent_host_operations.md:17`). Local tests alone cannot close that
authorized outcome.

Minimal repair: add a short ordered sequence for revision binding, candidate
construction, verification/publication, shared UI, and release. Map each critical
acceptance item to a test module. Specify old manifests as unknown, migration and
rollback handling, `make check` or the permitted CI equivalent, design and golden
checks, before/after browser evidence, canonical-host deployment validation, and
resource cleanup. Keep financial candidate publication outside deployment proof.

## Evidence limits

This is a scope review of the named plan and bounded repository slices. I read
the worktree AGENTS.md and the judging skill. I inspected save/persistence,
artifact indexing, report generation, preview/readiness checks, report snapshots,
job registry, bear-case cache, and source-chip interaction owners. No application
code was changed. No tests, services, model calls, live data access, or remote
actions were run. Implementation, browser, migration, fault-injection, latency,
and deployment evidence remains required at delivery. External library claims
were not independently revalidated; no new dependency is proposed.

A passing scope recommendation requires more than 85/100 and no unresolved
blockers. Repair findings 1 and 2, then reassess the revised artifact hash. This
review's score must not be carried forward to changed plan bytes.

## Second review — repaired plan

Date: 2026-10-24. Inspected baseline remains
`901b7bdf7488ff5b2843bd03c5901b35e72349c4`. No application code changed.
Reviewed plan SHA256:
`064d0cc9fadec1f673004a13d47acb94a6e3a2a145e1d8ca65b4d9a7254203bb`.

Recommendation: **BLOCK pending one remaining contract repair**. This is an ad hoc
independent advisory recommendation, not a qualified governance gate. The score
exceeds 85, but the unresolved blocker prevents a passing scope recommendation.
The first review above remains unchanged and applies only to its earlier hash.

| Category | Score |
| --- | ---: |
| User outcome and bounded scope | 19 / 20 |
| Modularity and reuse | 19 / 20 |
| State consistency, concurrency, and recovery | 22 / 25 |
| Financial evidence, caching, and approval integrity | 16 / 20 |
| Executable acceptance and release plan | 13 / 15 |
| Total | **89 / 100** |

### Disposition of the original findings

1. **Candidate transition: partly resolved.** Plan lines 178–232 choose isolated
   output, immutable candidate bytes, separate approval records, and atomic index
   publication. Those are concrete extensions of the existing owners. One public
   verification conflict remains, described below.
2. **Generation pinning: resolved at the design level.** Lines 155–176 require a
   typed context, selected-row reads, consistent database authority and snapshot,
   pinned file bytes, and a context digest in artifact identity. They explicitly
   reject fallback to latest and define the interleaved-save result. Implementation
   must still prove this through every affected reader and model verifier.
3. **Retries and shared index ownership: resolved at the design level.** Lines
   218–244 require a cross-process index lock, durable request identity, exact
   completion reuse, and process reconciliation before restart. A second task
   runner is not introduced.
4. **Release plan: substantially resolved.** Lines 257–288 add deterministic and
   rendered evidence, native Windows tests, reviewed release identity, production
   routing, rollback, and cleanup. The detailed test-module mapping can be a routine
   implementation step. The repository's operational-surface update/disposition
   requirement still applies.

### Remaining P1 blocker — approved drafts must have a public verification path

Plan lines 194–195 preserve the existing public retained-report guard and its
draft-rejection regression. Lines 203–211 keep the approved artifact's manifest
permanently `draft_preview` and make it eligible through an exact index approval.
Those rules conflict if the existing guard remains unconditional.

At the inspected baseline, `_assess_decision_brief` rejects that status before
checking content (`src/research/decision_brief.py:1239`), the public wrapper returns
the rejection as a non-ready receipt (`src/research/decision_brief.py:1571`), and NU
baseline replay calls the same checker (`src/research/decision_brief.py:983`). Thus
an artifact can become the approved current report while remaining impossible to
verify or reuse through the public readiness path. Candidate-only verification
does not repair that post-publication behavior.

Minimal repair: preserve rejection of **unapproved** drafts. Explicitly make
publication eligibility accept either a legacy retained artifact or an immutable
draft with a validated exact canonical-index approval. Route public readiness and
historical replay through that eligibility owner. A caller-provided boolean or
edited manifest must not establish approval. Retain the immutable body and
manifest; distinguish historical approval from whether the model remains current.
Require tests for an unapproved draft, a valid approved draft, a forged/mismatched
approval, and an approved NU baseline replay. This closes the remaining blocker.

### Implementation clarifications

- **Database fencing:** lines 220–226 require owner locks and a database
  "snapshot/write fence." Make the writer-excluding fence explicit in the lock
  design. A read snapshot supplies coherent reads; it must not be treated as proof
  that another writer cannot commit a new revision before index replacement.
  Require contention tests with the actual DCF save and source writers. I treat
  the plan's required owner fences and its explicit publication-blocker rule as
  the controlling intent, so this is a clarification rather than another blocker.
- **Approval-preserving index evolution:** lines 210–216 correctly require all
  writers and reconcilers to preserve approvals. Include read/filter/rewrite helpers
  and rollback compatibility in that audit. Existing `load_report_artifact_index`
  reconstructs the model from only timestamp/items (`src/report/artifacts.py:299`),
  as does `_write_index` (`src/report/artifacts.py:358`). An older writer could drop
  new approval fields after rollback. Test the chosen schema compatibility rule
  or hold incompatible writers; do not infer approval from retained files.
- **Current-model verifier reuse:** `src/research/memo_model_evidence.py:646`
  loads current readiness and requires its run ID to match the commitment. The new
  generation-context contract must reach this seam too. Preserve the difference
  between a historically valid pinned report and permission to publish it as the
  current report. The plan already requires pinned readiness checks and obsolete
  publication detection; implementation evidence must demonstrate both.

### Evidence limits for the second review

I verified the new plan hash and unchanged baseline. I read the added sections and
rechecked the artifact index, DCF artifact owner, public readiness/replay, model
evidence verifier, and host release runbook. No tests, services, model APIs, live
data access, or remote actions were run. Only this review file was changed. The
next review must bind to the repaired plan's new hash. A scope PASS, if earned,
will not establish implementation completion or approve any financial candidate.

## Third review — public eligibility and historical replay repair

Date: 2026-10-24. Inspected baseline remains
`901b7bdf7488ff5b2843bd03c5901b35e72349c4`. No application code changed.
Reviewed plan SHA256:
`ff5745f4c6ed026e7782feb3c2532d82b8ffdf90bbe319897cbc13f5e5fad6f3`.

Recommendation: **PASS for scope, as an ad hoc independent advisory review**.
No unresolved scope blocker was found in this bounded final review. This is not
a calibrated or purpose-qualified governance PASS. Whether this advisory review
can satisfy the task's acceptance gate remains an owner decision. It authorizes
neither deployment by itself nor publication of a financial report candidate.
Both earlier reviews remain unchanged and bound to their respective hashes.

| Category | Score |
| --- | ---: |
| User outcome and bounded scope | 19 / 20 |
| Modularity and reuse | 19 / 20 |
| State consistency, concurrency, and recovery | 22 / 25 |
| Financial evidence, caching, and approval integrity | 19 / 20 |
| Executable acceptance and release plan | 14 / 15 |
| Total | **93 / 100** |

### Remaining blocker disposition

**Resolved at the design level.** Plan lines 194–200 now require one eligibility
resolver for public readiness and replay. It accepts legacy retained artifacts or
an immutable draft with validated exact canonical-index approval. It rejects
missing, mismatched, corrupt, or inapplicable approval and forbids a downstream
unconditional draft-status guard. This resolves the conflict identified at
`src/research/decision_brief.py:1239` without changing the immutable manifest or
making candidate verification itself an approval mechanism.

Lines 224–231 also require replay of the historical model/evidence pins and keep
current revisions as publication preconditions. The publication knowledge-cutoff
check prevents a later approval from making an earlier draft retrospectively
published. The new public-reader and NU replay test cases cover the specific
status and time-order risks. The evidence/approval and acceptance scores increase
for these concrete repairs; the other category scores are unchanged.

### No new blocking contradiction found

The canonical-index publication record and immutable candidate manifest now have
distinct, compatible roles. The index remains the sole publication/current-report
selection authority. Approval is committed with selection through one atomic
replacement. Historical content remains pinned when later model saves make a
new publication obsolete. The candidate-specific verifier supplies content proof
without granting publication eligibility.

The second review's implementation clarifications remain applicable: use a real
writer-excluding database fence at publication; define and test lock ordering;
preserve approval records through all index helpers and compatible rollback; and
replace affected current-only model verification with the pinned historical path.
These are required implementation and verification work under the plan, not
evidence already achieved by this scope review. A failure to satisfy them can
block implementation acceptance even though this scope recommendation passes.

### Evidence limits for the third review

I verified the exact plan hash, baseline commit, and unchanged application status.
I reviewed the repaired eligibility, replay, publication, recovery, and release
sections against the previously inspected owners. No application code, tests,
services, model calls, or remote operations were run. Only this review file was
changed. Implementation, financial grounding, browser behavior, Windows locking,
fault recovery, and deployment still need the evidence specified in the plan.
