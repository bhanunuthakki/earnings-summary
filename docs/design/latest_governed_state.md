# Latest governed state materialization

Status: owner-approved staged cutover target. Live reader/writer ownership moves
only in tested clusters; retention deletion remains blocked until the complete
observation and final-approval gates in
`directives/data_provenance.md#11-legacy-fact-plane-retirement` pass.

## Goal

Keep the strict historical provenance ledgers and population-cutover proof as
the audit substrate while moving routine refreshes and default reads onto a
small, rebuildable projection of the latest usable governed state.

The hot path starts only after a complete `population_cutover_receipts` baseline
has closed every population plane, all 13 readiness gates, and legacy parity.
The materializer also requires a current promoted Ask retrieval scope bound to
that same baseline. Missing ontology, document, canonical, retrieval, Research
Snapshot, embedding, or Ask evidence therefore blocks materialization rather
than being treated as an empty current state.

## Deep-module boundary

`provenance.latest_governed_state` owns four decisions behind one interface:

1. `ChangeFrontier` binds the current population receipt, retrieval promotion,
   its authoritative issuer and reporting entity, publication cursor, fact
   projection seal, narrative manifests, and K/O clocks. The reporting entity
   must belong to the promoted Research Snapshot universe and issuer.
2. `DeltaPlan` compares that frontier with the published current head. An exact
   match is a no-op; a direct fact delta admits changed coordinates for the
   promoted reporting entity only, while superseding document manifests are
   validated against active current membership and publish changed current rows
   only. An unrelated lineage requires an explicit checkpoint rebuild. A scope
   whose issuer or reporting-entity binding changes is checkpointed so facts
   from the prior binding cannot survive.
3. Staging is deterministic and resumable. Staged rows are invisible to reads.
   Publication uses a short `BEGIN IMMEDIATE` transaction, rechecks the prior
   head, applies only the staged changes, writes immutable change and refresh
   receipts, and advances the head atomically.
4. Latest reads use indexed current fact, document, and current-only FTS
   projections. Historical reads remain available only through explicit
   generation, manifest, or receipt coordinates.

The idempotency key commits the scope, prior head, verified input frontier, and
policy version. Replaying the same key with different material is an error.

This module does not claim an upstream immutable changed/superseded-document
coordinate manifest. Until such a governed contract exists, document planning
may enumerate active-current membership to prove deletion and conflict
semantics. The bounded guarantee in this change is changed-only current writes,
not changed-only source validation reads.

## Audit receipt

Every refresh writes one immutable receipt, including a no-op. The receipt
retains:

- the baseline population run and receipt-set commitment;
- the retrieval promotion and fact/narrative source coordinates;
- the prior and resulting state commitments;
- source publication and document frontiers;
- knowledge, observation, and operation clocks;
- changed fact, document, and chunk counts;
- per-change prior and resulting commitments, evidence pointers, and selection
  reason.

No full historical snapshot is copied into the receipt. Source ledgers remain
the canonical history.

## Conflict and rollback rules

The materializer does not resolve source conflicts. It projects the result of
the governed canonical resolver. An unresolved or retired coordinate
tombstones the current fact; it never promotes an arbitrary candidate.

The prior receipt and prior per-change commitments make rollback deterministic.
Rollback is a new forward operation that reprojects a previously sealed head.
It does not delete source history or mutate an old receipt.

## Retention and compaction boundary

Policy `retain-source-history_compact-rebuildable-projection.v1` means:

- retain source observations, publications, evidence, resolution decisions,
  population receipts, and refresh receipts;
- current projections and FTS are rebuildable caches;
- duplicate historical projection material may be proposed for compaction only
  after parity, restore, and rollback evidence exists;
- this change performs no physical compaction or deletion.

Any deletion, current-reader ownership switch, or live database population
requires a separate owner-authorized rollout.

## Performance contract

Provider-free benchmarks use independent publication, fact, document, chunk,
and scope counts. Deterministic ratchets require:

- no-op: zero source events/cells/documents inspected, zero current-row or FTS
  mutations, and exactly one receipt;
- small delta: current writes proportional to changed coordinates; active
  document membership validation remains separately measured source work;
- scope isolation: a shared global fact generation is partitioned by the
  authoritative promotion binding, aggregate current facts equal the source
  fact population, and a target-scope delta cannot change non-target heads;
- latest reads: indexed current tables only, with no recursive generation or
  retained-history scan;
- history independence: 1x and 4x retained history produce identical current
  commitments and SQL work;
- resume: only uncommitted staging work is completed, and the final commitment
  equals an uninterrupted run.

Timings are reported as secondary budgets; exact work and write vectors are the
merge ratchets.
