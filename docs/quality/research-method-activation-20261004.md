# Research method activation baseline — 2026-10-04

## Scope and source of truth

The investing skill owns the public-evidence method at
`src/advisor/skills/earnings-summary-investing/references/research-method.md`.
`src/research/method_contract.py` loads that file for the affected application
prompts. Skill and application instructions share one analytical source.

The method tests economic mechanisms, alternative causes, the dated earnings
bar, complete question/answer components, comparable management language and
publicly observable next checks. Accepted thesis rules remain in force. Missing
inputs remain unavailable. Company quality, valuation and allocation are separate
judgments. Existing artifact sections remain unchanged.

## Deterministic enforcement

- Pre/post generators save the exact method and rendered prompt in their input
  manifests before cache lookup. Prompt versions change for affected purposes.
- Pre/post outputs must contain the existing four/five sections in order. Malformed
  outputs are rejected before persistence. No additional repair call is introduced.
- Post readouts retain every stored transcript segment, including late, empty and
  unknown-role rows. Transcript and complete-prompt size limits reject oversize
  input before synthesis. No silent head/tail selection replaces the stored text.
- All stored rows are not proof of full source acquisition, extraction or complete
  Q&A. Those coverage states remain unknown when no receipt proves them. Missing
  exchanges cannot establish avoidance or a dropped topic.
- The pre-call comparison retains exact historical body and input commitments,
  superseded versions, event identity and UTC timing. Missing, malformed or
  contradictory baselines remain unresolved. Event identity does not prove a
  fiscal target. Current mutable context is labeled separately.
- Thesis, bear, description, transcript-summary, tone and five-minute prompts load
  the method. Relevant caches bind method/version or actual rendered prompt. The
  bear report retains its existing source-freshness TTL; exact source freshness
  is not newly established there. Five-minute source identity remains partial.

These checks establish method loading, input retention and output structure.
They do not establish the truth of every inference or financial claim.

## Reviewed-source baseline and upkeep

The skill's `references/reviewed-sources.json` records reviewed active source
hashes. `scripts/check_investing_skill.py --check` detects source drift and broken
references without opening a database or calling a model. New hashes require
semantic review of the corresponding instructions. A clean hash check is not a
research-quality score.

The existing monthly assessment rotates synthetic research cases and reads
available evaluation outcomes. Changed application prompts retain the existing
held-out qualification and promotion process. No extra scheduler is added.
New synthetic regressions cover missing/stale thesis evidence, unsupported tone
comparisons, omitted transcript rows, exact historical commitments, cache changes
and explicit database routing.

## Initial compatibility evidence — historical

Shared local activation is scoped to the older `f7e15738` source plus the bounded
plain selected-input lineage port from main. Only the thesis evaluator and
research cockpit are additional product prerequisites. Compatibility tests use
the `0050` migration graph. Later migrations and unrelated acquisition, DCF and
retained-trace owners are not imported. The active skill explicitly marks newer
alias inventory and DCF recovery routes unavailable in this checkout.

The first bounded compatibility test passed 168 synthetic regressions. Five
independently generated assembled-prompt responses received an advisory semantic
pass after exact post-prompt reconciliation. The exact serving model was not
exposed. This is not qualification of the pinned application models. Execution
receipts retain original failures, repairs, final gates and activation preimages.

Windows deployment, native Windows checks, the coordinated service hold and the
separate retained-trace integration remain outside this local activation. Local
source activation does not establish production or historical-cutoff parity.

## Initial combined-worktree gate — historical

The full shared-source compatibility suite recorded 17,594 passes and eight
failures. Five task-induced failures were repaired. The repaired cases and
related method, prompt, registry, database and decision-condition suites then
passed 130 tests. One preserved design-compactness case was excluded from that
bounded run; it remains a failure of the broader gate.

Three failures reproduce without the research activation: two SEC scheduling
fixtures encounter the existing database-authority guard, and the preserved
design contract has 204 lines against a 190-line limit. The original CLI,
contract, tests and limit are retained. The combined full gate is **HOLD** until
those owners resolve the failures. Bounded task verification does not turn that
gate green or authorize a release.

Concurrent acquisition changes appeared after the isolated snapshot checks.
The source checker and independent reviewer detected five source-baseline
mismatches. The 52 research activation files remain verified. Current shared
source closure is **HOLD** until the other source owners review their changes.
Earlier passing closure receipts apply only to their recorded snapshots.

