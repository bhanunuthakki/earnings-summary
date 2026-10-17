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
  fingerprints agree. The identity covers the transitive extractor import closure,
  referenced vendor dependencies, active metric ladders and database schema.
  Shared domain helpers keep IR discovery and PDF parsing outside that closure. Unrelated UI files do not invalidate SEC receipts. Missing
  inputs, linked source files, unknown loaders and relevant in-process source
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
  retain ownership. Transient ownership-read failures retry within the same
  deadline, then return a typed lock-held error with an explicit unknown state.
  Cleanup retries preserve the original error. Windows explicitly unlocks each
  acquired guard before close. Acquisition remains bounded; holder lifetime and
  FIFO fairness are not guaranteed.
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
remain enabled. Static diagnostic and suppression ceilings only decrease.

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
2,050 inherited diagnostics and 1,854 inherited suppressions).
Golden and CI-helper comparison checks then passed 138 tests. The golden change is limited to accurate missing-or-
ambiguous version wording in source rows and citation popovers.

Synthetic rendered comparisons cover Cockpit, Company Desk and Performance at
1440x900 and 390x844. The selected screen is activated through production
navigation. Each final image matches the corresponding main image byte for
byte, and browser page errors are absent. These fixtures prove rendering parity,
not live data readiness, real-user latency or canonical-host throughput.

Architecture and duplication baselines use unchanged scanners for the combined
source population: 1,451 modules and 552,104 noncomment lines. Exact duplication
is 4 groups / 158 lines versus main's 6 / 238. Near duplication is 147 groups /
9,867 lines versus main's 162 / 11,266. The largest dependency cycle falls from
25 modules to 16; maximum internal fan-out falls from 137 to 40. Source size is
3,107 noncomment lines above main after retaining the PR's runtime changes.
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
final source against the original goal is required before merge. The owner
requires an independent Opus 5.5 score above 85/100. That score cannot waive
failed tests or merge rules.

No production database, installed application, live scheduler or provider policy
was changed. Production timing and live sealed retrieval remain outside this
source-integration task. Native Windows proof is limited to the writer protocol
checks described below.

## Controlled report benchmark

The same synthetic legacy-shadow report fixture runs against the archived main
source and the reconciled source. Its three KPIs, nine KPI observations and three
revenue observations produce identical serialized report output
(`49622260ef9f2b66a7bd15b29d58b2bc7e1d2f1b6165a8a34f6aab06a09f9b68`).
SQL statements fall from 156 to 65. Fixture setup is outside timing.

| Condition | Samples | Main median | Reconciled median | Main empirical p95 | Reconciled empirical p95 |
|---|---:|---:|---:|---:|---:|
| New in-memory connection | 30 | 3.336 ms | 2.159 ms | 3.752 ms | 2.479 ms |
| Reused connection | 60 | 2.392 ms | 1.463 ms | 2.751 ms | 1.830 ms |

These local samples measure one controlled reader and its exact output parity.
They do not establish live issuer coverage, browser task latency, production
throughput or a population percentile. Fixture source SHA-256 is
`92da02b435bc48fa2194abce264efd24baaa78d53069b8e440b84880857512d8`.

## Further review corrections and evidence

The next Opus review confirmed the earlier blockers were resolved but scored the
source 76/100. It identified broad invalidation of SEC receipts and immediate
failure on an unreadable holder file. The extractor identity is now scoped to its
owned import closure. The writer retries unknown reads within its deadline.
Native Windows evidence and final exact-head CI remain separate acceptance
checks; local test passes do not establish deployment.

A separate server benchmark uses twenty synthetic equities and the same database
bytes for main and this branch. It completes the real overview, portfolio and
evaluation Flask responses with real cockpit builders and SQL. App and fixture
setup are outside timing. External tracker data is a typed synthetic adapter;
all network is denied. Sparse enrichment remains explicitly unavailable.
Thirty cold cycles show three cockpit builds becoming one, with 566 SQL
statements becoming 71. Real cockpit rows and overview HTML match exactly;
hydration JSON differs only in its generated timestamp. Sixty warm cycles have
zero builds and zero SQL in both variants. Main cold median/p95 is
14.505/16.493 ms; the frozen candidate is 6.323/6.536 ms. Warm medians
are 0.890/0.873 ms respectively; no material warm improvement is claimed. These are local server completion measurements, not browser paint
or live-host throughput.

