# Directives — index & current plan

Read this first to find the active plan and avoid re-building finished programs.

**Roadmap source of truth is Linear (team `BHA`).** Open work, priorities, and the live
roadmap live in Linear — issues are `BHA-<n>` (e.g. `BHA-8`, `BHA-15`, `BHA-29`, `BHA-50`, each
landing via a `(BHA-n)` commit/PR). **Check Linear FIRST when asked what's open or what's next.**
The in-repo planning files below are historical records and per-track detail authorities, not the
live tracker: [`roadmap_2026_08_consolidated.md`](roadmap_2026_08_consolidated.md) (the 2026-08
debt-first sequencer) and [`platform_backlog.md`](platform_backlog.md) / [`data_fixes.md`](data_fixes.md)
(pre-Linear cross-workspace logs) describe how the current plan was reached, but Linear supersedes
them for "what is open now."

**Programs (record-only, do NOT re-build — each shipped or was superseded):**
Chronological stack, newest first. Everything here is deployed or folded into a later program;
treat as context, not open work.

- 2026-08 — [`roadmap_2026_08_consolidated.md`](roadmap_2026_08_consolidated.md): debt &
  simplification first, then improvements. Consolidates the data-infra audit + a security/debt
  roadmap + P0→P3 planning into one sequenced plan (Track A debt before Track B improvements).
  Detail authority for the infra phases: [`data_infra_audit_2026_08.md`](data_infra_audit_2026_08.md).
- 2026-07 — [`llm_quality_program_2026_07.md`](llm_quality_program_2026_07.md): LLM quality /
  eval / routing program (P0–P4). Governance mechanics landed in
  [`meta_eval_governance.md`](meta_eval_governance.md) (Wilson-gated switch + prompt A/B auto-apply).
- 2026-06 — [`close_the_loops_2026_06.md`](close_the_loops_2026_06.md): 20-agent goal-anchored
  re-score (performance-vs-index / risk / compounding loop); built + deployed, posture is dogfood.
  Its bounded residual chips: [`residual_backlog_2026_06.md`](residual_backlog_2026_06.md).
- 2026-06 — [`capture_every_number_program.md`](capture_every_number_program.md): shipped, in PROD
  (~8,839 capture facts). Companion seam: [`provenance_override_2026_06.md`](provenance_override_2026_06.md).
- 2026-06 — The Ledger + Thought Partner: [`the_ledger_build_plan_2026_06.md`](the_ledger_build_plan_2026_06.md),
  [`the_ledger_phase1_plan_2026_06.md`](the_ledger_phase1_plan_2026_06.md),
  [`ledger_seed_2026_06.md`](ledger_seed_2026_06.md),
  [`thought_partner_build_plan.md`](thought_partner_build_plan.md),
  [`journaling_thought_partner_2026_06.md`](journaling_thought_partner_2026_06.md).
- 2026-06 — the earlier top-of-stack lineage:
  [`master_build_2026_06.md`](master_build_2026_06.md) →
  [`fund_grade_build_2026_06.md`](fund_grade_build_2026_06.md) →
  [`interaction_paradigm_2026_06.md`](interaction_paradigm_2026_06.md) (all phases shipped).
  Shipped feature/decision specs from those programs:
  [`key_metrics_picker.md`](key_metrics_picker.md) (the DIY metric picker),
  [`concepts_spine_decision.md`](concepts_spine_decision.md) (the concepts-spine decision).
- **Superseded:** [`improvement_roadmap_2026_06.md`](improvement_roadmap_2026_06.md)
  (→ `master_build_2026_06.md`).

**Cost-aware routing (SHIPPED, ongoing theme):**
[`cheapest_model_routing.md`](cheapest_model_routing.md) — cost-aware cheapest-at-parity routing
(Claude + Gemini, eval-gated). Mechanics overlap [`gemini_backend.md`](gemini_backend.md),
[`openrouter_backend.md`](openrouter_backend.md), and [`model_eval_loop.md`](model_eval_loop.md).

**Living specs (not time-bound):**

- Layer-1 baselines (immutable without owner sign-off): [`data_pipeline_dag.md`](data_pipeline_dag.md),
  [`data_provenance.md`](data_provenance.md), [`per_ticker_enhancements.md`](per_ticker_enhancements.md).
- UI / output authority:

  | Concern | Current owner | Status |
  |---|---|---|
  | visual roles, controls, recipes, and verification | [`design_language.md`](design_language.md) | canonical |
  | interaction laws and executable interaction boundaries | [`interaction_contract.md`](interaction_contract.md) | living |
  | report comments and Work OS Copilot behavior | [`report_comments_and_chat.md`](report_comments_and_chat.md) | living |
  | navigation IA proposal | [`navigation_ia.md`](navigation_ia.md) | draft evidence only; non-governing until owner sign-off |
  | semantic design review cadence and ignored report output | [`design_conformance_audit.md`](design_conformance_audit.md) | runbook; explicit request or already registered schedule |
