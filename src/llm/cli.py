# pyright: reportPrivateUsage=false
#
# This module intentionally reads + writes `_setup_verified` and
# `_claude_cli_path` on the llm_client module via late `import llm_client`
# (see the docstring below + commit history for why). Pyright flags every
# such access as cross-module private-usage; the module-level directive
# above silences only that rule, preserving every other strict check.
"""
src/llm/cli.py
--------------
Claude Code CLI subprocess wiring + the public ``call_llm`` / ``call_llm_with_web``
entry points + per-purpose budget enforcement.

Primary path: ``claude -p`` via subprocess. The CLI honors whichever auth is
configured in the environment — ``ANTHROPIC_API_KEY`` for metered API billing,
or ``claude auth login`` for a Pro/Max subscription.

On any operational failure (timeout, non-zero exit, empty stdout, malformed
JSON envelope, binary missing mid-run), the call is routed through the Gemini
fallback in ``src/llm/fallback.py``. Setup-class errors (binary missing on
first call) propagate without fallback — they need operator action, not
papering over.

Second backend: ``src/llm/gemini_backend.py`` calls the Gemini Developer API
directly (metered key) with the same call contract. ``call_llm`` routes a
purpose there when its resolved model is a Gemini model id — model-first
dispatch, see directives/cheapest_model_routing.md — or when a caller forces
``backend="gemini"`` explicitly (the compare harness). A purpose only
resolves to a Gemini model after the LLM-evals judges grade its output
quality first; see directives/gemini_backend.md. Everything else runs
Claude, exactly as before.

Public API:
    DEFAULT_MODEL, FAST_CLASSIFIER_MODEL — canonical model ids.
    LLM_MODELS — per-purpose model selection table.
    DEFAULT_TIMEOUT_SECONDS, CLAUDE_WEB_TIMEOUT_SECONDS, CLAUDE_WEB_TOOLS.
    LLMBudgetExceeded — raised when a hard per-purpose monthly cap is over.
    call_llm(...) — single-shot LLM call. Canonical entry point.
    call_llm_with_web(...) — same, with Claude WebSearch + WebFetch tools.

Note on module-level state: ``_setup_verified`` and ``_claude_cli_path`` are
intentionally kept as live globals in ``src/llm_client.py`` (read via late
``import llm_client`` inside the functions below) so the existing test
monkeypatch surface — ``monkeypatch.setattr(llm_client, "_setup_verified",
True)`` — continues to work without test modification.

Extracted from src/llm_client.py during the llm subpackage split (PURE
refactor — zero behavior change).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
from datetime import UTC, datetime

from llm.capture import capture_exchange
from llm.ledger import fallback_call_logged, record_llm_call

log = logging.getLogger(__name__)

# A cwd with no project `.mcp.json`. The nested `claude -p` subprocess otherwise
# tries to boot every server in the project's `.mcp.json` on startup and hangs
# (observed ~5 min then killed). Running it from a neutral directory skips that
# while still loading the user-level `~/.claude` config. Cached per process.
_neutral_cwd_cache: str | None = None


def _neutral_subprocess_cwd() -> str:
    global _neutral_cwd_cache
    if _neutral_cwd_cache is None:
        _neutral_cwd_cache = tempfile.mkdtemp(prefix="es_claude_cwd_")
    return _neutral_cwd_cache


# Default Claude model for prompt calls. Sonnet 4.6 chosen as a balance of
# quality and speed across the pipeline's tasks. Per-function overrides via
# the `model` argument on _call_claude or by adding the purpose to LLM_MODELS.
DEFAULT_MODEL = "claude-sonnet-4-6"

# Fast classifier model — used for short, structured calls (intake doc-type
# classification, transcript metadata extraction, batch extractors) where
# Sonnet would be overkill. Haiku 4.5 returns ~5x faster at materially the
# same quality on narrowly-scoped JSON-output tasks.
FAST_CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"

# Per-purpose model selection. Every public generator below should resolve its
# model via _model_for(purpose) so retuning one section doesn't require touching
# the call site. Keys are stable strings; values are model identifiers Claude
# CLI accepts. Adding a new entry here is the only change needed to retune a
# section's quality/latency tradeoff.
#
# Rationale per entry:
#   sonnet (default): long-context analysis where reasoning matters
#       (transcript summary, thesis tracker, bear case, strategic compare).
#   haiku (FAST_CLASSIFIER_MODEL): short, structured, often-batched calls
#       where latency dominates and the task is narrowly scoped
#       (intake classification, per-line entity extraction).
LLM_MODELS: dict[str, str] = {
    # The Ledger longitudinal synthesis (Wave B). theme_synthesis consolidates a
    # holding's captured musings into a cited standing stance; theme_seed_cluster
    # groups musings/notes into themes. Both are analytical synthesis-with-
    # citations (latency unimportant; batch on the morning pipeline) → Sonnet.
    # Budget-capped + the citation set is deterministically validated before any
    # insight is recorded (no hallucinated "you said X").
    "theme_synthesis": DEFAULT_MODEL,
    "theme_seed_cluster": DEFAULT_MODEL,
    # The Ledger Phase-1 wondering classifier (the research-loop gate). A short
    # closed binary+extraction behind a deterministic regex pre-gate → the cheap
    # FAST tier; the golden set (evals/golden/wondering_detect.json) is its bar.
    "wondering_detect": FAST_CLASSIFIER_MODEL,
    # The Ledger Phase-1 research loop (the two-pass trifecta firebreak): fetch
    # (web on, no writer) / adversarial assess / narrate (no web). Sonnet-tier
    # reasoning + web orchestration; monthly budgets seeded warn/warn/skip (0124),
    # per-run $-cap clamped by the budget tier (research.tier).
    "research_fetch": DEFAULT_MODEL,
    "research_adversarial_assess": DEFAULT_MODEL,
    "research_narrate": DEFAULT_MODEL,
    # The Ledger Phase-2 generation seams (opt-in, web-less). musing_decision_extract
    # structures a free-text owner musing into a decision; drift_narrate rewords a
    # PRE-COMPUTED drift signal (wording only). Both short + closed → the cheap FAST
    # tier, like the sibling wondering_detect classifier.
    "musing_decision_extract": FAST_CLASSIFIER_MODEL,
    "drift_narrate": FAST_CLASSIFIER_MODEL,
    # The Ledger Phase-1 artifact drafters (web-less, feed the gated mutating kinds).
    # thesis_entry_draft distills a memo + evidence into an append-only ledger entry;
    # research_code_spec drafts an inert, human-reviewed code-change spec. Both are
    # open-ended drafting where structure/quality matters (like research_narrate) →
    # DEFAULT (Sonnet); the code spec is rare and never auto-applies.
    "thesis_entry_draft": DEFAULT_MODEL,
    "research_code_spec": DEFAULT_MODEL,
    # The Ledger DCF assumption-tweak extractor: a short, closed extraction of ONE
    # {param, new_value} edit from a what-if wondering (bounds-validated; the LLM emits
    # NO valuation number — the deterministic engine recompute is the oracle) → the
    # cheap FAST tier, like the sibling extractors.
    "dcf_assumption_extract": FAST_CLASSIFIER_MODEL,
    # Position-review verdict (src/advisor/position_review.py, the /review service).
    # Judgment over the grounded pre-analysis + the owner's convictions, calibrated
    # to his behavioral patterns → Sonnet-tier reasoning (latency unimportant, one
    # call per on-demand review). A deterministic behavioral guardrail backstops the
    # sell-winners-too-early rule regardless of the model's call.
    "position_review": DEFAULT_MODEL,
    # Long-context analytical writing
    "transcript_summary": DEFAULT_MODEL,
    "press_release_summary": DEFAULT_MODEL,
    "presentation_brief": DEFAULT_MODEL,
    "pairwise_analysis": DEFAULT_MODEL,
    "strategic_analysis": DEFAULT_MODEL,
    "thesis_pass_a": DEFAULT_MODEL,
    "thesis_pass_b": DEFAULT_MODEL,
    "bear_case": DEFAULT_MODEL,
    "event_brief": DEFAULT_MODEL,
    # Thesis pressure-test (execution/pressure_test_thesis.py): long-context
    # adversarial judgment over the whole evidence corpus. Was unregistered —
    # it landed on Sonnet anyway via the unknown-purpose fallback, but with a
    # warning logged on every call and the pin invisible to review. Registered
    # at the model it was already getting (llm_evals_plan.md §5.5).
    "pressure_test_thesis": DEFAULT_MODEL,
    # Investor-deck extraction: long-context structured-output. Decks run
    # ~30-60 pages of dense slide content; Sonnet's reasoning is needed to
    # distinguish forward-looking commitments from historical recap and to
    # bucket each target into the right `target_kind` enum value. Haiku
    # under-counted on this task in scratch experimentation.
    "investor_deck_extraction": DEFAULT_MODEL,
    # Company description is the analytical spine of the memo. Was pinned to
    # Opus on the belief it followed nuanced instruction far better than Sonnet
    # — but the model-downgrade eval (directives/model_eval_loop.md, 2026-06-11,
    # n=4 NU/MELI/NOW/BN, dual judge) contradicted that: Sonnet was at-parity-or-
    # better (won 4 / Opus 1 / 3 ties), the Gemini judge preferring Sonnet 3/4
    # and neither judge finding Opus clearly better, often MORE accurate (BN: the
    # correct $145B insurance-asset figure where Opus misstated it). Switched
    # down to Sonnet for ~40% lower cost at equal-or-better quality.
    "company_description": DEFAULT_MODEL,
    # 8-K exhibit segment extraction — copy-the-table-into-JSON from an earnings
    # press-release exhibit (src/provenance/edgar_8k.py), the front-end that
    # auto-populates fact_overrides. Deterministic structured extraction (no
    # judgment), so the cheapest at-parity tier — Haiku — per
    # directives/cheapest_model_routing.md. The extract_8k_overrides golden set
    # (evals/golden/extract_8k_overrides.json) gates any cheaper backend.
    "extract_8k_overrides": FAST_CLASSIFIER_MODEL,
    # Peer selection — the generator behind the §4 peer-comp panel
    # (src/compute/peer_selection.py, directives/peer_selection_llm.md). Proposes
    # the 6-10 best business-model comparables (replacing the FMP sector/cap
    # screen's wrong head). Starts at Sonnet; the peer_selection eval golden set
    # decides Sonnet-vs-Opus empirically rather than by belief. One cached call
    # per ticker on the LLM build — cost bounded.
    "peer_selection": DEFAULT_MODEL,
    # Key-metrics picker preselect (src/compute/key_metrics.py,
    # directives/key_metrics_picker.md). Ranks the metrics MOST important to a
    # ticker over its available extract vocabulary (the DIY picker catalog),
    # business-model aware — surfacing important CAPTURED metrics the tier-1/2
    # KPI grading hasn't reached yet. A closed pick-from-vocabulary task (every
    # returned token must be in the supplied catalog), low-stakes (it only
    # preselects clickable bubbles; the deterministic tier-graded baseline is
    # always shown regardless). Starts at Sonnet like peer_selection — its
    # business-model judgment is the same shape — and is eligible for the
    # eval-gated cheaper-at-parity downgrade once the key_metrics golden set
    # (evals/golden/key_metrics.json) certifies a cheaper tier. One cached call
    # per ticker on the --enable-llm build — cost bounded.
    "key_metrics": DEFAULT_MODEL,
    # DIET-lane podcast takeaway summarizer (S11): replaces short/absent RSS
    # description bodies with a 2-4 sentence investment-relevant briefing.
    # Sonnet: grounded distillation from marketing copy, latency unimportant
    # (batch daily after the RSS poll). Budget-capped at $5/month via 0103.
    "podcast_takeaway_summary": DEFAULT_MODEL,
    # Platform diagram is a narrowly-scoped JSON-output task (one diagram
    # string + one caption string). Sonnet was taking 6-20 min per call and
    # timing out on long 10-Ks; Haiku produces the same shape ~5x faster.
    "platform_diagram": FAST_CLASSIFIER_MODEL,
    "qa_topics": DEFAULT_MODEL,
    "saydo_filter": DEFAULT_MODEL,
    # SayDo commitment extraction — full-transcript (25-50K chars) scan for
    # quantitative forward-looking guidance, constrained to the ticker's KPI
    # catalog (src/compute/say_do_extractor.py). Ran UNGOVERNED via the private
    # _call_claude helper until 2026-07 (~$150/mo of purpose=NULL ledger rows —
    # the repo's largest anonymous cost line). Sonnet is the measured incumbent;
    # a long-context extraction with a closed output schema is a natural
    # downgrade-loop candidate once ledger history accumulates under this key.
    "saydo_commitment_extract": DEFAULT_MODEL,
    # Cross-quarter theme rollup over 4 transcripts, split into prepared /
    # Q&A buckets. Long-context analytical writing → Sonnet matches
    # transcript_summary and pairwise_analysis (the other prompts that
    # ingest several transcripts at once).
    "earnings_themes_split": DEFAULT_MODEL,
    # Valuation multiple selection is a sector/business-model judgment that
    # benefits from Opus's wider sector knowledge (knowing P/TBV is the right
    # bank lens, EV/NTM Revenue for SaaS, P/E for cyclicals, etc.). One call
    # per ticker, cached on disk — cost is bounded.
    "valuation_basis": "claude-opus-4-8",
    # SayDo importance ordering — judgmental sort across many commitments,
    # benefits from Opus's stronger ranking discipline.
    "saydo_importance": "claude-opus-4-8",
    # Auto KPI-registry seeding — proposes which KPIs load-bear the thesis,
    # their polarity, and grounded breaker thresholds with NO human review
    # gate (scratch/seed_kpi_registry.py --auto). The registry decides which
    # alerts fire, so this is a sector/business-model judgment where Opus's
    # wider knowledge and instruction-following materially reduce the two
    # catastrophic failure modes (wrong polarity, ungrounded breaker). One
    # call per ticker, run rarely — cost is bounded. A distinct purpose key
    # also gives portfolio-wide auto-seeding its own budget attribution. The
    # manual --propose purpose (kpi_registry_proposal) stays unregistered ->
    # Sonnet, so the two modes diverge cleanly.
    "kpi_registry_auto_proposal": "claude-opus-4-8",
    # News LLM modules — both on Opus per the news-table plan's explicit
    # instruction. material_news_classification is the material-news trigger's
    # per-headline materiality veto (src/triggers/material_news.py): it was
    # ABSENT here and so silently fell back to Sonnet; pinning it to Opus gives
    # the noise-filtering judgment the wider knowledge and instruction-following
    # it needs (one batched call per ticker per run, cached — cost bounded).
    # news_structuring is the WebSearch->structured-rows fallback extractor
    # (turning free-text news into news-table rows); registered now so it runs
    # on Opus the moment that feed lands. Both use claude-opus-4-8, the current
    # Opus — all the repo's Opus pins use the one id.
    "material_news_classification": "claude-opus-4-8",
    "news_structuring": "claude-opus-4-8",
    # Earnings-tone diff — the earnings_tone trigger's quarter-over-quarter
    # transcript shift detector (src/triggers/earnings_tone.py): it compares the
    # latest call against prior transcripts and emits confidence-scored, cited
    # tone shifts mapped to thesis KPIs. Like material_news_classification it was
    # ABSENT here and silently fell back to Sonnet; this is a high-stakes
    # analytical-judgment task — it decides whether an alert fires AND writes the
    # memo — so Opus's instruction-following and citation discipline matter. One
    # retry-capped call per ticker per run, cached on disk via llm_artifacts, so
    # cost is bounded. claude-opus-4-8, the current Opus (matches the other pins).
    "earnings_tone_diff": "claude-opus-4-8",
    # Recent-developments web brief (generate_recent_developments, via
    # call_llm_with_web). Stays on Sonnet — long-context news synthesis, not a
    # structured-judgment task. Pinned explicitly to DEFAULT_MODEL because
    # call_llm_with_web now resolves its model from purpose: registering this
    # keeps the brief on Sonnet AND silences the unknown-purpose warning.
    "recent_developments": DEFAULT_MODEL,
    # DCF forecast-assumption normalization (execution/improve_dcf_assumptions.py):
    # bottom-up reasoning about where each driver (growth, margins, capex, SBC,
    # working capital) should NORMALIZE over the horizon vs the naive flat-TTM
    # default. A sector/business-model judgment grounded in valuation best
    # practice — Opus's wider knowledge + instruction-following matter. One call
    # per ticker, cached on disk, run rarely — cost bounded. Latest Opus.
    "dcf_assumptions": "claude-opus-4-8",
    # Advisor memos (master build P2.3). The next-dollar memo is the flagship
    # cross-portfolio judgment artifact — same tier as cross_portfolio_synthesis
    # (Opus: wider sector knowledge + instruction-following on "evidence, never
    # directives"). Swap checks are per-pair and run several at a time after a
    # deterministic screen; Sonnet keeps the marginal cost proportionate.
    "advisor_next_dollar": "claude-opus-4-8",
    "advisor_swap_check": DEFAULT_MODEL,
    # Socratic think-through (P2.4): question generation is a short grounded
    # task (Sonnet); the decision memo weighs the owner's answers against the
    # evidence and commits to a scoreable stance — Opus judgment tier.
    "advisor_socratic_questions": DEFAULT_MODEL,
    "advisor_socratic_memo": "claude-opus-4-8",
    # Calibration coach (close_the_loops L8): names the owner's recurring biases
    # and proposes a behavioural experiment from his OWN graded track record —
    # the highest-judgement, lowest-volume call in the repo (monthly + on a
    # decision, off the hot path). Same Opus tier as the advisor memos it sits
    # beside; its output is eval-gated by the calibration_coach mode-B rubric
    # before it ever reaches the owner, so a cheaper model can only be promoted
    # in once that gate confirms parity.
    "calibration_coach": "claude-opus-4-8",
    # Short, structured, batch — Gemini Flash at parity with Haiku, lower cost
    # (Chip 2 PR D first promotions; model-eval cron watches for regression).
    "intake_classifier": "gemini-3-flash-preview",
    "transcript_metadata": "gemini-3-flash-preview",
    "market_signals": FAST_CLASSIFIER_MODEL,
    "patent_timeline": FAST_CLASSIFIER_MODEL,
    # Decision extraction (src/decision_extractor.py): per-paragraph
    # structured extraction of recommendation sentences from five_min_reread
    # lens artifacts — narrow JSON task, batched. Was a hardcoded Haiku
    # model= at the call site; moved here so the registry stays the single
    # reviewable surface (llm_evals_plan.md §5.5).
    "decision_extraction": FAST_CLASSIFIER_MODEL,
    # --- Straggler registrations (2026-07): each of these had an ad-hoc
    # model= at its call site (llm_calls.md rule 3 violation), which ALSO made
    # model_pin_overrides a no-op for them — an explicit model bypasses
    # _model_for, so the downgrade loop could never touch these purposes. The
    # call sites now resolve via this table at the model they were already
    # using; behavior unchanged, purposes now optimizer-eligible.
    # Exec-comp alignment narrative: pay-vs-thesis judgment on the report page.
    "exec_comp_alignment": "claude-opus-4-8",
    # Exec-comp table extraction from proxy text. Was pinned claude-opus-4-7 at
    # the call site — normalized to the repo's one current Opus id (same tier,
    # same price; all Opus pins use claude-opus-4-8).
    "exec_comp_extraction": "claude-opus-4-8",
    # Footnote extraction from 10-K/10-Q text: structured JSON over long
    # filings; Sonnet for recall on dense accounting prose.
    "footnote_extraction": DEFAULT_MODEL,
    # Risk-factor classify/diff (execution/extract_risk_factors.py): narrow
    # closed-enum JSON tasks, batched — Haiku tier.
    "risk_factor_classify": FAST_CLASSIFIER_MODEL,
    "risk_factor_diff": FAST_CLASSIFIER_MODEL,
    # Segment-name canonicalization: narrow JSON mapping task — Haiku tier.
    "canonicalize_segments": FAST_CLASSIFIER_MODEL,
    # Customer-concentration table extraction (src/table_extractors/): short
    # copy-the-table JSON from filing excerpts — Haiku tier.
    "customer_concentration_extraction": FAST_CLASSIFIER_MODEL,
    # Falsifiable-condition extraction (src/decision_conditions.py): turns the
    # "What would change my mind" memo section into structured
    # {metric, op, threshold, unit, for_periods} conditions against a supplied
    # metric vocabulary — a narrow copy-the-token JSON task over ~1-2KB of
    # prose, run once per new decision. Gemini Flash (Chip 2 PR D): same
    # closed-schema pick-from-list shape Flash excels at; golden set at
    # evals/golden/decision_conditions_extract.json guards quality.
    "decision_conditions_extract": "gemini-3-flash-preview",
    # Qualitative "what would change my mind" extraction (L9 PR2): the
    # non-numeric twin of the above — pull event-shaped conditions ("CEO
    # departs", "competitor enters") + a news/earnings routing tag from the same
    # prose. Same narrow, closed-vocab JSON shape, run once per new decision →
    # the same cheap Gemini Flash pick.
    "qualitative_conditions_extract": "gemini-3-flash-preview",
    # NL → ViewSpec compile (master build P5.2): the Explore panel's query
    # box. Narrowly-scoped JSON-output against a supplied metric vocabulary,
    # interactive (the owner is waiting at the input) — latency dominates.
    # Gemini Flash (Chip 2 PR D): copy-the-token task, golden set at
    # evals/golden/viewspec_compile.json guards quality; repair retry in
    # viewspec.nl_compile covers the degraded case.
    "viewspec_compile": "gemini-3-flash-preview",
    # Ask pack router (src/ask/router.py, fund-grade build S4): selects which
    # portfolio evidence packs (holdings / conviction / dcf / decisions /
    # journal / performance) a narrative ask turn needs. Closed-enum JSON
    # selection on the interactive ask path — latency dominates.
    # Gemini Flash (Chip 2 PR D): pick-from-a-list task, mis-selection fails
    # closed to document-only evidence. Scored by evals/golden/ask_pack_router.json;
    # budget row seeded by alembic 0089 (skip mode).
    "ask_pack_router": "gemini-3-flash-preview",
    # Ask claim-grounding audit (src/ask/claims.py, fund-grade build S8):
    # after a grounded narrative answer streams, one short call re-reads the
    # answer against its numbered evidence and emits the claims→cites map
    # (which sentences are factual claims, which evidence backs each).
    # Copy-the-sentence + pick-from-a-list over a few KB — Haiku-shaped, and
    # the call sits on the interactive ask path AFTER the final text, so
    # latency only delays the citation chips, never the answer. Mis-grading
    # fails closed to answer-level citations (the pre-S8 behavior); budget
    # row seeded by alembic 0090 (skip mode).
    "ask_claim_grounding": FAST_CLASSIFIER_MODEL,
    # Ask conversational answer (close_the_loops L3): the PRIMARY pass-1
    # narrative answer of the ask path — both the report-drawer chat and the
    # Ask tab's portfolio scope stream through chat_session.stream_llm_text.
    # It is the most expensive + highest-stakes LLM call in the repo, yet it
    # historically rode the bare `claude -p` CLI default: no purpose, no
    # budget, no ledger row, invisible to the model-downgrade loop. Pinned here
    # so it resolves through _model_for like every other purpose — eligible for
    # the eval-gated downgrade / Gemini-promotion loop, attributed in the
    # ledger + Call Health, and bounded by a budget row (alembic 0104, soft /
    # warn: an interactive answer must NEVER be hard-blocked mid-conversation).
    # Stays on the Sonnet chat tier by default for the same reason
    # ask_evidence_followup does (a downgraded conversational answer is visibly
    # worse); the ask_advisory_answer mode-B eval is the quality gate before
    # any cheaper model is promoted in.
    "ask_answer": DEFAULT_MODEL,
    # Ask evidence follow-up (src/ask/followup.py, fund-grade build S7): when
    # a narrative ask turn's first pass replies with a structured evidence
    # request instead of an answer, the engine retrieves the requested items
    # and makes this call (≤2 per turn) over the augmented evidence. It IS
    # the user-facing narrative answer (pass 2 of ask_answer), so it stays on
    # the Sonnet chat tier — downgrading it would make looped answers visibly
    # worse than one-shot ones. Budget row seeded by alembic 0091 (skip mode —
    # a blown cap disables the loop, turns fall back to one-shot retrieval).
    "ask_evidence_followup": DEFAULT_MODEL,
    # Pairwise backend judge (src/llm/backend_judge.py): grades Claude-vs-Gemini
    # paired outputs to decide whether a purpose may join the eval-gated Gemini
    # allowlist. Opus on purpose — the judge must out-discriminate BOTH
    # contestants (Sonnet-tier Claude, Gemini Pro), and the corpus is tiny + run
    # off the hot path, so the judgment-tier cost is immaterial. The Gemini-side
    # judge resolves to Pro via gemini_model_for (not a fast-classifier purpose).
    "backend_compare_judge": "claude-opus-4-8",
    # Bear-case outcome grading (src/bear_case_grader.py): judges whether a
    # past failure-mode hypothesis materialized against realized KPI data —
    # a judgment call over a small evidence table, run weekly per due
    # hypothesis. Was a hardcoded Sonnet model= at the call site; moved here
    # so the registry stays the single reviewable surface (llm_evals_plan.md
    # §5.5).
    "bear_case_grading": DEFAULT_MODEL,
    # Eval-harness judge (src/evals/judge.py + rubric_judge.py,
    # directives/llm_evals_plan.md): mode A — decides whether a model output
    # that DIVERGES from a golden expectation is still analytically
    # equivalent; mode B — scores production prose facet-by-facet against a
    # versioned rubric. Deliberately Haiku — the verdict is a narrow
    # schema-bound JSON object, the harness fails CLOSED on bad verdicts,
    # and judge volume must stay cheap enough to run on every prompt change
    # (cost model in the directive). Escalate per-purpose via
    # AuditSpec.judge_model in the grader config (not here) if agreement
    # spot-checks (execution/spot_check_eval_judge.py) come back weak.
    "eval_judge": FAST_CLASSIFIER_MODEL,
    # NOT here by design: the 14 dynamic `lens:<name>` purposes (plus the
    # scenario-suffixed lens:macro_scenario:<id> / lens:portfolio_macro_stress:<id>)
    # resolve their model from the Lens object itself
    # (src/synthesis/lenses/_shared.py, `Lens.model`) and pass it explicitly,
    # so they never hit the unknown-purpose fallback. The lens table stays
    # the reviewable surface for those (llm_evals_plan.md §5.5 decision).
}


def _model_for(purpose: str) -> str:
    """Resolve a purpose key to a model id.

    Resolution order (first match wins):
      1. Active ``model_pin_overrides`` row in the DB — set by the
         auto-switch loop when a cheaper model holds sustained parity.
         Fail-safe: any DB error falls through silently to the code pin.
      2. ``LLM_MODELS`` hardcoded pin (the default code-time choice).
      3. ``DEFAULT_MODEL`` fallback for unknown purposes (logged as a warning
         so the gap surfaces in observability).
    """
    # 1. DB-backed override (the auto-switch loop writes here).
    try:
        from llm.model_overrides import active_override

        override = active_override(purpose)
        if override is not None:
            log.info(
                {
                    "event": "llm_model_pin_override_active",
                    "purpose": purpose,
                    "model": override,
                }
            )
            return override
    except Exception as exc:
        log.debug(
            {
                "event": "llm_model_override_check_failed",
                "purpose": purpose,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )

    # 2. Code pin.
    model = LLM_MODELS.get(purpose)
    if model is None:
        log.warning(
            {
                "event": "llm_model_purpose_unknown",
                "purpose": purpose,
                "fallback": DEFAULT_MODEL,
            }
        )
        return DEFAULT_MODEL
    return model


# Default per-call timeout (seconds). Long-context thesis prompts can take
# a few minutes on Sonnet; the cap protects against runaway hangs. 20 min
# leaves headroom for the heaviest cases (4-quarter ticker x dense schema)
# while still catching CLI hangs in a reasonable wall time. Override via the
# CLAUDE_CLI_TIMEOUT_SECONDS env var for the rare mega-cap big-prompt names
# (e.g. COST/TSM/MSFT-class bear_case) whose generation legitimately exceeds
# 1200s; unset, behavior is identical to the prior hardcoded default.
DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("CLAUDE_CLI_TIMEOUT_SECONDS", "1200"))

# Web-search-enabled call: same subprocess as _call_claude but with the
# Claude CLI's --allowedTools flag turned on so the model can run WebSearch
# / WebFetch as part of producing its answer. Used by the memo generator
# for the "Recent Developments" section so memos cite real news URLs
# instead of leaning on a stale FMP news pre-pull.
CLAUDE_WEB_TOOLS = "WebSearch WebFetch"
CLAUDE_WEB_TIMEOUT_SECONDS = 1800  # web fetches add round-trips; bigger cap

# HARD per-call cost ceiling for a web-enabled call, enforced by the CLI's
# --max-budget-usd flag (terminates the call once API spend hits it). The prompt
# tells the model "AT MOST 2 web_search queries" etc., but that is advisory —
# nothing stopped a model that ignored it from issuing unbounded searches/fetches
# on the most expensive LLM path (the regrade memo's "soft caps" gap). A
# well-behaved call (≤2 searches + a few fetches) costs a few cents even on Opus,
# so $2 only bites a genuinely runaway call. Overridable via env for tuning.
CLAUDE_WEB_MAX_BUDGET_USD = float(os.environ.get("CLAUDE_WEB_MAX_BUDGET_USD", "2.0"))


class LLMBudgetExceeded(RuntimeError):  # noqa: N818
    """Raised by `_call_claude` when the per-purpose monthly cap is at/over
    AND the budget row has hard_block=True. Callers can catch this to
    degrade gracefully (skip the section, write a stub, queue for next
    month). Soft caps do NOT raise — they log a warning and the call
    proceeds. See src/llm_budget.py for the enforcement details.

    Attaches the failing BudgetCheck so structured callers can surface
    spend / cap / headroom without re-running the check.
    """

    def __init__(self, message: str, *, check: object | None = None) -> None:
        super().__init__(message)
        self.check = check


class LLMSetupError(RuntimeError):
    """Raised when the Claude CLI cannot be located (binary not installed /
    not on PATH).

    Distinct from a transient operational failure: a missing binary is a
    DETERMINISTIC, operator-actionable setup problem that affects EVERY LLM
    call identically, so re-running won't help. Section-level callers must let
    this PROPAGATE (fail the build loudly with the install hint) rather than
    degrade one section to a "transient, re-run" banner that would be a lie.

    Subclasses ``RuntimeError`` so the pre-existing ``except RuntimeError`` /
    ``pytest.raises(RuntimeError)`` call sites that caught the old bare
    RuntimeError keep working unchanged.
    """


def is_hard_stop(exc: BaseException) -> bool:
    """Classify an LLM-call exception as a HARD STOP (must propagate) vs a
    transient failure (a section may degrade and a re-run retries).

    Single source of truth for the section-degradation policy — every report
    section that wraps an LLM call routes its ``except`` through this so the
    "what's non-degradable" taxonomy lives in one place.

    Hard stops (return True → propagate, fail the whole build loudly):
      * ``LLMBudgetExceeded`` — a hard per-purpose monthly cap. Degrading would
        silently mask that spend is over budget; the operator must raise the
        cap or wait for the reset.
      * ``LLMSetupError`` — the ``claude`` CLI isn't installed / resolvable.
        Deterministic and re-run-proof; fail loudly with the install hint.

    Everything else (return False → degrade the affected section): subprocess
    timeouts, non-zero exits, both Claude + Gemini momentarily unavailable,
    empty / unparseable completions. One flaky call shouldn't nuke every other
    section + the render.
    """
    return isinstance(exc, (LLMBudgetExceeded, LLMSetupError))


def _verify_setup_once() -> None:
    """Resolve and cache the absolute path to the ``claude`` binary on first call.

    Windows-specific: bare ``"claude"`` fails because the npm-installed binary
    is ``claude.cmd`` and Python's subprocess doesn't apply PATHEXT to bare
    names. Cached so repeat calls in a long-running batch are free.

    State (``_setup_verified`` / ``_claude_cli_path``) lives on the
    ``llm_client`` module so the existing test monkeypatch surface keeps
    working without test changes; see this module's docstring.
    """
    import llm_client  # late import — breaks circular at import time

    if llm_client._setup_verified:
        return
    resolved = shutil.which("claude")
    if resolved is None:
        raise LLMSetupError(
            "Claude Code CLI ('claude') not found in PATH. Install it from "
            "https://code.claude.com/docs/en/setup, then either set "
            "ANTHROPIC_API_KEY in your shell / .env or run `claude auth login`."
        )
    llm_client._claude_cli_path = resolved
    llm_client._setup_verified = True


def _enforce_budget_pre_call(purpose: str | None, *, force_budget_bypass: bool) -> None:
    """Pre-call hook: consult llm_budget.check_budget for `purpose` and:

      * raise LLMBudgetExceeded when over a hard-block cap,
      * log a warning + proceed when over a soft cap,
      * log a warning + record a one-shot alert at the 80% threshold.

    Best-effort throughout — any unexpected error in the budget module
    is swallowed (we'd rather over-spend by one call than block the
    pipeline because of a budget bug). `force_budget_bypass=True` skips
    the check entirely for CLI tools that need to override.
    """
    if force_budget_bypass or purpose is None:
        return
    try:
        from llm_budget import check_budget, record_alert

        check = check_budget(purpose)
    except Exception as exc:
        log.debug(
            {
                "event": "llm_budget_check_skipped",
                "purpose": purpose,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return
    if not check.allowed:
        if check.hard_block:
            log.warning(
                {
                    "event": "llm_budget_hard_block",
                    "purpose": purpose,
                    "spend_usd": str(check.current_spend),
                    "cap_usd": str(check.cap),
                    "reason": check.reason,
                }
            )
            raise LLMBudgetExceeded(check.reason or f"{purpose}: monthly cap exceeded", check=check)
        log.warning(
            {
                "event": "llm_budget_soft_cap_exceeded",
                "purpose": purpose,
                "spend_usd": str(check.current_spend),
                "cap_usd": str(check.cap),
                "reason": check.reason,
            }
        )
        try:
            record_alert(purpose, 1.0, check.current_spend)
        except Exception:
            # Alerting is best-effort and must never break the budget-check
            # path — degrade, but don't swallow silently.
            log.warning(
                {"event": "llm_budget_alert_record_failed", "purpose": purpose, "level": "hard"},
                exc_info=True,
            )
    if check.warn:
        log.warning(
            {
                "event": "llm_budget_warn_threshold",
                "purpose": purpose,
                "spend_usd": str(check.current_spend),
                "cap_usd": str(check.cap),
                "headroom_pct": check.headroom_pct,
                "reason": check.reason,
            }
        )
        try:
            record_alert(purpose, 0.80, check.current_spend)
        except Exception:
            # Best-effort alerting (see above) — log and continue.
            log.warning(
                {"event": "llm_budget_alert_record_failed", "purpose": purpose, "level": "warn"},
                exc_info=True,
            )


def _call_claude(
    prompt: str,
    model: str = DEFAULT_MODEL,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    *,
    purpose: str | None = None,
    ticker: str | None = None,
    scope: str | None = None,
    run_id: str | None = None,
    force_budget_bypass: bool = False,
) -> str:
    """
    Single-shot LLM call. Tries the Claude Code CLI first. On any operational
    failure — timeout, non-zero exit, empty output, malformed JSON envelope,
    or the binary becoming unavailable mid-run — falls back to Gemini Flash if
    ``GEMINI_API_KEY`` is available in the environment.

    Setup errors (``claude`` binary missing on first call) raise
    ``LLMSetupError`` (a RuntimeError subclass) directly without invoking the
    fallback — that needs to be fixed by the operator, not papered over.
    ``is_hard_stop`` classifies it (and ``LLMBudgetExceeded``) as a hard stop
    so section-level callers propagate rather than degrade.

    Prompts are passed via stdin to avoid Windows CreateProcess command-line
    length limits (32K). Output is ``--output-format json`` so the wrapper can
    capture token usage + Anthropic-computed cost for the llm_calls ledger.

    The optional ``purpose``/``ticker``/``scope``/``run_id`` arguments are
    pass-through metadata for the ledger — they have no effect on the LLM call
    itself but enable cost-attribution queries downstream.

    Pre-call budget enforcement: when ``purpose`` is set, consults
    ``llm_budget.check_budget`` and raises ``LLMBudgetExceeded`` if the
    per-purpose monthly cap is at/over with hard_block=True. Pass
    ``force_budget_bypass=True`` to skip the check (CLI escape hatch).
    """
    _enforce_budget_pre_call(purpose, force_budget_bypass=force_budget_bypass)
    _verify_setup_once()  # setup errors propagate; do NOT route to fallback
    import llm_client  # late import — state lives on llm_client for test compat

    assert (
        llm_client._claude_cli_path is not None
    )  # set by _verify_setup_once when it returns successfully
    log.info(
        {
            "event": "llm_call_start",
            "model": model,
            "prompt_chars": len(prompt),
            "purpose": purpose,
        }
    )

    from llm_call_ledger import parse_claude_json_output, sha256_text

    prompt_sha = sha256_text(prompt)
    started_at = datetime.now(UTC)
    t0 = time.monotonic()
    try:
        result = subprocess.run(
            # --no-session-persistence: every call is a fresh `-p` subprocess and
            # NOTHING in this repo ever --resume/--continue-s, so writing a session
            # transcript to ~/.claude per call is pure waste — disabling it drops
            # that disk write (and stops the session files accumulating) with zero
            # behavioral change. (L14 latency: native prompt caching is unreachable
            # via `-p`, so transport hygiene like this is the only CLI-side lever
            # that's safe under subscription billing — `--bare` would force
            # ANTHROPIC_API_KEY billing and was rejected.)
            [
                llm_client._claude_cli_path,
                "-p",
                "--model",
                model,
                "--output-format",
                "json",
                "--no-session-persistence",
            ],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",  # Force UTF-8 — Windows otherwise defaults to cp1252 which dies on
            errors="replace",  # common financial-doc Unicode (U+2212 minus, en/em dashes, arrows).
            check=True,
            timeout=timeout_seconds,
            cwd=_neutral_subprocess_cwd(),  # avoid booting the project's MCP servers (hangs)
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        # Parse the JSON envelope. ValueError when malformed → caught below
        # and routed through the Gemini fallback (same as a CLI failure).
        text, meta = parse_claude_json_output(result.stdout.strip())
        text = text.strip()
        if not text:
            raise RuntimeError(
                f"claude -p returned empty `result`. stderr: {result.stderr.strip()[:200]}"
            )
        log.info({"event": "llm_call_done", "response_chars": len(text)})
        record_llm_call(
            started_at=started_at,
            elapsed_ms=elapsed_ms,
            model=model,
            prompt_sha=prompt_sha,
            prompt_chars=len(prompt),
            purpose=purpose,
            ticker=ticker,
            scope=scope,
            run_id=run_id,
            response_text=text,
            meta=meta,
        )
        # Opt-in full-text capture (off unless LLM_CAPTURE_DIR is set) — the
        # corpus source for execution/compare_backends.py --from-capture.
        capture_exchange(
            prompt=prompt,
            response=text,
            purpose=purpose,
            ticker=ticker,
            scope=scope,
            model=model,
            run_id=run_id,
        )
        return text
    except (subprocess.SubprocessError, OSError, RuntimeError, ValueError) as claude_error:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        record_llm_call(
            started_at=started_at,
            elapsed_ms=elapsed_ms,
            model=model,
            prompt_sha=prompt_sha,
            prompt_chars=len(prompt),
            purpose=purpose,
            ticker=ticker,
            scope=scope,
            run_id=run_id,
            error=f"{type(claude_error).__name__}: {str(claude_error)[:500]}",
        )
        # Operational failure — try Gemini fallback. fallback_call_logged raises
        # if no Gemini key is configured, surfacing both errors together. The
        # fallback writes its own ledger row tagged fallback_used='gemini'.
        return fallback_call_logged(
            prompt,
            claude_error,
            prompt_sha=prompt_sha,
            purpose=purpose,
            ticker=ticker,
            scope=scope,
            run_id=run_id,
        )


def call_llm(
    prompt: str,
    *,
    purpose: str | None = None,
    model: str | None = None,
    timeout_seconds: int | None = None,
    ticker: str | None = None,
    scope: str | None = None,
    run_id: str | None = None,
    force_budget_bypass: bool = False,
    backend: str | None = None,
) -> str:
    """Public single-shot LLM call. CANONICAL entry point for ALL LLM calls in
    this repo — including from `execution/` scripts, `src/report/sections/`, and
    anywhere else that needs a Claude-then-Gemini-fallback round-trip.

    Direct use of `google.generativeai`, the `anthropic` SDK, or any other
    provider client is forbidden outside this module's fallback wiring; route
    through call_llm so retunes (model swap, timeout change, billing change,
    fallback policy) happen in one place.

    Backend resolution (model-first: the resolved model id determines the backend):
      * ``backend="claude"`` / ``backend="gemini"`` — explicit force. The
        compare harness (execution/compare_backends.py) uses this; a forced
        Gemini call that fails raises rather than silently switching, because
        the caller asked for THAT backend's answer.
      * ``backend=None`` (default) — dispatches by the model family (``family_of``
        from ``llm.model_ladder``): a Gemini model id → Gemini backend, a Claude
        model id → Claude backend. Set a Gemini id in ``LLM_MODELS`` or
        ``model_pin_overrides`` to route a purpose to Gemini. A Gemini-backend
        call that fails OPERATIONALLY degrades to Claude so a model swap can
        never break the pipeline; setup and budget errors propagate per
        ``is_hard_stop``.

    Args:
        prompt: The fully-rendered prompt text.
        purpose: Logical key for model selection (see LLM_MODELS). Required
            for new code; the explicit `model` arg overrides it when both
            are passed (escape hatch for one-off retunes during debugging).
        model: Explicit model id — a Claude id, or a Gemini id when paired
            with ``backend="gemini"``. If neither purpose nor model is set,
            falls back to DEFAULT_MODEL with a warning log. An explicit
            model with no explicit backend always runs Claude (the allowlist
            only reroutes purpose-resolved calls).
        timeout_seconds: Per-call timeout. None = the backend's default
            (DEFAULT_TIMEOUT_SECONDS / GEMINI_BACKEND_TIMEOUT_SECONDS).
        ticker: Optional ticker for ledger attribution. Set when the call is
            scoped to a single name; helps cost queries break out by ticker.
        scope: Optional analytical scope for the ledger (e.g. 'portfolio',
            'segment:cloud'). Free-form; aggregated in the spend report.
        run_id: Optional grouping key — typically a uuid4 hex per logical
            refresh (one build_artifacts invocation, one daily cron) so the
            spend report can show "this run cost $X across N calls".
        force_budget_bypass: When True, skip the per-purpose budget check
            entirely. Use sparingly — CLI tools that need to force a refresh
            past a hard cap should pass this. Soft caps log+proceed anyway,
            so this is only meaningful when the cap is hard-blocked.
        backend: None (resolve from model family), "claude", or "gemini".
    """
    if backend not in (None, "claude", "gemini"):
        raise ValueError(f"Unknown LLM backend {backend!r}: expected 'claude' or 'gemini'.")

    from llm.model_ladder import GEMINI as _GEMINI_FAMILY  # late — avoids import cycle
    from llm.model_ladder import family_of

    # Model-first: resolve the intended model (DB pin → LLM_MODELS → DEFAULT).
    if model is None:
        if purpose is None:
            log.warning({"event": "llm_call_no_purpose", "fallback": DEFAULT_MODEL})
            resolved_model = DEFAULT_MODEL
        else:
            resolved_model = _model_for(purpose)
    else:
        resolved_model = model

    # Backend from the resolved model's family; explicit `backend` arg overrides.
    resolved_backend = backend or (
        "gemini" if family_of(resolved_model) == _GEMINI_FAMILY else "claude"
    )

    if resolved_backend == "gemini":
        from llm.gemini_backend import call_gemini  # late — avoids import cycle

        try:
            return call_gemini(
                prompt,
                model=resolved_model,
                timeout_seconds=timeout_seconds,
                purpose=purpose,
                ticker=ticker,
                scope=scope,
                run_id=run_id,
                force_budget_bypass=force_budget_bypass,
            )
        except (LLMBudgetExceeded, LLMSetupError):
            raise  # hard stops — never paper over with a backend switch
        except (subprocess.SubprocessError, OSError, RuntimeError, ValueError) as gemini_error:
            if backend == "gemini":
                raise  # explicitly forced: the caller wants Gemini's answer or its error
            log.warning(
                {
                    "event": "gemini_backend_failed_falling_back_to_claude",
                    "purpose": purpose,
                    "error": f"{type(gemini_error).__name__}: {str(gemini_error)[:200]}",
                }
            )
            # fall through to the Claude path below (its own ledger rows)

    # Claude path. If resolved_model is a Gemini ID (purpose pinned to Gemini but
    # backend fell back), re-resolve a Claude model. Guard against Gemini IDs in
    # LLM_MODELS (post-promotion) by falling back to DEFAULT_MODEL.
    if family_of(resolved_model) == _GEMINI_FAMILY:
        candidate = LLM_MODELS.get(purpose, DEFAULT_MODEL) if purpose is not None else DEFAULT_MODEL
        resolved_model = candidate if family_of(candidate) != _GEMINI_FAMILY else DEFAULT_MODEL

    return _call_claude(
        prompt,
        model=resolved_model,
        timeout_seconds=timeout_seconds or DEFAULT_TIMEOUT_SECONDS,
        purpose=purpose,
        ticker=ticker,
        scope=scope,
        run_id=run_id,
        force_budget_bypass=force_budget_bypass,
    )


def call_llm_with_web(
    prompt: str,
    model: str | None = None,
    timeout_seconds: int = CLAUDE_WEB_TIMEOUT_SECONDS,
    *,
    purpose: str | None = None,
    ticker: str | None = None,
    scope: str | None = None,
    run_id: str | None = None,
    force_budget_bypass: bool = False,
    max_budget_usd: float | None = None,
) -> str:
    """LLM call with Claude WebSearch + WebFetch tools enabled.

    Setup invariants are the same as `_call_claude` (subscription billing
    via the CLI, UTF-8, stdin prompt, JSON output for ledger capture). On
    Claude failure, falls through to plain `_call_claude` (which has its
    own Gemini fallback) so a memo is always produced even when web tools
    are unavailable.

    Use for memo generation, fact-finding on recent news, anything where
    the upstream context is stale and Claude needs to look something up.

    Same per-purpose budget enforcement as `_call_claude`; pass
    ``force_budget_bypass=True`` to skip the check.

    ``max_budget_usd`` sets the per-run agentic $-cap. It can only LOWER the hard
    module ceiling (``CLAUDE_WEB_MAX_BUDGET_USD``), never raise it — a tiered
    caller passes its budget here and the run is clamped to ``min(requested,
    ceiling)``. This is the S2 invariant: no caller can spend above the structural
    maximum, whatever budget it asks for.

    Model selection mirrors ``call_llm``: pass an explicit ``model`` to force
    one, or leave it ``None`` (the default) to resolve from ``purpose`` via
    ``LLM_MODELS`` / ``_model_for``; with neither set it falls back to
    ``DEFAULT_MODEL`` with a warning. This historically hard-defaulted to
    ``DEFAULT_MODEL`` and ignored ``purpose`` — resolving from purpose lets
    web-enabled callers (e.g. the news structurer) be retuned centrally in
    ``LLM_MODELS``. Callers passing an explicit ``model`` are unaffected.
    """
    if model is None:
        if purpose is None:
            log.warning({"event": "llm_web_call_no_purpose", "fallback": DEFAULT_MODEL})
            resolved_model = DEFAULT_MODEL
        else:
            resolved_model = _model_for(purpose)
    else:
        resolved_model = model
    _enforce_budget_pre_call(purpose, force_budget_bypass=force_budget_bypass)
    _verify_setup_once()
    import llm_client  # late import — state lives on llm_client for test compat

    assert llm_client._claude_cli_path is not None
    log.info(
        {
            "event": "llm_web_call_start",
            "model": resolved_model,
            "prompt_chars": len(prompt),
            "purpose": purpose,
        }
    )

    from llm_call_ledger import parse_claude_json_output, sha256_text

    prompt_sha = sha256_text(prompt)
    started_at = datetime.now(UTC)
    t0 = time.monotonic()
    # A caller may LOWER the per-run ceiling (the tiered research budget), never
    # raise it above the hard module maximum — clamped to [0.01, ceiling].
    effective_budget_usd = CLAUDE_WEB_MAX_BUDGET_USD
    if max_budget_usd is not None:
        effective_budget_usd = min(max(max_budget_usd, 0.01), CLAUDE_WEB_MAX_BUDGET_USD)
    cmd = [
        llm_client._claude_cli_path,
        "-p",
        "--model",
        resolved_model,
        "--output-format",
        "json",
        # See _call_claude: nothing resumes, so persisting the session is waste.
        "--no-session-persistence",
        "--allowedTools",
        *CLAUDE_WEB_TOOLS.split(),
        # Hard cost ceiling so the web path (the only agentic, multi-tool call)
        # cannot run away on cost if the model ignores the prompt's advisory
        # "AT MOST 2 searches" budget. Degrades like any other web-call failure.
        "--max-budget-usd",
        str(effective_budget_usd),
    ]
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            timeout=timeout_seconds,
            cwd=_neutral_subprocess_cwd(),  # avoid booting the project's MCP servers (hangs); WebSearch/WebFetch are built-in tools and don't need .mcp.json
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        text, meta = parse_claude_json_output(result.stdout.strip())
        text = text.strip()
        if not text:
            raise RuntimeError(
                f"claude -p with web tools returned empty `result`. stderr: {result.stderr.strip()[:200]}"
            )
        log.info({"event": "llm_web_call_done", "response_chars": len(text)})
        record_llm_call(
            started_at=started_at,
            elapsed_ms=elapsed_ms,
            model=resolved_model,
            prompt_sha=prompt_sha,
            prompt_chars=len(prompt),
            purpose=purpose,
            ticker=ticker,
            scope=scope or "web",
            run_id=run_id,
            response_text=text,
            meta=meta,
        )
        # Opt-in full-text capture (see _call_claude). scope tags it as a web
        # call so the replay step can flag web-grounded purposes (Gemini has no
        # web tools, so those comparisons are structurally Claude-favored).
        capture_exchange(
            prompt=prompt,
            response=text,
            purpose=purpose,
            ticker=ticker,
            scope=scope or "web",
            model=resolved_model,
            run_id=run_id,
        )
        return text
    except (subprocess.SubprocessError, OSError, RuntimeError, ValueError) as web_err:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        record_llm_call(
            started_at=started_at,
            elapsed_ms=elapsed_ms,
            model=resolved_model,
            prompt_sha=prompt_sha,
            prompt_chars=len(prompt),
            purpose=purpose,
            ticker=ticker,
            scope=scope or "web",
            run_id=run_id,
            error=f"{type(web_err).__name__}: {str(web_err)[:500]}",
        )
        log.warning(
            {
                "event": "llm_web_call_fallback_to_plain",
                "error": f"{type(web_err).__name__}: {web_err}",
            }
        )
        # Fall through to non-web path so the caller still gets output. The
        # plain _call_claude path records its own ledger row(s).
        return _call_claude(
            prompt,
            model=resolved_model,
            timeout_seconds=timeout_seconds,
            purpose=purpose,
            ticker=ticker,
            scope=scope,
            run_id=run_id,
        )
