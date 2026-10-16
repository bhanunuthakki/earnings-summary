# PR 1695 reconciliation

The source candidate combines the performance and consistency refactors with
current `main` (`dc942936`). The owner's original goal is faster, more consistent,
robust research with verifiable financial lineage, fixed and free-form semantic
search, and less code sprawl. This is source integration, not deployment.

## Corrections from independent review

- Preserve both historical Alembic chains through a merge revision. Do not rename
  historical migration identities. The extractor-identity migration follows that
  merge. Synthetic upgrades from either prior head retain both schema branches.
- Preserve current main's financial admission, immutable reporting projection,
  database authority, source-root forwarding, and native observation controls.
  Independently merged search features have one implementation in the combined
  tree.
- Reuse SEC completion receipts only when issuer, raw content and extractor
  fingerprints agree. The identity covers the transitive extractor import closure, governed
  dynamic adapter subtree, referenced vendor dependencies, active metric ladders
  and database schema. Unrelated UI files do not invalidate SEC receipts. Missing
  inputs, linked source files, unknown loaders and relevant in-process source
  replacement fail closed. An obsolete conditional response forces a fresh fetch.
- Stamp report fact chips with the exact admitted document version. A legacy
  document ID resolves only when its version is unique. Ambiguous and missing
  identities stay visibly degraded; the original and corrected versions remain
  directly readable.
- Retire unused current-selection mirror writes and convergence scans. Canonical
  reads still use the immutable resolution ledger and resolved views. Keep the
  historical schema and checkpoint fields for upgrade and resume compatibility.
  No cache read-speed gain is claimed.
- Apply the writer acquisition deadline to stale-lock removal failures. Release
  claims even when connection setup, close or tracing raises. Preserve the
  original failure and retain safe cleanup error types. A live writer is not
  evicted. A persistent operating-system guard serializes stale takeover and
  token-checked release. Unreadable holder metadata and unknown process state
  retain ownership. Transient ownership-read failures retry within the same
  deadline, then return a typed lock-held error with an explicit unknown state.
  Cleanup retries preserve the original error. Windows explicitly unlocks each
  acquired guard before close. Acquisition remains bounded; holder lifetime and
  FIFO fairness are not guaranteed.
- Start memo and response-cache age before the data build. Expired builds
  return directly without entering either cache. Remove the redundant startup
  retrieval probe; actual requests retain fail-closed readiness checks.
- Pass the selected database through discovery commands, build children and
  card rendering. Missing database authority fails closed.
- Reconcile the canonical-helper inventory against both parents while retaining
  the existing oracle captures and collapsed-site history.

## Verification and limits

Targeted source, migration, cache, citation, concurrency and recovery tests use
explicit disposable databases. Search tests exclude live providers. The project
format, lint, changed-file strict typing, suppression and design-sync checks
remain enabled. Whole-tree quality ceilings only decrease.

The first broad local test run passed 2,391 tests before stopping at one stale
CI timing-table coverage failure. Retained timing samples from both parents are
combined; new measurements retain their actual local timing provenance. This
interrupted run is not a full-suite pass. The final PR CI matrix is required. The new timing batch passed 286 tests.
The first endpoint/browser integration batch passed 386 tests and exposed timing
and text-golden failures. Those expectations were corrected and reviewed. The final integration run
passed 581 tests and exposed two obsolete scheduler-count expectations after
the deliberate task removal. Their targeted rerun verifies 45 declarations,
44 native registrations and absence of the removed scheduled writer. The
closure batch passed 213 tests with five optional benchmark-database skips.
Its in-progress graph manifest held correctly; the frozen manifest then
passed all 81 reachability tests. There are zero production unknown or
unresolved edges and zero graph diagnostics. Changed-file strict checks
pass; the complete static gate passes at lower exact ceilings (2,810 files,
2,050 inherited diagnostics and 1,856 inherited suppressions).
Golden and CI-helper comparison checks then passed 138 tests. The golden change is limited to accurate missing-or-
ambiguous version wording in source rows and citation popovers.

Synthetic rendered comparisons cover Cockpit, Company Desk and Performance at
1440x900 and 390x844. The selected screen is activated through production
navigation. Each final image matches the corresponding main image byte for
byte, and browser page errors are absent. These fixtures prove rendering parity,
not live data readiness, real-user latency or canonical-host throughput.

Architecture and duplication baselines use unchanged scanners for the combined
source population: 1,451 modules and 551,945 noncomment lines. Exact duplication
is 4 groups / 158 lines versus main's 6 / 238. Near duplication is 147 groups /
9,867 lines versus main's 162 / 11,266. The largest dependency cycle falls from
25 modules to 16; maximum internal fan-out falls from 137 to 40. Source size is
2,948 noncomment lines above main after retaining the PR's runtime changes.
No module path, size-threshold crossing or cycle membership is new relative to
the parent union. One existing pair of cache reservation methods crosses the
fixed near-match threshold (0.848947 to 0.856842) after both carry their build
start clock. This is an explained detector change; it does not justify a new
shared cache abstraction with different keys, entries and busy behavior.

