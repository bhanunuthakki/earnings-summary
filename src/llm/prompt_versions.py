"""Single source of truth for calibration prompt versions.

This is NOT a cache key. The LLM artifact CACHE is invalidated by the rendered
prompt's *content hash* (see ``src/llm/style.py`` and
``llm_artifact_store.compute_input_sha256``) — editing a prompt regenerates its
cached artifact automatically, with no version bump anywhere.

This module is the A/B dimension for the *calibration* loop. Scores land in
``prompt_calibration_scores`` tagged with ``(purpose, prompt_version)`` and
``llm.calibration.summarize_by_prompt_version`` groups by that tuple so the
dashboard can answer "is the rewritten prompt producing better-graded output
than the old one?".

The problem this fixes: every grader hardcoded ``prompt_version="v1"``, so every
score was ``v1`` and there was never a second version to compare — the A/B
machinery was *structurally* dead. Versions now live here, in one place. When
you MATERIALLY iterate a graded/cached prompt, bump its entry below (``"v1"`` ->
``"v2"``); subsequent ``record_score()`` rows (and, for the trigger sensors, new
artifacts) carry the new version and the calibration panel compares the two. A
purpose with no entry defaults to ``"v1"``.

The four trigger sensors now source their artifact ``_PROMPT_VERSION`` from this
registry (see the per-purpose comment below) rather than a hardcoded literal, so
the registry is the single bump-point for both the calibration A/B dimension and
the triggers' cache/version. Producing the *scores themselves* for the
prediction purpose is wired in ``execution/grade_predictions.py`` (a sound,
available-now signal: the fraction of due predictions the extraction prompt left
well-formed enough to grade against realized ``kpi_facts``), and all three
graders are now run on a schedule by ``execution/run_calibration_grading.py``.
"""

from __future__ import annotations

