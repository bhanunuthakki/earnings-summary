# Retail investor research roadmap

Date: October 3, 2026, America/Los_Angeles.
Status: source delivery in progress; reviewed draft PRs are separate from live deployment.
This document sets product priorities and
acceptance evidence. It does not authorize a merge, deployment, new provider,
schedule change, financial admission or trade. Canonical directives retain their
authority. The implementation history remains in
[pipeline_reconciliation.md](pipeline_reconciliation.md).

## Goal and scope

Help one retail investor understand a company, test the investment case, value it
with explicit assumptions, and compare the next investment with the actual book.
The application must make deep research repeatable without requiring the owner
to manage data plumbing or remember which script to run.

The recurring questions are:

- What changed in the business, and what supports that conclusion?
- Does it confirm or weaken my recorded thesis? What would prove me wrong?
- What is priced in? Which valuation assumptions can reverse the conclusion?
- How does a position change expected return, concentration and correlated risk?
- Which evidence is missing, and what exact action will obtain or repair it?

Research quality, source integrity, usability and recovery are product outcomes.
Feature count, document count and passing tests alone do not establish them.
A usable result includes its limitations. Missing valuation evidence must not
prevent reading a saved source or using separately verified research. It also
must not become an unsupported investment or cash recommendation.

## Current verified source delivery — October 3

