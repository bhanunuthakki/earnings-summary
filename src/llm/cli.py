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
Governed subscription-LLM routing + the public ``call_llm`` /
``call_llm_with_web`` entry points + per-purpose budget enforcement.

Primary path: the isolated Codex membership transport. Purpose-resolved
Haiku/Sonnet/Opus-class pins map to Luna/Terra/Sol respectively. Operational
Codex failures fall back to the Claude subscription transport and are ledgered
as such; ``LLM_PRIMARY_SUBSCRIPTION_BACKEND=claude`` is the reversible rollback
switch. Explicit provider-family model ids remain on their requested family.

``call_llm_with_web`` is Codex-first too: the same primary/backup order as
``call_llm``, with the Codex leg opting into the membership wrapper's
``web_search="live"`` mode so it can fetch fresh pages. Falls back to the
existing Claude WebSearch/WebFetch tool-call path on an OPERATIONAL Codex
failure only — never as a routing preference (2026-08-03 owner ratification;
see directives/llm_calls.md and procedures/llm-ops.TRANSPORTS.md).

Second backend: ``src/llm/gemini_backend.py`` calls the Gemini Developer API
directly (metered key) with the same call contract. ``call_llm`` routes a
purpose there when its resolved model is a Gemini model id — model-first
dispatch, see directives/cheapest_model_routing.md — or when a caller forces
``backend="gemini"`` explicitly (the compare harness). A purpose only
resolves to a Gemini model after the LLM-evals judges grade its output
quality first; see directives/gemini_backend.md.

Public API:
    DEFAULT_MODEL, FAST_CLASSIFIER_MODEL — canonical model ids.
    LLM_MODELS — per-purpose model selection table.
    DEFAULT_TIMEOUT_SECONDS, CLAUDE_WEB_TIMEOUT_SECONDS, CLAUDE_WEB_TOOLS.
    LLMBudgetExceeded — raised when a hard per-purpose monthly cap is over.
    call_llm(...) — single-shot LLM call. Canonical entry point.
    call_llm_with_web(...) — same, web-grounded: Codex `web_search="live"`
        first, Claude WebSearch/WebFetch tools as the operational fallback.

Note on module-level state: ``_setup_verified`` and ``_claude_cli_path`` are
intentionally kept as live globals in ``src/llm_client.py`` (read via late
``import llm_client`` inside the functions below) so the existing test
monkeypatch surface — ``monkeypatch.setattr(llm_client, "_setup_verified",
True)`` — continues to work without test modification.