The original Opus 5.5 review scored the unreconciled PR 6/10 and blocked it.
The first committed-source follow-up scored 70/100 and found an incomplete
extractor closure, stale-lock takeover risk, helper inventory drift and smaller
cache/boot/database issues. These are addressed before final acceptance.
The proposed daily backfill task is removed; the bounded CLI stays manual.
Its specific findings drove the corrections above. A separate review of the
final source against the original goal is required before merge. Its score is
advisory evidence; tests and merge rules still govern acceptance.

No production database, installed application, live scheduler or provider policy
was changed. Production timing and live sealed retrieval remain outside this
source-integration task. Native Windows proof is limited to the writer protocol
checks described below.

## Controlled report benchmark

The same synthetic legacy-shadow report fixture runs against the archived main
source and the reconciled source. Its three KPIs, nine KPI observations and three
revenue observations produce identical serialized report output
(`49622260ef9f2b66a7bd15b29d58b2bc7e1d2f1b6165a8a34f6aab06a09f9b68`).
SQL statements fall from 156 to 65. Fixture setup is outside timing.

| Condition | Samples | Main median | Reconciled median | Main empirical p95 | Reconciled empirical p95 |
|---|---:|---:|---:|---:|---:|
| New in-memory connection | 30 | 3.646 ms | 2.439 ms | 4.101 ms | 2.974 ms |
| Reused connection | 60 | 2.591 ms | 1.608 ms | 3.164 ms | 2.160 ms |

These local samples measure one controlled reader and its exact output parity.
They do not establish live issuer coverage, browser task latency, production
throughput or a population percentile. Fixture source SHA-256 is
`92da02b435bc48fa2194abce264efd24baaa78d53069b8e440b84880857512d8`.

## Further review corrections and evidence

The next Opus review confirmed the earlier blockers were resolved but scored the
source 76/100. It identified broad invalidation of SEC receipts and immediate
failure on an unreadable holder file. The extractor identity is now scoped to its
owned import closure. The writer retries unknown reads within its deadline.
Native Windows evidence and final exact-head CI remain separate acceptance
checks; local test passes do not establish deployment.

A separate server benchmark uses twenty synthetic equities and the same database
bytes for main and this branch. It completes the real overview, portfolio and
evaluation Flask responses with real cockpit builders and SQL. App and fixture
setup are outside timing. External tracker data is a typed synthetic adapter;
all network is denied. Sparse enrichment remains explicitly unavailable.
Thirty cold cycles show three cockpit builds becoming one, with 566 SQL
statements becoming 71. Real cockpit rows and overview HTML match exactly;
hydration JSON differs only in its generated timestamp. Sixty warm cycles have
zero builds and zero SQL in both variants. Main cold median/p95 is 15.394/16.128 ms; the frozen candidate is
7.450/8.246 ms. Warm medians are 0.896/0.958 ms respectively; no warm
improvement is claimed. These are local server completion measurements, not browser paint
or live-host throughput.

## Native writer verification

The managed Windows Python 3.13.11 interpreter ran copied, hash-matched writer,
run-lock and clock source with only temporary synthetic lock paths. The native
probe passed live/invalid PID checks, a 0.152-second live-holder timeout, killed-
holder recovery, sixty mutually exclusive windows from three contenders over a
stale claim, and repeated guard unlock/reacquisition. The harness distinguishes
Windows launcher PIDs from actual interpreter PIDs. Every test interpreter and
launcher stopped, the exact owned directory was removed, and a final read-only
postcheck found no task resources. No database was created or opened. This is
native protocol evidence, not a production pipeline or deployment test.

## Narrow extractor identity cost

The final closure contains 95 owned Python files (1,713,142 bytes) and twenty
active vendor distributions. Thirty cold calls have a 711.748 ms median and
741.009 ms empirical p95. This cost occurs lazily at first SEC use, with no
source scan at module import. Sixty warm calls have a 5.944 ms median and
7.126 ms empirical p95, versus 29.956/34.691 ms for the preceding conservative
whole-runtime identity. Cold cost increases; repeated cost and unrelated-code
invalidation decrease. Warm calls perform zero full-source digests. The identity
retains full schema hashing, so a schema migration conservatively invalidates
prior receipts. Source, dependency, schema and active-ladder evidence remain
attributable. Thirty-five focused tests pass; the earlier related pipeline,
capture and matcher batch passed 113 tests. Writer and liveness coverage passed
275 tests with five separate native-Windows suite skips, supplemented by the
native protocol evidence above.

## Credential-scan metadata reconciliation

The full credential scanner identified two Git commit identifiers in the helper
ledger and the fixed grounded-search compatibility digest as false positives.
The existing filter now recognizes only those two forty-hex metadata fields at
the exact ledger path and the complete named digest assignment at the exact test
path. The global key allowlist and detector set are unchanged. All 928 filter
tests pass, including actual detector subprocess checks that adjacent synthetic
credentials and identical hashes under unrelated paths still fail. The search
compatibility oracle keeps its exact digest value. No runtime source changed.
