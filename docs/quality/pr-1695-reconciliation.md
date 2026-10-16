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
  fingerprints agree. Unknown or changed identities trigger reprocessing; an
  obsolete conditional response triggers an unconditional fetch.
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
  evicted. Acquisition remains bounded; holder lifetime and FIFO fairness are
  not guaranteed.

## Verification and limits

Targeted source, migration, cache, citation, concurrency and recovery tests use
explicit disposable databases. Search tests exclude live providers. The project
format, lint, changed-file strict typing, suppression and design-sync checks
remain enabled. Whole-tree quality ceilings only decrease.

The first broad local test run passed 2,391 tests before stopping at one stale
CI timing-table coverage failure. Retained timing samples from both parents are
combined; new measurements retain their actual local timing provenance. This
interrupted run is not a full-suite pass. The final PR CI matrix is required. The new timing batch passed 286 tests.
The endpoint/browser integration batch passed 386 tests; its remaining timing
and text-golden failures were corrected. Final golden and CI-helper comparison
checks passed 138 tests. The golden change is limited to accurate missing-or-
ambiguous version wording in source rows and citation popovers.

Synthetic rendered comparisons cover Cockpit, Company Desk and Performance at
1440x900 and 390x844. The selected screen is activated through production
navigation. Each final image matches the corresponding main image byte for
byte, and browser page errors are absent. These fixtures prove rendering parity,
not live data readiness, real-user latency or canonical-host throughput.

Architecture and duplication baselines are remeasured with unchanged scanners
for the combined source population. A parent-by-parent analysis finds no new
module, dependency-cycle membership, size-threshold crossing or duplicate group
introduced by reconciliation. Before removal of the unused materialization, the
combined tree had 4 exact duplicate groups (158 LOC) versus main's 6 (238 LOC),
and 146 near-duplicate groups (9,816 LOC) versus main's 162 (11,266 LOC). The
largest dependency cycle has 16 modules versus main's 25. Generated baselines
contain the final measurements; these comparison figures identify their scope.

The original Opus 5.5 review scored the unreconciled PR 6/10 and blocked it.
Its specific findings drove the corrections above. A separate review of the
final source against the original goal is required before merge. Its score is
advisory evidence; tests and merge rules still govern acceptance.

No production database, installed application, live scheduler or provider policy
was changed. Production timing, native Windows behavior and live sealed retrieval
remain outside the evidence from this Mac source-integration task.
