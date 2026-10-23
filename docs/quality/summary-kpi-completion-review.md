# Summary KPI processing completion review

The owner authorized cleanup that preserves analysis depth. Sol implemented this
bounded change. Astra supplied architecture judgment and an independent advisory
review. The root reviewed callers, transaction boundaries and legacy semantics.

Allowlist extraction now reserves an existing pipeline attempt before model work.
The identity includes source and holdings bytes, requested metrics, effective
units, source/parent identity, extractor contract and resolved model route.
Existing facts no longer suppress another source. Exact replay skips model work;
refresh cannot replace a live allowlist attempt. Empty and partial results retain
unknown metrics in the existing stage journal. Facts, stage disposition and run
completion commit together. Persistence failures roll back pending work, remain
retryable and produce a nonzero CLI exit status. The shared writer distinguishes
real duplicates from rejected writes. Legacy duplicates preserve retained context.

Purpose, prompt, schema, model policy, financial admission and capture processing
remain unchanged. There is no schema migration or new completion store. The old
fact-presence skip and unused holdings reader are removed.

Validation includes 98 acceptance/accounting tests and a separate 158-test owning
regression run. These counts overlap and must not be added. Changed-file format,
lint and Pyright passed. Cases cover source/configuration changes, completed
batch continuation, empty/partial output, protected refresh, actual evidence-trigger
rejection, rollback and retry, legacy context preservation and CLI result codes.
The original empty-result regression failed before implementation. The changed-source
regression uses an evidence-backed synthetic old fact; its first draft exposed the
real evidence trigger instead of establishing the intended old-fact precondition.

Astra found no blocking defect and accepted a reviewed architecture baseline
refresh under the existing scanner policy. The unchanged scanner measured
561,956 -> 562,136 noncomment production lines (+180). LOC is the sole relaxed
constraint; module, size, dependency, cycle and duplication controls remain.
Measured source: `3a6f75f9378fbcc99d38733be174fdde796d44099f23ba3879f2082073ca5526`.
All 203 reachability disposition line fingerprints were verified. The sole changed
source file referenced by a disposition retains the exact owning function AST.
Targets and decisions remain unchanged before aggregate provenance is rebound.

This is a local backend MVP, not production lineage readiness. A newly registered
summary without an evidence binding still fails persistence after model work.
No evidence is manufactured. A later preflight at the existing evidence owner can
avoid that wasted call. Distinct source versions now receive the full metric
request, so initial model work can increase. No live data, source collection,
application model call, service, push or deployment ran.

The separate full-suite fixture-sealing blocker remains unresolved. The earlier
suite reached 100% test execution but did not finish teardown. It is not a passing
full gate, and this change does not claim release readiness.

Final local checks: `make check-fast` passes, including architecture/duplication,
format, lint, changed-file types and 171 changed tests across the cleanup branch.
The whole-tree static gate passes at 2,872 retained files, with existing ceilings
unchanged at 2,012 Pyright diagnostics and 1,838 suppressions. The directive
manifest and actual-head reachability closure test pass. These checks do not
replace the incomplete full suite.

## Follow-up: summary anchor preflight

Sol added a summary-only, read-only guard at the existing financial evidence owner.
It runs after completed-attempt suppression and before model work. Missing or
inconsistent anchors produce failed attempts with zero model calls. A selected
current binding cannot fall back to direct lineage when invalid. Valid scoped
bindings keep their content hash separate from the containing blob hash. Existing
issuer projections support recorded IDs and corrected aliases without creating or
approving mappings. The final transaction compares the selected immutable identity.

The guard establishes stored anchor coherence. It does not certify physical blob
availability, registry admission, revocation handling, complete write eligibility
or financial admission. General `_evidence_context` callers, database triggers,
capture processing, extraction prompt/model policy and completion identity remain
unchanged. Earlier completed empty attempts remain historical processing receipts.

The new missing-anchor regression failed before implementation because it observed
one unwanted model call. The final focused suite passes 40 tests. The owner matrix
passed 233 tests before the last failed-run parameter was added; the focused rerun
includes that added case. These counts overlap. Changed-file format, lint and types
pass. Cases include scope/whole-blob distinction, invalid current bindings despite
valid fallback evidence, recorded issuer IDs and aliases, lineage drift, anchor
repair followed by retry and retained historical completion.

Astra independently reviewed the frozen source and gave advisory acceptance with
no blocking defect. It accepted the unchanged scanner's measured 562,136 -> 562,294
noncomment production lines (+158) as an explicit LOC-only baseline refresh.
All other architecture constraints remain. Source identity:
`2d190e478ac0ca1d45297358218c49c05900be6c6ff211cab8c15c4b1be0c517`.
All 203 reachability disposition source bodies, line fingerprints and targets were
verified unchanged before aggregate provenance was rebound. This follow-up changes
no schema and creates no new evidence store, registry or pipeline.

Follow-up local gate: `make check-fast` passes with 183 changed tests. The whole-tree
static gate passes with the same 2,872 retained files, 2,012 existing diagnostics and
1,838 suppressions. The directive manifest validates. The previously known full-suite
teardown blocker remains open; no full-suite pass or production activation is claimed.
The actual-head reachability closure test also passes.