## Native writer verification

The managed Windows Python 3.13.11 interpreter ran copied, hash-matched writer,
run-lock and clock source with only temporary synthetic lock paths. The native
probe passed live/invalid PID checks, a 0.152-second live-holder timeout, killed-
holder recovery, sixty mutually exclusive windows from three contenders over a
stale claim, and repeated guard unlock/reacquisition. The harness distinguishes
Windows launcher PIDs from actual interpreter PIDs. Every test interpreter and
launcher stopped, the exact owned directory was removed, and a final read-only
postcheck found no task resources. No database was created or opened. This is
native protocol evidence, not a production pipeline or deployment test.

## Narrow extractor identity cost

The final closure contains 35 owned Python files (722,473 bytes) and twelve
active vendor distributions. Shared tier ranks and table-cell locators now live
in existing domain-model owners. Existing imports reexport the same objects.
SEC uses the small validation store directly. These changes remove accidental
IR discovery, PDF parsing and heavy loader dependencies. They also remove the
IR-specific dynamic registry checker: 114 net production lines are deleted,
with no new module or dependency.

Thirty cold calls have a 452.602 ms median and 501.878 ms empirical p95. Sixty
warm calls have a 1.927 ms median and 2.037 ms empirical p95. The previous
95-file closure measured 711.748/741.009 ms cold and 5.944/7.126 ms warm.
Discovery remains lazy; module import does not scan the source tree. Warm calls
perform zero full-source digests. Full schema hashing remains conservative.
The real-repository regression computes and hashes the actual closure, including
required capture, issuer, confidence and resolution owners, and rejects unrelated
IR, PDF, loader and KPI-persistence imports. The 256-file ceiling fails closed.
Narrowing, compatibility, locator, confidence, calibration, timeseries and
validation coverage passed 117 tests. Capture and onboarding coverage passed
38 tests after the replay correction below.

## Follow-up review and full-matrix corrections

Opus 5.5 scored commit `82bf9753` 77/100. It confirmed the earlier material
fixes but found unnecessary extractor complexity and incomplete final evidence.
The narrowed import boundaries above address that source finding. Final
exact-head CI and a new independent review are still required before merge.

The first full CI matrix found worker-specific writer-test parameter names,
stale renderer-registry counts, and an exact-observation SEC replay conflict.
Stable parameter names preserve both writer cases. Registry counts reflect the
retired key-metrics renderer. Exact SEC replays now reuse the original primary
link; later observations append retrieval links. Immutable IDs and uniqueness
constraints remain unchanged. Regression coverage checks row equality and
foreign-key integrity. These fixes are source changes, not production mutations.

## Credential-scan metadata reconciliation

The full credential scanner identified three Git commit fields in the helper
ledger and the fixed grounded-search compatibility digest as false positives.
The existing filter now recognizes only those three forty-hex metadata fields at
the exact ledger path and the complete named digest assignment at the exact test
path. The global key allowlist and detector set are unchanged. All 930 filter
tests pass, including actual detector subprocess checks that adjacent synthetic
credentials and identical hashes under unrelated paths still fail. The search
compatibility oracle keeps its exact digest value. No runtime source changed.

## Security scan corrections

The runtime dependency audit found CVE-2026-59951 in `mcp` 1.28.1. The owning
requirement now requires `mcp>=1.30.0,<2`; the hashed lock changes only that
package and its two hashes. The maintainer identifies 1.30.0 as the patched
1.x release: https://github.com/modelcontextprotocol/python-sdk/security/advisories/GHSA-84m7-p3x7-pcfv .
The final runtime audit covers 92 packages with zero known vulnerabilities.
The four-package design inventory also has zero known vulnerabilities. Its
intentional Linux/Windows wheel hashes prevent a Mac install-based audit; the
pinned inventory audit uses `--disable-pip --no-deps`, while CI checks Linux.

Six Bandit B608 findings contain only bound values, placeholder counts or closed
canonical query fragments. Reviewed, issue-specific annotations describe those
exact boundaries. All four edited Python syntax trees are identical before and
after the annotations. Bandit 1.9.4 passes against the unchanged baseline. The
scanner, global skips and global baseline remain unchanged.

## Browser path and native Windows coverage

Two independent Opus 5.5 reviews of `05edde2e` scored the work 81/100 and
found no blocking source defect. The second review included actual browser-path
evidence. The remaining verification gaps are described below.

