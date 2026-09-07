# Linear BHA-141 R1: deterministic 9+ score oracle (history)

This is measurement infrastructure only. It is not active product policy and
not a claim that the repository scores 9+.

The live roadmap is owned by the Linear/user mandate. This record is frozen
history and evidence only.

## Frozen registry

The source registry in `src/quality/scoring.py` is the sole inventory:
exactly 18 unique score blocks totaling exactly 100 points and exactly 10
unique hard gates.

Architecture elegance blocks are deterministically recomputed from the
`ArchitectureReceipt` metrics. They require no caller `PASS` and cannot be
overridden to `PASS`.

All other blocks and hard gates are admitted only through strict
`quality-score-admission/v1` receipts read immutably with
`git show <bundle_commit>:<receipt_path>`; worktree or absolute local files
are never read. The caller supplies only the repo-relative canonical receipt
path (`docs/quality/*.json`), the exact receipt SHA-256, and the exact
40-hex bundle commit; the `pass`/`fail` state lives inside the receipt.
Each admission binds `kind`, exact `key`, `state`, exact `subject_commit`,
the registered `src/quality/roadmap_reconciliation.py` generator, its SHA-256,
and a nonempty tuple of source references, hashes, and schemas.

Trust boundary (fixed in code, no caller-selectable ref): the bundle commit
must resolve exactly and be an ancestor of `origin/main`; the subject commit
must resolve exactly and equal `architecture.scoped_commit` (and
`ScoreEvidence.scoped_commit`); the subject must be an ancestor of the
bundle; `git diff --name-only subject..bundle` must contain only canonical
`docs/quality/` files; the generator path must be canonical under
`src/quality/` or `execution/` and its bytes at the subject commit must
match `generator_sha256`. Any unverifiable entry becomes `missing`/`HOLD`.

Ordinary verified failed blocks award zero but do not themselves force
`FAIL`. Precedence: any missing block/gate => `HOLD`; otherwise hard-gate
failure or architecture regression => `FAIL`; otherwise `>=90` => `PASS`,
`<90` => `FAIL`. Thus a verified failed 10-point block yields exact 90
`PASS`, while verified failures totaling 11 yield 89 `FAIL`.

Unknown score block or gate keys are rejected fail-closed. Duplicate or
inconsistent registry entries fail closed. Ordering is deterministic and
diagnostics are bounded.

```console
.venv/bin/python execution/score_code_quality.py --revision WORKTREE --architecture-only --output .tmp/quality/architecture.json
.venv/bin/python execution/score_code_quality.py --revision <subject-commit> --baseline .tmp/quality/architecture-baseline.json --evidence .tmp/quality/score-evidence.json --output .tmp/quality/score-result.json
```