The earnings-readout update is now draft [PR1621](https://github.com/bhanunuthakki/earnings-summary/pull/1621), stacked on PR1605. Head `cabaa31b` passes 23 hosted checks with two skips. Its current-schema tests verify saved evidence, exact fiscal requests, source bytes, historical context and cache separation. A stale test mock caused the earlier failed run. That failure remains retained; the corrected mock preserves the original cache checks. Source proof does not establish current financial completeness or live model readiness.

The present-valuation-upside update is draft [PR1622](https://github.com/bhanunuthakki/earnings-summary/pull/1622), stacked on PR1621. Head `71dfc5e0` passes 23 hosted checks with two skips. It labels present fair-value upside correctly, keeps historical Sharpe ratios separate, and refuses unsupported model inputs. The original profile-test failure is preserved. Its narrow fixture repair keeps all original test assertions and adds a real refusal control. Source and integration reviews give advisory PASS. Browser geometry and actual-route performance remain unverified after the browser refused the file preview. No alternate route was used.

The actual Windows process-control checks now pass normal completion, abrupt exit and timeout. The four unchanged application tests also pass with zero skips and errors. Their first run failed during setup because the test package omitted one same-commit JavaScript resource. The corrected package changes no test or fixture. Both failed and passing evidence are retained. Native handle checks and a separate complete process and listener inspection show no remaining task resources. The separately briefed GPT-6.1 Sol gives the actual four-test boundary advisory PASS. This proves the process controls at tested source `7ecd3a95`. Main has since changed the runtime module. The combined source needs new native checks. The earlier result does not prove AppContainer, the filing reader, an installed runtime or a fresh financial model.

Clean local release candidate `564fbcd9` now combines the reviewed source PRs with main `030bba8c`. It preserves the new main KPI migration and moves the undeployed thesis migration to revision 0051. Migration SQL remains unchanged. The separately briefed GPT-6.1 Sol gives the combined source advisory PASS. Its final four-file integration preserves all functional and migration bytes. It updates one test to use the actual migration head and rebinds three source hashes without changing their classifications. Original branches and small PRs remain available. The complete combined checks and current native checks remain pending. Source review or passing CI does not authorize production installation.

The temporary Mac cross-compiler produced the small Windows loader probe. Signature, saved build inputs, binary code and imports have independent checks. The separately briefed GPT-6.1 Sol gives the static binary evidence advisory PASS. No Windows probe has run. The retained sandbox launcher changes permissions and has unbounded waits, so it cannot serve as the next diagnostic unchanged. The next candidate must use finite process ownership and verified sandbox access. No sandbox permission has been relaxed.

The bounded SEC request adapter is draft [PR1629](https://github.com/bhanunuthakki/earnings-summary/pull/1629). It provides explicit plan, apply and status routes over the existing capture owner. Its first full CI run found an old dashboard route-count test. The targeted repair checks the exact three new routes and preserves all other assertions; independent source review gives advisory PASS. Head `62685253` passes 23 hosted checks with two skips. There is no rendered Library or Analytics control yet.

The synthetic MELI path now tests all 28 input slots through the real source, definition, resolution and research-snapshot owners. It checks that replay adds no duplicate definition revisions. This proves pipeline mechanics, not current issuer completeness. Tests also found a source-publication clock mismatch and an old-snapshot/new-definition mismatch. Separate fixes preserve historical receipts and require exact snapshot membership. Their independently briefed GPT-6.1 Sol source review gives advisory PASS. A further unpatched test found that changing the retained raw file did not block the model preview. The local repair checks exact saved bytes through the shared input consumer. It requires an explicit source root and records the present read time separately from the financial cutoff. The changed-file gate passed 142 tests. Another 174 existing reader, allocation, dashboard and golden-comparison tests passed after a narrow callback fix. Final source review and integration remain pending; no current valuation is accepted.

All 78 pending shared files are preserved at local branch `codex/parked/shared-source-pending-reconciliation-20261003`, commit `d20b1cf4`. The exact source bytes, original raw index and 11 intent-to-add entries have independent recovery evidence. A separately briefed GPT-6.1 Sol review gives advisory PASS for preservation. That recovery record describes the exact captured version. Other sessions have since committed shared source at `3d00a858`; their changes remain untouched. The root roadmap has a separate local revision. Further cleanup requires release of active writer ownership. The saved work remains pending reconciliation, not accepted release content.

## Current position

| Outcome | What is proved | What remains |
| --- | --- | --- |
| Source baseline and reconciliation | All 78 shared changes have exact recovery evidence on a local parked branch. The small reviewed source PRs are retained. The prior release candidate passed 920 fast tests and 46 golden comparisons. The newer main integration passed 210 targeted tests, changed-file checks and source review. Its complete combined gate remains pending. | Complete combined checks and current native proof; release approval and installation. Clearing the shared checkout needs release of active writer ownership. |
| Reliable data and context | Reviewed source keeps exact fiscal scope, raw evidence and complete stored thesis/card context. It prevents old models and present fair-value gaps from appearing as accepted forward returns. Selected SEC capture has exact plan/resume interfaces. | Independent review and integration of the local raw-byte repair; actual filing-reader sandbox repair; actual UI/performance proof. |
| Trustworthy valuation updates | The MELI recipe keeps commerce/non-credit FCFF and credit FCFE separate. Required input roles, lineage and scenario gates are explicit. | Current issuer source publication and 28-role input set; accepted assumptions/scenarios, economic tie-out and replay. No current MELI valuation is accepted. |
| Portfolio decisions | A current read-only linked-account snapshot is retained privately and its account/position totals reconcile. | Reserve location, all unlinked holdings, compatible risk evidence and accepted candidate return inputs. The two requested cash judges follow that same package. |

The inspected Windows runtime is older than the prepared source. Source commits,
hosted CI and local tests do not prove installation. The recent main changes,
including PR1591's retired Score/Fit fields and the current performance work,
remain preserved. The current main research-skill update is also included.
Its source-drift check exposed stale procedure descriptions. Their correction is
under separate review; a hash update alone cannot establish semantic accuracy.

FMP is inactive. The initial financial acquisition lane uses SEC and issuer
sources. The selected-accession result means readable text only. It cannot certify
financial extraction, semantic admission, issuer completeness or model readiness.
The actual Windows process-control tests pass at the older tested source. A current runtime check could not prove the complete dependency population within its bound. Its partial results remain failures. A smaller scoped inspection is being prepared; no limit or completeness gate has been relaxed. The filing reader remains blocked
by extension-module initialization in AppContainer. The non-CLR loader diagnostic
is intended to identify that cause without relaxing sandbox permissions.

Existing scheduled collection policy remains unchanged. Routine analysis uses
stored evidence. Additional collection starts through an explicit Library or
Analytics request. A source-data gap does not itself justify retaining cash or
buying a security. Supported concentration analysis can proceed while valuations
remain blocked, with its own coverage and account limits.

## Delivery milestones

| Order | Investor-visible outcome | Smallest coherent implementation | Acceptance evidence |
| --- | --- | --- | --- |
| 0. Establish a safe baseline | I know which source version runs, which work is pending, and which failures matter. | Resolve the three draft PRs through their review gates; preserve parked work; fix superseded valuation selection and misleading price labels; exclude or clearly label unready return factors. Reconcile concurrent UI work. | Exact source/installed identities remain distinct. An old or superseded model cannot appear as a current accepted valuation. Unavailable providers are reported without repeated calls. No unrelated changes enter a PR. |
| 1. Complete one MELI research path | I can read what changed, inspect the key numbers, compare valid periods, and test my thesis. | Select the required issuer package through existing inventory/capture/processing interfaces. Save source bytes, extract the required tables/text, admit the needed facts, and retain the exact earnings/thesis context and references. Provide one bounded report and its request/status/reuse loop through existing surfaces. | Each used number resolves to saved wording/locator, period, dimensions and definition. Invalid comparisons fail. A correction preserves the prior result and produces a new revision. Missing inputs stay visible. An offline run reconstructs the saved evidence and context. Saved-data reads make no hidden fetch; one explicit request shows success, partial failure, cancellation and exact-stage recovery without duplicate publication. |
| 2. Accept one current MELI valuation | I can understand and challenge the value, its ten main assumptions, and its downside. | Complete the existing 28-role recipe and annual/current/prior-YTD inputs. Review commerce/non-credit and credit attribution separately. Persist assumptions, scenario parameters, probabilities and acceptance through existing model owners. | Deterministic replay matches the saved result and economic identities reconcile. Credit is not counted twice. Wrong/stale/future inputs and unreviewed drivers block eligibility. Normal downside and stress outcomes are distinct. Probability and expected-return arithmetic reconcile. Sensitivity and the explanation of changes from the prior accepted model pass review. Quote date and financial period are shown separately. |
| 3. Prove the next required company and fund types | I can compare the names I own and the most relevant evaluation candidates on a defensible basis. | Prove one foreign-company financial interim package, then extend the next required model family. Prove one relevant Avantis fund through the existing ETF lane. Use the canonical roster and recorded research depth. Add operating, bank/credit or holding-company models only for actual needs. | An unrelated 6-K cannot satisfy a financial-period obligation. Each company model declares its own complete input roles. Fund identity, holdings, fees, published mandate and factor/exposure evidence have separate dates and provenance. Comparisons retain currency, period, basis and definition compatibility. No company or fund inherits readiness from MELI or a fresh price alone. |
| 4. Produce portfolio and cash decisions | I can compare expected returns, correlations and concentration with my actual constraints. | Start descriptive book/cash/exposure analysis alongside milestone 1 once its own inputs qualify. Add expected-return allocation after the relevant company and fund work qualifies. Retain account/holdings/cash/lot state, aligned price histories, covariance method, weights, valuation/scenario inputs and recorded goals. Use existing tools and obtain the requested two independent assessments. | Saved inputs reproduce risk and expected-return calculations. Cash, funds, unlinked holdings and excluded instruments are accounted for. Coverage and estimation limits remain explicit. Employment/exposure assumptions are labelled. The two judges may disagree; both use the same accepted input package for their conclusions. No trades execute. |
| 5. Make repeat use routine | I can request an update, see what happened, recover a failed run, and use the rest of the application. | Connect a small Library/Analytics request adapter to existing job/stage owners. Start the basic request/status/reuse loop during milestone 1; expand it with the supported analysis tracks. Consolidate scheduled collection and repair behavior without changing authorization implicitly. | Saved-data analysis makes no hidden fetch. A request declares issuer, purpose, periods and source scope. Successful work is reused; failed stages resume without duplicate capture or publication. Cancellation, unavailable providers and independent background failures have truthful states. Actual-route performance and restore tests pass. |

The first research question is bounded: for one supported reported MELI period
at an explicit knowledge cutoff, what changed in commerce, payments and credit,
which comparable numbers support it, and what does it mean for the recorded
thesis and counter-case? It does not require all 28 valuation roles. At milestone
1, opening the saved panel/source, request feedback, slow-dependency behavior
and a bounded evidence/output restore must also pass their applicable checks.

Milestone 5 is progressive, not a reason to postpone a usable request path until
all models exist. Milestone 1 is the first research delivery target; milestone 2
is the first accepted valuation target. Native qualification and consumer repairs
can progress independently. Do not publish a valuation merely to show progress.

The order is a dependency sequence, not a rule that all work must run in series.
Book reconciliation and descriptive risk can start early. A valid exposure result
does not establish expected return or justify a buy. Conversely, an unfinished
company valuation does not block a separately valid concentration calculation.

### Research depth by task

| Task | Minimum relevant evidence | Completion test and limit |
| --- | --- | --- |
| Earnings preview or readout | Release/transcript, valid prior periods, dated management guidance and estimates when actually available | Explain the meaningful change or testable expectation. A historical preview uses only knowledge available at its cutoff. No invented consensus comparison. |
| Thesis and counter-case | Exact owner rules/notes, supporting and conflicting observations, business/competitive evidence where material | Explain what supports, weakens or could disprove the thesis. An absence of evidence does not count as a passed threshold. |
| Company quality and capital allocation | Business model, unit economics, competition, reinvestment, financing and management allocation decisions | Answer the declared durability or allocation question with its strongest counter-case. Do not impose this depth on every quick source lookup. |
| Catalysts and expectations | Dated event, attributed owner/management/analyst expectation and subsequent evidence | Distinguish prediction from reported fact. Retain what was expected before the event and what later happened. |
| Valuation | Complete model recipe, accepted drivers, scenarios, horizon and observed price | Reproduce arithmetic, inspect economic validity, show sensitivity and price-implied assumptions. Missing required inputs block acceptance. |
| Peer or evaluation comparison | Versioned peer/candidate set, compatible periods/definitions, explicit selection criteria | Explain relative opportunity with coverage and comparability limits. A newer price does not cure stale fundamentals. |
| ETF diligence | Fund-specific mandate/process, fees, holdings, exposures, overlap and return conventions | Reconstruct claims and exposure arithmetic. Retain unresolved exposure weights. Do not apply a corporate DCF gate. |
| Portfolio risk and allocation | Reconciled book/cash, market histories/method, constraints; comparable accepted returns for allocation | Risk can precede valuations. Allocation reconciles funding, taxes/costs and alternatives. A modeled subset cannot silently represent the full book. |

Reuse existing report and card families. Deep research is the evidence required
to answer the question, not the length of a generated report.

## Required plumbing, kept proportional

### One shared evidence path

The core path is issuer/source identification, saved raw document, extracted
observation, reviewed meaning, as-of resolution, and analysis output. Existing
pipeline interfaces own each stage. Agents select the route; they do not become
an alternate financial-data writer.

Every number used for a model, comparison or thesis threshold needs issuer,
fiscal period, unit/currency, scope/segment, accounting basis, effective definition,
Observation Version, document and locator, capture/extractor identity, and
admission/comparability disposition. Preserve raw wording. Keep acquisition
completeness separate from extraction and expected-fact completeness. Corrected,
restated and conflicting values remain versioned and inspectable.

Verify source bytes on capture, retained evidence use and restore where the
owning contracts require it. Test missing/corrupt bytes and amendment packages.
A captured page or one extracted metric is not an archive-completeness receipt.
Use one shared resolver for every consumer. Keep provisional source observations
visible with their status; they cannot silently enter accepted calculations.

### An evidence package sized to the question

Earnings needs the specific release/transcript and valid prior comparisons.
Thesis evaluation needs exact rules and observations. Valuation needs its model
family's full input recipe and accepted assumptions. Descriptive portfolio risk
needs the book and market-history inputs; expected-return allocation also needs
accepted company valuations or explicit fund return assumptions. Peer research
needs a versioned peer set and compatible definitions. A quick source question
need not run a full DCF or trigger collection of every historical document.

Fund diligence uses the existing [ETF published-data lane](../../directives/etf_data.md),
not a corporate DCF. Verify the fund/series identity, published mandate and
selection rules, fees, holdings, country/sector exposures, overlap with the book,
factor estimates and price-history treatment. Retain source dates and unknowns
for each field. A new capture does not make old holdings or characteristics
current. Compare relevant Avantis funds on these dimensions. Documented adapter
status is historical evidence until current support is checked. Missing paid
enrichment must not silently become a neutral factor or a supported return claim.

Readiness belongs to the declared result and evidence scope, not to an entire
company with one green badge. A source-backed research result can be partial.
An accepted valuation cannot omit required operands. Unadmitted text can be
read as source context with a clear disposition. The central decision-grade
requirements still apply to any claim that uses that designation.

Retain novel and one-off management observations, with wording, speaker,
locator and period, even when they never become recurring KPI fields. External
research and owner notes retain their own source class and revision. They do
not become issuer-reported financial facts because they appear in the Library.

### A retained record of what produced the result

These records extend existing owners; they are not new stores:

| Record | Owner to use first | Required retained content |
| --- | --- | --- |
| Scoped request | Analysis scope and pipeline/job request owners | Question, instrument, periods/cutoffs, source scope, request identity and required evidence. |
| Source and fact | Inventory/capture, fact resolver, Processing and Research Snapshots | Exact bytes, source/observation/definition versions, locators, dimensions, completeness and dispositions. |
| Research context | Research/report and immutable artifact owners | Actual thesis/note/estimate context or immutable resolvable revisions, prompt/configuration, response and exact claim references. |
| Model or risk result | DCF input/persistence/readiness and portfolio snapshot owners | Effective operands, methods/formulas, attributed assumptions, scenarios/horizon, acceptance and deterministic output. |
| Execution and recovery | Pipeline Execution and job runtime | Attempts, stage/checkpoint, completed work, failure cause, cancellation and next permitted action. |

Reuse existing artifact and snapshot storage. Save actual input content and
references, not just a fingerprint. The result's manifest records the question,
scope/cutoffs, source/observation/definition versions, owner thesis/note revisions,
model/formula or prompt/schema/configuration version, assumptions and output ID.
Evidence-bearing claims use exact resolvable references. Label reported facts,
management claims, estimates, calculations and analyst inference separately.

Old outputs stay intact when new evidence or notes arrive. Deterministic model
and risk arithmetic must replay without providers. For narratives, retain the
context, prompt and response; do not promise that a new LLM call writes identical
prose. Compare generated claims with the saved evidence through representative
evaluations, including missing evidence and contradictory sources.

Test preservation by changing a note, thesis rule, quote and source version after
an output is saved. The old output must still open with its original content and
references; the new output receives a new identity. Test narrative overstatement
as well as invalid citations: a correctly cited number does not prove an
unsupported conclusion about the business.

### Valuation quality beyond replay

A reproducible model can still be economically wrong. Acceptance therefore
needs both arithmetic checks and a review of the business assumptions. For MELI,
reconcile the commerce and credit segments, shared costs, funding, required
capital, reinvestment, cash flows and the enterprise-to-equity bridge. Explain
which evidence supports the allocation and which parts are analyst assumptions.
Keep credit FCFE and non-credit FCFF treatment consistent with the existing
methodology; disclose and review any methodological departure.

Save every material assumption and each driver that can reverse the conclusion,
with value, attribution, rationale, supported range, evidence date/reference when
available, and changes from the prior accepted model. Present the ten most
important assumptions as the requested reading summary, not a cap on records.
Test plausible changes in growth,
margins, reinvestment, credit losses, funding, discount rates and terminal value.
Review terminal economics, the share of value supplied by terminal assumptions,
a simple valuation cross-check, and what the observed price implies under the
model. Review price-to-value reconciliation. A large revision
needs an explanation that separates new facts, changed assumptions, method
changes and errors corrected. Do not silently replace the model.

Use a common scenario meaning and investment horizon across companies while
allowing business-specific drivers. Ordinary bear cases describe a plausible
adverse business path. Severe stress is separately labelled. Persist coherent
joint driver assumptions, scenario probabilities and their rationale; probabilities
are analyst estimates, not known frequencies. Expected returns and any Sharpe
estimate must state their horizon, return convention, currency, dividends/dilution
treatment, risk-free assumption and uncertainty. DCF upside and annualized fund
returns are not directly comparable. Unweighted stress can remain useful without
pretending to be a probability-weighted forecast. Historical volatility or replay
cannot establish a future return.

Portfolio comparisons must show unresolved coverage without renormalizing the
modeled subset to the whole book. Reconcile each funding account and distinguish
owner constraints from analyst preferences. Include do-nothing and cash-only
alternatives. A tax or data gap is a limitation, not a conclusion to keep cash.

### Recovery that reflects the cause

The first request loop selects issuer/instrument, reported period, question and
knowledge cutoff. It shows reusable evidence and exact missing prerequisites
before submitting a bounded plan. Library and Analytics share the request identity
and job owner. Repeated clicks reuse the matching active or completed scope.
Show stage status separately from result acceptance. Support cancellation and
resume only where the stage owner can enforce them. Reopening a saved result
must work with external calls disabled.

The existing refresh route and job runtime are extension points. Their default
FMP/legacy dispatch is not the required SEC/issuer route. A button that invokes
that unchanged chain is insufficient. Use a narrow adapter that selects existing
inventory, capture and processing interfaces with explicit prerequisites.

Use one writer for each mutable stage/checkpoint. Reuse completed capture and
processing. Resume the exact failed stage after prerequisites are repaired.
Do not retry unchanged schema, identity, validation or entitlement failures.
Bound retries for transient failures and record attempts, deferred work and the
next permitted action. Provider failure must not block independent saved-data
research or unrelated panels.

The operator needs a concise explanation: source not found, capture failed,
format unsupported, extraction partial, definition unresolved, model input
missing, or scenario unaccepted. Offer the correct repair/request action through
existing job owners. A heartbeat or open port does not prove fresh data.

Qualify real Windows recovery and each required reader format through the
approved host route before relying on it. Do not make qualification of every
optional format a prerequisite for unrelated supported work. A format alternative
must have its own approved source/package and full evidence checks; it cannot
hide a failed required obligation. No success from a mocked native API establishes
installed safety. Restore a bounded evidence/output bundle and verify raw bytes,
source identities and deterministic replay before claiming reconstruction.

## Performance and reading flow

Proposed starting budgets are planning targets, not measured results. Confirm
or revise them from baseline measurements on representative data and the actual
canonical-host access route before making a release claim.

| Task | Proposed target | Required failure test |
| --- | --- | --- |
| First usable application shell | Within 2 seconds warm and 3 seconds cold | Optional tracker/provider delay does not block navigation or saved-source access. |
| Useful saved research or portfolio panel | Within 2 seconds warm and 5 seconds cold | Cached content shows its age; a failed refresh cannot silently replace a valid result with an empty one. |
| Ordinary control feedback | Within 150 milliseconds | Repeated input does not create duplicate jobs; controls show loading/cancel/error state. |
| Open a saved source/locator | Within 2 seconds warm and 5 seconds cold | Missing/corrupt bytes show an explicit failure and retain the output's source identity. |
| Refresh request | Acknowledge within 1 second; long work runs in the existing job system | Show real stage progress, cancellation and recovery. Completion time depends on the declared package and is measured, not promised as immediate. |

Measure time to useful content as well as shell time. Record cold/warm runs,
median and 95th percentile, sample size, dataset shape, hardware, source/runtime
revision and client route. Test slow and unavailable dependencies. Investigate
blocking I/O or repeated expensive reads before adding caches or raising timeouts.
Reuse existing panel/cache/job controls and reconcile the active UI work first.
Truthful partial states must remain usable. A fast placeholder with indefinitely
missing content does not satisfy the budget.

The investor-facing page should show the business conclusion, the numbers that
matter, the counter-case, valuation sensitivity when accepted, and the next
research action. Source detail and operating diagnostics remain accessible
without dominating the reading flow. Do not hide a material evidence gap in a
technical log.

## PR and release discipline

The first new implementation slice is **consumer correctness**, after checking
its boundaries against the concurrent UI work. Make earnings select the correct
non-superseded valuation, keep quote date/financial period separate, and prevent
an unready model from becoming an investment signal. This is a small repair with
reproduced failures and clear before/after evidence. It does not certify a new
valuation. Full historical as-of selection also needs effective-time checks;
filtering for today's `is_latest` alone does not solve historical selection.

The proposed PR groups are:

| Group | Purpose | Boundary |
| --- | --- | --- |
| Existing PR1605 | Consistent evidence selection and scope-clock repairs | Finish exact-version review. Do not add unrelated interface changes. |
| Existing PR1606–1607 | Process-owner checks and truthful close results | Source changes and real installed recovery qualification remain separate. |
| Draft PR1610: consumer correctness | Stop misleading valuation/price/return output | Fix the affected readers; do not revive the retired allocation interface. |
| Research record retention | Save full earnings/thesis context and exact claim references | Reuse existing artifact owners; tests must reconstruct content, not just match hashes. |
| MELI source-to-research path | Complete the required source package and a bounded request/status/reuse loop | Separate parser, data and UI changes when the evidence boundary remains enforceable. Reuse the active read-lifecycle work. |
| MELI valuation acceptance | Complete inputs, economic checks, scenario review and change explanation | Data collection success does not itself accept the model. |
| Next issuer and fund paths | Add the required financial 6-K package, model family or Avantis evidence | Extend only for selected real instruments. Fund diligence does not inherit corporate DCF rules. |
| Portfolio decisions and recurrence | Reproduce book risk, then accepted-return allocation and recurring requests | No implicit schedule change, provider activation or trade. |

These are intent boundaries, not a demand for one large PR per row. In particular,
source processing, request UI and model acceptance can each need a smaller PR.
Keep one owner for each mutable artifact and coordinate overlapping files before
starting parallel implementation. Preserve exploratory work on its parked branch;
bring back only the reviewed change needed for the current milestone.

Deliver small PRs by one intent: consumer correctness; context/reference retention;
one source-package obligation; required reader qualification; one accepted model
recipe/scenario path; one request/status interface. Combine tightly coupled changes
when splitting would leave unenforced evidence boundaries. Do not create a new
cross-cutting framework just to connect these milestones.

Use synthetic migrated fixtures, exact-head code checks, representative claim
evaluations and rendered evidence when appropriate. Native and actual-route
performance evidence are separate requirements. Local validation, merge,
installation and genuine scheduled recurrence remain distinct states. Native
work does not gain permission to admit financial facts or alter global privileges.

Keep one authoritative live database and service host. Back up recoverable state
and preserve prior source/model versions before an approved rollout. Keep the
initial source collection within SEC/issuer lanes and current provider policy.
No new paid provider, scheduler, replica, commercial auth/tenancy infrastructure,
or autonomous trading is part of this plan.

## Scope controls

Start with MELI because it is a priority holding and already has a detailed input
recipe. Select the next names from the current portfolio/evaluation roster and
recorded priorities, rather than processing the entire catalog. No fixed list in
this document becomes roster authority.

Repair or extend an existing owner when it can retain the required evidence and
serve the real task. Add a schema or abstraction only after proving that the
existing owner cannot represent the needed record. Add a model family after a
real company requires it. Broader crawlers, universal valuation languages,
additional dashboards and whole-repository rewrites are not prerequisites.

A phase ends when its named investor task and evidence checks work. Stop expanding
it then. Record unrelated findings for later work. Missing evidence can reduce
an output's scope or block the affected conclusion; it does not justify inventing
numbers, weakening completeness, or building an unrequested platform.

## Independent review

A separately briefed GPT-6.1 Sol reviewed the frozen initial proposal and immutable
source owners. Its recommendation is **revise the delivery gates, then proceed**.
The original report and structured findings are retained in
`.tmp/retail-research-roadmap-20261002/judge/`. That review binds to the initial
candidate, not a claim that the final revisions or application have passed a
release gate. No provider, database, host, build or test execution occurred in
the review. Formal blocking-purpose qualification is unavailable; this is
independent advisory evidence.

The root incorporated all six corrections:

- R1: request/status/reuse is a milestone 1 exit gate, with an explicit adapter.
- R2: a fund-specific workup is scheduled through existing ETF owners.
- R3: descriptive book/risk analysis can precede accepted-return allocation.
- R4: economic validity, material drivers, coherent scenarios and comparable
  return definitions supplement deterministic replay. All material assumptions
  are retained; the requested ten-assumption summary remains a reading aid.
- R5: the first MELI question and research-track evidence are explicit. Original
  context must survive later changes; narrative overstatement is tested.
- R6: performance and a bounded restore apply to the first durable path.

The recommended first implementation slice is consumer correctness followed by
the bounded MELI research path. Current ETF adapter support, native qualification,
live after-change performance and accepted valuations remain unproved. This
roadmap is a local proposal; it is not a claim those outputs have been delivered.