Extracted from src/llm_client.py during the llm subpackage split (PURE
refactor — zero behavior change).
"""

from __future__ import annotations

import json
import logging
import os
import random
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from llm.capture import capture_exchange
from llm.ledger import record_llm_call
from llm.transport import (
    FailureInfo,
    LLMQuotaExhausted,
    classify_cli_failure,
    clear_quota_block,
    quota_block_active,
    record_quota_exhausted,
    retry_budget,
)

# Backoff base for transient-class retries (llm.transport.retry_budget owns the
# per-class attempt counts). attempt 1 → ~3s, attempt 2 → ~6s, + up to 1s jitter.
_RETRY_BASE_SECONDS = float(os.environ.get("LLM_RETRY_BASE_S", "3"))

# Tier three is billed.  Adding a purpose here is an explicit operational
# approval; the default is to fail loudly after both subscription transports.
OPENROUTER_FALLBACK_PURPOSES: frozenset[str] = frozenset()

log = logging.getLogger(__name__)

_STREAM_TIMEOUT_SECONDS = 120.0
_PROCESS_TERMINATE_GRACE_SECONDS = 2.0

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


# Set to reroute THIS app's `claude -p` transport to a non-default endpoint.
# The inherited ANTHROPIC_BASE_URL is deliberately NOT honored — see
# _subprocess_env.
ES_CLAUDE_BASE_URL_VAR = "ES_CLAUDE_BASE_URL"

_stripped_base_url_logged = False


def _subprocess_env() -> dict[str, str]:
    """Sanitized environment for the `claude -p` subprocess.

    Strips an inherited ``ANTHROPIC_BASE_URL``: 2026-07 incident — a
    machine-level (User-scope) ``ANTHROPIC_BASE_URL=http://127.0.0.1:8787``
    left behind by the interactive Headroom-proxy wrapper silently rerouted
    every scheduled pipeline's CLI call to a proxy that was only running
    during interactive sessions; ~10 days of ~95% transport failure across
    all purposes (llm_calls 07-03..07-13), invisible because the ledger then
    recorded only exit codes. The same leak recurs process-scoped whenever
    the dashboard/poller is launched from inside a `ch` session, so cwd-style
    neutrality (see _neutral_subprocess_cwd) is applied to the env too:
    rerouting this app's transport must be an explicit app-level decision via
    ``ES_CLAUDE_BASE_URL``, never an ambient inheritance.
    """
    global _stripped_base_url_logged
    env = dict(os.environ)
    inherited = env.pop("ANTHROPIC_BASE_URL", None)
    explicit = env.get(ES_CLAUDE_BASE_URL_VAR)
    if explicit:
        env["ANTHROPIC_BASE_URL"] = explicit
    elif inherited and not _stripped_base_url_logged:
        _stripped_base_url_logged = True
        log.warning(
            {
                "event": "llm_env_base_url_stripped",
                "inherited": inherited,
                "hint": f"set {ES_CLAUDE_BASE_URL_VAR} to reroute this app deliberately",
            }
        )
    return env


# Default Claude model for prompt calls. Sonnet 4.6 chosen as a balance of
# quality and speed across the pipeline's tasks. Per-function overrides via
# the `model` argument on _call_claude or by adding the purpose to LLM_MODELS.
DEFAULT_MODEL = "claude-sonnet-4-6"

# Fast classifier model — used for short, structured calls (intake doc-type
# classification, transcript metadata extraction, batch extractors) where
# Sonnet would be overkill. Haiku 4.5 returns ~5x faster at materially the
# same quality on narrowly-scoped JSON-output tasks.
FAST_CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"

PRIMARY_SUBSCRIPTION_BACKEND_ENV_VAR = "LLM_PRIMARY_SUBSCRIPTION_BACKEND"
_PRIMARY_CODEX = "codex"
_PRIMARY_CLAUDE = "claude"
_CODEX_FAST_MODEL = "gpt-5.6-luna"
_CODEX_DEFAULT_MODEL = "gpt-5.6-terra"
_CODEX_JUDGMENT_MODEL = "gpt-5.6-sol"

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
    # The Worldview distiller (P2). Distils owner-flagged (incorporated/saved)
    # musings into candidate Tenets — beliefs about HOW the owner invests — each
    # cited to the musings it rests on (deterministically validated before any
    # Tenet is proposed). Analytical synthesis-with-citations, batch/optional +
    # owner-tapped (never automatic), budget-capped skip-mode (0132) → Sonnet.
    "tenet_distill": DEFAULT_MODEL,
    # The session-distill tap (B4 keystone, 2026-07-19 program overhaul). One
    # conversation (a thin Ask thread or a bridged deep Claude session) in,
    # at most 5 deterministically-grounded candidates out — the same
    # analytical synthesis-with-citations shape as tenet_distill, just fed by
    # the FULL conversational stream (idle Ask threads + landed transcripts)
    # rather than owner-flagged musings alone. Batch (daily 18:00 cron), not
    # latency-sensitive → Sonnet; budget-capped warn-mode (0190).
    "session_distill": DEFAULT_MODEL,
    # The Ledger Phase-1 wondering classifier (the research-loop gate). A short
    # closed binary+extraction behind a deterministic regex pre-gate → the cheap
    # FAST tier; the golden set (evals/golden/wondering_detect.json) is its bar.
    "wondering_detect": FAST_CLASSIFIER_MODEL,
    # The Ledger intent tap (research.intent). A closed enum classification —
    # observation/wondering/brief_artifact/stress_artifact — over one short musing,
    # run on EVERY owner musing (no lexical pre-gate) → the cheap FAST tier; the
    # golden set (evals/golden/capture_intent.json) is its bar.
    "capture_intent": FAST_CLASSIFIER_MODEL,
    # The B7 routing triage behind a positive wondering verdict (research.triage,
    # called from research.proposals.detect_and_create_task). A closed 3-way
    # route — answer_now/belief_candidate/research_task — over one short musing;
    # fails OPEN to research_task (today's pre-B7 behavior) on any failure →
    # the cheap FAST tier. Budget-capped, warn mode (0194).
    "research_triage": FAST_CLASSIFIER_MODEL,
    # The Ledger reply router (onmymind.reply). A closed enum classification —
    # research/save/worldview/dismiss/question/note — over one owner reply to a
    # feed card (the reply box replaced the per-card verb buttons). Fails open
    # to 'question' (converse, never act) → the cheap FAST tier.
    "ledger_reply_intent": FAST_CLASSIFIER_MODEL,
    # The Triage second-pass router (user_state.triage_suggest). Given the FULL
    # routable-intent menu, which route would the owner pick for a parked
    # comment, and how sure are we? high → auto-route, low → one-tap suggestion,
    # park/failure → unchanged. Closed enum → the cheap FAST tier.
    "triage_route_suggest": FAST_CLASSIFIER_MODEL,
    # The capture->answer primary gate (capture.triage, B3). A closed 3-way
    # route — answer_now/contradiction/plain — over one short musing plus its
    # rendered Worldview/decisions/recent-musings context; NEVER raises (any
    # LLM-layer failure degrades to the regex fallback). Closed enum, short
    # input → the cheap FAST tier; the golden set
    # (evals/golden/capture_triage.json) is its bar.
    "capture_triage": FAST_CLASSIFIER_MODEL,
    # The Ledger reply-box coach follow-up router (src/capture/coach_reply.py,
    # B3 — sibling PR). Registered here (this file's LLM_MODELS is the one
    # source of truth) even though the classifying module is owned by the
    # parallel B3 workstream. Closed enum, short input → the cheap FAST tier.
    "coach_reply_intent": FAST_CLASSIFIER_MODEL,
    # The Sunday packet's per-item verdict pre-draft (pipeline.weekly_packet,
    # PR2 — navigation_ia.md §3.1). A short, closed-shape suggestion
    # ("ratify|drop|defer - <reason>") grounded ONLY in the item text it's
    # given — no web, no open-ended reasoning → the cheap FAST tier. Runs
    # once per packet item, once a week; per-item degrade (a failure ships
    # the item without a draft, never blocks the packet). Budget-capped,
    # warn mode (0149).
    "weekly_packet_predraft": FAST_CLASSIFIER_MODEL,
    # The Ledger artifact brief (research.brief). Analytical writing — takeaways +
    # bull/bear (+ stress: falsifiers, second-order, portfolio map) over an extracted
    # article/deck → the default analytical tier (Sonnet). Budget-capped (0139).
    "artifact_brief": DEFAULT_MODEL,
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
    # Decision Draft parse (P2.1, personal_investment_partner_prd.md §9.2) — the
    # async tap that turns a landed free-text/voice capture into a confirmable
    # Owner Decision draft. Pinned to Opus (not the FAST tier): the input is
    # untrusted owner-channel text (spotlighted, llm.untrusted.spotlight) and a
    # wrong extraction can propose a consequential financial mutation, so this
    # purpose gets the strongest available model rather than the cheap
    # closed-classification default. Own golden set (evals/golden/
    # decision_draft_parse.json); budget seeded by migration 0195 ($8/mo, warn,
    # on_exceed='skip' — a blown cap degrades the tap to no-draft, never blocks
    # the already-landed capture).
    "decision_draft_parse": "claude-opus-4-8",
    # Meta-eval steering (meta_eval_governance.md §2): classifies a CAPTURED
    # prompt's difficulty (easy/moderate/hard) so the sweep sampler oversamples
    # hard cases. Reads the prompt only (never a response); cached forever per
    # (purpose, sha, version) in eval_case_features — one call per new distinct
    # prompt. Short + closed classification → FAST tier; traffic rides
    # scope="meta_eval" + CAPTURE_DENYLIST (never enters a harvest corpus).
    "case_difficulty_classify": FAST_CLASSIFIER_MODEL,
    # Meta-eval steering (§1.2/§10.1): the monthly nominator ranks what the
    # sweep tests next (risk tiering, family grouping, exclusions-with-TTL) and
    # the frontier researcher web-verifies the cross-provider candidate pool.
    # Both are judgment-tier calls, ~1-2/month each → Opus; scope="meta_eval" +
    # CAPTURE_DENYLIST; fail-closed validation + deterministic fallbacks mean a
    # bad answer steers nothing.
    "optimizer_nominator": "claude-opus-4-8",
    "model_frontier_research": "claude-opus-4-8",
    # Meta-eval per-case checklist deriver (§3): real reading comprehension over
    # long task prompts (latency irrelevant, cached forever per distinct prompt)
    # → Sonnet. Reads the TASK PROMPT ONLY — never any model's response — so a
    # checklist can't encode outcome knowledge; judge-side only (I1).
    "query_criteria_derive": DEFAULT_MODEL,
    # Meta-eval prompt-variant proposer (§4): prompt engineering is judgment-tier
    # (~2-4 experiments/mo) → Opus. Its edits are validated pre-spend (anchors
    # exactly-once) and only ever reach production through the A/B promotion bar
    # + prompt_pin_overrides (§10 Q1) — a bad proposal steers nothing.
    "prompt_variant_propose": "claude-opus-4-8",
    # P2 reflective mutation (llm.prompt_reflect): rewrites a whole template
    # from judged failure evidence — judgment-tier work, Opus-pinned like its
    # edit-splice predecessor.
    "prompt_reflect_rewrite": "claude-opus-4-8",
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
    # Per-name DCF scenario prior (src/dcf/scenario_prior.py). Judgment over the
    # thesis/bear/KPI anchors to skew the Bull/Base/Bear weights that move
    # allocation → Sonnet-tier reasoning (one batch call per name, latency
    # unimportant), like position_review. A non-degenerate simplex is enforced in
    # code and the owner's workbook edit always wins, so a bad call can't swing the
    # book; the scenario_prior golden set grades the directional skew.
    "scenario_prior": DEFAULT_MODEL,
    # Positioning coach (src/positioning/coach_pack.py via ask.engine): the
    # standing conversation about the book's SHAPE — socratic push-back,
    # inconsistency-spotting against the live book, iterating owner intent
    # toward concrete targets. Judgment-heavy dialogue with the ask_answer
    # quality bar -> Sonnet-tier; owner-initiated turns only, never scheduled.
    "positioning_coach_turn": DEFAULT_MODEL,
    # Positioning encode (src/positioning/encode.py): one structured call that
    # turns a converged coach thread into a PositioningProfile PROPOSAL.
    # Deterministic validation backstops it (Pydantic bounds + closed
    # vocabularies) and the owner's approval-form edits always win, so a bad
    # encode can never silently become the target book — but the extraction
    # itself is nuance-sensitive (emit ONLY expressed dimensions) -> Sonnet.
    "positioning_encode": DEFAULT_MODEL,
    # ETF role-in-portfolio one-pager (src/etf_role_synthesis.py): ONE synthesis
    # call over a fully deterministic workup payload (score/fit factors,
    # look-through overlap + country rollup, what-if rows, positioning target)
    # — the model judges only the ROLE. Sonnet-tier synthesis; sha-keyed in
    # llm_artifacts so reruns are free until the workup inputs move (~once per
    # ETF per data change).
    "etf_role_synthesis": DEFAULT_MODEL,
    # Whole-book thesis-collision audit (src/thesis_collision.py): one call over
    # every portfolio name's thesis + tier-1 break-rule drivers, flagging
    # shared-driver concentration and directly contradictory theses. Cross-name
    # judgment over natural-language text -> Sonnet-tier reasoning, like
    # scenario_prior/position_review; a low-frequency on-demand/cron call (the
    # thesis set changes rarely), never on the render path (cached in
    # llm_artifacts, keyed by a thesis-set hash).
    "thesis_collision": DEFAULT_MODEL,
    # Business-factor taxonomy (src/risk_factors.py, C3 — 2026-07-19 program
    # plan Workstream C keystone): per-holding loadings onto a small
    # controlled taxonomy ("Brazil consumer credit", "digital ad spend", …),
    # grounded in the C2 revenue mix + micro_thesis thesis text. Nuance-
    # sensitive judgment over natural-language + disclosed-mix inputs, same
    # tier as thesis_collision/scenario_prior/position_review -> Sonnet-tier
    # reasoning. Per-ticker, on-demand/weekly-cron call, never on the render
    # path (cached in llm_artifacts, keyed by a mix+thesis input hash; see
    # that module's compute_input_sha).
    "business_factor_taxonomy": DEFAULT_MODEL,
    # Pre-earnings brief (src/earnings_brief.py, 2026-07-31 owner ruling
    # relaxing D2 for this one artifact): one narrative brief per (ticker,
    # expected ER date), grounded in the composed thesis/bear/IR anchors,
    # tier-1 KPI deltas, watch items, queued prep notes, tone shifts, and
    # retrieved evidence. Same long-context-analytical-writing shape as
    # transcript_summary/event_brief -> Sonnet tier; Opus stays reserved for
    # alert-firing judgment. ≤2 calls per name per earnings cycle (artifact
    # input-hash cache + T-1 refresh gate), budget 'skip' mode ($5/mo, 0260).
    "pre_earnings_brief": DEFAULT_MODEL,
    # Post-earnings readout (src/earnings_readout.py): the same long-context
    # analytical-writing tier, persisted per selected transcript period_end.
    # Scheduled scope is portfolio-only; evaluation names are explicit-request
    # only. Input-hash caching makes unchanged quarters free.
    "post_earnings_readout": DEFAULT_MODEL,
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
    # Sector-benchmark-ETF proposal (docs/design/comparable_sets_bottoms_up.md
    # §4, Phase 3 ratification flow, execution/propose_sector_benchmarks.py):
    # "which published index ETF tracks this FMP industry" — a small factual
    # lookup, not judgment, same shape as extract_8k_overrides. Proposals are
    # cached to data/sector_benchmark_proposals/ for owner review and NEVER
    # auto-applied into sector_benchmark_map.SECTOR_BENCHMARK_MAP. Run on
    # demand (one call per unmapped industry, ~50-150 total) — cost bounded.
    "sector_benchmark_proposal": FAST_CLASSIFIER_MODEL,
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
    # News LLM modules. material_news_classification is the material-news
    # trigger's per-headline materiality veto (src/triggers/material_news.py):
    # it was ABSENT here and so silently fell back to Sonnet; pinning it to
    # Opus gives the noise-filtering judgment the wider knowledge and
    # instruction-following it needs (one batched call per ticker per run,
    # cached — cost bounded).
    # news_structuring (the WebSearch->structured-rows fallback extractor) ran
    # on Opus while FMP news 402'd for the whole book — 1,622 calls / $416 in
    # 30 days, over half the monthly LLM bill (2026-07-19 review), for a
    # structure-extraction task with deterministic output-contract invariants.
    # Downgraded to the Sonnet default (cheapest-at-parity); the golden set
    # (evals/golden/news_structuring.json, mode-A contract grading) gates the
    # switch, and the fallback is now portfolio-scoped in fetch_news anyway.
    "material_news_classification": "claude-opus-4-8",
    "news_structuring": DEFAULT_MODEL,
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
    # Incremental Dollar Recommendation (P0.4a, personal_investment_partner_prd.md
    # §7.4/§10): the governed selection over the deterministic frontier
    # (allocation.recommendation.build_frontier) — a cross-portfolio allocation
    # judgment with a hard frontier-grounding gate (invented tickers/weights/
    # probabilities are rejected and force a deterministic fallback). Same tier
    # as advisor_next_dollar / cross_portfolio_synthesis: Opus's wider
    # instruction-following matters when the prompt embeds an explicit
    # VALIDATION CONSTRAINTS block the model must actually honor. Budget-capped
    # at $10/mo, on_exceed='block' (migration 0188) — a blown cap is explicit
    # unavailability (a labeled deterministic-fallback artifact), never a
    # silent downgrade to a cheaper model.
    "incremental_dollar_recommendation": "claude-opus-4-8",
    # Investment Decision Card (P1.1, personal_investment_partner_prd.md
    # §8.1/§10): the governed synthesis of company hypothesis, security setup,
    # portfolio fit, and disconfirming case over deterministic inputs
    # (research.investment_decision_card.generate_card) — a per-ticker
    # judgment with the same "evidence_readiness is NEVER LLM-authored" hard
    # gate as Incremental Dollar Recommendation's frontier-grounding gate.
    # Same Opus tier: the prompt embeds an explicit VALIDATION CONSTRAINTS
    # block the model must actually honor. Budget-capped at $8/mo,
    # on_exceed='skip' (migration 0190) — per-ticker batch build work should
    # degrade one card at a time, not block the whole evaluation build.
    "investment_decision_card": "claude-opus-4-8",
    # Senior Partner Brief (P2.2, personal_investment_partner_prd.md
    # §9.1/§10): the weekly governed synthesis over the whole advisory
    # surface — latest Incremental Dollar Recommendation, Risk Budget,
    # wealth context, Investment Decision Cards, weekly-packet items,
    # governor-routed moments (calibration_finding/capacity_breach/
    # life_event_checkpoint/profile_drift — see research.governor.
    # BRIEF_ROUTED_CLASSES), v_decision_journal, Worldview/owner-profile
    # anchors, and open research proposals into five ordered sections. Same
    # Opus tier as Incremental Dollar Recommendation / Investment Decision
    # Card: the prompt embeds an explicit VALIDATION CONSTRAINTS block
    # (notably the <=3 action_requested/week cap unless an active week
    # explains the excess) the model must actually honor. Budget-capped at
    # $6/mo, on_exceed='block' (migration 0199) — one governed call/week, so
    # a blown cap is explicit unavailability (a labeled mechanical-digest
    # fallback), never a silent downgrade.
    "senior_partner_brief": "claude-opus-4-8",
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
    # Short, structured, batch classifiers — Haiku tier. Promoted to Gemini Flash
    # (#538), reverted 2026-07-02: the metered Gemini backend is off the active
    # pareto (owner opted out of paying for it). A cheaper open-weight candidate
    # (OpenRouter DeepSeek/Qwen) can re-take these via the eval-gated sweep.
    "intake_classifier": FAST_CLASSIFIER_MODEL,
    "transcript_metadata": FAST_CLASSIFIER_MODEL,
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
    # Discontinued-metric detector Stage 2 triage (P1,
    # docs/design/disclosure_change_signals.md §3, src/filings/metric_triage.py):
    # ONE batched call per ticker over tag name + label + last value + last
    # period ONLY (never a document) — a closed relevance/prior classification
    # over ~40 candidates, same shape as risk_factor_classify — Haiku tier.
    # No budget row / golden set added yet (same as its risk_factor_classify/
    # risk_factor_diff sibling purposes); flag if/when a golden-set-gated
    # downgrade loop is warranted.
    "metric_lifecycle_triage": FAST_CLASSIFIER_MODEL,
    # Guidance-withdrawal detector Stage 2/3 triage (D2.1,
    # docs/design/disclosure_intelligence_v1_prd.md, src/filings/guidance_triage.py):
    # ONE batched call per ticker over subject key + description + silence
    # pattern ONLY (never a document, never transcript text) — same
    # closed relevance/prior classification shape as metric_lifecycle_triage,
    # over a per-ticker candidate list from BOTH lanes (management_commitments
    # own-cadence + MD&A guidance/outlook heading own-cadence) — Haiku tier.
    # No budget row / golden set added yet (same posture as
    # metric_lifecycle_triage); flag if/when a golden-set-gated downgrade
    # loop is warranted.
    "guidance_lifecycle_triage": FAST_CLASSIFIER_MODEL,
    # P4 transcript longitudinal tracking (docs/design/disclosure_change_
    # build_stack.md P4, src/transcripts/transcript_judgment.py): ONE
    # batched call per transcript over paired analyst-question/management-
    # answer excerpts ONLY (never the full call, never prepared remarks) —
    # non-answer classification + per-speaker tone scoring together. Closed
    # judgment over a bounded (<=30) candidate list, same shape as
    # risk_factor_classify — Haiku tier.
    "transcript_qa_judgment": FAST_CLASSIFIER_MODEL,
    # P4 discontinued-topic triage: ONE batched call per ticker over KPI
    # catalog NAMES ONLY (never transcript text), mirroring
    # metric_lifecycle_triage's "tag names only" contract — Haiku tier.
    "transcript_topic_triage": FAST_CLASSIFIER_MODEL,
    # No budget row / golden set added yet for either purpose above (same
    # posture as risk_factor_classify/risk_factor_diff) — flag if/when a
    # golden-set-gated downgrade loop is warranted.
    # Segment-name canonicalization: narrow JSON mapping task — Haiku tier.
    "canonicalize_segments": FAST_CLASSIFIER_MODEL,
    # Customer-concentration table extraction (src/table_extractors/): short
    # copy-the-table JSON from filing excerpts — Haiku tier.
    "customer_concentration_extraction": FAST_CLASSIFIER_MODEL,
    # Falsifiable-condition extraction (src/decision_conditions.py): turns the
    # "What would change my mind" memo section into structured
    # {metric, op, threshold, unit, for_periods} conditions against a supplied
    # metric vocabulary — a narrow copy-the-token JSON task over ~1-2KB of
    # prose, run once per new decision — Haiku tier (Gemini un-pinned 2026-07-02,
    # off the active pareto); golden set at
    # evals/golden/decision_conditions_extract.json guards quality.
    "decision_conditions_extract": FAST_CLASSIFIER_MODEL,
    # Qualitative "what would change my mind" extraction (L9 PR2): the
    # non-numeric twin of the above — pull event-shaped conditions ("CEO
    # departs", "competitor enters") + a news/earnings routing tag from the same
    # prose. Same narrow, closed-vocab JSON shape, run once per new decision →
    # the same cheap Haiku tier (Gemini un-pinned 2026-07-02, off the active pareto).
    "qualitative_conditions_extract": FAST_CLASSIFIER_MODEL,
    # Diet information-quality scoring (src/signals/quality.py): batched 0..1
    # grading of news-backed diet signals (id+firm+headline in, id+score out)
    # so load_diet_signals can filter on the STORED score instead of the static
    # publisher denylist. Short, schema-validated, batched (≤20 rows/call,
    # ≤5 calls/run as a fetch_news follow-on) — Haiku tier.
    "diet_source_quality": FAST_CLASSIFIER_MODEL,
    # NL → ViewSpec compile (master build P5.2): the Explore panel's query
    # box. Narrowly-scoped JSON-output against a supplied metric vocabulary,
    # interactive (the owner is waiting at the input) — latency dominates.
    # Haiku tier (Gemini un-pinned 2026-07-02, off the active pareto): copy-the-token
    # task, golden set at evals/golden/viewspec_compile.json guards quality; repair
    # retry in viewspec.nl_compile covers the degraded case.
    "viewspec_compile": FAST_CLASSIFIER_MODEL,
    # Ask pack router (src/ask/router.py, fund-grade build S4): selects which
    # portfolio evidence packs (holdings / conviction / dcf / decisions /
    # journal / performance) a narrative ask turn needs. Closed-enum JSON
    # selection on the interactive ask path — latency dominates.
    # Haiku tier (Gemini un-pinned 2026-07-02, off the active pareto): pick-from-a-list
    # task, mis-selection fails closed to document-only evidence. Scored by
    # evals/golden/ask_pack_router.json; budget row seeded by alembic 0089 (skip mode).
    "ask_pack_router": FAST_CLASSIFIER_MODEL,
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
    # Exact-span, schema-bound claim/citation audit for sealed Ask answers.
    # Hard-blocked by migration 0253: failure prevents the answer from being
    # surfaced, so the cheap classifier tier is safe only behind strict decode.
    "ask_claim_audit": FAST_CLASSIFIER_MODEL,
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
    # Monthly Red Team engine (src/redteam/, directives/monthly_red_team.md
    # Phase 2, PR5). red_team_attack is the per-name rotating-lens adversarial
    # call (one held name x one lens per call); red_team_cross_book is the
    # three deterministic-input cross-book passes (factor-block / style-drift
    # / human-capital). Both are judgment over already-computed evidence
    # (thesis anchor, correlation clusters, style loadings) — Sonnet-tier
    # reasoning, cheapest-at-parity starting point (directives/
    # cheapest_model_routing.md); ~15 calls/run per llm_quota_scheduling.md,
    # eval coverage is an explicitly tracked follow-up (not this PR).
    "red_team_attack": DEFAULT_MODEL,
    "red_team_cross_book": DEFAULT_MODEL,
    # Annual letter-to-self (monthly_red_team.md Phase 3, PR7): ONE call/year
    # drafting a letter from a fully deterministic evidence pack (the year's
    # ledger entries, position_entries changes, red-team response log,
    # calibration/Brier trajectory, decision P&L rows) — judgment-heavy
    # synthesis in the owner's voice, latency irrelevant, ~1 call/year -> Sonnet
    # (cheapest-at-parity starting point, like red_team_attack/scenario_prior).
    # The owner edits/signs the output file directly; a bad draft costs nothing
    # but a re-run.
    "annual_letter": DEFAULT_MODEL,
    # 10-Q segment quarterly period-axis disambiguation (docs/design/
    # segment_quarterly_framework.md §2.4, compute.segment_quarterly_10q):
    # Stage B fallback when the deterministic column-header parser can't
    # resolve a 10-Q period axis — a narrow, closed-schema classification
    # task (per-column duration_months/end_date/is_cumulative), same shape
    # and same tier as canonicalize_segments — Haiku tier.
    "segment_10q_period_disambiguate": FAST_CLASSIFIER_MODEL,
    # Foreign-private-issuer 6-K segment/product/geography table extraction
    # (compute.segment_quarterly_6k). The input is a keyword-windowed exhibit
    # excerpt and the output is a narrow JSON breakdown contract, so the fast
    # structured tier is sufficient. Route through the governed entrypoint;
    # this purpose previously bypassed it via llm_client._call_claude.
    "segment_6k_breakdown_extract": FAST_CLASSIFIER_MODEL,
    # The behavioral-rules distiller (tenet-2 Phase 4, docs/design/
    # tenet2_advisory_program.md §3.2 Tier C / §7 ruling 3): distils the
    # owner's graded decisions corpus into candidate behavioral rules with
    # citations, deterministically validated before staging (mirrors
    # thesis_collision's hallucination guard). Per the phase spec this is
    # pinned to the FAST/cheap tier rather than tenet_distill's Sonnet pin —
    # a narrower, more structurally-constrained citation task (decision ids +
    # a claimed outcome label per citation) than tenet_distill's freer belief
    # synthesis; re-evaluate via the model-downgrade eval loop if quality
    # regresses once a golden/audit corpus accrues.
    "behavior_distill": FAST_CLASSIFIER_MODEL,
    # Semantic tenet-tension detection (B5, synthesis.semantic_tension): a
    # closed "restate/contradict which ONE shown tenet, or null" classification
    # over <=15 rendered tenets, deterministically grounded before use (a
    # fabricated token never resolves) — short, closed-schema, same shape as
    # capture_triage's own contradiction classification -> the cheap FAST tier.
    "tenet_semantic_tension": FAST_CLASSIFIER_MODEL,
    # Tenet accountability ledger (B5, synthesis.tenet_accountability — the
    # sibling B5 workstream's file; registered here since this file is the one
    # source of truth for LLM_MODELS/prompt_versions per the B5 PR split).
    # Analytical judgment over a Tenet + the decisions it should have governed
    # -> the default analytical tier (Sonnet), matching tenet_distill /
    # session_distill's own tier for belief-level synthesis.
    "tenet_accountability": DEFAULT_MODEL,
    # Exit post-mortem drafting (B6, 2026-07-19 program overhaul,
    # synthesis.exit_postmortem — a sibling B-workstream file; registered
    # here since this file is the one source of truth for LLM_MODELS).
    # Narrating why a closed position exited and what it taught from the
    # ticker's own decision trail is judgment-heavy synthesis in the owner's
    # voice, same shape as tenet_accountability/session_distill -> Sonnet.
    "exit_postmortem_draft": DEFAULT_MODEL,
    # P3 boilerplate-vs-substance triage (filings.boilerplate_triage,
    # docs/design/disclosure_change_build_stack.md §P3). Deterministic
    # specificity scoring (filings.specificity) already resolves the
    # confidently-boilerplate and confidently-substantive extremes in plain
    # code; this purpose only sees the ambiguous middle, ONE batched call per
    # ticker over diff hunks (never whole documents) — the same narrow,
    # closed-vocab classification shape as metric_lifecycle_triage -> Haiku
    # tier.
    "disclosure_item_specificity_triage": FAST_CLASSIFIER_MODEL,
    # Thesis-materiality elevation gate (filings.materiality_judgment, owner
    # ruling 2026-08-02): decides whether a disclosure-drift event may be
    # ELEVATED to an owner-facing surface at all — "restricts_measurement"
    # only when the change fundamentally restricts the ability to measure the
    # thesis (a tier-1 KPI / break-condition input dropped, aggregated away,
    # redefined, or obscured). Judgment of thesis-vs-disclosure semantics
    # against the thesis anchor, not a closed text-property classification,
    # and it is the SOLE elevation gate — Sonnet tier (cheapest-at-parity
    # starting point, like red_team_attack); batched ≤40 events/call, one to
    # a few calls per ticker per weekly sweep.
    "disclosure_thesis_materiality": DEFAULT_MODEL,
    # Legacy private callers migrated to the governed entrypoint.  These are
    # short, schema-bound document extractors; each has a golden/eval registry
    # entry and a versioned prompt identity.
    "kpi_summary_extract": FAST_CLASSIFIER_MODEL,
    "kpi_summary_enumerate": FAST_CLASSIFIER_MODEL,
    "segment_definition_extract": FAST_CLASSIFIER_MODEL,
    "segment_crosstab_extract": FAST_CLASSIFIER_MODEL,
    "ir_sheet_kpi_map": FAST_CLASSIFIER_MODEL,
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


_CODEX_MODEL_BY_CLAUDE_TIER: dict[str, str] = {
    "claude-haiku-4-5-20251001": _CODEX_FAST_MODEL,
    "claude-haiku-4-5": _CODEX_FAST_MODEL,
    "claude-sonnet-4-6": _CODEX_DEFAULT_MODEL,
    "claude-sonnet-5": _CODEX_DEFAULT_MODEL,
    "claude-opus-4-7": _CODEX_JUDGMENT_MODEL,
    "claude-opus-4-8": _CODEX_JUDGMENT_MODEL,
    "claude-fable-5": _CODEX_JUDGMENT_MODEL,
}


PRIMARY_CODEX = _PRIMARY_CODEX
PRIMARY_CLAUDE = _PRIMARY_CLAUDE


def primary_subscription_backend() -> str:
    value = os.environ.get(PRIMARY_SUBSCRIPTION_BACKEND_ENV_VAR, _PRIMARY_CODEX).strip().lower()
    if value not in {_PRIMARY_CODEX, _PRIMARY_CLAUDE}:
        raise LLMSetupError(
            f"{PRIMARY_SUBSCRIPTION_BACKEND_ENV_VAR} must be 'codex' or 'claude', got {value!r}"
        )
    return value


def _primary_subscription_backend() -> str:
    return primary_subscription_backend()


def _codex_model_for(claude_model: str) -> str:
    """Map the existing eval-gated Claude quality tier onto Codex's tier."""
    return _CODEX_MODEL_BY_CLAUDE_TIER.get(claude_model, _CODEX_DEFAULT_MODEL)


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

    ``LLMQuotaExhausted`` (the subscription window breaker) is DELIBERATELY not
    a hard stop: unlike a budget cap it is temporal and self-healing, so
    production pipelines defer the item and retry next run (the per-item
    degrade pattern) instead of crashing a whole build over a window that
    resets on its own. Measurement contexts are the exception — eval/judge
    callers must abort on it explicitly (see ``evals.rubric_judge``), because
    scoring under an exhausted quota measures the outage, not the subject.
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

    Budget enforcement fails closed. Unexpected checker errors are redacted
    and converted to ``LLMBudgetExceeded`` so bookkeeping defects cannot
    silently authorize token spend. ``force_budget_bypass=True`` remains the
    explicit operator escape hatch.
    """
    if force_budget_bypass:
        return
    budget_purpose = purpose or "__default__"
    try:
        from llm_budget import check_budget, record_alert

        check = check_budget(budget_purpose)
    except Exception as exc:
        log.error(
            {
                "event": "llm_budget_check_failed_closed",
                "purpose": budget_purpose,
                "error_type": type(exc).__name__,
            }
        )
        raise LLMBudgetExceeded(
            f"{budget_purpose}: budget enforcement unavailable; blocking LLM call"
        ) from None
    purpose = budget_purpose
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


def _authorize_metered_openrouter_fallback(
    purpose: str | None, *, force_budget_bypass: bool
) -> bool:
    """Fail-closed authorization for automatic metered fallback.

    A purpose must be in the reviewed allowlist and carry a positive,
    hard-block budget. The normal budget-bypass escape hatch is deliberately
    forbidden here: a subscription outage must never become unbounded spend.
    Every authorized use emits an ERROR-level event plus its normal ledger row.
    """
    if purpose is None or purpose not in OPENROUTER_FALLBACK_PURPOSES:
        return False
    if force_budget_bypass:
        raise LLMBudgetExceeded(
            f"{purpose}: automatic metered fallback cannot bypass its hard budget"
        )

    from llm_budget import check_budget

    check = check_budget(purpose)
    if check.cap <= 0 or not check.hard_block:
        raise LLMSetupError(
            f"{purpose}: metered OpenRouter fallback is allowlisted but lacks "
            "a positive hard-block budget"
        )
    _enforce_budget_pre_call(purpose, force_budget_bypass=False)
    log.error(
        {
            "event": "llm_metered_openrouter_fallback_authorized",
            "purpose": purpose,
            "current_spend_usd": str(check.current_spend),
            "cap_usd": str(check.cap),
        }
    )
    return True


def _stderr_tail(exc: BaseException, limit: int = 400) -> str:
    """Best-effort tail of a failed subprocess's captured stderr (falls back
    to stdout). ``str(subprocess.CalledProcessError)`` is just "Command '...'
    returned non-zero exit status N" — the actual reason (usage-limit reached,
    auth expired, network error) lives in ``.stderr``/``.stdout``, which
    ``capture_output=True`` populates on the exception but the default
    ``__str__`` drops. Every ledger row previously recorded that generic
    message regardless of cause, which is why a systemic quota-exhaustion
    incident and a one-off CLI bug were indistinguishable after the fact."""
    stderr = str(getattr(exc, "stderr", None) or "").strip()
    if stderr:
        return stderr[-limit:]
    stdout = str(getattr(exc, "stdout", None) or "").strip()
    return stdout[-limit:] if stdout else ""


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
    fallback_used: str | None = None,
    allow_codex_fallback: bool = True,
    fallback_from_provider: str | None = None,
    fallback_from_transport: str | None = None,
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

    # Quota circuit breaker (July-2026 incident): once the subscription window
    # is exhausted, every spawn is doomed for hours. Fail fast — typed, ledgered,
    # zero subprocesses — until the recorded reset passes.
    blocked_until = quota_block_active()
    if blocked_until is not None:
        msg = (
            f"LLM quota breaker engaged until {blocked_until.isoformat()} — "
            "call skipped without spawning the CLI (defer and retry next run)"
        )
        record_llm_call(
            started_at=datetime.now(UTC),
            elapsed_ms=0,
            model=model,
            prompt_sha=prompt_sha,
            prompt_chars=len(prompt),
            purpose=purpose,
            ticker=ticker,
            scope=scope,
            run_id=run_id,
            error=f"[quota_blocked] {msg}",
            fallback_used=fallback_used,
            prompt=prompt,
            provider="anthropic",
            transport="subscription_cli",
            auth_class="subscription",
            attempts=0,
            retries=0,
            failure_class="quota_blocked",
            fallback_from_provider=fallback_from_provider,
            fallback_from_transport=fallback_from_transport,
        )
        raise LLMQuotaExhausted(msg, blocked_until=blocked_until)

    started_at = datetime.now(UTC)
    t0 = time.monotonic()
    last_error: Exception | None = None
    last_info: FailureInfo | None = None
    attempt = 0
    while True:
        attempt += 1
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
                encoding="utf-8",  # Force UTF-8 — Windows otherwise defaults to cp1252 which dies
                errors="replace",  # on financial-doc Unicode (U+2212 minus, en/em dashes, arrows).
                check=True,
                timeout=timeout_seconds,
                cwd=_neutral_subprocess_cwd(),  # avoid booting the project's MCP servers (hangs)
                env=_subprocess_env(),  # strip ambient ANTHROPIC_BASE_URL (2026-07 incident)
            )
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            # Parse the JSON envelope. ValueError when malformed → caught below
            # and classified (the raised message now carries the envelope's
            # `result` head, so is_error-with-exit-0 failures classify too).
            text, meta = parse_claude_json_output(result.stdout.strip())
            text = text.strip()
            if not text:
                raise RuntimeError(
                    f"claude -p returned empty `result`. stderr: {result.stderr.strip()[:200]}"
                )
            log.info({"event": "llm_call_done", "response_chars": len(text)})
            clear_quota_block()  # no-op unless a breaker file exists
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
                fallback_used=fallback_used,
                prompt=prompt,
                provider="anthropic",
                transport="subscription_cli",
                auth_class="subscription",
                attempts=attempt,
                retries=attempt - 1,
                fallback_from_provider=fallback_from_provider,
                fallback_from_transport=fallback_from_transport,
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
            last_error = claude_error
            last_info = classify_cli_failure(claude_error)
            if last_info.kind == "usage_limit":
                # Deterministic until the window resets — engage the breaker so
                # every process fails fast instead of spawning doomed CLIs.
                record_quota_exhausted(last_info)
                break
            if attempt < retry_budget(last_info.kind):
                delay = _RETRY_BASE_SECONDS * (2 ** (attempt - 1)) + random.uniform(0.0, 1.0)
                log.warning(
                    {
                        "event": "llm_call_retry",
                        "purpose": purpose,
                        "attempt": attempt,
                        "kind": last_info.kind,
                        "delay_s": round(delay, 1),
                        "detail": last_info.detail[:200],
                    }
                )
                time.sleep(delay)
                continue
            break

    # Final failure: ONE ledger row carrying the classified cause (retries are
    # log events, not rows — a 3-attempt blip is one failure, not three).
    assert last_error is not None and last_info is not None
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    error_msg = (
        f"{last_info.ledger_prefix} {type(last_error).__name__} "
        f"(attempt {attempt}/{retry_budget(last_info.kind)}): {last_info.detail[:400]}"
    )
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
        error=error_msg,
        fallback_used=fallback_used,
        prompt=prompt,
        provider="anthropic",
        transport="subscription_cli",
        auth_class="subscription",
        attempts=attempt,
        retries=attempt - 1,
        failure_class=last_info.kind,
        fallback_from_provider=fallback_from_provider,
        fallback_from_transport=fallback_from_transport,
    )
    if (
        os.environ.get("LLM_FALLBACK_DISABLED", "").lower() in {"1", "true", "yes"}
        or not allow_codex_fallback
    ):
        if last_info.kind == "usage_limit":
            raise LLMQuotaExhausted(
                f"usage limit exhausted: {last_info.detail[:300]}",
                blocked_until=last_info.retry_after,
            ) from last_error
        raise last_error
    try:
        from llm.codex_backend import call_codex_llm

        codex_fallback_model = _codex_model_for(model)
        text = call_codex_llm(
            prompt,
            model=codex_fallback_model,
            purpose=purpose,
            ticker=ticker,
            scope=scope,
            run_id=run_id,
            fallback_used="codex",
            fallback_from_provider="anthropic",
            fallback_from_transport="subscription_cli",
        )
        capture_exchange(
            prompt=prompt,
            response=text,
            purpose=purpose,
            ticker=ticker,
            scope=scope,
            model=codex_fallback_model,
            run_id=run_id,
            backend="codex",
        )
        return text
    except Exception as codex_error:
        if not _authorize_metered_openrouter_fallback(
            purpose, force_budget_bypass=force_budget_bypass
        ):
            if last_info.kind == "usage_limit":
                raise LLMQuotaExhausted(
                    "Claude quota is exhausted and the Codex subscription fallback failed",
                    blocked_until=last_info.retry_after,
                ) from codex_error
            raise RuntimeError(
                "Both subscription LLM transports failed "
                f"(claude={type(last_error).__name__}, codex={type(codex_error).__name__}); "
                "this purpose is not approved for metered OpenRouter fallback."
            ) from codex_error
        from llm.openrouter_backend import call_openrouter, openrouter_model_for

        openrouter_fallback_model = openrouter_model_for(purpose)
        text = call_openrouter(
            prompt,
            model=openrouter_fallback_model,
            purpose=purpose,
            ticker=ticker,
            scope=scope,
            run_id=run_id,
            force_budget_bypass=force_budget_bypass,
            fallback_used="openrouter",
            fallback_from_provider="openai",
            fallback_from_transport="subscription_cli",
        )
        capture_exchange(
            prompt=prompt,
            response=text,
            purpose=purpose,
            ticker=ticker,
            scope=scope,
            model=openrouter_fallback_model,
            run_id=run_id,
            backend="openrouter",
        )
        return text
        # Typed so eval/judge callers can abort instead of scoring the outage;
        # production callers defer per-item (is_hard_stop → False).
    # Operational failure — try Gemini fallback. fallback_call_logged raises
    # if the fallback is disabled/unconfigured, surfacing both errors together;
    # an actual Gemini attempt writes its own ledger row (fallback_used='gemini').


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
    db_path: Path | str | None = None,
) -> str:
    """Public single-shot LLM call. CANONICAL entry point for ALL LLM calls in
    this repo — including from `execution/` scripts, `src/report/sections/`, and
    anywhere else that needs a Claude-then-Gemini-fallback round-trip.

    Direct use of `google.genai`, the `anthropic` SDK, or any other
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
            for new code. Legacy ``None`` is normalized to the metered
            ``__default__`` purpose; the explicit `model` arg overrides model
            selection when both are passed.
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
        backend: None (resolve from model family), "claude", "gemini", or
            "openrouter". An OpenRouter model id (provider/model slug) routes to
            the OpenRouter backend; an operational failure there also degrades to
            Claude, while a forced backend="openrouter" call raises.
        db_path: Explicit portfolio.db for THIS call's DB-backed layers —
            model-pin overrides, prompt A/B, budget check/alerts, and the
            llm_calls cost ledger. Those layers resolve ``db.DB_PATH``
            internally, so a caller holding an explicit db_path (library call
            with no ``db.set_db_path`` bootstrap) would otherwise get its
            ledger row silently written to the wrong DB ("no such table:
            llm_calls"). None = current behavior (the process global).
    """
    if backend not in (None, "codex", "claude", "gemini", "openrouter"):
        raise ValueError(
            f"Unknown LLM backend {backend!r}: expected 'codex', 'claude', "
            "'gemini', or 'openrouter'."
        )

    if db_path is not None:
        # Re-enter with the ambient DB context active: every internal
        # resolve_db_path(None) — pins, prompt A/B, budget, ledger, fallback —
        # now targets the caller's DB. Scoped + thread-safe, unlike a
        # db.set_db_path global mutation.
        from db_paths import db_path_context

        with db_path_context(db_path):
            return call_llm(
                prompt,
                purpose=purpose,
                model=model,
                timeout_seconds=timeout_seconds,
                ticker=ticker,
                scope=scope,
                run_id=run_id,
                force_budget_bypass=force_budget_bypass,
                backend=backend,
            )

    if purpose is None:
        purpose = "__default__"
        log.warning({"event": "llm_purpose_defaulted", "purpose": purpose})

    # Apply production prompt experiments before any provider is selected so
    # Codex, Claude, Gemini, and OpenRouter all receive the same governed
    # prompt. Eval scopes deliberately bypass the override in
    # apply_prompt_override so captured replays remain byte-identical.
    try:
        from llm.prompt_ab import apply_prompt_override

        prompt = apply_prompt_override(purpose, scope, prompt)
    except Exception as hook_exc:
        from log_redact import redact

        log.debug(
            {
                "event": "prompt_override_hook_failed",
                "error": redact(f"{type(hook_exc).__name__}: {hook_exc}")[:120],
            }
        )

    # Attribute the legacy/plain-string majority at the canonical seam.
    # Registered RenderedPrompt objects pass through unchanged; raw prompts
    # gain a purpose version and immutable body hash without caller churn.
    from llm.prompt_registry import attribute_plain_prompt

    prompt = attribute_plain_prompt(prompt, purpose=purpose)

    from llm.model_ladder import (
        GEMINI as _GEMINI_FAMILY,
    )
    from llm.model_ladder import (
        OPENROUTER as _OPENROUTER_FAMILY,
    )
    from llm.model_ladder import (
        family_of,
    )
    from llm.resolver import resolve_model_and_backend

    # Model and backend resolution via canonical single-point resolver
    resolved_model, resolved_backend = resolve_model_and_backend(
        purpose=purpose,
        model=model,
        backend=backend,
    )

    codex_fell_back = False
    primary_codex_error: Exception | None = None
    fallback_from_provider: str | None = None
    fallback_from_transport: str | None = None
    if resolved_backend == "codex":
        from llm.codex_backend import call_codex_llm

        _enforce_budget_pre_call(purpose, force_budget_bypass=force_budget_bypass)
        codex_model = (
            resolved_model
            if resolved_model.startswith("gpt-")
            else _codex_model_for(resolved_model)
        )
        try:
            text = call_codex_llm(
                prompt,
                model=codex_model,
                timeout_seconds=timeout_seconds or DEFAULT_TIMEOUT_SECONDS,
                purpose=purpose,
                ticker=ticker,
                scope=scope,
                run_id=run_id,
            )
            capture_exchange(
                prompt=prompt,
                response=text,
                purpose=purpose,
                ticker=ticker,
                scope=scope,
                model=codex_model,
                run_id=run_id,
                backend="codex",
            )
            return text
        except (OSError, RuntimeError, ValueError) as codex_error:
            if backend == "codex":
                raise
            from log_redact import redact

            log.warning(
                {
                    "event": "codex_backend_failed_falling_back_to_claude",
                    "purpose": purpose,
                    "error": f"{type(codex_error).__name__}: {redact(codex_error)[:200]}",
                }
            )
            resolved_backend = "claude"
            codex_fell_back = True
            primary_codex_error = codex_error
            fallback_from_provider = "openai"
            fallback_from_transport = "subscription_cli"

    if resolved_backend == "gemini":
        from llm.gemini_backend import call_gemini  # late — avoids import cycle

        try:
            text = call_gemini(
                prompt,
                model=resolved_model,
                timeout_seconds=timeout_seconds,
                purpose=purpose,
                ticker=ticker,
                scope=scope,
                run_id=run_id,
                force_budget_bypass=force_budget_bypass,
            )
            capture_exchange(
                prompt=prompt,
                response=text,
                purpose=purpose,
                ticker=ticker,
                scope=scope,
                model=resolved_model,
                run_id=run_id,
                backend="gemini",
            )
            return text
        except (LLMBudgetExceeded, LLMSetupError):
            raise  # hard stops — never paper over with a backend switch
        except (subprocess.SubprocessError, OSError, RuntimeError, ValueError) as gemini_error:
            if backend == "gemini":
                raise  # explicitly forced: the caller wants Gemini's answer or its error
            from log_redact import redact

            log.warning(
                {
                    "event": "gemini_backend_failed_falling_back_to_claude",
                    "purpose": purpose,
                    "error": f"{type(gemini_error).__name__}: {redact(str(gemini_error)[:200])}",
                }
            )
            fallback_from_provider = "google"
            fallback_from_transport = "metered_api"
            # fall through to the Claude path below (its own ledger rows)

    if resolved_backend == "openrouter":
        from llm.openrouter_backend import call_openrouter  # late — avoids import cycle

        try:
            text = call_openrouter(
                prompt,
                model=resolved_model,
                timeout_seconds=timeout_seconds,
                purpose=purpose,
                ticker=ticker,
                scope=scope,
                run_id=run_id,
                force_budget_bypass=force_budget_bypass,
            )
            capture_exchange(
                prompt=prompt,
                response=text,
                purpose=purpose,
                ticker=ticker,
                scope=scope,
                model=resolved_model,
                run_id=run_id,
                backend="openrouter",
            )
            return text
        except (LLMBudgetExceeded, LLMSetupError):
            raise  # hard stops — never paper over with a backend switch
        except (OSError, RuntimeError, ValueError) as openrouter_error:
            # requests.RequestException subclasses OSError, so network failures land here.
            if backend == "openrouter":
                raise  # explicitly forced: the caller wants OpenRouter's answer or its error
            from log_redact import redact

            log.warning(
                {
                    "event": "openrouter_backend_failed_falling_back_to_claude",
                    "purpose": purpose,
                    "error": (
                        f"{type(openrouter_error).__name__}: {redact(str(openrouter_error)[:200])}"
                    ),
                }
            )
            fallback_from_provider = "openrouter"
            fallback_from_transport = "metered_api"
            # fall through to the Claude path below (its own ledger rows)

    # Claude path. If resolved_model is a non-Claude ID (purpose pinned to Gemini
    # or OpenRouter but that backend fell back), re-resolve a Claude model. Guard
    # against non-Claude IDs in LLM_MODELS (post-promotion) by falling back to
    # DEFAULT_MODEL.
    _non_claude_families = (_GEMINI_FAMILY, _OPENROUTER_FAMILY)
    if family_of(resolved_model) in _non_claude_families:
        candidate = LLM_MODELS.get(purpose, DEFAULT_MODEL)
        resolved_model = (
            candidate if family_of(candidate) not in _non_claude_families else DEFAULT_MODEL
        )

    try:
        return _call_claude(
            prompt,
            model=resolved_model,
            timeout_seconds=timeout_seconds or DEFAULT_TIMEOUT_SECONDS,
            purpose=purpose,
            ticker=ticker,
            scope=scope,
            run_id=run_id,
            force_budget_bypass=force_budget_bypass,
            fallback_used="claude" if fallback_from_provider is not None else None,
            allow_codex_fallback=not codex_fell_back,
            fallback_from_provider=fallback_from_provider,
            fallback_from_transport=fallback_from_transport,
        )
    except LLMQuotaExhausted:
        raise
    except (OSError, RuntimeError, ValueError) as claude_error:
        if primary_codex_error is None:
            raise
        raise RuntimeError(
            "Both subscription LLM transports failed "
            f"(codex={type(primary_codex_error).__name__}, "
            f"claude={type(claude_error).__name__})"
        ) from claude_error


def _kill_windows_process_tree(proc: subprocess.Popen[str]) -> bool:
    """Ask Windows to terminate ``proc`` and every descendant process.

    Claude CLI is a Node wrapper that can leave its child process alive when
    only ``Popen.terminate()`` is called. ``taskkill`` is invoked with an
    integer PID argument (never a shell command), so no untrusted text reaches
    command parsing. Return False when the tree kill could not be attempted or
    did not succeed; the caller then uses the bounded single-process fallback.
    """
    pid = getattr(proc, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=_PROCESS_TERMINATE_GRACE_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        from log_redact import redact

        log.warning(
            {
                "event": "llm_stream_tree_terminate_failed",
                "error": redact(f"{type(exc).__name__}: {exc}")[:200],
            }
        )
        return False
    return result.returncode == 0


def _terminate_stream_process(proc: subprocess.Popen[str]) -> None:
    """Bounded, idempotent shutdown of a streaming transport and its children."""
    if proc.poll() is not None:
        return

    tree_killed = os.name == "nt" and _kill_windows_process_tree(proc)
    if not tree_killed:
        try:
            proc.terminate()
        except OSError as exc:
            if proc.poll() is not None:
                return
            from log_redact import redact

            log.warning(
                {
                    "event": "llm_stream_terminate_failed",
                    "error": redact(f"{type(exc).__name__}: {exc}")[:200],
                }
            )
            try:
                proc.kill()
            except OSError as kill_exc:
                log.error(
                    {
                        "event": "llm_stream_kill_failed",
                        "error": redact(f"{type(kill_exc).__name__}: {kill_exc}")[:200],
                    }
                )
                return
    try:
        proc.wait(timeout=_PROCESS_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=_PROCESS_TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            log.error({"event": "llm_stream_process_reap_failed"})


def _record_stream_call(
    purpose: str,
    model: str,
    prompt: str,
    response: str,
    started_at: datetime,
    *,
    error: str | None = None,
    meta: dict[str, object] | None = None,
    outcome: str | None = None,
    failure_class: str | None = None,
) -> None:
    """Write the central ledger row for one Claude subscription stream."""
    from llm_call_ledger import sha256_text

    record_llm_call(
        started_at=started_at,
        elapsed_ms=int((datetime.now(UTC) - started_at).total_seconds() * 1000),
        model=model,
        prompt_sha=sha256_text(prompt),
        prompt_chars=len(prompt),
        purpose=purpose,
        ticker=None,
        scope="ask",
        run_id=None,
        response_text=response or None,
        meta=meta,
        error=error,
        prompt=prompt,
        provider="anthropic",
        transport="subscription_cli",
        auth_class="subscription",
        attempts=1,
        retries=0,
        outcome=outcome,
        failure_class=failure_class,
    )


def _buffered_stream_answer(
    prompt: str, *, purpose: str, backend: str | None = None
) -> Iterator[dict[str, object]]:
    """Expose a governed single-shot backend as one delta plus one final event."""
    try:
        answer = call_llm(prompt, purpose=purpose, scope="ask", backend=backend).strip()
    except Exception as exc:
        from log_redact import redact

        log.warning(
            {
                "event": "llm_buffered_stream_failed",
                "purpose": purpose,
                "error": redact(f"{type(exc).__name__}: {exc}")[:200],
            }
        )
        yield {"type": "error", "error": "answer failed; retry the request"}
        return
    if not answer:
        yield {"type": "error", "error": "answer failed; retry the request"}
        return
    yield {"type": "delta", "text": answer}
    yield {"type": "final", "text": answer}


def stream_llm(
    prompt: str,
    *,
    purpose: str,
    scope: str = "ask",
    allowed_tools: tuple[str, ...] = ("Read",),
    timeout_seconds: float | None = None,
) -> Iterator[dict[str, object]]:
    """Canonical governed streaming LLM entry point.

    Model selection, budget enforcement, subprocess isolation, telemetry, and
    cancellation all live beside ``call_llm``. Claude-family purposes retain
    true incremental output. Backends without a streaming subscription
    transport use the canonical buffered path and still present the same event
    contract to clients.
    """
    from llm.model_ladder import CLAUDE as _CLAUDE_FAMILY
    from llm.model_ladder import family_of
    from log_redact import redact

    if scope != "ask":
        raise ValueError("stream_llm currently supports only the governed 'ask' scope")

    from llm.prompt_registry import attribute_plain_prompt

    prompt = attribute_plain_prompt(prompt, purpose=purpose)

    model = _model_for(purpose)
    try:
        _enforce_budget_pre_call(purpose, force_budget_bypass=False)
    except LLMBudgetExceeded:
        yield {
            "type": "error",
            "error": "Ask monthly budget reached — raise the cap or wait for the reset.",
        }
        return

    if family_of(model) != _CLAUDE_FAMILY:
        yield from _buffered_stream_answer(prompt, purpose=purpose)
        return

    claude_bin = shutil.which("claude")
    if claude_bin is None:
        yield {"type": "error", "error": "answer transport is not configured"}
        return

    cmd = [
        claude_bin,
        "-p",
        "--model",
        model,
        "--output-format",
        "stream-json",
        "--no-session-persistence",
    ]
    if allowed_tools:
        cmd.extend(["--allowedTools", " ".join(allowed_tools)])
    cmd.append("--verbose")

    started_at = datetime.now(UTC)
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=_neutral_subprocess_cwd(),
            env=_subprocess_env(),
        )
    except OSError as exc:
        safe_error = redact(f"{type(exc).__name__}: {exc}")[:200]
        log.error({"event": "llm_stream_launch_failed", "error": safe_error})
        _record_stream_call(
            purpose,
            model,
            prompt,
            "",
            started_at,
            error=f"launch: {safe_error}",
            failure_class="launch",
        )
        yield {"type": "error", "error": "answer failed; retry the request"}
        return

    assert proc.stdin and proc.stdout
    full_text_parts: list[str] = []
    result_meta: dict[str, object] | None = None
    timed_out = threading.Event()
    completed = False
    canceled = False
    transport_error: str | None = None
    termination_lock = threading.Lock()

    def _stop_process() -> None:
        with termination_lock:
            _terminate_stream_process(proc)

    def _watchdog_expired() -> None:
        if proc.poll() is None:
            timed_out.set()
            _stop_process()

    timeout = timeout_seconds if timeout_seconds is not None else _STREAM_TIMEOUT_SECONDS
    watchdog = threading.Timer(timeout, _watchdog_expired)
    watchdog.daemon = True
    watchdog.start()
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                raw_event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw_event, dict):
                continue
            event = cast("dict[str, object]", raw_event)
            event_type = event.get("type")
            if event_type == "assistant":
                message_value = event.get("message")
                if not isinstance(message_value, dict):
                    continue
                message = cast("dict[str, object]", message_value)
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for block_value in cast("list[object]", content):
                    if not isinstance(block_value, dict):
                        continue
                    block = cast("dict[str, object]", block_value)
                    chunk = block.get("text")
                    if block.get("type") == "text" and isinstance(chunk, str) and chunk:
                        full_text_parts.append(chunk)
                        yield {"type": "delta", "text": chunk}
            elif event_type == "result":
                result_meta = event
        completed = True
    except GeneratorExit:
        canceled = True
        raise
    except (OSError, ValueError) as exc:
        transport_error = redact(f"{type(exc).__name__}: {exc}")[:200]
    finally:
        if not completed and not timed_out.is_set():
            _stop_process()
        elif proc.poll() is None:
            try:
                proc.wait(timeout=_PROCESS_TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                timed_out.set()
                _stop_process()
        watchdog.cancel()
        watchdog.join(timeout=(_PROCESS_TERMINATE_GRACE_SECONDS * 2) + 0.1)
        if canceled:
            _record_stream_call(
                purpose,
                model,
                prompt,
                "".join(full_text_parts).strip(),
                started_at,
                error="[canceled] stream consumer disconnected",
                outcome="canceled",
                failure_class="canceled",
            )

    full_text = "".join(full_text_parts).strip()
    if timed_out.is_set():
        _record_stream_call(
            purpose,
            model,
            prompt,
            full_text,
            started_at,
            error="[timeout] streaming response exceeded watchdog",
            failure_class="timeout",
        )
        yield {"type": "error", "error": "answer timed out; retry the request"}
        return
    if transport_error is not None:
        log.error({"event": "llm_stream_transport_failed", "error": transport_error})
        _record_stream_call(
            purpose,
            model,
            prompt,
            full_text,
            started_at,
            error=f"[transport] {transport_error}",
            failure_class="transport",
        )
        yield {"type": "error", "error": "answer failed; retry the request"}
        return
    result_succeeded = (
        proc.returncode == 0
        and result_meta is not None
        and result_meta.get("is_error") is False
        and result_meta.get("subtype") == "success"
    )
    if not result_succeeded:
        _record_stream_call(
            purpose,
            model,
            prompt,
            full_text,
            started_at,
            error="[partial] stream ended without a successful result envelope",
            outcome="partial",
            failure_class="partial_response",
        )
        yield {"type": "error", "error": "answer failed; retry the request"}
        return
    if not full_text:
        stderr_text = redact((proc.stderr.read() if proc.stderr else "").strip())[:200]
        log.error({"event": "llm_stream_empty_response", "error": stderr_text})
        _record_stream_call(
            purpose,
            model,
            prompt,
            "",
            started_at,
            error=f"[empty] {stderr_text}",
            failure_class="empty_response",
        )
        yield {"type": "error", "error": "answer failed; retry the request"}
        return

    _record_stream_call(purpose, model, prompt, full_text, started_at, meta=result_meta)
    capture_exchange(
        prompt=prompt,
        response=full_text,
        purpose=purpose,
        ticker=None,
        scope=scope,
        model=model,
        run_id=None,
        backend="claude",
    )
    yield {"type": "final", "text": full_text}


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
    require_grounding: bool = True,
) -> str:
    """Web-grounded LLM call — Codex-first, Claude WebSearch/WebFetch as the
    operational fallback.

    Routing mirrors ``call_llm``: an explicit ``model`` argument is a forced
    Claude request and bypasses Codex entirely (same "explicit model = forced
    family" contract as the plain path). Otherwise, whenever
    ``_primary_subscription_backend() == "codex"`` (the production default;
    ``LLM_PRIMARY_SUBSCRIPTION_BACKEND=claude`` is the reversible rollback),
    the Codex membership wrapper runs FIRST with ``web_search="live"`` so it
    can fetch fresh pages. An OPERATIONAL Codex failure — never a routing
    preference — falls through to this function's Claude WebSearch/WebFetch
    path below, which itself falls through to plain ``_call_claude`` (no web
    tools) on failure, with Codex re-entry disabled on that final hop (it
    already failed once). A memo is always produced even when every
    web-search transport is unavailable. 2026-08-03 owner ratification: see
    directives/llm_calls.md and procedures/llm-ops.TRANSPORTS.md.

    Use for memo generation, fact-finding on recent news, anything where
    the upstream context is stale and the model needs to look something up.

    Same per-purpose MONTHLY budget enforcement as `_call_claude`
    (``_enforce_budget_pre_call``, backend-agnostic); pass
    ``force_budget_bypass=True`` to skip it.

    ``max_budget_usd`` / ``CLAUDE_WEB_MAX_BUDGET_USD`` is a Claude-CLI-only
    HARD PER-CALL cost ceiling enforced via ``--max-budget-usd``. It applies
    only to the Claude WebSearch/WebFetch leg — Codex is membership-billed
    with no per-call price and the wrapper reports no token usage, so there
    is no dollar figure to clamp on the Codex leg. That is a deliberate gap,
    not a silently dropped concept: the per-purpose MONTHLY budget above
    still gates every leg (Codex included) identically, and Codex's own
    isolation (read-only sandbox, no shell/apps) bounds a runaway call's
    blast radius even without a $-ceiling. A tiered caller's ``max_budget_usd``
    is clamped to ``min(requested, CLAUDE_WEB_MAX_BUDGET_USD)`` — the S2
    invariant — for whichever call actually reaches the Claude leg.

    ``require_grounding`` (default True) enforces the groundedness gate on the
    Codex leg: a web-grounded answer that cites no source did not search, so it
    is treated as an operational failure and falls through to Claude rather
    than being served. Pass False only for a web purpose whose output
    legitimately carries no URL.

    Model selection mirrors ``call_llm``: pass an explicit ``model`` to force
    one, or leave it ``None`` (the default) to resolve from ``purpose`` via
    ``LLM_MODELS`` / ``_model_for``; with neither set it falls back to
    ``DEFAULT_MODEL`` with a warning. This historically hard-defaulted to
    ``DEFAULT_MODEL`` and ignored ``purpose`` — resolving from purpose lets
    web-enabled callers (e.g. the news structurer) be retuned centrally in
    ``LLM_MODELS``. Callers passing an explicit ``model`` are unaffected.
    """
    if purpose is None:
        purpose = "__default__"
        log.warning({"event": "llm_web_purpose_defaulted", "purpose": purpose})
    explicit_model = model is not None
    resolved_model = _model_for(purpose) if model is None else model
    # Prompt-override auto-apply (§10 Q1) — web purposes are A/B-eligible even
    # though downgrade-ineligible. Same fail-open, production-scopes-only hook
    # as call_llm.
    try:
        from llm.prompt_ab import apply_prompt_override

        prompt = apply_prompt_override(purpose, scope, prompt)
    except Exception as hook_exc:
        from log_redact import redact

        log.debug(
            {
                "event": "prompt_override_hook_failed",
                "error": redact(f"{type(hook_exc).__name__}: {hook_exc}")[:120],
            }
        )
    from llm.prompt_registry import attribute_plain_prompt

    prompt = attribute_plain_prompt(prompt, purpose=purpose)
    _enforce_budget_pre_call(purpose, force_budget_bypass=force_budget_bypass)

    codex_fell_back = False
    fallback_from_provider: str | None = None
    fallback_from_transport: str | None = None
    if not explicit_model and _primary_subscription_backend() == _PRIMARY_CODEX:
        from llm.codex_backend import call_codex_llm

        codex_model = (
            resolved_model
            if resolved_model.startswith("gpt-")
            else _codex_model_for(resolved_model)
        )
        try:
            text = call_codex_llm(
                prompt,
                model=codex_model,
                # The wrapper's own default (600s) is too short for a
                # multi-round-trip web search; reuse this function's own
                # (web-appropriate, default 1800s) timeout instead of
                # inheriting the short plain-call default.
                timeout_seconds=timeout_seconds,
                purpose=purpose,
                ticker=ticker,
                scope=scope or "web",
                run_id=run_id,
                web_search="live",
            )
            # GROUNDEDNESS GATE (measured defect, 2026-08-03). A web-grounded
            # answer that cites NOTHING did not actually search — and the
            # failure is invisible, because the model returns a confident,
            # correctly-formatted answer with exit code 0. Measured on the real
            # recent_developments prompt: Codex returned the template's own
            # sanctioned escape hatch ("*No material news in the last 7 days.*",
            # 57 chars, 0 URLs) for NU/MELI/UBER while Claude found real
            # material news for all three the same day; MELI came back in 15.5s,
            # too fast to have searched. The prompt is written against Claude's
            # tool loop (it names web_search/web_fetch, front-loads HARD CAPS,
            # and offers an explicit say-nothing output), so a transport swap
            # silently changed behaviour. Routing was correct; only the OUTPUT
            # was wrong, which is precisely what the routing guard cannot see.
            # Treat an uncited web answer as an OPERATIONAL failure so it falls
            # through to the Claude web leg below and is ledgered as a fallback
            # rather than served as a finding. A genuine no-news day still
            # resolves correctly: Claude runs and also reports no news.
            if require_grounding and "http" not in text:
                raise RuntimeError(
                    "codex web answer cited no sources — ungrounded, treating as "
                    "an operational failure"
                )
            capture_exchange(
                prompt=prompt,
                response=text,
                purpose=purpose,
                ticker=ticker,
                scope=scope or "web",
                model=codex_model,
                run_id=run_id,
                backend="codex",
            )
            return text
        except (OSError, RuntimeError, ValueError) as codex_error:
            from log_redact import redact

            log.warning(
                {
                    "event": "codex_web_backend_failed_falling_back_to_claude",
                    "purpose": purpose,
                    "error": f"{type(codex_error).__name__}: {redact(codex_error)[:200]}",
                }
            )
            codex_fell_back = True
            fallback_from_provider = "openai"
            fallback_from_transport = "subscription_cli"

    fallback_used = "claude" if fallback_from_provider is not None else None
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

    # Quota breaker: a blocked window dooms the (expensive, agentic) web spawn
    # and the plain fall-through alike — fail fast with the typed error.
    blocked_until = quota_block_active()
    if blocked_until is not None:
        msg = (
            f"LLM quota breaker engaged until {blocked_until.isoformat()} — "
            "web call skipped without spawning the CLI (defer and retry next run)"
        )
        record_llm_call(
            started_at=datetime.now(UTC),
            elapsed_ms=0,
            model=resolved_model,
            prompt_sha=prompt_sha,
            prompt_chars=len(prompt),
            purpose=purpose,
            ticker=ticker,
            scope=scope or "web",
            run_id=run_id,
            error=f"[quota_blocked] {msg}",
            fallback_used=fallback_used,
            prompt=prompt,
            provider="anthropic",
            transport="subscription_cli",
            auth_class="subscription",
            attempts=0,
            retries=0,
            failure_class="quota_blocked",
            fallback_from_provider=fallback_from_provider,
            fallback_from_transport=fallback_from_transport,
        )
        raise LLMQuotaExhausted(msg, blocked_until=blocked_until)

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
        # Claude-CLI-only — see the docstring's budget/cost note.
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
            env=_subprocess_env(),  # strip ambient ANTHROPIC_BASE_URL (2026-07 incident)
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
            fallback_used=fallback_used,
            prompt=prompt,
            provider="anthropic",
            transport="subscription_cli",
            auth_class="subscription",
            attempts=1,
            retries=0,
            fallback_from_provider=fallback_from_provider,
            fallback_from_transport=fallback_from_transport,
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
        web_info = classify_cli_failure(web_err)
        web_error_msg = (
            f"{web_info.ledger_prefix} {type(web_err).__name__}: {web_info.detail[:400]}"
        )
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
            error=web_error_msg,
            fallback_used=fallback_used,
            prompt=prompt,
            provider="anthropic",
            transport="subscription_cli",
            auth_class="subscription",
            attempts=1,
            retries=0,
            failure_class=web_info.kind,
            fallback_from_provider=fallback_from_provider,
            fallback_from_transport=fallback_from_transport,
        )
        if web_info.kind == "usage_limit":
            # Engage the breaker here too — the plain-path fall-through below
            # will then fail fast (typed) instead of spawning a second doomed
            # subprocess against the same exhausted window.
            record_quota_exhausted(web_info)
        log.warning(
            {
                "event": "llm_web_call_fallback_to_plain",
                "kind": web_info.kind,
                "error": f"{type(web_err).__name__}: {web_info.detail[:200]}",
            }
        )
        # Fall through to non-web path so the caller still gets output. The
        # plain _call_claude path records its own ledger row(s), carries the
        # retry/breaker policy, and raises LLMQuotaExhausted when blocked.
        # allow_codex_fallback=False when Codex already failed once above —
        # a second internal attempt inside _call_claude would be a silent
        # re-entry into a transport this call already gave up on.
        return _call_claude(
            prompt,
            model=resolved_model,
            timeout_seconds=timeout_seconds,
            purpose=purpose,
            ticker=ticker,
            scope=scope,
            run_id=run_id,
            allow_codex_fallback=not codex_fell_back,
        )