# purpose -> current prompt version. Bump when a graded/cached prompt is
# materially rewritten. Purposes absent here default to "v1".
#
# Two kinds of consumer read this, both keyed on the same per-purpose version so
# ONE bump governs both:
#   * the calibration graders (bear_case, decision_audit, management_prediction)
#     tag every ``record_score`` row with ``prompt_version_for(purpose)`` so the
#     A/B comparison has a version dimension;
#   * the four Personal-CIO trigger sensors (earnings_tone_diff,
#     kpi_inflection_context, material_news_classification, saydo_due_context)
#     source their artifact ``_PROMPT_VERSION`` here instead of a hardcoded
#     literal — so a materially rewritten trigger prompt is bumped in ONE place,
#     which both busts that artifact's cache key and tags new artifacts with the
#     new version. (Before, each trigger hardcoded ``"v1"`` and never consulted
#     this registry, so every artifact was permanently ``v1`` — v6 re-grade.)
_PROMPT_VERSIONS: dict[str, str] = {
    # Weekly model-frontier web research. The prompt is stable and its output
    # is independently schema/eval gated before it can affect routing.
    "model_frontier_research": "v1",
    # Governed private-caller migrations: all are schema-bound extractors and
    # must have a stable prompt version for eval/ledger attribution.
    "kpi_summary_extract": "v1",
    "kpi_summary_enumerate": "v1",
    "segment_6k_breakdown_extract": "v1",
    "segment_definition_extract": "v1",
    "segment_crosstab_extract": "v1",
    "ir_sheet_kpi_map": "v1",
    # Graded calibration purposes.
    # bear_case v2 (2026-06-12, S9): untrusted-content spotlighting — the IR
    # anchor block is now wrapped in BEGIN/END UNTRUSTED-DATA markers with an
    # instruction-priority notice (src/llm/untrusted.py).
    "bear_case": "v2",
    "decision_audit": "v1",
    "management_prediction": "v1",
    # Personal-CIO trigger artifact purposes.
    # v2 across the board (2026-06-12, S9): spotlighting of untrusted prompt
    # inputs — transcript bodies (earnings_tone_diff, + template priority
    # rule), news headlines/snippets (material_news_classification), and the
    # composed anchor block every trigger embeds (compose_anchor_block now
    # wraps). saydo/kpi prompts changed only via the anchor wrap.
    "earnings_tone_diff": "v2",
    "kpi_inflection_context": "v2",
    # material_news v3 (2026-07-30): event-type taxonomy (primary / results /
    # commentary) + novelty framing — the v2 prompt scored topical relevance,
    # so opinion pieces and post-earnings recaps about thesis-relevant topics
    # fired as alerts (owner: "low quality/noise signals"). Commentary is now
    # vetoed code-side regardless of score; relevance floor 0.6 → 0.7.
    # v4 (2026-07-31): event_key clustering — each entry now labels the
    # underlying real-world event so multi-outlet coverage collapses to one
    # alert (scan-side same-batch dedup + 72h cross-day guard).
    # v5 (2026-07-31, backtest calibration): classify by the UNDERLYING event,
    # not headline packaging — editorialized/price-action headlines that
    # first-report a real primary event (UBER/Rivian $1.2B, NVO EU approval)
    # were filed commentary and killed. Floor 0.7 → 0.65 rides the same
    # calibration (the BN $100B campus wire report scored 0.68).
    "material_news_classification": "v5",
    "saydo_due_context": "v2",
    # Pairwise backend judge (src/llm/backend_judge.py). Bump when the A/B judge
    # rubric is materially reworded so a re-grade of the same corpus is comparable
    # to the prior verdict instead of being silently confounded by the prompt.
    "backend_compare_judge": "v1",
    # Eval-harness purposes (src/evals/, directives/llm_evals_plan.md): every
    # eval run records prompt_version_for(purpose), so bumping here is what
    # makes a prompt rewrite show up as a comparable A/B slice in
    # summarize_by_prompt_version. Bump when nl_compile._build_prompt changes
    # materially.
    # v2 (2026-06-11): no transform-stacking on pre-transformed kpi tokens +
    # most-specific colloquialism resolution — the first live eval run's
    # failure modes (vs-002/vs-016 YoY-of-YoY, vs-010 R&D->opex).
    "viewspec_compile": "v2",
    # Ask pack router (src/ask/router.py, S4). Bump when _build_prompt's
    # catalog/rules are materially rewritten, then re-run
    # `run_llm_evals.py --purpose ask_pack_router`.
    "ask_pack_router": "v1",
    # Ask claim-grounding audit (src/ask/claims.py, S8). Bump when
    # _build_prompt's rules are materially rewritten — the citation-accuracy
    # golden set scores this purpose, so the version keys its score history.
    "ask_claim_grounding": "v1",
    # Ask evidence follow-up loop (src/ask/followup.py, S7). Bump when
    # need_protocol_block / _compose_followup_prompt are materially
    # rewritten, then re-run `run_llm_evals.py --purpose
    # ask_evidence_followup`.
    "ask_evidence_followup": "v1",
    # Rubric-audited prose purposes (mode B, PR 2): their audit runs are keyed
    # to these versions, so bump when generate_summary's prompt /
    # _NEXT_DOLLAR_PROMPT is materially rewritten and the score history forks
    # cleanly. (bear_case is already registered above with the outcome
    # graders — one entry governs both its grading modes.)
    # transcript_summary v2 (2026-06-12, S9): transcript body + anchor block
    # spotlighted as untrusted data.
    "transcript_summary": "v2",
    "advisor_next_dollar": "v1",
    # Incremental Dollar Recommendation (P0.4a, mode-B rubric,
    # personal_investment_partner_prd.md §7.4/§10). Bump when
    # allocation.recommendation_artifact._build_prompt is materially rewritten.
    "incremental_dollar_recommendation": "v1",
    # Investment Decision Card (P1.1, mode-B rubric,
    # personal_investment_partner_prd.md §8.1/§10). Bump when
    # research.investment_decision_card._build_prompt is materially rewritten.
    "investment_decision_card": "v1",
    # Senior Partner Brief (P2.2, mode-B rubric,
    # personal_investment_partner_prd.md §9.1/§10). Bump when
    # advisor.senior_partner_brief._build_prompt is materially rewritten, then
    # re-run `run_llm_evals.py --purpose senior_partner_brief` so the rewrite
    # forks the score history cleanly.
    "senior_partner_brief": "v1",
    # Ask advisory answer (mode-B rubric, close_the_loops L3). The audit run
    # records this version, so bump when the conversational ANSWER prompt (the
    # system context + evidence + thread assembly in src/ask/engine.py /
    # ask.narrative_transport.stream_llm_text, generation purpose `ask_answer`) is
    # materially rewritten, then re-run `run_llm_evals.py --purpose
    # ask_advisory_answer` so the rewrite forks the score history cleanly.
    "ask_advisory_answer": "v1",
    # Golden-set classifier purposes (mode A, PR 4). Bump when the prompt in
    # identify_transcript_metadata / classify_intake_document /
    # structure_recent_news_json is materially rewritten, then re-run
    # `run_llm_evals.py --purpose <p> --min-score ...` per the prompt-change
    # workflow in directives/llm_calls.md.
    "transcript_metadata": "v2",
    "intake_classifier": "v1",
    # news_structuring v2 (2026-06-12, S9): UNTRUSTED WEB CONTENT priority
    # rule added; thesis anchor spotlighted at the fetch_news_websearch call
    # site (which also now sources its artifact version from this entry).
    # v3 (2026-08-03): transport-neutral search-first obligation and evidenced
    # empty-result branch; model and transport are unchanged.
    "news_structuring": "v3",
    # Falsifiable-condition extraction (src/decision_conditions.py, mode-A
    # golden set). Bump when _EXTRACTION_PROMPT is materially rewritten, then
    # re-run `run_llm_evals.py --purpose decision_conditions_extract`.
    # v2 (2026-07-16): milestone-dated tripwires — the prompt now carries the
    # decision date and asks for `not_before` (decision date + stated horizon)
    # on forward-looking milestone conditions, so "reaches $X in ~12 months"
    # stops being encoded as an immediately-evaluable threshold.
    "decision_conditions_extract": "v2",
    # Qualitative-condition extraction (src/decision_conditions.py, L9 PR2 — the
    # non-numeric news/earnings-tone bridge). Bump when _QUALITATIVE_PROMPT is
    # materially rewritten.
    "qualitative_conditions_extract": "v1",
    # Diet information-quality scoring (src/signals/quality.py — the ingest-time
    # replacement for the static publisher denylist). Bump when _PROMPT's rubric
    # is materially rewritten.
    "diet_source_quality": "v1",
    # LLM peer selection (src/compute/peer_selection.py, mode-A overlap golden
    # set). Bump when _build_prompt is materially rewritten, then re-run
    # `run_llm_evals.py --purpose peer_selection --min-score ...`.
    # v2 (2026-07-02): ticker-resolvability rules — US primary/ADR symbol only
    # (SE not SEA, SAP not SAPS), never delisted/taken-private names. Fixes the
    # ~15% of v1 suggestions that could never resolve FMP fundamentals.
    "peer_selection": "v2",
    # LLM key-metrics preselect (src/compute/key_metrics.py, mode-A recall golden
    # set). Bump when _build_prompt is materially rewritten, then re-run
    # `run_llm_evals.py --purpose key_metrics --min-score ...`.
    "key_metrics": "v1",
    # Sector-benchmark-ETF proposal (src/compute/sector_benchmark_proposal.py,
    # mode-A exact-match golden set, comparable_sets_bottoms_up.md §4). Bump
    # when _build_prompt is materially rewritten, then re-run
    # `run_llm_evals.py --purpose sector_benchmark_proposal --min-score ...`.
    "sector_benchmark_proposal": "v1",
    # The eval judge itself (src/evals/judge.py + rubric_judge.py prompt
    # templates). Bump when either judge prompt is materially reworded so
    # spot-check agreement rates (execution/spot_check_eval_judge.py) stay
    # comparable within a version.
    "eval_judge": "v1",
    # Untrusted-content spotlighting wave (2026-06-12, S9 sec-llm pass;
    # directives/llm_injection_threat_model.md). First registration for each,
    # at v2: their prompts changed — either directly (document text / web
    # rules) or because the composed anchor block they embed is now wrapped
    # by compose_anchor_block.
    "press_release_summary": "v2",  # press-release body spotlighted
    "presentation_brief": "v2",  # deck text spotlighted
    "event_brief": "v2",  # event document text spotlighted
    # v3 (2026-08-03): transport-neutral search-first obligation and evidenced
    # no-news branch; model and transport are unchanged.
    "recent_developments": "v3",
    "company_description": "v2",  # 10-K excerpts + IR blocks spotlighted
    "platform_diagram": "v2",  # 10-K + transcript excerpts spotlighted
    "pairwise_analysis": "v2",  # composed anchor block now spotlighted
    "saydo_filter": "v2",  # composed anchor block now spotlighted
    "advisor_socratic_questions": "v2",  # composed anchor block now spotlighted
    "advisor_socratic_memo": "v2",  # composed anchor block now spotlighted
    # The injection-canary golden SET (evals/golden/injection_canaries.json),
    # not an LLM prompt of its own — versioned so a future canary-set revision
    # forks the security-pass-rate history cleanly. Bump when cases change.
    "injection_canaries": "v1",
    # Podcast takeaway summarizer (S11): 2-4 sentence investment briefing from
    # RSS description copy. Bump when _build_prompt is materially rewritten and
    # re-run the golden-set eval (evals/golden/podcast_takeaway_summary.json).
    "podcast_takeaway_summary": "v1",
    # Pre-earnings brief (src/earnings_brief.py, 2026-07-31): the scheduled
    # per-(ticker, ER-date) narrative brief. The version participates in the
    # llm_artifacts input hash, so a bump here alone forces regeneration on
    # the next in-window run — bump ONLY when _PROMPT_HEADER / the section
    # assembly is materially rewritten.
    "pre_earnings_brief": "v1",
    # Persisted post-earnings readout (src/earnings_readout.py). A version bump
    # supersedes within each reported period on its next generation/request.
    "post_earnings_readout": "v1",
    # Rubric-audited prose purposes (Chip 2). Bump when the generating prompt
    # in llm_client.extract_qa_vs_prepared_themes / generate_qa_topics is
    # materially rewritten and re-run `run_llm_evals.py --purpose <p>`.
    "earnings_themes_split": "v1",
    "qa_topics": "v1",
    # Calibration coach (close_the_loops L8): the monthly scorecard's named
    # biases + behavioural experiment. Registered here so it counts as a known
    # audit purpose (the rubric-wiring test gates AUDIT_SPECS ⊆ registered).
    "calibration_coach": "v1",
    # The Ledger longitudinal synthesis (Wave B). Bump when the per-holding
    # stance-consolidation prompt (theme_synthesis) or the theme-clustering
    # prompt (theme_seed_cluster) is materially rewritten.
    "theme_synthesis": "v1",
    "theme_seed_cluster": "v1",
    # The Worldview distiller (P2). Bump when the Tenet-distillation prompt is
    # materially rewritten. v2 (2026-07-19): identity-level bar, conditional
    # phrasing over flat contradictory rules, standing Tenets ride in and
    # overlap must revise (reuse scope_key) instead of stacking, max 3.
    "tenet_distill": "v2",
    # The session-distill tap (B4 keystone). Bump when _build_prompt in
    # synthesis.session_distill is materially rewritten.
    "session_distill": "v1",
    # The Ledger Phase-1 wondering classifier (research-loop gate). Bump when the
    # detect prompt is materially rewritten, then re-run the golden set.
    "wondering_detect": "v1",
    # The Ledger intent tap (research.intent._build_prompt). Supersedes
    # wondering_detect on the live tap. Bump when the intent prompt is materially
    # rewritten, then re-run `run_llm_evals.py --purpose capture_intent`.
    "capture_intent": "v1",
    # The B7 routing triage (research.triage._build_prompt). Bump when the
    # triage prompt is materially rewritten.
    "research_triage": "v1",
    # The Ledger reply router (onmymind.reply._build_prompt). Bump when the
    # reply-intent prompt is materially rewritten.
    "ledger_reply_intent": "v1",
    # The Triage second-pass router (user_state.triage_suggest._build_prompt).
    # Bump when the route-suggest prompt is materially rewritten.
    "triage_route_suggest": "v1",
    # The capture->answer primary gate (capture.triage._build_prompt, B3).
    # Bump when the triage prompt is materially rewritten, then re-run
    # `run_llm_evals.py --purpose capture_triage`.
    "capture_triage": "v1",
    # The async Decision Draft parser riding the landed capture note
    # (capture.decision_draft, P2.1 — PRD §9.2/§10.1). Bump with its prompt;
    # re-run `run_llm_evals.py --purpose decision_draft_parse`.
    "decision_draft_parse": "v1",
    # The Ledger reply-box coach follow-up router (B3 sibling PR). Registered
    # here alongside capture_triage since this file is the one source of
    # truth for prompt versions.
    "coach_reply_intent": "v1",
    # The Ledger artifact brief (research.brief._build_prompt). Bump when the brief /
    # stress prompt is materially rewritten.
    "artifact_brief": "v1",
    # The Ledger Phase-1 research loop (two-pass): fetch / adversarial assess / narrate.
    "research_fetch": "v2",  # transport-neutral search-first web prompt
    "research_adversarial_assess": "v1",
    "research_narrate": "v1",
    # The Ledger Phase-2 generation seams (decision extraction + drift phrasing).
    "musing_decision_extract": "v1",
    "drift_narrate": "v1",
    # Meta-eval sampler difficulty classifier (evals/sampler.py, §2). This version
    # IS the eval_case_features cache key: bumping it invalidates every cached
    # classification by key and forks stratification history cleanly.
    "case_difficulty_classify": "v1",
    # Meta-eval steering (llm/nominator.py + llm/frontier.py, §1.2/§10.1).
    "optimizer_nominator": "v1",
    # Per-case checklist deriver (llm/query_criteria.py, §3). This version IS
    # the query_criteria cache key: bumping it forks checklist history cleanly.
    "query_criteria_derive": "v1",
    # Prompt-variant proposer (llm/prompt_ab.py, §4).
    "prompt_variant_propose": "v1",
    "prompt_reflect_rewrite": "v1",
    # SayDo commitment extraction (compute/say_do_extractor.build_extraction_prompt).
    # Bump when the extraction prompt is materially rewritten; commitment_scan_log
    # rows carry the version, so stale-version scans can be invalidated (DELETE
    # WHERE prompt_version != current) to force a re-scan under the new prompt.
    "saydo_commitment_extract": "v1",
    # The Ledger Phase-1 artifact drafters (thesis entry + code-change spec).
    "thesis_entry_draft": "v1",
    "research_code_spec": "v1",
    # The Ledger DCF assumption-tweak extractor (dcf_assumption_extract).
    "dcf_assumption_extract": "v1",
    # Position-review verdict (src/advisor/position_review.py). Bump when
    # _build_verdict_prompt / _behavioral_rules is materially rewritten.
    # v2 (PR5): rule 1's evidence is interpolated from the live
    # graded_sell_record base rate instead of a hardcoded MU/GOOGL/TSM.
    "position_review": "v2",
    # Per-name DCF scenario prior (src/dcf/scenario_prior.py, mode-A golden set).
    # Bump when the weight-setting prompt (dcf.scenario_prior._PROMPT) is materially
    # rewritten, then re-run `run_llm_evals.py --purpose scenario_prior`.
    "scenario_prior": "v1",
    # Positioning coach + encode (src/positioning/coach_pack.py + encode.py).
    # Bump coach when _INSTRUCTIONS materially changes posture; bump encode
    # when _prompt's schema/rules change (the approval form re-validates
    # owner-side regardless, so an encode bump never risks silent state).
    "positioning_coach_turn": "v1",
    "positioning_encode": "v1",
    # ETF role-in-portfolio one-pager (src/etf_role_synthesis.py). The version
    # rides the llm_artifacts input sha — bumping it forks every cached
    # one-pager cleanly (a regenerate is forced even if no input moved).
    "etf_role_synthesis": "v1",
    # Whole-book thesis-collision audit (src/thesis_collision.py). IS the
    # llm_artifacts cache key alongside the thesis-set hash — bumping it forks
    # cached findings cleanly (a re-run is forced even if no thesis changed).
    "thesis_collision": "v1",
    # Monthly Red Team engine (src/redteam/, PR5). Bump when a lens's framing
    # in lenses._LENS_FRAMING (red_team_attack) or a cross-book prompt in
    # cross_book.py (red_team_cross_book) is materially rewritten.
    "red_team_attack": "v1",
    "red_team_cross_book": "v1",
    # Annual letter-to-self (monthly_red_team.md Phase 3, PR7,
    # execution/draft_annual_letter.py). Bump when the drafting prompt is
    # materially rewritten.
    "annual_letter": "v1",
    # 10-Q segment quarterly period-axis disambiguation Stage B fallback
    # (docs/design/segment_quarterly_framework.md §2.4,
    # compute.segment_quarterly_10q._build_disambiguate_prompt). Bump when
    # the prompt is materially rewritten, then re-run
    # `run_llm_evals.py --purpose segment_10q_period_disambiguate`.
    "segment_10q_period_disambiguate": "v1",
    # Behavioral-rules distiller (tenet-2 Phase 4,
    # synthesis.behavior_distill._build_prompt). Bump when the distill prompt
    # is materially rewritten, then re-run
    # `run_llm_evals.py --purpose behavior_distill`.
    "behavior_distill": "v1",
    # Semantic tenet-tension detection (B5, synthesis.semantic_tension._build_prompt).
    # Bump when the restate/contradict prompt is materially rewritten, then
    # re-run any golden-set eval added for this purpose.
    "tenet_semantic_tension": "v1",
    # Tenet accountability ledger (B5, synthesis.tenet_accountability — the
    # sibling B5 workstream's file). Registered here since this file is the
    # one source of truth for prompt versions.
    "tenet_accountability": "v1",
    # Exit post-mortem drafting (B6, 2026-07-19 program overhaul,
    # synthesis.exit_postmortem._build_prompt). Bump when the drafting
    # prompt is materially rewritten, then re-run any golden-set eval added
    # for this purpose.
    "exit_postmortem_draft": "v1",
    # Business-factor taxonomy (C3, 2026-07-19 program overhaul Workstream C
    # keystone, src/risk_factors.py). IS the llm_artifacts cache key
    # alongside the per-ticker mix+thesis input hash — bumping it (e.g. after
    # a TAXONOMY definition rewrite) forks every cached derivation cleanly.
    "business_factor_taxonomy": "v1",
    # D1e ground truth (disclosure_intelligence_v1_prd.md): the two P1/P3
    # judgment-layer classifiers, first registered here at v1 alongside their
    # first golden sets (evals/golden/metric_lifecycle_triage.json,
    # evals/golden/disclosure_item_specificity_triage.json). Bump when
    # filings.metric_triage._build_prompt / filings.boilerplate_triage._build_prompt
    # is materially rewritten, then re-run `run_llm_evals.py --purpose <p>`.
    # NOTE (D2.1 fold-in, PR follow-on to #1037): metric_lifecycle_triage's
    # prompt gained an explicit materiality input and the mandatory-GAAP
    # noise source moved candidates out of this purpose entirely BEFORE they
    # reach it — not a version bump on its own (no golden-set re-grade
    # requested alongside), but flagged here so a future re-grade knows the
    # prompt changed since the v1 golden set was captured.
    "metric_lifecycle_triage": "v1",
    "disclosure_item_specificity_triage": "v1",
    # Thesis-materiality elevation gate (filings.materiality_judgment
    # ._build_prompt, owner ruling 2026-08-02). Bump when the
    # restricts-measurement bar or the anchor framing is materially rewritten.
    "disclosure_thesis_materiality": "v1",
    # Guidance-withdrawal detector Stage 2/3 triage (D2.1,
    # filings.guidance_triage._build_prompt). Bump when the relevance/prior
    # prompt is materially rewritten.
    "guidance_lifecycle_triage": "v1",
    # Explicit governance coverage for model-pinned purposes that previously
    # inherited the v1 fallback. These rows are behavior-preserving, but make
    # capture cohorts and eval attribution auditable.
    "advisor_swap_check": "v1",
    "ask_answer": "v1",
    "ask_claim_audit": "v1",
    "bear_case_grading": "v1",
    "canonicalize_segments": "v1",
    "customer_concentration_extraction": "v1",
    "dcf_assumptions": "v2",
    "decision_extraction": "v1",
    "exec_comp_alignment": "v1",
    "exec_comp_extraction": "v1",
    "extract_8k_overrides": "v1",
    "footnote_extraction": "v1",
    "investor_deck_extraction": "v1",
    "kpi_registry_auto_proposal": "v1",
    "market_signals": "v1",
    "patent_timeline": "v1",
    "pressure_test_thesis": "v2",
    "risk_factor_classify": "v2",
    "risk_factor_diff": "v2",
    "saydo_importance": "v1",
    "strategic_analysis": "v1",
    "thesis_pass_a": "v1",
    "thesis_pass_b": "v1",
    "transcript_qa_judgment": "v1",
    "transcript_topic_triage": "v1",
    "valuation_basis": "v1",
    "weekly_packet_predraft": "v1",
}

_DEFAULT_VERSION = "v1"


def prompt_version_for(purpose: str) -> str:
    """Current prompt version for ``purpose`` (default ``"v1"``).

    The single bump-point for both the calibration A/B dimension and the trigger
    sensors' artifact version — see the registry comment above.
    """
    return _PROMPT_VERSIONS.get(purpose, _DEFAULT_VERSION)


def registered_purposes() -> frozenset[str]:
    """The purposes with an explicit registry entry (vs. silently defaulting to
    ``"v1"``) — lets callers enumerate without touching the private dict."""
    return frozenset(_PROMPT_VERSIONS)
