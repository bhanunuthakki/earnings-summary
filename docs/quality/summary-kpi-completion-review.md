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