## Shared fiscal-calendar pruning

Six duplicated compute date tables are removed. `compute.fiscal_calendar` now
owns the existing IR endpoint math, filename membership and summary year-label
adapter. Summary extraction and the retired backfill share that actual adapter;
transcripts retain their ending-year label. Existing IR and summary import names
remain available. No historical facts are restamped, and backfill writes remain
rejected. The common owner uses only the standard library.

Backfill uses the existing entrypoint bootstrap. Two manual-import lint
suppressions and one private-usage suppression are removed. The execution ceiling
is tightened from 841 to 838. The old summary tests are unchanged. The public-only
calendar matrix has its own test file; it requires no test-only API or suppression.
A separate transcript inverse-inference defect remains outside this refactor.

Final owner tests pass: 360. `make check-fast` passes: 443 tests, architecture,
format, lint, types and changed-file suppressions. These counts overlap.
The whole static gate passes with 2,874 retained files, 2,012 existing diagnostics
and 1,835 suppressions. Direct backfill `--help` exits successfully without opening
a database. No runtime speedup is claimed. The full-suite fixture-sealing blocker
remains unresolved; these checks do not establish release readiness.

Astra found no architecture blocker in the final source. The unchanged scanner
measures one added shared module (1,468 → 1,469) and 12 fewer noncomment production
lines. The reviewed refresh changes only the module-count constraint; all other
constraints remain or tighten. Architecture source identity: `5bfd7adc255f2eb0f3f43eba8ca0b95e2554d8db50398ad75a0eefc0cf1ff8f1`.
All 203 reachability disposition source bodies, line fingerprints and targets
remain unchanged before the aggregate input identity is rebound.

Independent Opus 5.5 review: **ACCEPT, 91/100**, no blocking findings. The exact
membership route resolved `claude-opus-5-5`; no model fallback was used. This meets
the owner's strict score-above-90 rule for local implementation acceptance. It is
an advisory review with an owner-imposed threshold, not a calibrated release
qualification. The final whole-static result satisfies its stated condition.
The previously unsupplied bootstrap was inspected and its direct invocation was
smoke-tested. Review prompt SHA-256: `86af10853bcaa45b85476db9a14caf674ec99ffbd30a000cef466fd6d8d2122c`.

Reviewed source SHA-256 values:

- `execution/backfill_fiscal_period_stamps.py`: `704745c151acb67bad0236e1de23264224839b9229745fbc5d4a12aa60fdee12`
- `src/compute/fiscal_calendar.py`: `f534cf3a229fa45661331c0835f59046dd407013ec3a8254a2e8535c068a0131`
- `src/compute/kpi_extract_summaries.py`: `b042fa9e97e07fff2437edfb7acc42d7130b336d5ae513a9d77d626b5b9869dd`
- `src/compute/transcript_ingest.py`: `63c6408aba7a2ee69558d6e148cadd3b901ef587633595d9a0a3ec3d72797c7c`
- `src/ir_uploads.py`: `ea548506b3ffad212b9cb39f28978ec6dc5f68f3d76cd2bf2c40d46251891cb8`
- `tests/test_fiscal_calendar.py`: `33c578e111af5bc74ea9b0408e69e775e6e37d1b7e1ce0624e8444e264deafa3`

No live database, collection, application model call, service, push or deployment
ran. The external calls were the owner's requested independent reviews. The
implementation and its code-only rollback remain local.

Final directive validation and actual-head reachability closure pass. No
disposition decision or source body changed. The closure rerun follows tracking
the final public-only test module and rebinding the resulting scanner input hash.

## Reader-consolidation assessment before implementation

The spreadsheet comparison is recorded in `spreadsheet-publication-parity-audit.md`.
Four existing synthetic tests passed. No writer replacement is established.

A task-owned runtime wrapper extended the existing
`test_fact_read_model_batch::test_batch_preserves_valid_publications_and_revalidates_each_call`
without editing source or tests. After the original case, it compared scalar and
required-batch bundles for `observation-independent` at 2026-07-27T12:00:00Z.
Complete object and JSON equality passed. Both carried
`derivation-seal-independent`; relation tuples matched but were empty.
The exact test node passed: 1 test in 3.38s. This is owner-interface evidence,
not a nonempty-derived-relation or end-to-end report qualification.

Source inspection confirms the bounded batch calls the same bundle and relation
methods, with only publication-verification reuse overridden. Astra accepted
uniform bundle reuse without a per-kind branch. It also approved removing the
unused discovery construction, the same-clock duplicate report lineage check,
and the selected-ID mismatch guard. Paired financial outputs, fresh-connection
behavior and explicit-O cases remain required before implementation acceptance.
No new application implementation has started while the roadmap's Opus gate is
unmet. The known full-suite teardown blocker remains open.

Retain `.tmp/verify_derived_bundle_parity.py` and its small log until the assertions
move into the owning test during implementation. Superseded scan payloads were
removed after calendar closure; prior failed full-suite evidence is preserved.
