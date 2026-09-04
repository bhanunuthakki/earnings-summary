# Code-quality evidence

This directory contains deterministic Train 0 evidence for the 9+ quality
program. It is evidence, not a new policy authority. The roadmap is maintained
in Linear, and the frozen score definition is retained in
`directives/code_quality_score.md`.

The initial architecture and duplicate receipts scope the pre-program commit
`09d35d1a2785ff7e6a218031eb43952781be3a93`. The `*-ratchet.json` receipts scope
the instrumented Train 0 tree and are enforced by `make quality-ratchets`.
Scanner and source hashes inside each receipt make definition drift explicit.

Current evidence at the Train 0 instrumentation baseline:

- 1,316 executable modules and 485,879 non-comment lines.
- 91 modules above 1,000 lines, 19 above 2,000, and 4 at or above 3,000.
- 17 import cycles spanning 80 modules; the largest contains 25 modules.
- 8 exact normalized-AST clone groups covering 337 body lines.
- 166 near-miss groups covering 11,772 body lines.
- 2 whole-tree Ruff findings, 63 format findings, 4,377 active strict-Pyright
  diagnostics, and 627 suppression directives. The typed static receipt is
  `PASS`: every configured source root is included and immutable archived
  migrations remain a separately reported denominator.
- 691 database-building test files classified with zero unclassified cases.
- The operational graph has no parse failures, unresolved targets, stale
  dispositions, or unknown production edges. The remaining 85 unknown edges
  are confined to tests and instruction tests and remain visible in the raw
  graph.
- The lifecycle receipt classifies all 824 candidates with zero omissions,
  extras, or duplicate identities. It records 149 scheduled, 4 service, 181
  UI-reachable, 187 internal-delegate, and 303 time-bounded dormant entries.
- The roadmap reconciliation receipt covers 96 named claims: 30 are reproduced
  or corrected by typed generators and 66 are explicitly rejected from scoring
  for lack of an admissible generator.

Reproduce the receipts from the repository root:

```bash
python execution/score_code_quality.py --revision WORKTREE --architecture-only --output docs/quality/architecture-ratchet.json
python execution/analyze_code_duplicates.py --revision WORKTREE --out docs/quality/duplicates-ratchet.json
python execution/capture_compatibility_evidence.py --baseline 09d35d1a2785ff7e6a218031eb43952781be3a93 --out docs/quality/compatibility-baseline.json
python execution/inventory_static_quality.py --output docs/quality/static-baseline.json
python execution/audit_test_db_patterns.py --output docs/quality/test-db-patterns-baseline.json
python execution/build_operational_reachability.py --output .tmp/quality/reachability-check.json
python execution/classify_operational_lifecycle.py --output docs/quality/lifecycle-baseline.json
python execution/reconcile_quality_baseline.py --output docs/quality/reconciliation-baseline.json
```

The raw reachability graph is deliberately generated under `.tmp/` because it
is several megabytes. Its typed schema, parser version, source hash, counts,
disposition-manifest hashes, and regeneration command remain checked in here;
absence of the raw local artifact never becomes a silent pass. Each disposition
is bound to the exact source line by SHA-256. A stale, duplicate, or unmatched
disposition forces `HOLD`.
