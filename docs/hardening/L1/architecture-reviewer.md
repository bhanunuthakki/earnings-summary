# L1 Architecture Judge — Data Backbone

**Verdict: BLOCK**

Audit date: 2026-08-11. Source snapshot: `c6ebbe471343a080be5479ad4c26334fe8630b04`.

Do not merge an L1-clearance or cutover change and do not activate governed readers or scheduled historical backfills from this snapshot. The database is physically healthy, but configuration alignment and test breadth do not prove runtime completeness.

## High findings

1. Windows task ownership monitors the Scheduler service's ancestor instead of the actual wrapper, matching the live orphaned-descendant failure.
2. The governed population protocol has no production orchestrator that owns the dependency graph, checkpoints, seven plane receipts, parity, audit, and cutover seal.
3. Weekly synthesis used a stale hardcoded portfolio, continued after failed stages, released the write lock between stages, and returned only the final stage's code.
4. IR discovery, quarterly refresh, earnings backfill, and stale `ingestion_runs` lack one lease/checkpoint/terminal-receipt authority.
5. Earnings history and current DCF promotion do not require immutable, reproducible lineage.
6. Restore verification accepts foreign-key-corrupt databases.
7. Four LLM output paths bypass strict structured validation before filesystem or canonical-data effects.

## Medium findings

- `.harden/state.json` and the prior activation receipt were stale and not bound to the newly audited SHA and runtime state.
- Q2 pre/post generation has point tools, but no governed portfolio-quarter backfill, completeness policy, or terminal receipt.

## Smallest coherent remediation sequence

1. Freeze activation and invalidate stale L1 state.
2. Fix Windows process ownership and replace weekly batch orchestration with one dynamic, locked, fail-fast Python job; prove stop and junction behavior on Windows.
3. Add a shared run-attempt lease/checkpoint/terminal-receipt contract; use it for IR retry, quarterly refresh, earnings partial failures, and stale-run reconciliation.
4. Require FK-safe restore and route the four LLM bypasses through strict Pydantic structured calls.
5. Enforce append-only earnings observations and immutable DCF input lineage at promotion boundaries.
6. Wire one population orchestrator to `PopulationCompletenessLedger` and populate/seal the governed planes in dependency order.
7. Run a bounded Q2 artifact backfill against explicit portfolio/evaluation obligations.
8. Re-audit the exact deployed SHA and require zero strict-integrity blockers, a successful restore drill, IR timeout-to-retry proof, Windows stop proof, one full daily chain, one dynamic weekly synthesis, and a provider-backed eval receipt with zero errors.

## Important qualifications

- `43/43` Scheduler registration and schema revision alignment prove configuration, not execution health.
- Empty governed planes do not require disabling dormant legacy readers, but they block governed-reader activation and L1 completion.
- The invalid Gemini credential is an external eval/activation blocker; the four unvalidated output paths are source blockers.
- Ubuntu-only CI is not independently blocking; the reproduced Windows ancestry defect is.
- Historical Q2 sparsity is medium until explicit acceptance obligations are defined; the absent governed backfill receipt is the actionable gap.