Chromium exercised the real Cockpit-to-Evaluation path for 80 synthetic task
cycles across 1440- and 390-pixel widths. Cold builders fell from two to one,
and SQL statements fell from 397 to 73. Warm SQL fell from six to zero.
Useful-content readiness stayed near 76–78 ms for Cockpit and 23 ms for
Evaluation. This fixture shows less backend work, with no material browser
timing gain. The readiness check uses exact fixture rows and two animation
frames; it is not physical paint or production throughput. The earlier
three-route server benchmark also requested `/api/panel/overview`, which the
native initial Cockpit path does not request. Output parity passed. All owned
browser/server processes stopped and both listeners closed.

Two isolated native Windows attempts produced no recoverable test result.
Their pass/fail/skip counts remain unknown. Both temporary scopes were removed
and cleanup found zero matching processes. The earlier successful writer probe
does not establish the separate process-tree integration cases.

The CI workflow now runs those five original cases on a disposable hosted
Windows runner with Python 3.13 in a disposable virtual environment. This
exercises the Windows environment launcher as well as the native APIs. It loads
no application fixtures or database.
The aggregate CI gate requires the Windows job for code changes. Its JUnit
receipt must contain each exact case once, with no skip, failure or error.
Missing or malformed receipts fail the gate. Receipts are retained for seven
days. The first hosted run passed four cases and failed one fixture readiness check.
That fixture exposed its PID file before the write completed. It now publishes
the complete PID by atomic rename. Process termination assertions are unchanged.
A fresh native run and complete exact-head CI are required.
This is a tested **no surface change** disposition under
`directives/operations_governance_surface.md`: application routes, Operations
actions, scheduler registration and production host authority stay unchanged.
Hosted Windows evidence does not prove deployment or canonical-host parity.

## Real search integration repairs

A disposable synthetic fixture now exercises the source owners together instead
of replacing admission, corpus and vector boundaries. It exposed five concrete
interoperability defects:

- Source coverage and population processing used different deterministic binding
  delimiters. Population processing now creates the source owner's identity. It
  preserves exact historical bindings and their bytes, and accepts the original
  source clock only when it matches the authoritative document recording clock.
  Payload changes, arbitrary old clocks and future visibility still fail closed.
- Retrieval population constructed reporting reasons locally. The strict corpus
  builder requires the canonical reporting projection. The caller now passes
  that projection unchanged and requires every positive governed document to
  match the selected issuer, binding, document, family and reporting entity.
  An extra governed document cannot silently expand the accepted scope.
- Vector seals record naive UTC timestamps. Research admission now normalizes
  that clock before comparison with aware UTC. The stored seal and its
  commitment stay unchanged; a future seal remains inadmissible.
- Identity population preserves an earlier valid binding. Its verifier
  incorrectly required the new cutoff exactly. The verifier now uses the
  existing latest-visible selection at knowledge and observation cutoffs.
  Later invalid revisions remain blocking. No immutable binding is rewritten.
- Sealed Ask dropped the unit when currency was present and omitted verified
  financial context. It now retains literal unit and currency, full fiscal
  dates, exact definition and binding revisions, source observation, locator,
  basis, scope and recorded precision. The source owner verifies the selected
  publication before model context is assembled; substitutions block the trace.
  Shared publications are verified once per bounded batch. Answer and auditor
  retain identical fragments and hashes. Native records lack separate
  actual/guidance/consensus and scale fields. These remain explicitly unavailable;
  reported status, currency and decimals cannot supply those missing meanings.
  The source-read owners now participate in retrieval verifier identity, so
  a changed owner invalidates an earlier promotion.

Focused regressions reproduce the original faults through their public owners.
The final fixture receipt and exact-head release checks remain separate proof.
The fixture uses public pinned embedding weights, a fixed synthetic corpus and
an exact evaluation-bound human approval. Approval covers that fixture only.
No production model or provider policy changes. Synthetic relevance results do
not establish issuer coverage, general financial retrieval quality or production
latency.

Candidate `13292451` adds 219 noncomment production lines relative to
`05edde2e`. Module count, size buckets, duplication groups and dependency cycles
are unchanged. Canonical reachability is complete with zero production unknown
or unresolved edges. All 189 disposition decisions retain their fingerprints
and targets; only source provenance hashes change.