- Operating governance: [`operations_governance_surface.md`](operations_governance_surface.md)
  (any change to an operation / operator action must follow this), [`operating_ritual.md`](operating_ritual.md).
- LLM mechanics & governance: [`llm_calls.md`](llm_calls.md), [`llm_evals_plan.md`](llm_evals_plan.md),
  [`model_eval_loop.md`](model_eval_loop.md), [`meta_eval_governance.md`](meta_eval_governance.md),
  [`gemini_backend.md`](gemini_backend.md), [`openrouter_backend.md`](openrouter_backend.md)
  (backend mechanics), [`llm_injection_threat_model.md`](llm_injection_threat_model.md),
  [`llm_quota_scheduling.md`](llm_quota_scheduling.md) (protected windows + quota discipline),
  [`peer_selection_llm.md`](peer_selection_llm.md).
- Data-model & schema specs: [`holdings_json_schema.md`](holdings_json_schema.md),
  [`folder_structure.md`](folder_structure.md), [`cross_asset_data_model.md`](cross_asset_data_model.md),
  [`document_tables_design.md`](document_tables_design.md),
  [`annual_kpi_cadence_design.md`](annual_kpi_cadence_design.md),
  [`next_dollar_model.md`](next_dollar_model.md), [`news_sources_plan.md`](news_sources_plan.md),
  [`per_ticker_segment_extraction_notes.md`](per_ticker_segment_extraction_notes.md),
  [`scenario_prior_cadence.md`](scenario_prior_cadence.md), [`etf_data.md`](etf_data.md),
  [`provenance_override_2026_06.md`](provenance_override_2026_06.md) (the `fact_overrides` seam).

**Runbooks / task SOPs:** [`fetch_transcripts.md`](fetch_transcripts.md),
[`fetch_qa_transcript.md`](fetch_qa_transcript.md), [`backfill_transcripts.md`](backfill_transcripts.md),
[`qa_transcripts.md`](qa_transcripts.md), [`fetch_ir_documents.md`](fetch_ir_documents.md),
[`ir_browser_assisted_fetch.md`](ir_browser_assisted_fetch.md), [`intake_documents.md`](intake_documents.md),
[`ir_events_ingestion.md`](ir_events_ingestion.md), [`onboard_pending_tickers.md`](onboard_pending_tickers.md),
[`quarterly_refresh.md`](quarterly_refresh.md), [`post_earnings_readout.md`](post_earnings_readout.md),
[`micro_thesis_runbook.md`](micro_thesis_runbook.md), [`micro_thesis_skill.md`](micro_thesis_skill.md),
[`dcf_gsheets_setup.md`](dcf_gsheets_setup.md), [`nvo_external_sources.md`](nvo_external_sources.md),
[`design_conformance_audit.md`](design_conformance_audit.md) (monthly semantic design-conformance audit),
[`monthly_red_team.md`](monthly_red_team.md) (monthly adversarial audit),
[`edgar_pipeline.md`](edgar_pipeline.md) (EDGAR = weekly free statement freshness; FMP = ~6-monthly
paid backpop), [`fmp_backpop.md`](fmp_backpop.md) (the diff-aware `execution/fmp_backpop.py` playbook),
[`db_garbage_collection.md`](db_garbage_collection.md) (DB GC / pruning),
[`weekly_cleanup.md`](weekly_cleanup.md) (Sunday git + memory cleanup).

**Self-hosting:** [`self_host_scoping.md`](self_host_scoping.md),
[`self_host_phase1_laptop.md`](self_host_phase1_laptop.md).
**Agent tooling:** [`claude_session_bridge.md`](claude_session_bridge.md) (Claude Code session bridge).

**Historical trackers / data-quality logs (superseded by Linear for live work):**
[`platform_backlog.md`](platform_backlog.md) (pre-Linear cross-workspace tracker),
[`data_fixes.md`](data_fixes.md).

**Cross-repo decision (record-only):** [`cio_advisor_governance_2026_06.md`](cio_advisor_governance_2026_06.md)
— advisor / Personal-CIO governance; scope is the companion **portfolio-tracker** repo, not this one.

**Re-grade memos (historical, dated point-in-time audits):**
[`regrade_memo_post_wedge.md`](regrade_memo_post_wedge.md) (v3),
[`regrade_memo_v5_independent.md`](regrade_memo_v5_independent.md) (v5),
[`regrade_memo_v6.md`](regrade_memo_v6.md).
