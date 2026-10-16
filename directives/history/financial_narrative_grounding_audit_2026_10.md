# Financial narrative grounding audit — 7 October 2026

**Class:** history. This file records findings, local repairs and a proposed implementation order.
It does not create another policy owner. [LLM calls](../llm_calls.md#financial-narrative-grounding)
owns the financial narrative contract; [data provenance](../data_provenance.md) owns source lineage
and admission. All code references in the baseline appendices refer to commit
`6aa0b8de53403d310b1bd77fb56ca7eeb0649f96`, unless marked as a repair.

## Outcome

Grounding is not enforced uniformly across the application. The main HTTP Ask route has
substantial protections: default grounded retrieval, buffered answer delivery, required
claim/citation coverage and rejection before release. Sealed Ask adds exact evidence, snapshot,
call and audit identities. Other chat callers can select legacy mode by omission. Most standard
reports, advisor prose, summaries and synthesis lenses validate structure or references without
checking every financial meaning against the selected verified corpus. Some adapters drop
meaning before generation. Some caches can preserve unsupported or obsolete prose.

The MBGL investigation showed why matching numbers is insufficient. Free cash flow was called
cash flow; a pro-forma expense estimate was presented as actual; a margin change lost its fiscal
basis. An empty customer table triggered a hardcoded 5% negative concentration statement.
These were semantic and template failures. Available source data alone did not prevent them.
The corrected MBGL brief was saved separately as
`report_MBGL_2026-10-07_f70197ae906a9ea78867`. Its source checks do not establish canonical
financial admission or valuation readiness. The prior report remains retained.

## Scope and evidence limits

The audit covers all tracked Python source under `src`, `execution` and `cron`: 1,435 files,
zero parse failures and 177 recognized LLM call sites. The inventory includes wrapper layers
and evaluation infrastructure, so these are not 177 unique product operations. It cross-checks
116 ordinary purpose pins, fourteen dynamic lens purposes, six Ask consumers, generator
wrappers, scheduled dispatch, artifact writers, caches and downstream reuse. The family map
covers 34 narrative families. The AST inventory and manual code review are reproducible and
record exact baseline file/call hashes.

This is a source audit, not a live runtime certification. No application model calls, production
database reads, scheduler inspection, service restart or deployment were performed. Static
analysis cannot prove every dynamic dispatch, live environment mode, issuer corpus population,
installed Windows version or model behavior. Tests, notebooks and non-Python scripts are outside
the AST population; schedule launchers were reviewed separately. Existing mock-based grounding
tests establish control flow, not independent model entailment. Live financial semantic evaluation
remains required before claiming universal prevention.

Generated receipts are retained under `output/research/financial-grounding-audit-2026-10-07/`:
`narrative-inventory.json`, the baseline family map, detailed chat audit, reproducible scanner,
and template render evidence. The scanner is retained unchanged; reproduce it at the same
baseline by placing it under `.tmp/financial-grounding-audit/` with the family map, then running
it from the repository root. These generated artifacts are local deliverables. This dated
report is the tracked evidence record.

## Local changes delivered

| Change | Result and limit |
|---|---|
| Top-level `AGENTS.md` invariant and canonical `directives/llm_calls.md` contract | All financial narratives must use existing verified facts first and check complete output before release/cache promotion. Missing required evidence requires an early bounded source/window/effort decision. Existing exact authorization is honored. This declares the requirement; it does not retrofit every runtime caller. |
| Investing skill, research method and chat directive | Named routes inherit the same contract. One-year ad hoc research reduces coverage, not accuracy of included facts. Conceptual hypotheses remain distinct from reported actuals, guidance, consensus and calculations. |
| Shared `src/llm/style.py` prompt block | Existing standard brief/summary/lens users receive grounding instructions. The style hash changes cache identity for consumers that include it. Prompt delivery and model self-check do not replace application verification; historical callers omitting this block remain inventory gaps. |
| Transcript summary prompt | Removes the unsupported assertion that a populated FMP table always exists. Supplied transcript evidence does not prove canonical financial admission. |
| Customer concentration template | Empty rows render data unavailable. They no longer imply a 5% threshold, no material concentration or diversification. |
| Bear-case financial input template | Serializes the existing typed canonical projection within the report's selected quarterly/annual window. Preserves metric revisions, periods, currency/unit, scope/basis, immutable source observations and locators. Legacy-only and rejected values are withheld. This repairs input fidelity; full-output verification and evidence-bound cache acceptance remain open. |
| Bear-case segment input template | Preserves unit and extraction-origin label. It explicitly avoids claiming that this label proves canonical admission. Segment source/admission completeness remains open. |
| Executive compensation prompt and renderer | Missing amounts stay unavailable; true zero stays zero. Per-row currency survives, mixed-currency headers are explicit, insider currency is unavailable where the model has no currency field, and missing performance metrics do not imply no disclosure. Removes the unsourced, undated S&P 500 pay-ratio benchmark. Complete source/admission identities and output verification remain open. |
| Test inventory and source-bound reachability receipts | Registers measured times for the two new regression files, retains every prior duration/default and regenerates CI shard pins with the canonical packer. Rebinds the three existing reachability dispositions to the changed source population. Existing expressions, classifications, targets and refusal tests are retained. |

No service, route, schedule, model pin, provider, budget, database schema or operational write action
changed. The operational-surface disposition is no change. Instructions and local renderer/input
repairs are uncommitted and undeployed until explicitly recorded otherwise.

## Instruction ownership review

| Item | Disposition | Owner |
|---|---|---|
| Existing financial source/admission/completeness invariants | Keep | `AGENTS.md` and `directives/data_provenance.md` |
| Cross-surface first-source and whole-output grounding requirement | Add concise invariant; put detailed flow in existing owner | `AGENTS.md` -> `directives/llm_calls.md` |
| Missing-data and bounded research decision | Merge into financial narrative contract; link from research/chat workflows | `directives/llm_calls.md`, investing skill and research method |
| Formatting-only common prompt guidance | Extend existing shared delivery block; no per-generator copies | `src/llm/style.py` |
| Claim that adjacent FMP table always exists | Replace with actual canonical reader boundary | `src/llm_client.py` |
| Audit inventory, observations and proposed remedy | Retain as dated evidence; never current policy | This history file |
| Runtime wrappers | Keep imports to project source | `CLAUDE.md`, `GEMINI.md` |

The project source remains the owner of project instructions. No global fleet/routing policy was
changed. Runtime import wrappers require no generated text change; portability/reference checks
verify that they still reach the edited project source. The research method source hash and shared
style hash invalidate their participating consumers' caches. They do not invalidate every cache.

## Recommended implementation order

Both Ask modes lose financial context between retrieval and prompt rendering.
Grounded text retains value/unit, fiscal period/year, period end and source IDs but does not
explicitly render all currency, start-period, scale, basis, scope and status fields retained
upstream (`src/ask/grounding.py:757-831`). Sealed text uses currency **or** unit with metric,
value and period end (`src/ask/sealed_retrieval.py:1448-1483`). The model and support auditor
see this text. A richer stored manifest cannot repair meaning that neither received.

1. **Preserve financial meaning and make caller policy explicit.** Extend grounded/sealed Ask text
   projection with all existing typed fact context. Replace underweighted-facts raw queries with
   the shared resolver. Remove implicit legacy selection on product callers while preserving
   conceptual and owner discussion without invented issuer claims. Require explicit verification
   mode on stored answers. Do not force sealed portfolio-only semantics onto incompatible packs.
2. **Require an evidence-bound acceptance result before release.** Reuse the canonical resolver,
   Ask audit and decision-brief verification owners. Bind each factual assertion to exact selected
   fact/source IDs; render/replay values and calculations deterministically. Check semantic support
   and conflicts for qualitative claims against the scoped primary corpus. A matching number,
   referenced URL, valid JSON or auditor `supported=true` alone cannot establish financial truth.
   Unsupported output stays a rejected draft or returns a visible missing-evidence result.
3. **Add common preflight to request intake.** Report what is missing before generation or new
   collection. Present the smallest sufficient issuer/window/source plan, effort and output limits.
   Honor exact existing approval. An authorized one-year review plus forward estimates can proceed
   within that scope; recurring-series completeness and decision-grade labels retain their own gates.
   Unattended work must defer when scope authority is absent.
4. **Apply the result to every sink and cache.** Use the inventory to cover summaries, lenses,
   standard briefs/readouts/cards, advisor/portfolio memos, comments, triggers and scheduled output.
   Bind output hash, corpus/observation revisions, cutoff, source/claim manifest and verifier/policy
   versions. Check before any answer delta, accepted artifact, current cache or durable memory write.
   Retain historical unverified artifacts with an honest status. Generated prose stays interpretation,
   never independent verification evidence. A valid replay should avoid an unnecessary model call.

Use existing owners and seams. This recommendation does not need a new service, service handoff,
parallel database or generic verifier framework. New typed acceptance state may need a reviewed
schema change; that work is not represented as delivered by this audit.

## Open template and authority gaps

- Bear-case cache hits precede current evidence assembly and bind method/prompt, not selected
  financial observations or corpus revisions. A restatement can leave stale prose within the TTL.
- Bear-case strategic targets read the prohibited checkout-default `data/portfolio.db`, omit
  target currency and exact source identity, and describe extracted deck content as on-record
  commitments. Replace this path with the configured resolver and evidence-aware target projection.
- Filing-change booleans can create broad negative disclosure statements without a coverage receipt.
  A false flag is not sufficient evidence of no change. Preserve unknown/partial/failed coverage.
- Segment and compensation models lack full admission/source context. A typed default currency
  still does not prove source-established currency. The local repairs expose known gaps rather than
  falsely certify those models.
- Advisor and Socratic persistence constructs checkout-default DB paths while advisor context
  uses the configured DB resolver. This is a separate authority defect found by inspection.
  No database was opened or repaired in this task.

## Acceptance evidence for the remaining implementation

Exercise the same supported and unsupported claims through Ask, Ledger, coach, standup, comments,
standard reports, briefs/readouts/cards, portfolio/advisor memos, lenses and cache replay. Verify
rejection before any visible answer delta or accepted persistence. Tests must include:

- Correct number with wrong metric, issuer, currency, scale, fiscal period, scope or accounting basis.
- CFO vs FCF; cash outflow sign; quarter vs YTD/LTM; adjusted vs GAAP; carve-out vs consolidated.
- Guidance, consensus and pro-forma forecast presented as actual; omitted management qualifications.
- Restated/conflicting observations and old cache after semantic admission changes.
- Missing rows presented as zero, no concentration, no risk, no change or complete extraction.
- Generated summary or assistant assertion promoted as issuer fact or owner belief.
- Citation present but wrong source meaning; malicious or mistaken auditor says `supported=true`.
- Bounded one-year source review presented as comprehensive decision-grade evidence.
- Every product caller omitting mode; unknown new financial purpose; failure/timeout before release.

Deterministic tests prove replay and refusal mechanics. Representative independent live evaluations
must cover plausible semantic errors that deterministic structure cannot settle. No test suite can
support a promise that an LLM will never err; the application can enforce that failed/unverified
financial claims do not become accepted output.

## Local validation

The integrated prompt, canonical-input, template, bear-case, transport, research-method and
workspace-golden suite passed: **157 passed, one skipped**. The skipped prompt migration case
has no capture corpus. `make check-fast` passed, including clean changed-file formatting, lint,
types and suppression checks, and **55 changed-file tests**. `make instruction-check` passed
with **47 instruction tests**, directive/folder validation and pre-push-hook tests. Instruction
portability, investing-skill source closure, design sync and `git diff --check` passed.

The `make check` attempt passed changed-file gates and the whole-project static ceiling gate:
**2,261 Pyright diagnostics and 1,995 suppressions across 2,739 retained files** at that check.
Changed files had zero diagnostics. No static baseline was weakened. The test run was stopped
after **2,105 passed, one skipped and one failed**: the two new files put the CI duration
registry below its required coverage threshold. Passing serial JUnit times were added for
those exact files (0.146 and 2.260 seconds). All earlier timings/defaults remain unchanged.
The canonical packer regenerated pins/totals; **92 CI-helper tests passed** after this repair.

The subsequent fail-fast full-suite attempt reached **2,879 passed, two skipped and one failed**.
The remaining failure required fresh source identities in the reachability dispositions. The
existing reviewed expressions/classifications/targets are unchanged; their source commitments
were refreshed. **All 68 operational-reachability tests passed** after this repair, including
zero production unknown/unresolved edges. The full suite was not completed. This is local audit
and repair evidence, not a passed pre-push/release matrix. Retained logs name both attempts and
the focused repairs. The full matrix remains required before push or release.

Eight synthetic before/after browser renders cover 1440 and 768 pixels. The empty concentration
panel fits both widths. The isolated compensation fixture has horizontal overflow at 768 pixels
both before and after; full-shell responsive behavior is unverified. Desktop changes and the
exact golden expectation changes were reviewed; comparison mode passed. The fixture browser
closed and no service was started.

Tests use synthetic or explicitly migrated temporary databases. No live corpus/population
qualification, financial semantic model evaluation, production data access or deployment was
performed. These checks prove the local deterministic repairs and instruction delivery only.

## Baseline family inventory

The following map records pre-repair behavior. Findings about absent canonical policy or lossy
bear-case input describe the baseline; the delivered changes above supersede those specific findings.
Remaining runtime checks and sinks are not silently certified by those instruction edits.

## Central contracts and strengths

`directives/llm_calls.md` governs purpose, schema, prompt identity, telemetry, budgets, and transport. `src/llm/cli.py:1632-1651` applies production prompt overrides and attributes the resulting prompt. It does not load a scoped admitted financial corpus, require a financial evidence manifest, or audit returned prose across all calls. Web transport grounding establishes source-evidence presence, not admission through the canonical financial resolver.

`directives/data_provenance.md:73` requires selected financial evidence/calculation lineage. The canonical financial resolver and report projection preserve admission, units, currency, period, scope, basis, revisions, locators, and immutable observations. They are real existing authorities and should be reused. At the audited commit, `src/report/sections/bear_case.py:448-467` converts the financial section to a value-only Markdown table and loses units/source/scope. The delivered repair serializes the typed canonical projection. This was not a baseline strength.

The recent decision brief verifier (`src/research/decision_brief.py:475-637`, `src/research/memo_claim_support.py:74`) has exact source/snapshot membership and deterministic reported number/quotation checks, calculation replay, corpus references, source closure, and valuation readiness. It is a specialized manual decision-brief path. It is not a universal post-generation validator for every narrative. Its helper explicitly says numeric-value support alone does not prove free-form semantics. Do not overstate it as a full semantic oracle.

`src/research/method_contract.py:84-108` validates maximum input length and exact Markdown heading/body structure. Its own docstring says structure only. Prompt advice about public evidence and honest limitations does not substitute for an executable grounding result.

## Financial narrative families

| Family and purposes | Existing input grounding | Output checks and missing evidence behavior | Stored output and surface |
|---|---|---|---|
| Interactive Ask: `ask_answer`, `ask_claim_grounding`, `ask_claim_audit` | Canonical fact series, deterministic text retrieval, optional portfolio packs. HTTP endpoint passes configured mode (`execution/comments_server.py:3076`). Default environment mode is grounded (`src/ask/engine.py:714`). | Grounded path buffers text, verifies every required clause against numbered evidence, rejects unsupported/uncited clauses before delivery (`engine.py:1606-1654`, `claims.py:425-443`). Sealed path binds exact evidence/ledger/audit identities and retains the package (`engine.py:1092-1245`); sealed is limited to portfolio sessions and `ask_answer` (`engine.py:377-403`). No evidence emits deterministic no-answer, not an early bounded collection/scope approval. Explicit legacy override remains possible. | `ask_turns`, exchange persistence, retrieval traces; sealed answer audit package. Ask panel and report chat. |
| Ledger capture answers, card-reply Q&A, Telegram followups | Same portfolio ContextPack; `src/onmymind/respond.py:224,276` calls Ask without specifying retrieval mode. | Function default is legacy (`engine.py:367`), so strict mode does not follow the HTTP configured default. Legacy can stream before audit and degrade to answer-level citations (`claims.py:359-397`). Capture stores text only in note context (`respond.py:235`). | Analyst note `ledger_answer`, web Ledger and Telegram. |
| Positioning coach: `positioning_coach_turn` | ContextPack includes deterministic current book and owner positioning; uses Ask narrative seam (`src/positioning/coach_pack.py:212`). | HTTP endpoint omits retrieval_mode (`comments_server.py:3134`), so legacy default. Sealed path rejects this purpose. Proposal encoding schema validates expressed positioning dimensions, not independent issuer financial truth. | Ask turns scoped positioning, coach panel, positioning proposals after owner edits. |
| Standup composition/judge | Signals framed from deterministic decision conditions, DCF staleness, portfolio weights, journal, and prior conclusion as prior. Uses production Ask seam (`src/standup/compose.py:197`). | Omits retrieval mode -> legacy. A separate rubric judge gates composed text (`src/standup/gate.py:96-145`), but the judge receives answer/citation labels, not a guaranteed complete reconstructed scoped financial corpus. No universal exact financial claim replay. | Standup threads, peek/feed delivery, dedup ledger. Morning pipeline schedule. |
| Pre-earnings brief: `pre_earnings_brief` | Thesis/bear/IR anchors, admitted KPI references, DCF row, notes, tone, Ask retrieval (`src/earnings_brief.py:225-330`). Manifest honestly labels partial source identity (`:445`). | Nonempty and heading checks only (`:516-520`), followed by artifact upsert. Missing source/valuation blocks become text or empty; no shared missing-data intake approval or semantic output check. | `llm_artifacts` per earnings event; prep peek and baseline for readout. Hash cache/T-1 refresh window. Scheduled morning portfolio and enabled names. |
| Post-earnings readout: `post_earnings_readout` | Exact transcript quarter, source-identified blocks, verified pre-call artifact version, owner notes, current context explicitly separated, selected KPI inputs and consensus limitations (`src/earnings_readout.py:422-774`). | Nonempty and heading checks only (`:881-889`), then upsert. Complete input manifest helps reconstruction but does not prove every returned financial assertion. | `llm_artifacts` keyed transcript quarter; report/earnings readout peek. Portfolio scheduled; evaluation on request. |
| Transcript, press release, earnings deck, investor update, event summaries: `transcript_summary`, `press_release_summary`, `presentation_brief`, `event_brief` | Supplied source document text and optional owner/IR anchors (`src/llm_client.py:673,722,764,1397`). | Generator emits free-form string. No common verified fact lookup or semantic corpus check. `execution/process_ir_documents.py:180-193` dispatches via function-valued config and writes text directly; no ticker argument passed there, weakening call attribution. Merely existing cache path is considered successful (`:168`). | `.tmp/<ticker>_<quarter>_<year>_*summary.txt`, event brief caches, document processed marker. Report earnings, SayDo, KPI candidate extraction and later lenses consume these outputs. |
| SayDo pairwise: `pairwise_analysis` | Two generated summaries plus anchors; prompt prohibits inventing facts (`src/llm_client.py:464-543`). | No primary-source/verified-financial reconciliation of each generated pairwise claim. Governed synchronous batch writes and existing-file cache. Downstream commitment matching is distinct from auditing this text. | `.tmp` pairwise text, SayDo sections, commitment extraction. |
| Thesis tracker: `thesis_pass_a`, `thesis_pass_b` | Statistics/raw prepared context, summaries, thesis anchors; Pass B based on Pass A (`src/llm_client.py:1092-1167`). | Prompt says note Pass A figures believed wrong rather than asserting postchecked correctness. No shared per-claim corpus proof. Wrapper no current caller found in scanned roots except potential external invocation; keep retained public capability in scope. | Assembled Markdown tracker returned by wrapper, external/manual consumers need inventory confirmation. |
| Strategic analysis/filing intelligence: `strategic_analysis` | Caller-supplied contexts and filing excerpts (`src/llm_client.py:1255`, `execution/analyze_filing_intelligence.py:255`). | Structured schema or text format checks. No universal canonical financial fact preflight or postcheck. | Filing intelligence/research outputs; generic strategic wrapper retained but current direct production caller not found. |
| Bear case: `bear_case` | Financial/segment section values serialized into lossy display tables, earnings summaries, accepted thesis/break conditions, signals (`src/report/sections/bear_case.py:116,448`). The delivered remediation preserves typed canonical projection and source/unit context. | Structured schema; no per-clause financial check. Seven-day native JSON cache is read before new evidence assembly (`:73-78`) and written after parsed response (`:239`). Input changes are not shown to be reverified by this TTL cache alone. | `data/bear_case/<ticker>.json`, report bear-case section, owner anchors and other prompts. |
| Recent developments/news: `recent_developments`, `news_structuring` | Web source evidence plus anchors, no pre-read of scoped verified issuer facts (`src/llm_client.py:1529,1640`). | Web grounding evidence presence and JSON shape. No admitted fact identity or exact output claim support checks. Report cache TTL only (`src/report/sections/recent_developments.py:132-177`). | `.tmp/news_cache`, report news narrative, `news` table structured feed. |
| Company description/platform diagram: `company_description`, `platform_diagram` | 10-K/source text/profile plus optional IR narrative (`src/compute/company_description.py:401`, `src/compute/platform_diagram.py:256`). | Structured shapes/cache fields; no general audited financial claim package for free-form description. Diagram structural validation is a different oracle. | Native description/diagram cache, `llm_artifacts` projection, report company tab. |
| Report comments/questions and edits: reused `company_description`, `intake_classifier` | Comment ask route supplies thesis/bear/IR only (`execution/process_report_comments.py:1680-1706`). Other edit/coherence routes supply relevant owner fields. | Direct free-form Q&A return, no canonical fact fetch or corpus audit. Reusing description purpose obscures distinct Q&A purpose. Owner edit extraction can be valid without verified facts, but its authored factual additions still need explicit disposition. | Follow-up thread, holdings JSON for applied edits, refreshed report. |
| Investment Decision Card: `investment_decision_card` | Deterministic readiness, typed price/security data, owner hypothesis, report corpus and allowed source references (`src/research/investment_decision_card.py`). | Schema, deterministic readiness/price override, two bounded generation attempts, mechanical fallback. `validate_grounding` checks nonempty/distinct sections and reference membership, including substring acceptance (`:198-234`); it does not require full clause coverage or prove stated values/semantic context. | `llm_artifacts`, report/pipeline card, inert research tasks for gaps. |
| Manual full decision brief | Scope-aware verification snapshot and report financial table, exact claims/quotations/derivations supplied by author (`src/research/decision_brief.py`). | Strong specialized deterministic verifier; degraded receipts for missing evidence. Workflow can block onboarding/valuation source closure. Not all standard report sections are tied into this verifier; author/model passage classification remains load bearing. | Saved immutable report artifacts and memo evidence receipts. |
| Generic synthesis lenses: twelve `lens:<name>` purposes | Per-lens context; many summarize prior generated summaries, DCF, owner text, predictions, footnote/customer tables. `underweighted_facts` uses raw financial_facts snapshot dropping unit/currency/scope/admission (`_shared.py:477-499`, `underweighted_facts.py:56-76`). | Shared nonempty/section check then optional narrow DCF/MoS regex drift warning; contradictions are preserved and persisted with footnote (`_shared.py:260-276`). No full financial corpus check. Most source/parent ID lists are empty even when context uses source-derived summaries. | `llm_artifacts`, synthesis report sections and cockpit. Daily/on-demand, weekly lens/synthesis schedules. |
| Macro scenario and portfolio macro stress lenses | Deterministic scenario and portfolio exposures, DCF and thesis inputs, text context (`src/synthesis/lenses/macro_scenario.py`, `portfolio_macro_stress.py`). | Dedicated writers bypass generic lens numeric check; no per-claim financial audit. Source/parent references initialized empty. | `llm_artifacts`, macro/scenario report panels. |
| Next dollar/swap memos: `advisor_next_dollar`, `advisor_swap_check` | Deterministic book/sizing context, valuation readiness through `load_valuation_readiness`, wealth/tax/owner constraints (`src/advisor/context.py:169`, `src/advisor/memos.py`). | Next-dollar rejects if model rows not ready; then plain text/nonempty only (`memos.py:337-376`), swap likewise (`:489`). No exact claim corpus audit before persistence. | `advisor_memos`, analyst notes, decision ledger where scoped; advisor UI and monthly task. |
| Socratic questions and decision memo: `advisor_socratic_questions`, `advisor_socratic_memo` | Refetched advisor/holding/owner answers context and optionally eval-gated pre-mortem (`src/advisor/socratic.py:299-381`). | Questions parse and memo stance enum/fallback; no financial prose reconciliation. Owner answers are evidence of owner beliefs, not issuer facts. | Prelude artifact, memo/note/ledger, Socratic UI. |
| Position review: `position_review` | Deterministic pre-analysis and valuation readiness, convictions, behavioral scored record, owner profile (`src/advisor/position_review.py:1700-1766`). | Pydantic verdict and deterministic behavioral guard; not a financial claim corpus audit. | Persisted review memo and advisor panel. |
| DCF assumptions and scenario weights: `dcf_assumptions`, `scenario_prior`, `valuation_basis` | DCF assumptions command requires available actuals; canonical model inputs where available. Scenario prior mainly thesis/bear/KPI anchors (`src/dcf/scenario_prior.py:168`). Valuation-basis picker source/business context. | Typed ranges/segment key guards, simplex and owner override, multiple choices. These constrain model inputs but do not establish evidence-supported forecast rationale. Forecasts must remain proposals, distinct from reported facts and sourced consensus. | `data/dcf_assumptions`, scenario priors, valuation basis, DCF calculation inputs and views. |
| ETF role synthesis: `etf_role_synthesis` | Deterministic ETF workup, fit, lookthrough, what-if and positioning payload (`src/etf_role_synthesis.py:172`). | Schema and required role/verdict only, then hash-keyed artifact (`:179-203`). No semantic claim audit of rationale or suggested weight. | `llm_artifacts`, ETF workup/role panel. |
| Whole-book thesis collision and business factor taxonomy: `thesis_collision`, `business_factor_taxonomy` | Owner thesis/break rules and mix/taxonomy deterministic payloads. | Known ticker and field checks, factor enum/weights, owner edits win. Rationale not independently corpus checked. Collision parse failure becomes empty report (`src/thesis_collision.py:405-421`), which must not read as proven no collisions. | `llm_artifacts`, factor exposures, portfolio risk panels. Weekly tasks/input hashes. |
| Red-team attacks: `red_team_attack`, `red_team_cross_book` | Per-name/cross-book deterministic evidence packs; prompts require checkable attacks (`src/redteam/lenses.py:345-357`, `cross_book.py:74`). | Structured attack fields, known names; no universal clause/financial identity proof. Hypotheses must remain hypotheses. | Red-team rows, research cards, owner reply/decision follow-up. Monthly task. |
| Pressure test: `pressure_test_thesis` | Rendered financial/risk/ratio/summary corpus (`execution/pressure_test_thesis.py:300`). | Structured output only (`:326`), prompt evidence requirements. No automated source/context reconciliation of each counterclaim. | Dated pressure-test `.tmp` JSON and thesis-review outputs. |
| Exec compensation alignment: `exec_comp_alignment` | Package/insider rows and thesis KPI names (`src/report/sections/exec_compensation.py:385-426`). | Plain narrative directly persisted after call (`:431-448`). Provenance of numeric input packages does not prove wording/status or output factual support. | `llm_artifacts`, executive compensation report section. |
| Trigger rationales: `kpi_inflection_context`, `saydo_due_context`, `earnings_tone_diff`, `material_news_classification` | Deterministic trigger candidate, latest/prior KPI or commitment inputs, transcript/owner/source article context. | Closed alert/triage shapes and caching, but narrative rationales still may repeat unsupported figures. KPI/saydo plain strings saved in artifacts after call (`src/triggers/kpi_inflection.py:839`, `saydo_due.py:716`). | Alerts and caches, morning pipeline peeks and notifications. |
| Disclosure materiality: `disclosure_thesis_materiality` | Exact accession event/source text and accepted thesis/break rule context (`src/filings/materiality_judgment.py:318`). | Typed event/ref disposition and bounded recovery; narrow classification not independent financial corroboration. Preserve management disclosure vs factual validation. | Materiality receipts, disclosure alerts and archive. Weekly changed accession sweep. |
| Transcript Q&A tone/topic judgements: `transcript_qa_judgment`, `transcript_topic_triage` | Exact Q&A speaker/exchange snapshots; topic names only for triage (`src/transcripts/transcript_judgment.py:202,319`). | Schema, degraded result on failure, explicit truncation; no generated neutral result. This is sentiment/topic classification, not a verified financial fact authority. | Tone artifacts/topic statuses, transcript/earnings surfaces. |
| Saved article/deck brief and research loop: `artifact_brief`, `research_fetch`, `research_adversarial_assess`, `research_narrate`, `thesis_entry_draft` | Article text or bounded web search evidence; owner identified ticker context (`src/research/brief.py:80`, `src/research/run.py:146-219`). | Schemas and adversarial assessment; no existing verified fact preflight. External article can be summarized as article claims, but issuer financial assertions must reconcile to available admitted facts before conclusions persist. | Note engage_brief, research proposals, thesis drafts, web feed and Telegram. |
| Annual letter/exit postmortem: `annual_letter`, `exit_postmortem_draft` | Scored decisions/calibration, positions, owner beliefs and archived thesis/model context (`execution/draft_annual_letter.py:185-228`, `src/synthesis/exit_postmortem.py:395`). | Schema/valid references where assigned, no general financial claim corpus check. Owner history and scored calculations are distinct truth domains and need source-kind tags. | Saved draft letter and postmortem/insight artifacts, journal/review UI. |
| Theme, Tenet, session and behavior synthesis: `theme_synthesis`, `tenet_distill`, `session_distill`, `tenet_accountability`, `tenet_semantic_tension`, `behavior_distill`, `calibration_coach` | Owner notes/conversations and scored record; specific citation/ID validators exist. | These can correctly quote the owner yet wrongly restate an issuer fact from the conversation. ID membership proves conversation attribution, not company financial truth. Exclude pure belief paraphrase from issuer numeric gate; require lookup if adopting it as fact. | Insights, proposed Tenets, bias coaching/experiments/pre-mortems, journal and Worldview. |
| Suggesters/pickers: `peer_selection`, `key_metrics`, `sector_benchmark_proposal`, `saydo_importance`, `weekly_packet_predraft` | Actual available token/menu/evidence inventory, business text and owner packet. | Closed menu and ID membership validation guards returned selections. Free-form reasons can still contain financial claims. Preserve proposal status; do not let generated relevance prose become factual authority. | Cached picker selections, suggested peer set, approval packet drafts. |

## Explicit exclusions and conditional boundaries

- **Source extraction is not downstream synthesis.** Company/deck/segment/KPI/footnote/customer/commitment extraction must read primary bytes even before facts exist. Treat model outputs as extraction candidates. Schema/grounding/extraction checks, semantic admission and completeness govern promotion. Requiring pre-existing canonical facts for the extraction itself would deadlock onboarding. The audit includes those 25 sites but they need a different policy from report prose.
- **Closed routing and owner instruction extraction** need shape/allowed-target/authorization validation, not mandatory issuer factual corpus loading. They must not grant acquisition scope or turn owner text into verified financial evidence. Free-form rationales inside these outputs remain conditionally financial.
- **Model/provider/eval/prompt operations** can contain financial sample text without being end-user financial narratives. They require quality governance and purpose independence; policy instrumentation must preserve exact replay/prompt attribution.
- **README drafting and inert code spec** are nonfinancial. They need their existing repository evidence/security boundaries. Do not add a financial corpus dependency.
- **Macro/market/portfolio/account/tax/owner inputs** are not issuer reported financial facts. Retain their own source kinds, timestamps and user authority. The shared financial narrative contract must carry these typed sources rather than falsely applying company SEC admission semantics.

## Prioritized gaps

1. **P1 — no universal financial narrative contract.** Current canonical LLM directive/facade governs transport and shape but not scoped verified inputs or completed corpus reconciliation. New narrative callers can bypass everything by supplying a plain string to an existing purpose. Remedy: closed purpose classification; typed evidence scope and input manifest; shared post-generation acceptance proof bound to output and snapshot; no durable/public current financial narrative without proof or an explicitly authorized bounded/degraded mode.
2. **P1 — chat/standup policy differs by caller.** Environment default grounded does not alter `respond_turn(..., retrieval_mode='legacy')` default. Four product call paths omit it (two Ledger, standup, positioning) and one eval replay also omits it. Remedy: eliminate implicit legacy on product callers and give every ContextPack a compatible evidence contract. Preserve grounded Ask strengths and do not force sealed portfolio-only mode onto incompatible packs without implementation.
3. **P1 — correct source values lose meaning before generation.** Underweighted lens raw query drops admission, unit, source, period start/type, accounting basis and scope. It then calls values 'facts' and demands exactly five even with insufficient evidence. Remedy: serialize the existing canonical projection and permit fewer/no supported facts; preserve management vs analyst vs consensus categories.
4. **P1 — detected contradictions are still durable.** Generic lens check only detects explicit DCF/fair-value/MoS patterns, tolerates 15% NPV deviation and five percentage points of MoS, and appends a warning (`src/synthesis/grounded_numbers.py:125-153`). Remedy: deterministic typed claim support/calculation checks and exact scope/context checks before saving; block or bounded repair unsupported assertions; do not let a warning certify a contradictory passage.
5. **P1 — ordinary generated briefs/readouts/cards/memos are not semantically postchecked.** Heading/schema/readiness/reference membership checks can all pass 'correct number, wrong metric/unit/period/forecast status'. Remedy: shared exact financial claim inventory, deterministic replay for numbers/calculations and exact quotation checks, then qualified semantic support audit over selected primary evidence and available conflicting corpus. Reuse sealed Ask and decision-brief authorities; do not build parallel unsupported source registries.
6. **P1 — no common early collection-scope approval.** Standard report generators either run on partial context, return missing, or start web research. Existing bounded company workflow has many source controls, but arbitrary chat/research/memos do not share request-specific approved period/metric/source scope. Remedy: deterministic preflight; when unavailable, expose missing facts, minimal source/period plan, effort and expected output status, and obtain the requested scope decision before acquisition. If human already bounded/authorized this exact research scope, respect that authority rather than asking again.
7. **P2 — caches can outlive grounding.** TTL/native existence caches and many source-less artifact contexts preserve old prose without a current admission/postcheck receipt. Remedy: cache acceptance must bind evidence revisions/cutoffs, selected scope, policy/validator versions and output hash. Retain historic outputs honestly; do not silently label old cached prose verified/current. Verification should not require another expensive model call when the same evidence/output proof still replays.
8. **P2 — purpose reuse hides distinct financial Q&A.** Report-comment Q&A uses `company_description` and anchors only; several editing purposes share that pin too. Remedy: explicit operation type or dedicated governed purpose with representative evals and the shared financial request contract.
9. **P2 — generated summaries become later evidence.** Summaries feed SayDo/lenses/KPI candidate extraction, sometimes with empty source/parent references and existing-file cache semantics. Remedy: parent-chain manifest to exact primary bytes/locators plus producer/audit identity; generated text stays prior interpretation and cannot alone certify a reported number.

## Coherent acceptance criteria

Every user-visible financial narrative operation should declare its evidence mode, named issuer(s), period window, actual/forecast/consensus categories, metric/claim needs, permitted acquisition sources, and explicit owner scope decision when needed. A bounded one-year ad hoc review is valid when the owner chooses it; it must not impersonate comprehensive decision-grade coverage.

Before synthesis, select the available admitted financial facts through existing resolvers and retain their complete identity. Select source-node passages separately for management statements and qualitative claims. Use typed model/portfolio/owner sources for calculations and nonissuer inputs. Never substitute generated prior prose or generic search for an available verified issuer financial observation.

After synthesis, inventory every substantive financial claim. Reconstruct exact reported values and source quotations, replay calculations, check metric/unit/period/scope/basis/forecast status, and test semantic support and conflicts against the authorized scoped corpus. Retain the outcome and exact output hash. Unsupported statements must not be delivered/saved as accepted narrative; a clearly labelled research hypothesis can survive only in the approved hypothesis role.

Required regression cases: CFO vs FCF; positive source outflow vs normalized signed cashflow; quarter vs YTD vs LTM; millions vs full currency units; carrying amount vs face/total debt; forecast/pro-forma vs measured actual; 10% customer disclosure vs fabricated 5% absence; corrected/restated vs prior observation; cross-issuer source mismatch; source-derived unknown/empty tables; missing structured source identity; old cache after semantic admission change; owner assertion/LLM summary as reported fact; transcript management promise vs reported financial fact; partial bounded scope vs full decision-grade label; Ask/standup/Ledger/coach/report-comment paths with the same seeded evidence and unsupported output.

The deterministic controls can reject known semantic substitutions without live LLM calls. Free-form semantic audit still needs representative independent evaluations, including unsupported-but-plausible numerical prose. A green format/type/unit suite alone cannot prove a model will never make a factual mistake.


## Detailed baseline chat evidence

## Complete scoped path inventory

| Surface / entry | Selection and input | Gate and delivery | Missing-data / authority behavior |
|---|---|---|---|
| Work OS Copilot / Ask: `execution/comments_server.py:3035` | Typed session/research context, canonical fact/source handles; builds portfolio pack and explicitly passes `ask_retrieval_mode()` at `:3082`. Copilot sends durable request ID, revision and context at `src/pipeline/work_os_copilot.py:1284`. | Default mode is grounded (`src/ask/engine.py:711`). Strict narrative route described below. Durable exchange additionally holds final and traced answer/fragment/citation events until assistant/artifact transaction commits (`src/ask/exchange_store.py:738`). UI renders every received delta immediately (`src/pipeline/work_os_copilot.py:1140`). | Empty/invalid requests reject. Strict no evidence produces exact refusal before answer LLM. Failed retrieval/audit/binding produces error; no strict answer release. Actual live mode was not inspected. |
| Grounded narrative: `src/ask/engine.py:1386` | One strict lexical/SQL retrieval; financial series uses shared `read_financial_consumer_series` (`src/ask/grounding.py:785`). KPI queries require semantic admission/identity (`:927`). Filing/transcript passages and portfolio packs also enter. Immutable source/context trace (`src/ask/grounding_trace.py:65`). | Buffers all transport output. `build_citations_payload(strict=True)` requires exact clause population and per-clause visible supported citations (`src/ask/claims.py:425`). Persists assistant before delivery when engine owns session. No complete-corpus seal or deterministic financial-semantic gate. | No evidence -> exact refusal (`src/ask/engine.py:1562`). Retrieval, trace, audit and binding errors fail closed. Unavailable financial series becomes textual unavailable evidence (`src/ask/grounding.py:793`). Other facts/passages may still answer. |
| Sealed narrative: `src/ask/engine.py:995` | Requires portfolio session, explicit production ticker scope, current promoted scopes/cutover/readiness, exact Research Snapshot and verified heterogeneous traces (`:1038`; `src/ask/sealed_retrieval.py:1300`). | Buffered `call_llm`, schema auditor, exact span/citation-domain coverage (`src/ask/engine.py:873`, `:920`), call identities, prompt reconstruction, immutable answer audit (`src/ask/audit_store.py:739`, `:988`), assistant binding, then delta/citations/final (`src/ask/engine.py:1253`). Audit's supported flag is an LLM verdict, not deterministic source entailment. | Missing session, wrong purpose, slash commands, missing readiness/evidence or failed verification reject. No fallback to legacy (`src/ask/engine.py:377`, `:1263`). Durable exchange integration does not persist sealed answer ID/claim audit as a typed exchange field; sealed global audit exists separately. |
| Legacy narrative: `src/ask/engine.py:1468` | Same best-effort SQL/lexical/packs retrieval plus portfolio context and history; missing retrieval may return empty list. Non-strict NEED follow-up can request up to two bounded rounds (`src/ask/followup.py:200`). | Usually streams immediately (`src/ask/engine.py:628`, `:1603`). Citation map is advisory and may degrade; final may precede persistence (`:1681`, `:1690`). NEED final is buffered per follow-up call, but still has non-strict citation gate. | No evidence still calls answer model. Budget/parse/provider failures in claim extraction can yield answer-level/no citations. Transport errors surface. |
| Shadow narrative: `src/ask/engine.py:949`, `:1004` | Executes verified sealed retrieval as diagnostic shadow, then uses exact legacy prompt/answer path. | Legacy delivery guarantees only. Test explicitly preserves legacy events (`tests/test_ask_sealed_mode.py:24`). | Shadow readiness/retrieval failure logs and preserves legacy path. It is not a sealed answer. |
| Data / `/view` / NL compile: `src/ask/engine.py:491`; `execution/comments_server.py:2995` | LLM chooses typed ViewSpec only; deterministic `execute_view` renders values (`src/viewspec/engine.py`). Grounded Ask persists exact SQL-view source trace (`src/ask/grounding_trace.py:87`). | No LLM-written numerical prose on successful view route: deterministic summary/fragment. Sources and unit retained in view rows. Grounded trace/binding required before delivery. | Forced `/view` fails instead of prose fallback. Ordinary compile/execute failure falls back to grounded narrative only when caller supplied grounded mode; legacy/shadow caller falls back legacy (`src/ask/engine.py:514`, `:540`). Zero rows visible. Detail/segment domains have explicitly degraded legacy admission semantics; not all ViewSpec domains are canonical financial facts. |
| Slash commands: `src/ask/engine.py:441`, `src/ask/commands.py:46` | `/help`, `/discovery`, `/review`. `/review` first returns deterministic position facts via `review_reply_text` (`src/advisor/position_review.py:2041`) and may start separately billed full review (`src/ask/commands.py:65`). | Immediate deterministic reply, no claim auditor. Full position review is separate memo flow below. | Command surface requires job registry; disabled full verdict stays instant-only. Commands can start jobs; they are not model tool calls. Sealed mode disallows them. |
| Report chat entry: `src/report/renderers/workspace_chat.py:36` | Handoff ticker/report date and optional governed fact reference to Copilot; standalone same-origin Work OS link. | No second answer generator or report-owned session. `/chat/<ticker>` and `/chat/<ticker>/apply` return 410 (`execution/comments_server.py:5295`). | Legacy history not imported. Test checks physical removal of `chat_session.py`, ticker packs and JSON chat storage (`tests/test_legacy_chat_session_removed.py:10`). |
| Positioning coach: `execution/comments_server.py:3116`, `:3134` | `build_positioning_pack`: active profile, tracker/book/risk context and scope positioning (`src/positioning/coach_pack.py`). Calls engine without mode -> legacy. | Folded JSON hides intermediate streaming, but no strict claim gate. Coaching context/prompt asks for grounded numbers; this is not validation. | Offline book is visible. Propose encodes typed profile and owner-edited approval is the write seam (`src/positioning/encode.py:147`; `execution/comments_server.py:3171`). No deterministic proof that every encoded target came from an owner utterance. |
| Ledger captured question answer: `src/onmymind/respond.py:180`, `:224`; `execution/comments_server.py:1251` | Triage first: plain capture skips, contradiction generates deterministic challenge, answer invokes portfolio engine in legacy mode. | Folded answer saved as `{text,status}` under note context (`:234`); citations/trace discarded. Poll route returns stored text (`execution/comments_server.py:1324`). | Disabled answers, ambiguous ticker, errors/empty answer clear pending or mark failure. No strict source/no-data guarantee. |
| Ledger follow-up answer / Telegram card question: `src/onmymind/respond.py:253`, `:276` | Arbitrary text and ticker list through same legacy engine. Web reply classifier routes question to Ask client (`execution/comments_server.py:1302`); `answer_text` is separate inline answer function for other consumers. | Returns plain answer text, drops folded citation/context fields. Reply intent parse errors fall back to question, never action (`src/onmymind/reply.py:119`). | Exceptions return None. No exact-source audit for returned text. |
| Proactive standup: `src/standup/compose.py:185`, `:197` | Frames deterministic signal question then same portfolio engine, implicitly legacy. Keeps citation chip payload. | Additional rubric Judge threshold before delivery (`src/standup/gate.py:100`; `src/standup/run.py:254`). Judge receives answer + citation labels/confidence, not cited source passages/full source manifest (`src/standup/gate.py:57`). Thus its grounding grade cannot independently reconstruct financial claims. | Missing answer/errors skip; Judge failures retry. Advisory score is not deterministic verification. External delivery belongs to caller authorization; no delivery performed here. |
| Advisor next-dollar / swap-check: `src/advisor/memos.py:313`, `:469`; action `execution/comments_server.py:4844` | Context reads configured DB via `require_db_path`, current verdicts/DCF valuations, live holdings/analytics, conviction and thesis (`src/advisor/context.py:260`). Model eligibility uses valuation evidence-ready, freshness/breach/stub/outlier screens (`:133`, `:393`). Input prose rounds quantities; context does not retain complete canonical financial claim manifest. | Raw call then nonempty check, preamble/title stripping and memo/note/ledger persistence (`src/advisor/memos.py:203`). No claim support or financial-number gate. | Next-dollar no eligible rows -> valuation evidence not ready, no call. Transient call errors skip, hard stops propagate. Observed path mismatch: memo persistence still constructs `repo_root/data/portfolio.db` (`:324`) while context resolves configured DB; no DB touched in audit. |
| Socratic questions/prelude: `src/advisor/socratic.py:292`; action `execution/comments_server.py:4970` | Advisor context + cached bear anchors + best-effort eval-gated premortem. | Parser requires 3–5 questions; persists prelude (`src/advisor/socratic.py:204`, `:234`). No factual claim validation. Questions can themselves contain incorrect premises. | Premortem failure degrades except hard stop; unparseable questions fail. Cached current prelude read at `:267`. |
| Socratic decision memo: `src/advisor/socratic.py:342`; `execution/comments_server.py:5026` | Fresh advisor context, cached anchors, owner-provided questions/answers, horizon. | Length/nonempty/horizon checks, LLM raw body, stance parser, same memo persistence. Missing stance only logs; body still persists (`:391`–`:405`). | Transient call skip/hard stop propagate. Questions supplied by client are not required to equal persisted prelude. No source/fact gate. Persistence constructs checkout DB path (`:406`). |
| Full calibrated position review: `src/advisor/position_review.py:1682`; `execution/comments_server.py:4882` | Deterministic PreAnalysis with framework/value/weight/tax/risk/capacity. Model response `_VerdictWire` strings + choices. | Schema parse and deterministic behavioral guard (`:1755`–`:1767`). Deterministic fact block accompanies model narrative, but wrong/contradictory numerical rationale is not rejected before memo persist (`:1525`, `:1636`). | No framework -> encode-first deterministic reply; guard can override unsupported price-only trim. These are action guards, not source entailment. |
| Ask session restore / cached completed exchange: `execution/comments_server.py:3361`; `src/ask/exchange_store.py:854` | Stored text/citations/trace; replay checks turn/exchange IDs and existing trace row, reconstructs UI events without new work. | Restores historical answer; no rerun of corpus, source semantic or current verifier. No typed qualification field in generic turn distinguishes legacy, strict grounded and sealed answers. | Missing trace/binding disagreement fails. Session GET can return empty turns/artifacts on read errors. Copilot labels completion generically “Grounded response complete” (`src/pipeline/work_os_copilot.py:1190`) even when active mode is legacy/shadow. |
| Session distillation: `execution/comments_server.py:3421`; `src/synthesis/session_distill.py:279` | User and assistant turn text, current beliefs/notes. Structured candidates reference actual shown turn tokens. | Valid token existence gate (`:365`), valid scope/revision/note IDs; adopts belief updates per owner rule. Does not compare candidate content to cited turn meaning or underlying financial source. Assistant hallucination can become cited durable memory. | Invalid reference skips; transient defer/hard stop propagate; repeat completed distill returns conflict. This governs owner beliefs, not financial semantic admission. |

## Retrieval, tool, cache and verification distinctions

- Default grounded retrieval is not `retrieve_grounded_ask`. `src/ask/grounded_retrieval.py:68` provides a sealed corpus helper but no production caller imports it. Actual default engine uses `gather_evidence(strict=True)` (`src/ask/engine.py:1402`). Do not infer sealed completeness from the word grounded.
- File evidence is keyword-selected filing sections/transcript lines (`src/ask/grounding.py:1288`, `:1440`). Strict query error handling does not certify whole package or corpus completeness. Source-complete projection/snapshot checks belong to sealed route.
- The model cannot invoke file, shell, MCP or plugin tools through Ask: `allow_read` ignored (`src/ask/narrative_transport.py:28`), canonical Claude streaming uses `--safe-mode --tools ""` and neutral cwd (`src/llm/cli.py:2077`). Buffered provider fallback uses canonical call seam (`:2056`). Prompt-based evidence NEED is schema-bounded deterministic retrieval, not provider tools. No provider tool call occurred in this audit.
- Pack routing can fail/budget-skip to empty packs (`src/ask/router.py:87`); pack SQL helpers tolerate missing schema with empty rows (`src/ask/packs.py:234`). Offline tracker text is explicit (`:224`), but default strict mode does not transform all pack errors into typed completeness failures.
- Fact/evidence cache is bounded and short-lived (60 seconds gather; `src/ask/turn_cache.py:141`). Key includes evidence DB revision (`:251`), scope, normalized question, session. Parsed files use path/mtime/size (`:155`). Reused evidence retains its original cutoff (`src/ask/grounding.py:1508`). This is retrieval cache, not verified-answer promotion.
- Grounded trace records manifest and text digest, not complete answer+exact rendered prompt+auditor record (`src/ask/grounding_trace.py:65`). Sealed answer audit has stronger reconstruction/immutability (`src/ask/audit_store.py:988`). Neither numeric/citation identity alone proves arbitrary financial paraphrase semantics.
- Copilot diff output is a proposal. Typed durable proposal validation and explicit owner decision own writes (`src/research/proposal_approval.py:273`, `:349`). It does not confer verified financial status on proposal prose.

## Tests/evals inspected, not run

- `tests/test_ask_grounded_default.py:38`, `:174`, `:226`, `:294`: no evidence/no call, traced retrieval failure, audit failure/no leaked answer, binding failure/no answer.
- `tests/test_ask_claims.py:262` onward: empty audit, omitted/unsupported clauses, partial compound spans, invalid/unaudited visible cites. Structural tests mock auditor output; they do not prove financial entailment.
- `tests/test_ask_sealed_mode.py:74`, `:106`, `:179`: session/readiness/policy pre-call gates, omission/citation-domain rejection. `tests/test_ask_sealed_answer_audit.py:408` onward: exact append-only identities, source substitution, incomplete support, call identity and span verification.
- `tests/test_ask_external_exchange_integration.py:104`, `:206`, `:340`: atomic final/artifacts, traced no-leak failure and fact refs; `tests/test_claude_tool_isolation.py:144` ensures tools cannot be enabled by override.
- `evals/golden/ask_claim_grounding.json`: adversarial wrong value/metric/entity/ticker/period and unsupported claims. `src/evals/ask_citations.py:294`, `:515` scores map accuracy or answer generation. These are representative probabilistic tests, not a deterministic runtime semantic validator.
- `evals/golden/ask_claim_audit.json` has only two live model eval cases (supported growth and injected cite-domain instruction), plus structural span/verdict cases. It has no focused million-vs-billion, actual-vs-forecast, scope/basis contradiction or correct-number/wrong-metric cases.
- Positioning/advisor/Socratic tests validate shapes, persistence, eligibility and behavior, not complete financial claim semantics (`tests/test_positioning_coach.py`, `tests/test_advisor_memos.py:594`, `tests/test_socratic.py:295`). Distill tests reject nonexistent citations but permit content with any valid turn token (`tests/test_session_distill.py:199`).

## Smallest prevention change

1. Preserve the existing financial resolver result as a typed prompt entry: immutable fact/reference ID, metric and definition, value/currency/unit/scale, period start/end/fiscal role, reporting entity, basis/scope and reported/forecast status. Render these fields for both grounded and sealed routes; do not reconstruct meaning from a prose label.
2. Use a structured financial-assertion block referencing those exact IDs. Deterministic code renders the factual quantity and qualifying wording; model prose supplies interpretation. Reject unknown IDs and mismatched metric/period/denomination/basis/scope/status before assistant or artifact promotion. Keep general source-backed narrative auditor for claims that require judgment; explicitly preserve its limited guarantee.
3. Remove caller-dependent silent legacy mode. Require an explicit mode for every production caller, then choose the appropriate safe contract for coaching, Ledger and standup without forcing source-complete research scopes onto owner-life discussion. Financial factual claims should use the same assertion gate regardless of surface.
4. Preserve verification mode, source/audit IDs and cutoff in stored turns, Ledger answers, memo records and restores. Label historical/advisory answers accurately. Distillation should distinguish owner utterances from assistant claims and retain underlying evidence for financial content.
5. Add isolated negative fixtures: correct number/wrong metric; million/billion; currency; H1/quarter; carve-out/consolidated; adjusted/GAAP; forecast/actual; contradictory repeated number; false absence claim; malicious auditor `supported=true`; and caller mode omission. Test rejection before any delta/persistence. Do not claim these pass until run.

No new release machinery, generic verifier framework, or parallel host ownership is needed. Extend the existing resolver, Ask gates, artifact state and deterministic rendering seams.
