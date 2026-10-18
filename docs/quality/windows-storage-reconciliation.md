# Windows storage reconciliation

The installed Windows runtime contains storage fixes that were not retained in
`main`. A direct release replacement would remove the semantic export descriptor
reader and storage lifecycle controls. This change reconciles those installed
bytes with release `68181820`, without changing financial facts or model routing.

The source port uses a three-way comparison with the Mac source commit. It retains
all release changes. Shared semantic blobs preserve the exact legacy export bytes,
clocks, hashes, and cursors. Synthetic fixtures require explicit producer ownership
before retirement. Source trees, links, live databases, operator holds, unknown
files, and failed attempts remain protected.

Review found two gaps. Windows accounts used separate temporary directories for
volume reservations. The ledger now uses OS-resolved common application data and
fails closed when it is unavailable. Successful pytest sessions could restore an
empty database environment before fixture retirement. The cleanup now passes its
explicit database guard; the new regression demonstrates the original failure.

## Architecture measurement

The architecture baseline is deliberately remeasured for the retained storage
behavior and its shared reservation module. The increase is functional code for
verified descriptor reconstruction, producer ownership, recovery, and bounded
storage admission. It does not relax the scanner, metric definitions, or admission
policy. The reviewed baseline delta is recorded in the pull request. Subsequent
growth remains subject to the same ratchet.

## Operational reachability

The source inventory follows the existing runtime entrypoints. Weekly cleanup
imports the temporary-run lifecycle, artifact retention, and coverage modules.
The temporary-run lifecycle imports the shared storage reservation module.
Semantic export storage remains reachable from review preparation, Operations
routes, Windows review fetching, refresh-manifest construction, and quarantined
KPI correction preparation.

The three disposition manifests are rebound to the final scanner input digest.
All 189 existing reviewed entries retain their exact source-line fingerprints:
28 dynamic imports, 69 attribute lookups, and 92 process calls. No disposition,
target, scanner rule, or unknown-edge allowance changes. The added storage module
and changed storage modules introduce no unknown production edges. Regenerate
the graph after the final source and test bytes are fixed to verify closure.

## Operations surface disposition

No Operations action or public capability is added. Existing cleanup and semantic
review preparation retain their existing entrypoints. The change improves their
storage accounting, compatibility, and exact producer closure. Existing disabled
scheduler holds remain unchanged. Native Windows locks, junctions, source identity,
backup/restore, migration readiness, and private dashboard health must pass before
activation. A manual activation does not establish future scheduled recurrence.