A fresh server measurement against the same main source and synthetic fixture
preserves exact rows and response content except generated timestamps. Cold
median completion is 15.810 ms on main and 7.336 ms on the integration source;
builds remain three versus one and SQL statements 566 versus 71. Warm medians
are 0.968 and 0.983 ms, with zero SQL and builds. No warm gain is claimed.
This remains a three-response server measurement, distinct from browser
readiness and live host throughput.

The genuine fixture passes all seven source-plane verifiers and real lexical
and semantic qualification. Cutover remains **HOLD**: the established parity
gate requires a non-empty legacy universe. Its only accepted legacy bridge
requires SEC CompanyFacts coordinates; this exact fixture is issuer HTML.
No SEC locator, parity receipt or immutable observation was fabricated.
The evaluation, approved model, original source and observations remain
unchanged. The full promoted Ask answer/auditor/persistence driver is therefore
not executed and is not claimed as proof. This is a fixture limitation under
the retained migration contract, not permission to bypass it.

The architecture receipt is deliberately remeasured under the existing
receipt-refresh contract. Candidate `13292451` adds 219 noncomment lines for
source-owner repairs. The final clock correction adds 43 more, for 262 total. Scanner identity, module count,
size buckets, cycle shapes, fan-out ceiling and duplication baseline stay
unchanged. This is an explicit reviewed increase, not a claim that code size
fell. Two inherited test-only private-access directives were removed when the
required suppression gate rejected their changed files. Typed test seams
replace them; no suppression exception or error ceiling is added.

A second 80-cycle browser comparison uses the repaired runtime at `bf00d333`.
The same real shell, Flask handlers, JavaScript, synthetic rows and readiness
checks pass at both widths. Cold builders remain two versus one and SQL397
versus73; warm SQL remains six versus zero. Desktop cold/warm Cockpit medians
are 65.0/48.3 ms on main and 69.8/61.3 ms on the repaired runtime. Narrow
medians are 79.4/66.0 ms and 71.3/53.2 ms. Evaluation remains near22 ms.
The direction differs by width; these ten-sample groups do not establish a
consistent browser speed gain or a causal regression. Both process groups
stopped, both listeners closed, and fixture bytes and row payloads match.

The owning suppression inventory measures 1,854 remaining suppressions, down
from1,856. The test-subsystem ceiling decreases from921 to919 to retain that
cleanup. No diagnostic ceiling increases. The reachability expectation for
the unchanged native child invocation moves from line328 to330 because atomic
PID publication adds two fixture lines; its target and assertions remain.

The complete owning static-quality gate passes on the frozen integration
source: 2,810 retained files, 2,050 inherited Pyright diagnostics and1,854
suppressions at exact descending subsystem ceilings. Changed owning files
also pass strict checks. This does not claim that inherited debt is zero.

## Final context-clock and fixture correction

Full CI on `13292451` passed20 jobs, including all five native Windows cases,
and failed the source-version fixture shard plus the aggregate gate. That
fixture manually constructs projection rows and already replaces trace
admission. Its route-only source-context double now preserves the real emitter,
inherited-generation lookup, HTTP route and persisted mapping assertions.
All eight route tests pass. It is not native source-admission evidence.

Review also found that the new context adapter collapsed knowledge cutoff K
and recording cutoff O. It now reads the exact verified projection head's O,
including inherited entries, and requires selected cell and observation
knowledge at or before K and recording at or before O. Query time cannot
widen these bounds. The source owner still verifies the selected publication
and exact commitments. Context carries both cutoffs and observation clocks;
relation metadata is excluded. Real published dual-clock regressions and
microsecond boundary failures pass. No FactReadModel API, schema or temporal
policy changes. A new exact-head CI run and independent review are required.

The final frozen runtime measures 552,104 noncomment lines across 1,451 modules.
The clock correction adds 43lines only in the existing Ask owner. Module
count, fan-out ceiling, cycle shapes, size buckets and duplication measures
stay unchanged. The unchanged owning static gate again passes at 2,810 files,
2,050 inherited diagnostics and 1,854 suppressions.


## Follow-up to the independent source review

The independent Opus 5.5 review of `77d2334f` returned 82/100 and no blocking
defect. All 22 CI jobs passed on that exact source, including all five native
Windows process-tree cases. The owner's acceptance threshold remains separate;
that score did not authorize merge.

