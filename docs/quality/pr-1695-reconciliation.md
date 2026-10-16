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
  fingerprints agree. The identity covers the existing full runtime-artifact
  owner, dependency lock and installed versions, active metric ladders, and
  database schema. Missing inputs, linked source files and in-process source
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
  retain ownership. Acquisition remains bounded; holder lifetime and FIFO
  fairness are not guaranteed.
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
source population: 1,451 modules and 551,579 noncomment lines. Exact duplication
is 4 groups / 158 lines versus main's 6 / 238. Near duplication is 147 groups /
9,867 lines versus main's 162 / 11,266. The largest dependency cycle falls from
25 modules to 16; maximum internal fan-out falls from 137 to 40. Source size is
2,582 noncomment lines above main after retaining the PR's runtime changes.
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
was changed. Production timing, native Windows behavior and live sealed retrieval
remain outside the evidence from this Mac source-integration task.

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

## Extractor identity cost

The actual source tree contains 1,510 Python files plus its dependency lock
(26,637,130 bytes). The full static source digest runs once per process. On an
in-memory synthetic schema, 30 cold identity calls had a 218.394 ms median
(273.386 ms empirical p95). Sixty warm calls had a 29.956 ms median
(34.691 ms empirical p95) and zero full-source hashing calls. Paired metadata
scans preserve exactly the same manifest while reducing median time from
36.120 to 30.355 ms. These are local component measurements, not production
ingestion throughput.
