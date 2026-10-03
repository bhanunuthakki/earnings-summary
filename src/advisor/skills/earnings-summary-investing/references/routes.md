# Project research routes

Paths below are relative to the earnings-summary repository. Resolve the installed
skill's real path first. Read the closest canonical owner and the route's current
arguments before execution. CLI database defaults are not production authority.

## Source and policy owners

| Concern | Current authority |
| --- | --- |
| Policy classification | `directives/directive_manifest.json` |
| Artifact location and lifecycle | `directives/folder_structure.md` |
| Live host and database access | `directives/agent_host_operations.md`; global machine operations |
| Acquisition, pipeline identity and resumption | `directives/data_pipeline_dag.md` |
| Document/fact lineage and decision-grade gate | `directives/data_provenance.md`; `DEFINITIONS.md` |
| Thesis schema and approved thresholds | `directives/holdings_json_schema.md`; `micro_thesis/holdings/<TICKER>.json` |
| Operator actions and status | `directives/operations_governance_surface.md` |
| Readout schema | `directives/post_earnings_readout.md` (runbook) |
| DCF and model update mechanics | `directives/quarterly_refresh.md`; `directives/dcf_gsheets_setup.md` (runbooks) |
| ETF evidence | `directives/etf_data.md` |
| Portfolio allocation factors | `directives/next_dollar_model.md` |
| LLM execution and quota | `directives/llm_calls.md`; `directives/llm_quota_scheduling.md`; global LLM ops |
| Implementation traps | `directives/agent_implementation_traps.md` (runbook) |

`directives/micro_thesis_skill.md` and `directives/micro_thesis_runbook.md` provide
task mechanics. Their static roster or examples do not replace canonical live
membership. `directives/ir_events_ingestion.md` is a draft, not a live event policy.

## Routing matrix

“Read” means no deliberate durable write. Generation, refresh, ingestion and
disposition routes can write state, files or invoke paid model calls. A mutation
route is not authority to run it on production.