The review identified an availability error in retrieval scope planning.
An inventory can contain a positively governed document from a family whose
obligation is no longer selected. That extra document must not block an active
family's run. Scope planning now distinguishes those additional documents from
selected expected documents. A selected document still must match the exact
issuer, entity, family and document version. A mismatch reports
`reporting_document_scope_widened`; an absent obligation binding retains
`expected_document_binding_missing`. The canonical reporting projection remains
unchanged. The real projection regression fails on the earlier source and the
15 owning tests pass with the correction.

The Ask adapter now has one fact path. Its internal emitter requires the
selected entry, source bundle and recording bound that production already
supplies. The former single-fact fallback is removed. Real-source regressions
now call the production batch adapter; only trace admission is doubled for
the isolated preverified trace fixture. Two published facts prove ranked
output order and one verification of their shared publication. Reordered,
missing and invalid owner results block the complete response. A malformed
result with neither bundle nor failure now raises the typed verification
error instead of a raw `KeyError`. Literal unit semantics remain usable;
separate unavailable scale and status fields remain unavailable. All 93
owning context, route and cache tests pass, with strict checks clean.
This proves the adapter boundary, not full retrieval promotion or LLM quality.

The follow-up reduces production source by five noncomment lines relative to
`77d2334f`: 552,099 lines across the same 1,451 modules. Module-size buckets,
maximum fan-out, dependency cycles and duplicate measures do not regress.
The unchanged architecture and duplication gate passes. Its baseline and
scanner are unchanged.


The independent Opus 5.5 review of `c6982c36` returned 83/100 with no blocking
defect. Its two wording and documentation findings are addressed below.

The corpus authority remains the exact governed reporting projection. Active
acquisition obligations determine the required family canaries; they do not
remove otherwise eligible archived reporting documents. The additional
expired-family document remains an included, searchable corpus member. Its
source identity and selection disposition remain committed. A real lexical
query now verifies that the retained earnings document can be retrieved with
its exact source URL. This does not claim semantic qualification of every
inactive family. Filtering those canonical members would change the existing
corpus contract, whose owner rejects incomplete projection inputs.

The financial context warning refers to the selected fact's own `unit_key`.
It no longer places a fixed unit example into every fact's context.


A new CompanyFacts fixture exposed an actual legacy/canonical parity fault.
The legacy writer records fiscal civil dates as SQLite midnight text, while
parity accepted only a bare date or a timezone-aware timestamp. The legacy
boundary now accepts only its exact midnight date formats. It does not infer
a timezone for other naive times, and it does not change canonical timestamp
or knowledge/recording-cutoff requirements. No persisted value is rewritten.
The old source fails three new regressions; all 23 owning tests pass after
correction, including fiscal-day mismatch and naive-timestamp rejection.
The unchanged scanner measures 552,105 noncomment lines after this fix, one
above the prior reviewed baseline and 3,108 above main. The baseline refresh
records this correction explicitly. Module counts, size buckets, dependency
cycles, fan-out and duplicate ceilings do not change.

## Larger synthetic performance probe

A predeclared fixture contains 50 portfolio companies, 50 evaluation companies
and USD 500,000 of portfolio value. Main and candidate use the same fixture
bytes and produce the same rows and responses, apart from generated timestamps.
Thirty cold server cycles reduce the median from 55.42 to 16.46 ms, SQL
statements from 2,646 to 231, and cockpit builds from three to one. Sixty warm
cycles remain similar at 0.933 and 0.911 ms, with no SQL or cockpit build.

Eighty actual Chromium Cockpit-to-Evaluation cycles cover widths of 1440 and
390 pixels. Cold Cockpit medians improve from 100.55 to 87.55 ms on desktop
and from 98.50 to 88.80 ms at the narrow width. Evaluation navigation improves
from 40.95 to 24.05 ms and from 41.30 to 24.65 ms. Warm navigation remains
similar. Each cycle checks the exact rows and portfolio value, then waits for
two animation frames. Browser interception disables the HTTP cache. No script
or external network error occurs. The test servers and listeners are stopped.

These observations concern a fixed synthetic task. They do not measure physical
paint, largest contentful paint, production coverage or general throughput.
The candidate snapshot contains `c6982c36` and the selected-unit wording fix.
It predates the fiscal-date correction, which is outside this browser path.
The retained source hashes define that scope; the measurements do not establish
whole-tree equivalence with a later commit. The initial fixture row-count
assertion errors are retained. They occurred before the timing loops.