## Successor repair and regression record — 2026-10-04

This record supersedes the pending local repair status above. It does not rewrite
the original failed runs or establish deployment or financial qualification.

The inherited SEC fixtures now use explicit migrated test databases. The design
contract keeps all panel and space requirements within its unchanged 190-line and
9,000-byte bounds. Their retained cohort passes 25 tests; design sync passes.
The later service-binding fixture now creates its synthetic database and passes
its original binding assertions. The database guard remains intact.

The skill checker now handles fenced examples, multi-backtick spans, empty spans,
new source registration, source/prose drift during review, and unsafe symlink or
directory targets. All 28 maintenance regressions pass. The instruction gate
passes 47 tests and its pre-push hook checks. The installed skills resolve to the
maintained shared source. A clean baseline records 122 explicitly reviewed public
source owners, including material cash-flow, state and workflow dependencies.
Hash equality remains a source-currency check, not a financial-quality score.

The acquisition owner repaired the three reviewed seams: acquisition-only mode
keeps transcript and IR capture while suppressing optional models; entrypoints
refuse inferred state-root databases; the IR-summary child receives the selected
database. Source duties correctly retain exact registration forms under package
scope@6 and document-processing v2, with research-snapshot selection still v1.
The static inventory includes 31 new nonignored Python owners without shared
staging; diagnostic and suppression ceilings decrease only.

Two exact source states have separate evidence. The research candidate includes
fixed main `71c2fa79` and locator repair `27f6ba8b`. Its earlier complete run had
18,158 passes, 68 skips and one moved test locator. The graph itself passed with
zero production gaps; the corrected reachability module passes 68 tests.
The candidate complete rerun passes: **18,159 passed, 68 skipped**, with 12
warnings. Its source gate is separate from the combined snapshot below.

The combined local snapshot uses shared base `f7e15738`, migration head
`0051_filing_xbrl_unit_protocol`, all 33 named new public files and the final owner
handoff. Its reviewed graph is COMPLETE/PASS with zero production unknowns,
zero unresolved production references and zero diagnostics. The 135 remaining
unknown calls are test-owned; ten added fixture calls are individually recorded.
The initial fast gate passes 1,184 tests, with three Windows-only skips. The
first complete combined run had 17,826 passes, 67 skips and four failures. Two
MELI callers now reject a mismatched input ticker before database resolution or
child dispatch. Transcript fixtures bind the explicit state root. Historical
0051 migration tests use the shared fixture and retain all refusal and downgrade
assertions. The migration-builder cap stays 101. Memo calculations also bind all
numeric occurrences and verify separately reported operands.

The corrected source passes the complete combined gate: **17,836 passed,
67 skipped**, with 12 warnings. The skips require unavailable platform, optional dependency or data
evidence. All 74 reachability and migration-ratchet tests pass; all 47 instruction
tests and hook checks pass. The final graph has 178 reviewed dispositions and
135 test-only unknown calls. Six moved process calls retain the exact same AST;
their line-bound fingerprints are renewed. Intermediate failed metadata checks
remain recorded. Whole-tree quality keeps 2,687 retained files, with exact
descending ceilings of 2,415 Pyright diagnostics and 2,121 suppressions.

The tested source tree is `20a7bbfd6db37a8befd6e15739771082fefa9e16`. Post-run
checks rehash all 3,541 public files against both copies without a mismatch.
That parity receipt precedes this documentation-only update. The 3,540 other
files remain unchanged; the final note has a separate hash and closure check.
The 122-source baseline adds the MELI child and memo evidence owners through
explicit semantic review. This is local deterministic closure, not a research
quality score or decision-grade qualification.

The shared source and skill baseline match this reviewed snapshot. The shared Git
index tree remains unchanged, with zero staged paths. Git stat-cache refreshes
can change its physical file hash; root performed no shared staging. Complete graph evidence applies to the fully staged isolated
inventory, not the shared index that still omits the new acquisition sources.
The source-control owner must retain those exact files and regenerate aggregate
commitments after any later source edit before claiming indexed release closure.

Independent assessment, plan and execution reviews remain advisory under the
existing Judge calibration boundary. Final execution review: **advisory PASS
for local implementation and deterministic verification**, with no material
implementation finding. Its two documentation precision corrections are applied.
No provider calls, production database access, main merge, release, publication
or trades followed from these repairs. Pinned-model qualification, native Windows
qualification and actual company source-to-memo evidence remain separate.