| Task | Read or inspect first | Authorized generation or mutation route |
| --- | --- | --- |
| Portfolio/evaluation roster and evaluation level | `GET /api/work-os/portfolio`; `GET /api/work-os/evaluation`; `src/pipeline/work_os_evaluation.py` | Onboarding: `execution/onboard_ticker.py`; use current owner-approved scope |
| Company research / initiating coverage / memo | Existing `output/research/<TICKER>/`; `src/report/builder.py`; `src/report/sections/` | `execution/build_artifacts.py --db-path <explicit> --ticker <TICKER>`; `src/report/renderers/workspace_html.py` |
| Pre-earnings preview | Current roster eligibility, event date, exact fiscal identity and current brief | `execution/generate_pre_earnings_briefs.py --db-path <explicit> --ticker <TICKER> --as-of <date>`; implementation `src/earnings_brief.py` |
| Post-earnings readout | Selected transcript's fiscal period and complete package | `execution/generate_post_earnings_readouts.py --db-path <explicit> --ticker <TICKER>`; `src/earnings_readout.py`; evaluation names: `POST /api/earnings-readout/generate` |
| Thesis monitoring / threshold check | Holdings JSON; `src/compute/thesis_evaluator.py`; `src/report/sections/thesis.py` | The evaluator CLI writes run accounting even with `--dry-run`; use only when that write is authorized. For observation-only assessment, use the no-write evaluator function with an explicit read-only connection and canonical holdings directory |
| Counter-case / prove-or-kill research | Approved thesis and annual filings, risk factors and transcripts | Source-backed analyst counter-case through admitted readers. Legacy `execution/pressure_test_thesis.py` is unavailable for this skill: it forces a checkout-local DB, reads legacy facts and writes diligence |
| Thesis revision from notes / analysis / article | Approved narrative and rule versions; supplied claims; [revision path](analysis-paths.md#thesis-revision-from-notes-or-an-article) | Prepare a cited before/after proposal. Governed Ask diff and decision: `src/research/proposal_approval.py`; ledger drafts: `src/research/thesis_artifact.py`; explicit amendment approval remains required |
| Valuation / comps / scenarios / model update | `execution/valuation_preflight.py --db-path <explicit> --ticker <TICKER>`; `src/dcf/readiness.py`; current `dcf/` workbook | `execution/refresh_dcf.py`; `execution/dcf_sheets.py`; preserve owner assumptions and applicable business-model route |
| Investment decision card | `src/research/investment_decision_card.py`; current artifact and input hash | `execution/build_investment_decision_card.py --db-path <explicit> --ticker <TICKER>`; `POST /api/research/card/<ticker>/refresh`; disposition is a separate owner action |
| ETF diligence / exposures | Existing ETF workup; `src/etf_sources/`; ETF-specific source coverage | `execution/build_etf_workup.py --db-path <explicit> --ticker <TICKER>`; `execution/fetch_etf_data.py`; `execution/fetch_etf_published_data.py` |
| Portfolio risk / concentration / overlap | `execution/get_portfolio_risk_matrix.py`; `src/allocation/book_risk.py`; `src/allocation/what_if.py`; snapshot as-of and coverage; full holdings/account readers in the allocation skill | `execution/refresh_portfolio_risk_snapshot.py` for authorized snapshot refresh |
| Next-dollar allocation / taxes / sizing | `src/advisor/skills/next-dollar-allocation/SKILL.md`; its `references/interfaces.md` | Follow that skill; use full holdings/accounts, owner context and recorded intent; no trade execution |
| Research conversation / meeting preparation | Existing artifact, sources and typed context | `POST /api/ask/stream`; `execution/comments_server_research_routes.py`; can invoke LLM and retain conversation |
| Acquisition gaps / issuer releases, slides, filings | `execution/capture_issuer_document_inventory.py`; current source receipts | `execution/manage_issuer_document_sources.py` prepare → validate → publish; `execution/fetch_ir_documents.py`; `execution/intake_documents.py` |
| SEC expected coverage and financial facts | Existing issuer/filing identity and coverage receipts | `execution/sync_sec_filing_inventory.py`; `execution/capture_expected_sec_documents.py`; `execution/ingest_sec_filing_xbrl.py`; `execution/fetch_sec_xbrl.py` |
| Transcript gaps / provenance | `transcripts/`; `execution/audit_transcript_evidence.py` | `execution/backfill_transcripts.py`; `execution/scan_ir_transcripts.py`; `execution/ingest_transcripts.py`; do not collect audio/webcasts |
| Reviewed KPI and segment population | `src/compute/kpi_resolver.py`; `src/pipeline/kpi_report_reference_resolver.py` | `execution/produce_issuer_fact_manifest.py` creates inert reviewed input; `execution/apply_issuer_fact_manifest.py` admits KPI and segment facts only |
| Sealed financial-statement facts | `src/provenance/fact_read_model.py`; declared issuer/document scope and governed extraction receipts | `execution/populate_source_fact_plane.py` plans publication from governed extraction runs; inspect its scope and commitments before authorized `--apply` |
| Research snapshot / completeness | `src/provenance/research_snapshot.py`; `src/provenance/population_research_snapshots.py` | `execution/populate_research_snapshots.py --db <explicit>`; inspect receipt before authorized `--apply` |

## Route limits and traps

- The pre-earnings CLI's repeated `--ticker` flags only narrow portfolio plus
  opted-in evaluation eligibility. Its date window still applies. Do not claim
  that it created a requested quarter for every ticker without checking output.
- The post-earnings CLI is portfolio-only. The explicit cockpit generation route
  supports owner-requested evaluation names. The stored fiscal period comes from
  the selected transcript; fiscal labels in filenames do not establish identity.
- The post-earnings CLI supports a paired `--period-end <YYYY-MM-DD>` and
  `--fiscal-period-type <Q1|Q2|Q3|Q4>` selector. It requires an active selected,
  reported transcript and validates the complete requested portfolio scope
  before generation. Without the pair, it selects the latest reported quarter.
  Verify the deployed version before live use. The cockpit API still selects
  the latest quarter; the pre-earnings CLI has no exact fiscal selector.
  `--as-of` changes the run date. Never relabel a body or change live transcript
  selection to fit a requested period.
- `capture_issuer_document_inventory.py` v1 validates calendar quarter ends. It
  cannot represent every off-calendar fiscal period. The released workflow has
  no exact-date analysis-scope planner. Retain the precise issuer dates and mark
  this capability unavailable; do not round dates or route through unmerged code.
  The released research-snapshot path uses full-population scope. A selected
  brief quarter does not narrow that scope or prove archive completeness.
- `execution/track_evaluation_names.py` has a fixed ticker list and checkout-local
  database assumptions. It is not the general evaluation-list route.
- Model preflight is read-only and does not fetch facts or grant write authority.
  Readiness exit codes are 0 ready, 2 blocked and 3 unavailable. Respect bank,
  holding-company, platform and sum-of-parts routes. An ETF has no corporate DCF.
- A deterministic fallback decision card is a fallback, not evidence of passing
  the decision-grade gate. Check the selected artifact, source manifest and input
  hash after generation. Do not alter evaluation level from analyst output.
- Do not replace fact lineage with ad hoc SQL or normalized figures in a memo.
  Preserve exact raw documents in `ir_documents/`, typed receipts and rejected or
  conflicting observations. Source capture and extraction are different receipts.
- The risk-matrix CLI selects research tickers and can fall back to DCF runs.
  It does not establish full holdings or all-account coverage. Reconcile the full
  book before making portfolio-wide concentration or overlap claims.
- `--dry-run` on the thesis evaluator skips verdict persistence but still commits
  operational run logs. Do not describe it as read-only.
- Some entrypoints retain legacy defaults or helper database lookups. Inspect
  runtime configuration and explicit path propagation before live execution.

## Investor coverage without a generic plugin dependency

Use the same evidence for earnings analysis, thesis tracking, financial
normalization, catalyst research, model tie-outs, scenarios and memos. Extend the
analysis only when it changes the decision. For long/short or event-driven work,
state the expectation, alternative case, catalyst, valuation sensitivity and
prove/kill evidence. Do not require a deck or a fixed memo template. For broad
idea generation or economic impact work, state the bounded universe and evidence
coverage. Do not imply a market-wide screen or validated portfolio optimizer.

## Verification

Check named tickers against the current roster and map each to its fiscal cycle.
For source work, retain capture hashes, locators, acquisition and extraction
receipts, and semantic dispositions. Verify that report consumers use admitted
facts and show the same evidence. Required reconstruction checks are owned by
`reconstruction_manifest.json` and `execution/verify_reconstruction_inventory.py`.

For generated outputs, check the selected database artifact and complete
source/context manifest. Verify supported fiscal identity, omission labels and
the actual output path. Preserve the existing report schema and required gates.
For live verification, follow host operations, compare the live runtime revision
with the prepared revision, and inspect the artifact selected in the front end.
Report commit, deployment, availability and public disclosure as separate states.
Do not start a local replacement server to verify the canonical host.

## Maintenance

The monthly job checks a freshly fetched, pinned remote default-branch revision
in an isolated checkout and compares the installed skill with that revision.
An unchanged retained worktree is not evidence that app procedures are current.
Missing released workflow files or installed differences produce an explicit
hold. Prepared corrections require integration before release currency can be
claimed. Preserve the worktree that owns an installed skill.

`scripts/check_investing_skill.py` compares current source files with
`src/advisor/skills/earnings-summary-investing/references/reviewed-sources.json`.
It does not open a database or invoke a model. Exit 0 means no source drift;
exit 1 means a source changed; exit 2 means invalid input or broken references.
For drift, inspect the changed procedure or executable route, correct the affected
skill instructions, and validate them before recording a new review baseline.
Use `--record-review --review-note` and explicit `--reviewed-source` arguments.
A renamed owner also requires `--replace-reviewed-source OLD=NEW` and corrected
route references. Do not accept a hash merely to remove a warning.

Behavioral assessment follows [assessment](assessment.md). It checks task selection,
analysis plans, source coverage and write boundaries as well as source drift.
